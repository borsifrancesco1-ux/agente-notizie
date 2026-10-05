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

import difflib
import json
import time

import requests

from filtro import Gruppo, autorevolezza

URL = "https://generativelanguage.googleapis.com/v1beta/models/{modello}:generateContent"
richieste_fatte = 0  # richieste inviate a Gemini in questa esecuzione, per il contatore giornaliero


class QuotaEsaurita(Exception):
    """Il piano gratuito ha esaurito le richieste disponibili."""


class ErroreIA(Exception):
    """Risposta mancante o non valida dal modello."""


ISTRUZIONI = """Sei il filtro di un agente di notizie finanziarie per un team.
Ricevi le notizie già inviate di recente (ognuna con un codice, es. A3) e un elenco numerato di notizie
nuove (titolo, testata con il tipo di fonte, data, breve descrizione). Devi:
1. raggruppare le notizie nuove sullo stesso fatto o su sviluppi strettamente collegati della stessa
   vicenda, anche da testate o lingue diverse: ogni gruppo diventa UNA sola notifica, quindi è meglio
   un gruppo con più fonti che più notifiche quasi uguali;
2. dare a ogni gruppo un voto di rilevanza da 1 a 10 per il team descritto nel profilo;
3. confrontare ogni gruppo con le notizie già inviate:
   - stesso fatto senza novità importanti: "gia_inviata" = true e "aggiorna" vuoto;
   - stesso fatto con novità importanti (nuovi numeri, reazioni, decisioni): "gia_inviata" = false e
     "aggiorna" = codice della notizia già inviata (es. "A3"); la notifica racconta cosa c'è di nuovo;
   - fatto nuovo: "gia_inviata" = false e "aggiorna" vuoto;
4. per i gruppi con voto almeno {soglia} e "gia_inviata" = false (fatti nuovi e aggiornamenti),
   scrivere la notifica completa:
   - "titolo": il titolo originale più informativo del gruppo, preferibilmente della fonte più
     autorevole, copiato senza modifiche e senza tradurlo;
   - "riassunto": tre o quattro frasi in italiano (massimo 500 caratteri) che uniscono quanto riportano
     tutte le notizie del gruppo: cosa è successo, i numeri chiave, il contesto (cause, precedenti,
     reazione dei mercati);
   - "perche_conta": una o due frasi in italiano (massimo 220 caratteri): perché il fatto conta per il
     portafoglio o il progetto, senza ripetere il riassunto;
   - "impatto": il possibile effetto sulle attività del portafoglio, solo tra quelle elencate in
     ATTIVITÀ DEL PORTAFOGLIO, con una freccia ↑ o ↓ ciascuna (es. "Treasury ↓ · dollaro ↑ · BTP ↓");
     niente voci generiche come "geopolitica", "energia" o "volatilità"; vuoto se l'effetto non è
     ragionevolmente chiaro;
   - "reparti": da 1 a 3 reparti del team (vedi REPARTI DEL TEAM) a cui il fatto è più utile,
     dal più al meno interessato;
   - "aziende": le società quotate al centro del fatto, al massimo 3, ognuna con "nome" e "simbolo" su
     Yahoo Finance (es. Apple "AAPL", Eni "ENI.MI", LVMH "MC.PA", SAP "SAP.DE"); vuoto se il fatto non
     riguarda società precise;
   - "valori": da 0 a 3 valori di mercato toccati dal fatto, scelti tra VALORI DI MERCATO: un tasso per
     le notizie su banche centrali e titoli di Stato, un cambio per quelle sulle valute, una materia prima
     per quelle su petrolio, gas o metalli, un indice per quelle sulle borse; vuoto se nessuno è pertinente.
   Per gli altri gruppi lascia questi campi vuoti.

Scala dei voti:
- 9-10: evento che muove i mercati o il portafoglio seguito (decisione di una banca centrale,
  dato macro chiave fuori dalle attese, cambi di rating sovrani, forti movimenti di tassi, spread,
  cambi o borse); il 9-10 fa suonare il telefono, quindi usalo solo per fatti davvero importanti;
- 7-8: notizia nuova e specifica su un tema seguito; comunicati e discorsi di BCE, Fed
  e Banca d'Italia sull'economia e sui tassi;
- 6: analisi, commento o notizia di contesto utile ad almeno uno dei reparti del team;
I TITOLI SEGUITI (es. Eni) sono fuori dal portafoglio gestito: notizie di routine su di loro (contratti,
scoperte, dichiarazioni, prezzi dei carburanti, controllate) valgono al massimo 5; solo eventi
straordinari sul titolo (risultati molto sorprendenti, operazioni societarie, crolli) arrivano a 9.
- 4-5: notizia marginale, ripetitiva o anteprima senza contenuti;
- 1-3: irrilevante, generica, promozionale o acchiappaclic.
Tipi di fonte: (istituzione) banca centrale o autorità; (testata) testata giornalistica principale;
(ricerca web) sito trovato con una ricerca. A parità di contenuto preferisci istituzioni e testate;
dai voti bassi ai fatti riportati solo da siti poco autorevoli e alle previsioni generiche senza fatti
nuovi. Ogni numero deve comparire in un solo gruppo. "tema": il tema o titolo seguito più vicino al
fatto ("Altro" se nessuno). "motivo": massimo 12 parole in italiano.
Usa solo i fatti presenti nelle notizie: non inventare numeri, date o citazioni. L'impatto è una
valutazione prudente dei possibili effetti, non un consiglio di investimento. Tono asciutto e informativo.
"""

TIPO_FONTE = {0: "istituzione", 1: "testata", 2: "testata", 3: "ricerca web"}


def _schema(temi: list[str], reparti: list[str], valori: list[str]) -> dict:
    testo = {"type": "STRING"}
    valore = {"type": "STRING", "format": "enum", "enum": valori} if valori else testo
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
                        "aggiorna": testo,
                        "motivo": testo,
                        "titolo": testo,
                        "riassunto": testo,
                        "perche_conta": testo,
                        "impatto": testo,
                        "reparti": {"type": "ARRAY",
                                    "items": {"type": "STRING", "format": "enum", "enum": reparti}},
                        "aziende": {"type": "ARRAY", "items": {
                            "type": "OBJECT", "properties": {"nome": testo, "simbolo": testo},
                            "required": ["nome", "simbolo"]}},
                        "valori": {"type": "ARRAY", "items": valore},
                    },
                    "required": ["ids", "voto", "tema", "gia_inviata", "aggiorna", "motivo",
                                 "titolo", "riassunto", "perche_conta", "impatto", "reparti", "aziende", "valori"],
                },
            }
        },
        "required": ["gruppi"],
    }


ESEMPIO_RISPOSTA = {"gruppi": [{"ids": [1, 4], "voto": 8, "tema": "BCE", "gia_inviata": False, "aggiorna": "",
                                 "motivo": "...", "titolo": "...", "riassunto": "...", "perche_conta": "...",
                                 "impatto": "Bund ↓ · euro ↑", "reparti": ["Obbligazionario", "Macroeconomia"],
                                 "aziende": [], "valori": ["Tasso BCE sui depositi", "EUR/USD"]}]}


def seleziona(gruppi: list[Gruppo], recenti: list[dict], profilo: str, config: dict,
              motore: dict, chiave: str, massimo: int, esempi: str = "") -> list[dict]:
    """Restituisce le notifiche da mandare, dalla più rilevante.
    recenti: le notifiche inviate di recente (per riconoscere doppioni e aggiornamenti);
    motore: una voce di ia.motori; esempi: le notizie già votate dal team."""
    opzioni = config["ia"]
    soglia = opzioni["soglia_rilevanza"]
    soglie_reparti = opzioni.get("soglie_reparti") or {}
    soglia_ufficiali = opzioni.get("soglia_ufficiali", soglia)
    soglia_minima = min([soglia, soglia_ufficiali, *soglie_reparti.values()])
    soglie_titoli = {t["nome"]: t["soglia"] for t in config.get("titoli") or [] if t.get("soglia")}
    istruzioni = ISTRUZIONI.format(soglia=soglia_minima) + _contesto(profilo, config) + esempi
    temi = [t["nome"] for t in (config.get("titoli") or []) + (config.get("temi") or [])]
    reparti = [r["nome"] for r in config.get("reparti") or []]
    valori = list(config.get("quotazioni") or {})
    gruppi = gruppi[:motore.get("max_notizie") or len(gruppi)]  # alcuni servizi accettano richieste piccole

    elenco = "\n".join(_riga(i, g) for i, g in enumerate(gruppi, 1))
    gia = "\n".join(f"[A{k}] {r['titolo']}" + (f" — {r['riassunto'][:150]}" if r.get("riassunto") else "")
                    for k, r in enumerate(recenti, 1)) or "(nessuna)"
    testo = f"NOTIZIE GIÀ INVIATE (ultime 48 ore):\n{gia}\n\nNUOVE NOTIZIE:\n{elenco}"
    istruzioni += (f"\nValori ammessi per \"tema\": {', '.join(temi)}, Altro.\n"
                   f"Valori ammessi per \"reparti\": {', '.join(reparti)}.\n"
                   f"VALORI DI MERCATO (valori ammessi per \"valori\"): {', '.join(valori)}.")
    risposta = genera(motore, chiave, istruzioni, testo, _schema(temi, reparti, valori), ESEMPIO_RISPOSTA)

    scelte, usati = [], set()
    for v in sorted(risposta.get("gruppi", []), key=lambda v: -v.get("voto", 0)):
        ids = [i for i in v.get("ids", []) if 1 <= i <= len(gruppi) and i not in usati]
        usati.update(ids)
        codice = (v.get("aggiorna") or "").strip().upper().lstrip("A")
        aggiorna = recenti[int(codice) - 1] if codice.isdigit() and 1 <= int(codice) <= len(recenti) else None
        if not ids or (v.get("gia_inviata") and not aggiorna):
            continue
        if aggiorna and not (v.get("riassunto") or "").strip():
            continue  # un aggiornamento senza testo non dice cosa c'è di nuovo
        gruppo = Gruppo.unisci([gruppi[i - 1] for i in ids])
        reparti_gruppo = [r for r in v.get("reparti") or [] if r in reparti][:3]
        # Soglia del gruppo: la più bassa tra quella generale, quelle dei suoi reparti e,
        # se contiene un comunicato ufficiale, quella delle istituzioni
        soglia_gruppo = min([soglia, *[soglie_reparti[r] for r in reparti_gruppo if r in soglie_reparti],
                             *([soglia_ufficiali] if gruppo.ufficiale else [])])
        # I titoli seguiti con una soglia propria (es. Eni, fuori dal portafoglio) passano solo sopra quella
        solo_titoli = gruppo.titoli and gruppo.titoli <= soglie_titoli.keys() and not gruppo.temi
        if v.get("tema") in soglie_titoli or solo_titoli:
            soglia_gruppo = max(soglia_gruppo, *[soglie_titoli[t] for t in {v.get("tema"), *gruppo.titoli}
                                                 if t in soglie_titoli])
        if v.get("voto", 0) < soglia_gruppo:
            continue
        scelte.append({"gruppo": gruppo, "voto": v["voto"], "tema": v.get("tema", ""),
                       "titolo": titolo_originale(v.get("titolo") or "", gruppo),
                       "riassunto": v.get("riassunto", ""), "perche_conta": v.get("perche_conta", ""),
                       "impatto": v.get("impatto", ""), "reparti": reparti_gruppo, "aggiorna": aggiorna,
                       "aziende": [a for a in v.get("aziende") or [] if isinstance(a, dict) and a.get("simbolo")][:3],
                       "valori": [x for x in v.get("valori") or [] if x in valori][:3]})
        if len(scelte) == massimo:
            break
    return scelte


def titolo_originale(proposto: str, gruppo: Gruppo) -> str:
    """Il titolo vero di una delle fonti del gruppo. Il modello a volte cambia una parola mentre lo copia
    (es. "Delegates Say" diventato "Delegates Stay"): si prende il titolo originale più simile a quello
    scelto; se non ne somiglia nessuno (titolo tradotto o riscritto), quello della fonte più autorevole."""
    def normale(titolo: str) -> str:
        return " ".join(titolo.lower().split())

    def somiglianza(titolo: str) -> float:
        return difflib.SequenceMatcher(None, normale(titolo), normale(proposto)).ratio()

    originali = [n.titolo for n in gruppo.notizie if n.titolo]
    if not proposto.strip() or not originali:
        return gruppo.principale.titolo
    migliore = max(originali, key=somiglianza)
    return migliore if somiglianza(migliore) >= 0.6 else gruppo.principale.titolo


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
    return f"[{i}] {n.titolo} | {n.fonte} ({TIPO_FONTE[autorevolezza(n)]}){altre} | {quando}{descrizione}"


def _contesto(profilo: str, config: dict) -> str:
    titoli = "\n".join(f"- {t['nome']} ({t.get('ticker', '')}): {t.get('perche', '')}"
                       for t in config.get("titoli") or [])
    temi = "\n".join(f"- {t['nome']}" for t in config.get("temi") or [])
    reparti = "\n".join(f"- {r['nome']}: {r.get('descrizione', '')}" for r in config.get("reparti") or [])
    attivita = ", ".join((config.get("notifiche") or {}).get("attivita_impatto") or [])
    return (f"\nPROFILO DELLA PERSONA (scritto da lei: seguilo come istruzione)\n{profilo}\n"
            f"\nTITOLI SEGUITI\n{titoli}\n\nTEMI SEGUITI\n{temi}\n\nREPARTI DEL TEAM\n{reparti}\n"
            f"\nATTIVITÀ DEL PORTAFOGLIO (per l'impatto atteso): {attivita}\n")
