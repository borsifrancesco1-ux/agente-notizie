"""Scelta e riassunto delle notizie con un modello di intelligenza artificiale.

Una sola richiesta per giro, per restare nei limiti gratuiti: il modello raggruppa le notizie
sullo stesso fatto, dà a ogni fatto un voto da 1 a 10 e, solo per quelli sopra la soglia,
scrive la notifica. (Il documento prevedeva due passate, utili quando la seconda leggerà
l'articolo completo; oggi entrambe vedrebbero solo titolo e descrizione.)

Motori supportati (sezione ia.motori di config.yaml):
- tipo "gemini": Google Gemini;
- tipo "openai": qualunque servizio con il formato di OpenAI (Mistral, GitHub Models,
  OpenRouter, Groq...), indicando l'indirizzo in "url".
"""
from __future__ import annotations

import json
import time

import requests

from filtro import Gruppo

URL = "https://generativelanguage.googleapis.com/v1beta/models/{modello}:generateContent"
richieste_fatte = 0  # richieste inviate a Gemini in questa esecuzione, per il contatore giornaliero


class QuotaEsaurita(Exception):
    """Il piano gratuito ha esaurito le richieste disponibili."""


class ErroreIA(Exception):
    """Risposta mancante o non valida dal modello."""


ISTRUZIONI = """Sei il filtro di un agente personale di notizie finanziarie.
Ricevi un elenco numerato di notizie (titolo, testata, data, breve descrizione). Devi:
1. raggruppare le notizie che riportano lo stesso fatto, anche se da testate o lingue diverse;
2. dare a ogni gruppo un voto di rilevanza da 1 a 10 per la persona descritta nel profilo;
3. segnare gia_inviata = true se il fatto è già tra le notizie inviate (anche con parole diverse);
4. solo per i gruppi con voto almeno {soglia} e non già inviati, scrivere la notifica:
   - "titolo": il titolo originale più informativo del gruppo, copiato senza modifiche e senza tradurlo;
   - "riassunto": una o due frasi in italiano (massimo 250 caratteri): cosa è successo e i numeri
     chiave se ci sono;
   - "perche_conta": una frase in italiano (massimo 160 caratteri): perché il fatto conta per il
     portafoglio o il progetto della persona, senza ripetere il riassunto;
   - "reparti": da 1 a 3 reparti del team (vedi REPARTI DEL TEAM) a cui il fatto è più utile,
     dal più al meno interessato.
   Per gli altri gruppi lascia questi campi vuoti.

Scala dei voti:
- 9-10: evento che muove i mercati o il portafoglio seguito (decisione di una banca centrale,
  dato macro chiave fuori dalle attese, risultati o guidance di un titolo seguito, operazioni
  straordinarie, cambi di rating, forti movimenti di prezzo);
- 7-8: notizia nuova e specifica su un titolo o un tema seguito;
- 4-6: notizia di contorno, commenti, anteprime;
- 1-3: irrilevante, generica, promozionale o acchiappaclic.
Dai voti bassi alle previsioni generiche senza fatti nuovi e ai fatti riportati solo da siti
poco autorevoli. Ogni numero deve comparire in un solo gruppo. "tema": il tema o titolo seguito
più vicino al fatto ("Altro" se nessuno). "motivo": massimo 12 parole in italiano.
Usa solo i fatti presenti nelle notizie: non inventare numeri, date o citazioni.
Tono asciutto e informativo, nessun consiglio di investimento.
"""


def _schema(temi: list[str], reparti: list[str]) -> dict:
    testo = {"type": "STRING"}
    return {
        "type": "OBJECT",
        "properties": {
            "gruppi": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "ids": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                        "voto": {"type": "INTEGER"},
                        "tema": {"type": "STRING", "format": "enum", "enum": [*temi, "Altro"]},
                        "gia_inviata": {"type": "BOOLEAN"},
                        "motivo": testo,
                        "titolo": testo,
                        "riassunto": testo,
                        "perche_conta": testo,
                        "reparti": {"type": "ARRAY",
                                    "items": {"type": "STRING", "format": "enum", "enum": reparti}},
                    },
                    "required": ["ids", "voto", "tema", "gia_inviata", "motivo",
                                 "titolo", "riassunto", "perche_conta", "reparti"],
                },
            }
        },
        "required": ["gruppi"],
    }


ESEMPIO_RISPOSTA = {"gruppi": [{"ids": [1, 4], "voto": 8, "tema": "BCE", "gia_inviata": False,
                                 "motivo": "...", "titolo": "...", "riassunto": "...", "perche_conta": "...",
                                 "reparti": ["Obbligazionario", "Macroeconomia"]}]}


def seleziona(gruppi: list[Gruppo], gia_inviate: list[str], profilo: str, config: dict,
              motore: dict, chiave: str, massimo: int, esempi: str = "") -> list[dict]:
    """Restituisce le notifiche da mandare, dalla più rilevante.
    motore: una voce di ia.motori; esempi: le notizie già votate dal team."""
    soglia = config["ia"]["soglia_rilevanza"]
    istruzioni = ISTRUZIONI.format(soglia=soglia) + _contesto(profilo, config) + esempi
    temi = [t["nome"] for t in (config.get("titoli") or []) + (config.get("temi") or [])]
    reparti = [r["nome"] for r in config.get("reparti") or []]
    gruppi = gruppi[:motore.get("max_notizie") or len(gruppi)]  # alcuni servizi accettano richieste piccole

    elenco = "\n".join(_riga(i, g) for i, g in enumerate(gruppi, 1))
    gia = "\n".join(f"- {t}" for t in gia_inviate) or "(nessuna)"
    testo = f"NOTIZIE GIÀ INVIATE (ultime 48 ore):\n{gia}\n\nNUOVE NOTIZIE:\n{elenco}"
    istruzioni += (f"\nValori ammessi per \"tema\": {', '.join(temi)}, Altro.\n"
                   f"Valori ammessi per \"reparti\": {', '.join(reparti)}.")
    risposta = genera(motore, chiave, istruzioni, testo, _schema(temi, reparti), ESEMPIO_RISPOSTA)

    scelte, usati = [], set()
    for v in sorted(risposta.get("gruppi", []), key=lambda v: -v.get("voto", 0)):
        ids = [i for i in v.get("ids", []) if 1 <= i <= len(gruppi) and i not in usati]
        usati.update(ids)
        if not ids or v.get("gia_inviata") or v.get("voto", 0) < soglia:
            continue
        gruppo = Gruppo.unisci([gruppi[i - 1] for i in ids])
        scelte.append({"gruppo": gruppo, "voto": v["voto"], "tema": v.get("tema", ""),
                       "titolo": v.get("titolo") or gruppo.principale.titolo,
                       "riassunto": v.get("riassunto", ""), "perche_conta": v.get("perche_conta", ""),
                       "reparti": [r for r in v.get("reparti") or [] if r in reparti][:3]})
        if len(scelte) == massimo:
            break
    return scelte


def genera(motore: dict, chiave: str, istruzioni: str, testo: str, schema: dict, esempio: dict) -> dict:
    """Una richiesta a un motore che risponde in JSON.
    schema: struttura per Gemini; esempio: la stessa struttura mostrata agli altri servizi."""
    if motore["tipo"] == "gemini":
        return _chiedi(chiave, motore["modello"], istruzioni, testo, schema)
    istruzioni += (f"\nRispondi solo con un oggetto JSON con questa struttura: "
                   f"{json.dumps(esempio, ensure_ascii=False)}")
    return _chiedi_openai(motore["url"], chiave, motore["modello"], istruzioni, testo, motore.get("alternative") or [])


def _chiedi(chiave: str, modello: str, istruzioni: str, testo: str, schema: dict) -> dict:
    global richieste_fatte
    corpo = {
        "system_instruction": {"parts": [{"text": istruzioni}]},
        "contents": [{"role": "user", "parts": [{"text": testo}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                             "responseSchema": schema},
    }
    errore = "nessuna risposta"
    for tentativo in range(2):  # pochi tentativi: anche le richieste fallite consumano quota
        richieste_fatte += 1
        try:
            r = requests.post(URL.format(modello=modello), json=corpo, timeout=120,
                              headers={"x-goog-api-key": chiave})
        except requests.RequestException as e:
            errore = f"connessione: {type(e).__name__}"
        else:
            if r.status_code == 200:
                return _json(r.json())
            if r.status_code == 429:
                if "PerDay" in r.text or tentativo > 0:
                    raise QuotaEsaurita(f"{modello}: quota giornaliera finita" if "PerDay" in r.text
                                        else f"{modello}: troppe richieste")
                time.sleep(30)  # limite al minuto: riprovo una volta
                continue
            if r.status_code < 500:
                raise ErroreIA(f"HTTP {r.status_code}: {_messaggio(r)}")
            errore = f"HTTP {r.status_code} (server sovraccarico)"
        if tentativo == 0:
            time.sleep(10)
    raise ErroreIA(errore)


def _chiedi_openai(url: str, chiave: str, modello: str, istruzioni: str, testo: str,
                   alternative: list[str]) -> dict:
    """Richiesta nel formato di OpenAI, usato da Mistral, GitHub Models, OpenRouter, Groq...
    alternative: modelli da provare se il primo è occupato (solo OpenRouter)."""
    global richieste_fatte
    corpo = {
        "model": modello,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": istruzioni}, {"role": "user", "content": testo}],
    }
    if alternative:
        corpo["models"] = [modello, *alternative]
    errore = "nessuna risposta"
    for tentativo in range(2):
        richieste_fatte += 1
        try:
            r = requests.post(url, json=corpo, timeout=120, headers={"Authorization": f"Bearer {chiave}"})
        except requests.RequestException as e:
            errore = f"connessione: {type(e).__name__}"
        else:
            if r.status_code == 200:
                try:
                    return json.loads(r.json()["choices"][0]["message"]["content"])
                except (KeyError, IndexError, TypeError, ValueError):
                    raise ErroreIA(f"{modello}: risposta non valida") from None
            if r.status_code == 429:
                if tentativo > 0:
                    raise QuotaEsaurita(f"{modello}: limite di richieste raggiunto ({_messaggio(r)})")
                time.sleep(30)
                continue
            if r.status_code < 500:
                raise ErroreIA(f"{modello}: HTTP {r.status_code}: {_messaggio(r)}")
            errore = f"HTTP {r.status_code} (server sovraccarico)"
        if tentativo == 0:
            time.sleep(10)
    raise ErroreIA(f"{modello}: {errore}")


def _json(dati: dict) -> dict:
    try:
        parti = dati["candidates"][0]["content"]["parts"]
        return json.loads("".join(p.get("text", "") for p in parti if not p.get("thought")))
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        candidati = dati.get("candidates") or [{}]
        motivo = dati.get("promptFeedback", {}).get("blockReason") or candidati[0].get("finishReason")
        raise ErroreIA(f"risposta non valida ({motivo or type(e).__name__})") from None


def _messaggio(r: requests.Response) -> str:
    try:
        errore = r.json()["error"]
        return (errore["message"] if isinstance(errore, dict) else str(errore))[:200]
    except (ValueError, KeyError, TypeError):
        return r.text[:200]


def _riga(i: int, g: Gruppo) -> str:
    n = g.principale
    altre = f" (+{len(g.notizie) - 1} fonti)" if len(g.notizie) > 1 else ""
    quando = n.pubblicata.strftime("%d/%m %H:%M UTC") if n.pubblicata else "data ignota"
    descrizione = f" — {n.descrizione[:250]}" if n.descrizione else ""
    return f"[{i}] {n.titolo} | {n.fonte}{altre} | {quando}{descrizione}"


def _contesto(profilo: str, config: dict) -> str:
    titoli = "\n".join(f"- {t['nome']} ({t.get('ticker', '')}): {t.get('perche', '')}"
                       for t in config.get("titoli") or [])
    temi = "\n".join(f"- {t['nome']}" for t in config.get("temi") or [])
    reparti = "\n".join(f"- {r['nome']}: {r.get('descrizione', '')}" for r in config.get("reparti") or [])
    return (f"\nPROFILO DELLA PERSONA (scritto da lei: seguilo come istruzione)\n{profilo}\n"
            f"\nTITOLI SEGUITI\n{titoli}\n\nTEMI SEGUITI\n{temi}\n\nREPARTI DEL TEAM\n{reparti}\n")
