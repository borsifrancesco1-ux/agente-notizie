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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml as pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

import archivio
import dati
import ia
import notifiche

if TYPE_CHECKING:
    from agente import Giro

AIUTO = """Comandi del bot (oppure scrivimi in italiano normale, es. "segui anche Mediobanca"):

Cosa seguire
/segui <società> – aggiungo un titolo da seguire
/tema <argomento> – aggiungo un tema da seguire
/smetti <nome> – smetto di seguire un titolo o un tema
/soglia <1-10> – cambio la soglia di rilevanza (più bassa = più notizie)
/profilo <frase> – aggiungo un'indicazione al profilo
/annulla – annullo l'ultima modifica fatta con questi comandi

Notizie e domande
/chiedi <domanda> – rispondo usando l'archivio delle notizie, con le fonti
/azienda <società> [domanda] – dati e risposte su una società quotata negli USA
   (bilanci, multipli, crescita, dividendi, rischio, conference call, DCF in Excel, report),
   es. /azienda Apple com'è andato l'ultimo trimestre?
/oggi – le notizie inviate oggi, per reparto
/cerca <parole> – cerco nell'archivio delle notizie
/notizie – faccio subito un giro di notizie
/stato – com'è andata oggi

Invii
/pausa <durata> – sospendo gli invii (es. /pausa 3h, /pausa 2g)
/riprendi – riprendo gli invii
/iscrivimi <reparti> [subito|sera] – notizie dei tuoi reparti in privato
/disiscrivimi – smetto di mandarti le notizie in privato
/iscrizioni – a cosa sei iscritto
/aiuto – questo elenco"""

COMANDI = {"/segui": "segui", "/tema": "tema", "/smetti": "smetti", "/soglia": "soglia",
           "/profilo": "profilo", "/notizie": "notizie", "/stato": "stato", "/chiedi": "chiedi",
           "/oggi": "oggi", "/cerca": "cerca", "/pausa": "pausa", "/riprendi": "riprendi", "/annulla": "annulla",
           "/azienda": "azienda"}

ISTRUZIONI_INTERPRETA = """Sei l'assistente di un agente di notizie finanziarie. L'utente ti scrive in italiano
per cambiare cosa segue l'agente o per chiedere qualcosa. Traduci il messaggio in un'azione:
- "segui": seguire una società o un titolo quotato (argomento: il nome della società);
- "tema": seguire un argomento (argomento: l'argomento);
- "smetti": smettere di seguire un titolo o un tema (argomento: il nome);
- "soglia": cambiare la soglia di rilevanza da 1 a 10 (argomento: il numero; "più notizie" = la soglia
  attuale meno 1, "meno notizie" = la soglia attuale più 1);
- "profilo": un'indicazione su cosa interessa o non interessa (argomento: la frase da aggiungere al
  profilo, riscritta in modo chiaro e in prima persona);
- "chiedi": una domanda sui fatti, i mercati o le notizie (argomento: la domanda);
- "azienda": informazioni su una società quotata: prezzo, bilanci, risultati, multipli, dividendi, rischio,
  conference call, valutazione DCF o report (argomento: tutta la richiesta, con il nome della società);
- "oggi": sapere quali notizie sono uscite oggi;
- "cerca": cercare notizie su un argomento preciso (argomento: le parole da cercare);
- "pausa": sospendere gli invii (argomento: la durata, es. "3h" o "2g");
- "riprendi": riprendere gli invii;
- "annulla": annullare l'ultima modifica;
- "notizie": fare subito un giro di notizie;
- "stato": sapere com'è andata oggi l'agente;
- "nessuna": il messaggio non chiede nessuna di queste cose ("risposta": breve risposta cortese in italiano).
"""
AZIONI = ["segui", "tema", "smetti", "soglia", "profilo", "chiedi", "azienda", "oggi", "cerca", "pausa", "riprendi",
          "annulla", "notizie", "stato", "nessuna"]

ISTRUZIONI_CHIEDI = """Rispondi in italiano alla domanda del team usando SOLO le notizie e i dati elencati
(dall'archivio dell'agente). Risposta chiara e concreta, da 3 a 6 frasi, con i numeri quando ci sono.
Se l'archivio non basta per rispondere, dillo apertamente invece di inventare.
- "risposta": il testo della risposta;
- "fonti": i numeri delle voci usate, al massimo 5, dalla più importante.
"""
SCHEMA_CHIEDI = {
    "type": "OBJECT",
    "properties": {"risposta": {"type": "STRING"}, "fonti": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
    "required": ["risposta", "fonti"],
}
ESEMPIO_CHIEDI = {"risposta": "...", "fonti": [3, 1]}

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


class Html(str):
    """Risposta già in HTML (con link): non va ripulita prima dell'invio.
    allegati: file da mandare dopo il messaggio, come [(percorso, didascalia)] (es. il foglio del DCF)."""

    def __new__(cls, testo: str, allegati: list | None = None):
        risposta = super().__new__(cls, testo)
        risposta.allegati = allegati or []
        return risposta


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
    esempi = {"segui": "Mediobanca", "tema": "banche italiane", "smetti": "Eni", "soglia": "6",
              "profilo": "più notizie sul credito", "chiedi": "cosa è successo ai BTP questa settimana?",
              "cerca": "BTP", "pausa": "3h", "azienda": "Apple com'è andato l'ultimo trimestre?"}
    if azione in esempi and not argomento:
        return f"Manca l'argomento: per esempio /{azione} {esempi[azione]}"
    return {"segui": segui, "tema": tema, "smetti": smetti, "soglia": soglia_, "profilo": profilo,
            "notizie": notizie, "stato": stato, "chiedi": chiedi, "oggi": oggi, "cerca": cerca,
            "pausa": pausa, "riprendi": riprendi, "annulla": annulla, "azienda": azienda}[azione](giro, argomento)


# ---------------- domande e archivio ----------------

def chiedi(giro: Giro, domanda: str) -> str:
    """Risponde a una domanda usando l'archivio: notizie e dati più pertinenti, poi l'IA."""
    voci = archivio.cerca(_archivio(giro), domanda, 30)
    if not voci:
        return "Nell'archivio non trovo notizie su questo argomento. Prova con altre parole, oppure /cerca."
    elenco = "\n".join(f"[{k}] {v['quando'][:10]} · {v.get('titolo', '')} — {v.get('riassunto', '')[:400]}"
                       for k, v in enumerate(voci, 1))
    risposta = giro.con_motori(
        lambda m, k: ia.genera(m, k, ISTRUZIONI_CHIEDI, f"DOMANDA: {domanda}\n\nARCHIVIO:\n{elenco}",
                               SCHEMA_CHIEDI, ESEMPIO_CHIEDI), giro.motori())
    if not risposta:
        return "Non riesco a rispondere adesso (IA non disponibile): intanto prova /cerca."
    fonti = []
    for numero in risposta.get("fonti") or []:
        if isinstance(numero, int) and 1 <= numero <= len(voci) and voci[numero - 1].get("link"):
            v = voci[numero - 1]
            fonti.append(f"• <a href=\"{notifiche.esc(v['link'], virgolette=True)}\">{notifiche.esc(v['titolo'][:90])}</a>")
    return Html(f"💬 <b>{notifiche.esc(domanda)}</b>\n\n{notifiche.esc(risposta.get('risposta', ''))}"
                + ("\n\n<i>Fonti:</i>\n" + "\n".join(fonti[:5]) if fonti else ""))


def azienda(giro: Giro, richiesta: str) -> str:
    """Dati della piattaforma defeatbeta su una società: l'IA sceglie quali servono e risponde (aziende.py)."""
    import aziende  # la libreria defeatbeta è pesante: si carica solo per questo comando

    testo, allegati = aziende.domanda(giro, richiesta)
    return Html(_taglia(testo), allegati)


def oggi(giro: Giro, _argomento: str) -> str:
    inizio = giro.adesso.replace(hour=0, minute=0, second=0, microsecond=0)
    inviate = giro.memoria.inviate_dal(inizio)
    if not inviate:
        return "Oggi non è ancora uscita nessuna notizia."
    per_reparto: dict[str, list[dict]] = {}
    for n in inviate:
        per_reparto.setdefault((n.get("reparti") or ["Altro"])[0], []).append(n)
    righe = [f"🗞️ <b>Le notizie di oggi</b> ({len(inviate)})"]
    for reparto, notizie_ in per_reparto.items():
        righe.append(f"\n{notifiche.hashtag(reparto)}")
        righe += [f"• <a href=\"{notifiche.esc(n['link'], virgolette=True)}\">{notifiche.esc(n['titolo'][:110])}</a>"
                  for n in notizie_]
    return Html(_taglia("\n".join(righe)))


def cerca(giro: Giro, parole: str) -> str:
    voci = archivio.cerca(_archivio(giro), parole, 10)
    if not voci:
        return f"Nessuna notizia trovata per «{parole}»."
    righe = [f"🔎 <b>{notifiche.esc(parole)}</b>: {len(voci)} risultati"]
    for v in voci:
        titolo = notifiche.esc(v.get("titolo", "")[:110])
        link = f"<a href=\"{notifiche.esc(v['link'], virgolette=True)}\">{titolo}</a>" if v.get("link") else titolo
        righe.append(f"• {v['quando'][8:10]}/{v['quando'][5:7]} · {link}")
    return Html(_taglia("\n".join(righe)))


def _archivio(giro: Giro) -> list[dict]:
    """Archivio mensile più le notizie in memoria (anche quelle inviate prima che l'archivio esistesse)."""
    voci = archivio.leggi(giro.cartella / "archivio")
    visti = {v.get("link") for v in voci}
    voci += [{**n, "tipo": "notizia"} for n in giro.memoria.inviate if n.get("link") not in visti]
    return voci


def _taglia(testo: str, massimo: int = notifiche.LUNGHEZZA_MASSIMA) -> str:
    """Un messaggio Telegram non può superare i 4096 caratteri visibili (i link non contano): taglio alla riga."""
    if notifiche.visibile(testo) <= massimo:
        return testo
    righe: list[str] = []
    for riga in testo.split("\n"):
        if notifiche.visibile("\n".join([*righe, riga, "…"])) > massimo:
            break
        righe.append(riga)
    return "\n".join([*righe, "…"])


# ---------------- pausa e annulla ----------------

def pausa(giro: Giro, durata: str) -> str:
    trovato = re.search(r"(\d+)\s*(h|ore|ora|g|giorni|giorno|m|min|minuti)?", durata.lower())
    if not trovato:
        return "Indica la durata, per esempio /pausa 3h oppure /pausa 2g."
    quanto, unita = int(trovato.group(1)), (trovato.group(2) or "h")[0]
    delta = timedelta(days=quanto) if unita == "g" else timedelta(minutes=quanto) if unita == "m" else timedelta(hours=quanto)
    fino = datetime.now(timezone.utc) + delta
    giro.memoria.pausa_fino = fino.isoformat(timespec="seconds")
    return (f"⏸️ Invii sospesi fino a {fino.astimezone(giro.fuso):%d/%m alle %H:%M}. "
            f"Scrivi /riprendi per ricominciare prima.")


def riprendi(giro: Giro, _argomento: str) -> str:
    era_in_pausa = giro.memoria.in_pausa()
    giro.memoria.pausa_fino = ""
    return "▶️ Invii ripresi: le notizie tornano ad arrivare ogni ora." if era_in_pausa else "Gli invii non erano in pausa."


def annulla(giro: Giro, _argomento: str) -> str:
    """Rimette la versione precedente del file cambiato dall'ultimo comando."""
    ultima = giro.memoria.ultima_modifica
    if not ultima:
        return "Non c'è nessuna modifica da annullare."
    percorso = giro.cartella / ultima["file"]
    if giro.prova:
        print(f"(prova) {ultima['file']} non ripristinato")
    else:
        percorso.write_text(ultima["contenuto"], "utf-8")
    giro.memoria.ultima_modifica = {}
    if ultima["file"] == "config.yaml":
        giro.config = pyyaml.safe_load(ultima["contenuto"])
        giro.aggiorna_da_config()
    else:
        giro.profilo = ultima["contenuto"]
    return f"↩️ Annullata l'ultima modifica a {ultima['file']} (del {ultima['quando'][8:10]}/{ultima['quando'][5:7]})."


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
    prima = percorso.read_text("utf-8")
    if giro.prova:
        modifiche = [r for r in difflib.unified_diff(prima.splitlines(), testo.splitlines(), lineterm="", n=0)
                     if r.startswith(("+", "-")) and not r.startswith(("+++", "---"))]
        print(f"(prova) {percorso.name} non modificato. Righe che cambierebbero:\n" + "\n".join(modifiche))
        return
    # la versione precedente resta in memoria per /annulla
    giro.memoria.ultima_modifica = {"file": percorso.name, "contenuto": prima,
                                    "quando": datetime.now(timezone.utc).isoformat(timespec="seconds")}
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
