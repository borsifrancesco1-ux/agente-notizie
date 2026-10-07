"""Composizione e invio dei messaggi al bot Telegram."""
from __future__ import annotations

import html
import re
import time
from collections import Counter
from datetime import datetime

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"
API_DOCUMENTO = "https://api.telegram.org/bot{token}/sendDocument"
FONTI_MASSIME = 6
PULSANTI_PER_RIGA = 2
LUNGHEZZA_MASSIMA = 3800   # Telegram accetta 4096 caratteri visibili per messaggio
NOME_MASSIMO = 22          # caratteri del nome in una tabella di valori, per stare nello schermo del telefono
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
        "settembre", "ottobre", "novembre", "dicembre"]


def hashtag(reparto: str) -> str:
    """'Risk management' -> '#RiskManagement': toccandolo, Telegram mostra le notizie di quel reparto."""
    return "#" + "".join(parola[:1].upper() + parola[1:] for parola in re.findall(r"\w+", reparto))


def componi(titolo: str, riassunto: str, perche: str, icona: str, tema: str, voto: int | None,
            reparti: list[str], etichetta_perche: str = "Perché conta", nota: str = "",
            impatto: str = "", etichetta_impatto: str = "Impatto atteso", aggiornamento: bool = False,
            mercati: list | None = None, aggiorna_titolo: str = "", valori: list | None = None) -> str:
    """Notizia in HTML: reparti, icona e titolo, riassunto, perché conta, impatto atteso, mercati,
    tema e voto. aggiornamento: la notizia aggiunge novità a una già inviata (va in risposta a quella);
    aggiorna_titolo: il titolo di quella notizia, se non si può rispondere al suo messaggio
    (es. era nel riepilogo della notte); mercati: come si sono mosse le quotazioni nell'ultima ora;
    valori: prezzo attuale delle società citate e livello di tassi, cambi o materie prime toccati
    (mercati e valori: righe per tabella)."""
    righe = []
    if aggiornamento:
        righe.append("🔄 <b>Aggiornamento</b>" + (f" di «{esc(aggiorna_titolo[:90])}»" if aggiorna_titolo else ""))
    if reparti:
        righe.append(" ".join(hashtag(r) for r in reparti))
    righe.append(titolo_notizia(icona, titolo))
    if riassunto:
        righe += ["", esc(riassunto)]
    # ogni sezione separata da una riga vuota, con l'etichetta in grassetto
    if valori:
        righe += ["", f"💹 <b>Valori</b>\n{tabella(valori)}"]
    if mercati:
        righe += ["", f"📊 <b>Mercati nell'ultima ora</b>\n{tabella(mercati)}"]
    if impatto:
        righe += ["", f"🧭 <b>{esc(etichetta_impatto)}</b>\n{frecce(esc(impatto))}"]
    if perche:
        righe += ["", f"🎯 <b>{esc(etichetta_perche)}</b>\n{esc(perche)}"]
    info = [x for x in (f"🏷 {esc(tema)}" if tema else "", f"{voto}/10" if voto else "", esc(nota)) if x]
    if info:
        righe += ["", " · ".join(info)]
    return "\n".join(righe)


def tabella(righe: list) -> str:
    """Valori in un blocco a larghezza fissa, come i numeri della settimana: nome a sinistra, poi
    livello, variazione e data allineati a destra. righe: tuple di testi, es. ("EUR/USD", "1,1257",
    "+0,11%", ""); un testo semplice è una nota sotto la riga prima (es. capitalizzazione e P/E)."""
    tabellari = [r for r in righe if not isinstance(r, str)]
    if not tabellari:
        return ""
    colonne = max(len(r) for r in tabellari)
    larghezze = [max((len(r[i]) for r in tabellari if i < len(r)), default=0) for i in range(colonne)]
    larghezze[0] = min(larghezze[0], NOME_MASSIMO)
    testo = []
    for r in righe:
        if isinstance(r, str):
            testo.append(f"  {r}")
            continue
        celle = [r[0][:NOME_MASSIMO].ljust(larghezze[0])]
        celle += [(r[i] if i < len(r) else "").rjust(larghezze[i]) for i in range(1, colonne) if larghezze[i]]
        testo.append(" ".join(celle).rstrip())
    return f"<pre>{esc(chr(10).join(testo))}</pre>"


def titolo_notizia(icona: str, titolo: str) -> str:
    """Il titolo ben distinto dal resto: icona, grassetto e sottolineato."""
    return f"{icona} <b><u>{esc(titolo)}</u></b>"


def frecce(impatto: str) -> str:
    """'Bund ↓ · euro ↑' -> una voce per riga con 🟢⬆️ / 🔴⬇️, più leggibili delle frecce semplici."""
    impatto = impatto.replace("↑", "🟢⬆️").replace("↓", "🔴⬇️").replace("→", "⚪️➡️")
    return "\n".join(voce.strip() for voce in impatto.split("·") if voce.strip())


def riepilogo_notte(voci: list[dict], giorno: datetime, prima_notizia: str, calendario: list[str],
                    cambio: str = "", ogni_ore: int = 1, mercati: list | None = None) -> str:
    """Buongiorno in un unico messaggio: cambio del giorno, mercati, calendario e riepilogo della notte.
    voci: dict con titolo, riassunto, icona, reparti, fonti; calendario e cambio: testo già pronto;
    mercati: righe per tabella;
    ogni_ore: ogni quante ore arriva un giro di notizie."""
    data = f"{GIORNI[giorno.weekday()]} {giorno.day} {MESI[giorno.month - 1]}"
    fine = f"\n\nDalle {prima_notizia.lstrip('0')} gli aggiornamenti {cadenza(ogni_ore)}."
    testo = f"☀️ <b>Buongiorno!</b>\n<i>{data}</i>"
    if cambio:
        testo += f"\n\n💱 {cambio}"
    if mercati:
        testo += f"\n\n📈 <b>Mercati</b> <i>(con la data: chiusura di un giorno precedente)</i>\n{tabella(mercati)}"
    if calendario:
        testo += "\n\n📅 <b>Oggi in calendario</b> (ora italiana)\n" + "\n".join(calendario[:15])
    else:
        testo += "\n\n📅 Nessun appuntamento importante in calendario oggi."
    if not voci:
        return testo + f"\n\n🌙 Notte tranquilla: nessuna notizia rilevante per il PSP.{fine}"

    testo += "\n\n🌙 <b>Cosa è successo nella notte</b>"
    for numero, v in enumerate(voci, 1):
        righe = [f"\n\n{numero}. " + " ".join(hashtag(r) for r in v["reparti"]),
                 titolo_notizia(v["icona"], v["titolo"])]
        if v["riassunto"]:
            righe.append(esc(breve(v["riassunto"])))
        righe.append(link_testuali(v["fonti"], massimo=2).strip())
        blocco = "\n".join(righe)
        if visibile(testo + blocco + fine) > LUNGHEZZA_MASSIMA:
            break
        testo += blocco
    return testo + fine


def cadenza(ogni_ore: int) -> str:
    """2 -> 'ogni 2 ore'; 1 -> 'ogni ora'."""
    return "ogni ora" if int(ogni_ore) <= 1 else f"ogni {ogni_ore} ore"


def breve(testo: str, massimo: int = 280) -> str:
    """Le prime frasi intere che stanno in 'massimo' caratteri (per il riepilogo della notte)."""
    if len(testo) <= massimo:
        return testo
    frasi = re.split(r"(?<=[.!?])\s+", testo)
    risultato = frasi[0]
    for frase in frasi[1:]:
        if len(risultato) + 1 + len(frase) > massimo:
            break
        risultato += " " + frase
    return risultato if len(risultato) <= massimo else risultato[:massimo].rsplit(" ", 1)[0] + "…"


def chiusura(inviate_oggi: list[dict], apertura: str, mercati: list | None = None) -> str:
    """Fine delle comunicazioni, con i mercati a fine giornata e il conto delle notizie del giorno per reparto.
    mercati: righe per tabella (dati.quadro_mercati)."""
    testo = "🌙 <b>Fine delle comunicazioni per oggi</b>\n"
    if inviate_oggi:
        per_reparto = Counter(r for i in inviate_oggi for r in i.get("reparti", []))
        dettaglio = " · ".join(f"{hashtag(r)} {n}" for r, n in per_reparto.most_common())
        testo += f"Oggi {len(inviate_oggi)} notizie" + (f": {dettaglio}" if dettaglio else "") + "."
    else:
        testo += "Oggi nessuna notizia ha superato la soglia di rilevanza."
    if mercati:
        testo += f"\n\n📈 <b>Mercati a fine giornata</b> <i>(variazione sulla chiusura precedente)</i>\n{tabella(mercati)}"
    return testo + f"\n\nCi risentiamo domani alle {apertura.lstrip('0')} con il riepilogo della notte."


def riepilogo_serale(notizie: list[dict], reparti: list[str]) -> str:
    """Per chi è iscritto in modalità "sera": le notizie del giorno dei suoi reparti, in un messaggio."""
    etichette = " ".join(hashtag(r) for r in reparti)
    if not notizie:
        return f"🌙 Oggi nessuna notizia per i tuoi reparti ({etichette})."
    testo = f"🌙 <b>Le notizie di oggi per i tuoi reparti</b> ({etichette})"
    for n in notizie:
        riga = f"\n• <a href=\"{esc(n['link'], virgolette=True)}\">{esc(n['titolo'][:110])}</a>"
        if visibile(testo + riga) > LUNGHEZZA_MASSIMA:
            break
        testo += riga
    return testo


def dividi(testo: str, massimo: int = LUNGHEZZA_MASSIMA) -> list[str]:
    """Spezza un testo lungo in più messaggi, tra un paragrafo e l'altro."""
    parti, attuale = [], ""
    for paragrafo in testo.split("\n\n"):
        candidato = f"{attuale}\n\n{paragrafo}" if attuale else paragrafo
        if attuale and visibile(candidato) > massimo:
            parti.append(attuale)
            attuale = paragrafo
        else:
            attuale = candidato
    return [*parti, attuale] if attuale else parti


def pulsanti(fonti: list[tuple[str, str]]) -> dict:
    """Un pulsante per testata, che apre l'articolo."""
    tasti = [{"text": f"🔗 {nome}", "url": url} for nome, url in fonti[:FONTI_MASSIME]]
    return {"inline_keyboard": [tasti[i:i + PULSANTI_PER_RIGA] for i in range(0, len(tasti), PULSANTI_PER_RIGA)]}


def link_testuali(fonti: list[tuple[str, str]], massimo: int = FONTI_MASSIME) -> str:
    """I link come testo: nel riepilogo, o se Telegram rifiuta i pulsanti (es. un indirizzo non valido)."""
    link = " · ".join(f'<a href="{html.escape(url, quote=True)}">{esc(nome)}</a>' for nome, url in fonti[:massimo])
    return f"\n🔗 {link}"


def invia(testo: str, token: str, chat_id: str, silenzioso: bool = False,
          tastiera: dict | None = None, rispondi_a: int | None = None) -> int:
    """Manda il messaggio. Restituisce il suo numero (0 se non è partito).
    rispondi_a: numero di un messaggio a cui rispondere (es. aggiornamento di una notizia)."""
    corpo = {"chat_id": chat_id, "text": testo, "parse_mode": "HTML",
             "disable_web_page_preview": True, "disable_notification": silenzioso}
    if tastiera:
        corpo["reply_markup"] = tastiera
    if rispondi_a:
        corpo["reply_parameters"] = {"message_id": rispondi_a, "allow_sending_without_reply": True}
    for _ in range(3):
        try:
            r = requests.post(API.format(token=token), json=corpo, timeout=30)
            dati = r.json()
        except (requests.RequestException, ValueError) as e:
            # Mai stampare l'eccezione intera: contiene l'URL con il token del bot.
            print(f"  ✗ Telegram non raggiungibile ({type(e).__name__})")
            time.sleep(5)
            continue
        if dati.get("ok"):
            return dati["result"]["message_id"]
        if r.status_code == 429:
            time.sleep(dati.get("parameters", {}).get("retry_after", 5))
            continue
        print(f"  ✗ Telegram ha rifiutato il messaggio: {dati.get('description')}")
        return 0
    return 0


def invia_file(percorso, token: str, chat_id: str, didascalia: str = "") -> bool:
    """Manda un file come documento (Telegram accetta fino a 50 MB); didascalia in HTML, max 1024 caratteri."""
    try:
        with open(percorso, "rb") as f:
            r = requests.post(API_DOCUMENTO.format(token=token), timeout=120,
                              data={"chat_id": chat_id, "caption": didascalia[:1000], "parse_mode": "HTML"},
                              files={"document": (percorso.name, f)})
        dati = r.json()
    except (OSError, requests.RequestException, ValueError) as e:
        print(f"  ✗ file non inviato ({type(e).__name__})")  # mai l'eccezione intera: contiene il token
        return False
    if not dati.get("ok"):
        print(f"  ✗ Telegram ha rifiutato il file: {dati.get('description')}")
    return bool(dati.get("ok"))


def esc(testo: str, virgolette: bool = False) -> str:
    """Testo sicuro dentro un messaggio HTML di Telegram (virgolette=True dentro un attributo)."""
    return html.escape(str(testo), quote=virgolette)


def visibile(testo_html: str) -> int:
    """Lunghezza del testo come la conta Telegram: senza i tag HTML."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", testo_html)))
