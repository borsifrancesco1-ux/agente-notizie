"""Comandi dal bot Telegram: cambiare cosa segue l'agente scrivendogli in chat.

Si usano i comandi qui sotto oppure si scrive in italiano normale ("segui anche Mediobanca",
"meno notizie sulla Cina"): l'IA traduce la frase nel comando giusto. Le modifiche finiscono in
config.yaml e profilo.md (con commenti e formattazione intatti) e il workflow le salva su GitHub.
"""
from __future__ import annotations

import difflib
import io
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml as pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

import dati
import ia

if TYPE_CHECKING:
    from agente import Giro

AIUTO = """Comandi del bot (oppure scrivimi in italiano normale, es. "segui anche Mediobanca"):

/segui <società> – aggiungo un titolo da seguire
/tema <argomento> – aggiungo un tema da seguire
/smetti <nome> – smetto di seguire un titolo o un tema
/soglia <1-10> – cambio la soglia di rilevanza (più bassa = più notizie)
/profilo <frase> – aggiungo un'indicazione al profilo
/notizie – faccio subito un giro di notizie
/stato – com'è andata oggi
/aiuto – questo elenco"""

COMANDI = {"/segui": "segui", "/tema": "tema", "/smetti": "smetti", "/soglia": "soglia",
           "/profilo": "profilo", "/notizie": "notizie", "/stato": "stato"}

ISTRUZIONI_INTERPRETA = """Sei l'assistente di un agente di notizie finanziarie. L'utente ti scrive in italiano
per cambiare cosa segue l'agente. Traduci il messaggio in un'azione:
- "segui": seguire una società o un titolo quotato (argomento: il nome della società);
- "tema": seguire un argomento (argomento: l'argomento);
- "smetti": smettere di seguire un titolo o un tema (argomento: il nome);
- "soglia": cambiare la soglia di rilevanza da 1 a 10 (argomento: il numero; "più notizie" = la soglia
  attuale meno 1, "meno notizie" = la soglia attuale più 1);
- "profilo": un'indicazione su cosa interessa o non interessa (argomento: la frase da aggiungere al
  profilo, riscritta in modo chiaro e in prima persona);
- "notizie": fare subito un giro di notizie;
- "stato": sapere com'è andata oggi;
- "nessuna": il messaggio non chiede nessuna di queste cose ("risposta": breve risposta cortese in italiano).
"""
AZIONI = ["segui", "tema", "smetti", "soglia", "profilo", "notizie", "stato", "nessuna"]
SCHEMA_INTERPRETA = {
    "type": "OBJECT",
    "properties": {"azione": {"type": "STRING", "format": "enum", "enum": AZIONI},
                   "argomento": {"type": "STRING"}, "risposta": {"type": "STRING"}},
    "required": ["azione", "argomento", "risposta"],
}
ESEMPIO_INTERPRETA = {"azione": "segui", "argomento": "Mediobanca", "risposta": ""}

ISTRUZIONI_TITOLO = """Prepara la scheda di un titolo quotato da seguire con un agente di notizie finanziarie,
per il team descritto nel profilo.
- "nome": il nome con cui la società compare nei giornali (es. "Mediobanca");
- "ticker": il simbolo su Yahoo Finance (es. "MB.MI" a Piazza Affari, "AAPL" negli USA; vuoto se non quotata);
- "ticker_usa": il simbolo dell'eventuale quotazione a New York, anche ADR (vuoto se non c'è);
- "sinonimi": altri nomi con cui compare nei titoli dei giornali, compresi i vertici noti (massimo 4);
- "icona": una emoji adatta al settore;
- "reparti": da 1 a 3 reparti del team a cui interessa, scelti tra quelli elencati;
- "perche": una frase su perché seguirla per il portafoglio del profilo.
"""
SCHEMA_TITOLO = {
    "type": "OBJECT",
    "properties": {k: {"type": "STRING"} for k in ("nome", "ticker", "ticker_usa", "icona", "perche")}
    | {"sinonimi": {"type": "ARRAY", "items": {"type": "STRING"}},
       "reparti": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["nome", "ticker", "ticker_usa", "sinonimi", "icona", "reparti", "perche"],
}
ESEMPIO_TITOLO = {"nome": "Mediobanca", "ticker": "MB.MI", "ticker_usa": "", "sinonimi": ["Mediobanca SpA"],
                  "icona": "🏦", "reparti": ["Azionario"], "perche": "..."}

ISTRUZIONI_TEMA = """Prepara la scheda di un tema da seguire con un agente di notizie finanziarie,
per il team descritto nel profilo.
- "nome": nome breve del tema in italiano;
- "icona": una emoji adatta;
- "reparti": da 1 a 3 reparti del team a cui interessa, scelti tra quelli elencati;
- "parole_chiave": da 6 a 12 parole o espressioni, in italiano e in inglese, che compaiono nei titoli
  delle notizie sul tema (nomi propri e sigle con le maiuscole, parole comuni in minuscolo);
- "ricerca_it" e "ricerca_en": una ricerca breve per Google News in italiano e in inglese.
"""
SCHEMA_TEMA = {
    "type": "OBJECT",
    "properties": {k: {"type": "STRING"} for k in ("nome", "icona", "ricerca_it", "ricerca_en")}
    | {"parole_chiave": {"type": "ARRAY", "items": {"type": "STRING"}},
       "reparti": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["nome", "icona", "reparti", "parole_chiave", "ricerca_it", "ricerca_en"],
}
ESEMPIO_TEMA = {"nome": "Banche italiane", "icona": "🏦", "reparti": ["Azionario"], "parole_chiave": ["banche"],
                "ricerca_it": "banche italiane", "ricerca_en": "Italian banks"}

SEZIONE_PROFILO = "## Indicazioni aggiunte dal bot"


def esegui(giro: Giro, testo: str) -> str:
    """Esegue un comando (o una frase in italiano) e restituisce la risposta da mandare."""
    testo = testo.strip()
    parola, _, argomento = testo.partition(" ")
    parola = parola.lower().split("@")[0]  # /segui@nome_del_bot
    argomento = argomento.strip()
    if parola in ("/aiuto", "/help", "/start"):
        return AIUTO
    if parola == "/proposta":
        return proposta(giro, argomento)
    if parola in COMANDI:
        azione = COMANDI[parola]
    elif parola.startswith("/"):
        return f"Non conosco il comando {parola}.\n\n{AIUTO}"
    else:
        soglia = (giro.config.get("ia") or {}).get("soglia_rilevanza", 7)
        risposta = giro.con_motori(
            lambda m, k: ia.genera(m, k, ISTRUZIONI_INTERPRETA + f"\nSoglia attuale: {soglia}.", testo,
                                   SCHEMA_INTERPRETA, ESEMPIO_INTERPRETA), giro.motori())
        if not risposta:
            return "Non riesco a interpretare il messaggio adesso (IA non disponibile). Prova con un comando: /aiuto"
        azione, argomento = risposta.get("azione", "nessuna"), (risposta.get("argomento") or "").strip()
        if azione == "nessuna":
            return risposta.get("risposta") or f"Non ho capito cosa vuoi che faccia.\n\n{AIUTO}"
    if azione in ("segui", "tema", "smetti", "soglia", "profilo") and not argomento:
        return f"Manca l'argomento: per esempio /{azione} {'Mediobanca' if azione == 'segui' else '...'}"
    return {"segui": segui, "tema": tema, "smetti": smetti, "soglia": soglia_, "profilo": profilo,
            "notizie": notizie, "stato": stato}[azione](giro, argomento)


# ---------------- azioni ----------------

def segui(giro: Giro, nome: str) -> str:
    config = _leggi_config(giro)
    for t in config.get("titoli") or []:
        if t["nome"].lower() == nome.lower() or str(t.get("ticker", "")).lower() == nome.lower():
            return f"Seguo già {t['nome']}."
    scheda = giro.con_motori(lambda m, k: ia.genera(m, k, ISTRUZIONI_TITOLO + _contesto(giro), nome,
                                                    SCHEMA_TITOLO, ESEMPIO_TITOLO), giro.motori())
    if not scheda:
        return "Non riesco a preparare la scheda adesso (IA non disponibile): riprova più tardi."
    voce = CommentedMap()
    voce["nome"] = scheda.get("nome") or nome
    voce["icona"] = scheda.get("icona") or "📰"
    voce["reparti"] = _lista(_reparti_validi(giro, scheda.get("reparti")))
    controllati = []
    for campo in ("ticker", "ticker_usa"):
        simbolo = (scheda.get(campo) or "").strip().upper()
        if simbolo and _ticker_esiste(simbolo):
            voce[campo] = simbolo
            controllati.append(simbolo)
    voce["sinonimi"] = _lista([s for s in scheda.get("sinonimi") or [] if s and s != voce["nome"]][:4])
    voce["perche"] = scheda.get("perche") or ""
    config.setdefault("titoli", CommentedSeq()).append(voce)
    _salva_config(giro, config)
    ticker = f" ({', '.join(controllati)})" if controllati else " (ticker non trovato: lo cerco per nome)"
    return (f"✅ Ora seguo {voce['nome']}{ticker}.\nReparti: {', '.join(voce['reparti']) or '-'}\n"
            f"Lo cerco anche come: {', '.join(voce['sinonimi']) or '-'}\nPerché: {voce['perche']}\n\n"
            f"Se qualcosa non va, correggilo in config.yaml oppure scrivimi /smetti {voce['nome']}.")


def tema(giro: Giro, argomento: str) -> str:
    config = _leggi_config(giro)
    for t in config.get("temi") or []:
        if t["nome"].lower() == argomento.lower():
            return f"Seguo già il tema {t['nome']}."
    scheda = giro.con_motori(lambda m, k: ia.genera(m, k, ISTRUZIONI_TEMA + _contesto(giro), argomento,
                                                    SCHEMA_TEMA, ESEMPIO_TEMA), giro.motori())
    if not scheda:
        return "Non riesco a preparare il tema adesso (IA non disponibile): riprova più tardi."
    voce = CommentedMap()
    voce["nome"] = scheda.get("nome") or argomento
    voce["icona"] = scheda.get("icona") or "📰"
    voce["reparti"] = _lista(_reparti_validi(giro, scheda.get("reparti")))
    voce["parole_chiave"] = _lista([p for p in scheda.get("parole_chiave") or [] if p][:12])
    for campo in ("ricerca_it", "ricerca_en"):
        if scheda.get(campo):
            voce[campo] = scheda[campo]
    config.setdefault("temi", CommentedSeq()).append(voce)
    _salva_config(giro, config)
    return (f"✅ Nuovo tema: {voce['icona']} {voce['nome']}\nReparti: {', '.join(voce['reparti']) or '-'}\n"
            f"Parole chiave: {', '.join(voce['parole_chiave'])}\n"
            f"Ricerche su Google News: {voce.get('ricerca_it', '-')} / {voce.get('ricerca_en', '-')}")


def smetti(giro: Giro, nome: str) -> str:
    config = _leggi_config(giro)
    trovati = [(sezione, voce) for sezione in ("titoli", "temi") for voce in config.get(sezione) or []
               if nome.lower() in voce["nome"].lower() or nome.lower() == str(voce.get("ticker", "")).lower()]
    if not trovati:
        elenco = ", ".join(v["nome"] for s in ("titoli", "temi") for v in config.get(s) or [])
        return f"Non trovo «{nome}» tra quelli che seguo: {elenco}."
    if len(trovati) > 1:
        return f"«{nome}» corrisponde a più voci: {', '.join(v['nome'] for _, v in trovati)}. Scrivi il nome completo."
    sezione, voce = trovati[0]
    config[sezione].remove(voce)
    _salva_config(giro, config)
    return f"✅ Non seguo più {voce['nome']}."


def soglia_(giro: Giro, argomento: str) -> str:
    numeri = re.findall(r"\d+", argomento)
    if not numeri or not 1 <= int(numeri[0]) <= 10:
        return "La soglia è un numero da 1 a 10: per esempio /soglia 6 (più bassa = più notizie)."
    config = _leggi_config(giro)
    vecchia = config["ia"].get("soglia_rilevanza")
    config["ia"]["soglia_rilevanza"] = int(numeri[0])
    _salva_config(giro, config)
    return f"✅ Soglia di rilevanza: da {vecchia} a {numeri[0]}."


def profilo(giro: Giro, frase: str) -> str:
    percorso = giro.cartella / "profilo.md"
    testo = percorso.read_text("utf-8").rstrip()
    if SEZIONE_PROFILO not in testo:
        testo += f"\n\n{SEZIONE_PROFILO}"
    frase = frase.strip().rstrip(".")
    testo += f"\n- {frase[:1].upper() + frase[1:]}. (aggiunta il {giro.adesso:%d/%m/%Y})\n"
    _scrivi(giro, percorso, testo)
    giro.profilo = testo
    return f"✅ Aggiunto al profilo: «{frase}». Lo trovi in fondo a profilo.md, nella sezione delle indicazioni del bot."


def notizie(giro: Giro, _argomento: str) -> str:
    giro.azioni_extra.append("notizie")
    return "Faccio subito un giro di notizie."


def stato(giro: Giro, _argomento: str) -> str:
    m = giro.memoria
    oggi = m.inviate_dal(giro.adesso.replace(hour=0, minute=0, second=0, microsecond=0))
    richieste = m.richieste_ia.get(max(m.richieste_ia), 0) if m.richieste_ia else 0
    guasti = [f"{nome} ({n} giri)" for nome, n in m.feed_guasti.items()]
    ultimi_dati = [d["nome"] for d in m.dati_usciti[-3:]]
    return (f"📋 Stato dell'agente ({giro.adesso:%d/%m %H:%M})\n"
            f"Notizie inviate oggi: {len(oggi)}\nRichieste all'IA oggi: {richieste}\n"
            f"Soglia di rilevanza: {(giro.config.get('ia') or {}).get('soglia_rilevanza')}\n"
            f"Titoli seguiti: {', '.join(t['nome'] for t in giro.config.get('titoli') or [])}\n"
            f"Temi seguiti: {len(giro.config.get('temi') or [])}\n"
            f"Ultimi dati usciti: {', '.join(ultimi_dati) or 'nessuno di recente'}\n"
            f"Feed che non rispondono: {', '.join(guasti) or 'nessuno'}")


def proposta(giro: Giro, argomento: str) -> str:
    """Risposta ai pulsanti della proposta settimanale di modifica al profilo."""
    scelta, _, id_ = argomento.partition(" ")
    attuale = giro.memoria.proposta
    if not attuale or attuale.get("id") != id_.strip():
        return "Questa proposta non è più valida (ne è arrivata una più recente o è già stata gestita)."
    giro.memoria.proposta = {}
    if scelta != "applica":
        return "👌 Proposta ignorata: il profilo resta com'è."
    _scrivi(giro, giro.cartella / "profilo.md", attuale["testo"].rstrip() + "\n")
    giro.profilo = attuale["testo"]
    return "✅ Profilo aggiornato con la proposta. Dal prossimo giro le notizie terranno conto delle modifiche."


# ---------------- strumenti ----------------

def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _leggi_config(giro: Giro):
    return _yaml().load((giro.cartella / "config.yaml").read_text("utf-8"))


def _salva_config(giro: Giro, config) -> None:
    uscita = io.StringIO()
    _yaml().dump(config, uscita)
    _scrivi(giro, giro.cartella / "config.yaml", uscita.getvalue())
    giro.config = pyyaml.safe_load(uscita.getvalue())  # i giri successivi usano già la versione nuova
    giro.aggiorna_da_config()


def _scrivi(giro: Giro, percorso: Path, testo: str) -> None:
    if giro.prova:
        prima = percorso.read_text("utf-8").splitlines()
        modifiche = [r for r in difflib.unified_diff(prima, testo.splitlines(), lineterm="", n=0)
                     if r.startswith(("+", "-")) and not r.startswith(("+++", "---"))]
        print(f"(prova) {percorso.name} non modificato. Righe che cambierebbero:\n" + "\n".join(modifiche))
        return
    percorso.write_text(testo, "utf-8")


def _lista(valori: list) -> CommentedSeq:
    """Lista scritta su una riga, come le altre di config.yaml: [a, b, c]."""
    seq = CommentedSeq(valori)
    seq.fa.set_flow_style()
    return seq


def _reparti_validi(giro: Giro, reparti) -> list[str]:
    validi = [r["nome"] for r in giro.config.get("reparti") or []]
    return [r for r in reparti or [] if r in validi][:3]


def _ticker_esiste(simbolo: str) -> bool:
    try:
        return bool(dati.ultime_osservazioni({"fonte": "yahoo", "simbolo": simbolo}, 1))
    except Exception:  # noqa: BLE001 — simbolo inesistente o servizio non raggiungibile
        return False


def _contesto(giro: Giro) -> str:
    reparti = ", ".join(r["nome"] for r in giro.config.get("reparti") or [])
    return f"\nREPARTI DEL TEAM: {reparti}\n\nPROFILO\n{giro.profilo}\n"


def repository() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "borsifrancesco1-ux/agente-notizie")
