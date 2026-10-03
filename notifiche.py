"""Composizione e invio dei messaggi al bot Telegram."""
from __future__ import annotations

import html
import re
import time
from collections import Counter
from datetime import datetime

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"
FONTI_MASSIME = 6
PULSANTI_PER_RIGA = 2
LUNGHEZZA_MASSIMA = 3800   # Telegram accetta 4096 caratteri visibili per messaggio
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
        "settembre", "ottobre", "novembre", "dicembre"]


def hashtag(reparto: str) -> str:
    """'Risk management' -> '#RiskManagement': toccandolo, Telegram mostra le notizie di quel reparto."""
    return "#" + "".join(parola[:1].upper() + parola[1:] for parola in re.findall(r"\w+", reparto))


def componi(titolo: str, riassunto: str, perche: str, icona: str, tema: str, voto: int | None,
            reparti: list[str], etichetta_perche: str = "Perché conta", nota: str = "") -> str:
    """Notizia in HTML: reparti, icona e titolo, riassunto, perché conta, tema e voto."""
    righe = [" ".join(hashtag(r) for r in reparti)] if reparti else []
    righe.append(f"{icona} <b>{_e(titolo)}</b>")
    if riassunto:
        righe += ["", _e(riassunto)]
    if perche:
        righe += ["", f"🎯 <i>{_e(etichetta_perche)}:</i> {_e(perche)}"]
    info = [x for x in (f"🏷 {_e(tema)}" if tema else "", f"{voto}/10" if voto else "", _e(nota)) if x]
    if info:
        righe += ["", " · ".join(info)]
    return "\n".join(righe)


def riepilogo_notte(voci: list[dict], giorno: datetime, prima_notizia: str) -> str:
    """Buongiorno con il riepilogo della notte, in un unico messaggio.
    voci: dict con titolo, riassunto, icona, reparti, fonti."""
    data = f"{GIORNI[giorno.weekday()]} {giorno.day} {MESI[giorno.month - 1]}"
    fine = f"\n\nDalle {prima_notizia.lstrip('0')} gli aggiornamenti ogni ora."
    if not voci:
        return (f"☀️ <b>Buongiorno!</b>\n<i>{data}</i>\n\n"
                f"Notte tranquilla: nessuna notizia rilevante per il PSP.{fine}")

    testo = f"☀️ <b>Buongiorno! Ecco cosa è successo nella notte</b>\n<i>{data}</i>"
    for numero, v in enumerate(voci, 1):
        righe = [f"\n\n{numero}. " + " ".join(hashtag(r) for r in v["reparti"]),
                 f"{v['icona']} <b>{_e(v['titolo'])}</b>"]
        if v["riassunto"]:
            righe.append(_e(v["riassunto"]))
        righe.append(link_testuali(v["fonti"], massimo=2).strip())
        blocco = "\n".join(righe)
        if _visibile(testo + blocco + fine) > LUNGHEZZA_MASSIMA:
            break
        testo += blocco
    return testo + fine


def chiusura(inviate_oggi: list[dict], apertura: str) -> str:
    """Fine delle comunicazioni, con il conto delle notizie del giorno per reparto."""
    testo = "🌙 <b>Fine delle comunicazioni per oggi</b>\n"
    if inviate_oggi:
        per_reparto = Counter(r for i in inviate_oggi for r in i.get("reparti", []))
        dettaglio = " · ".join(f"{hashtag(r)} {n}" for r, n in per_reparto.most_common())
        testo += f"Oggi {len(inviate_oggi)} notizie" + (f": {dettaglio}" if dettaglio else "") + "."
    else:
        testo += "Oggi nessuna notizia ha superato la soglia di rilevanza."
    return testo + f"\n\nCi risentiamo domani alle {apertura.lstrip('0')} con il riepilogo della notte."


def pulsanti(fonti: list[tuple[str, str]]) -> dict:
    """Un pulsante per testata, che apre l'articolo."""
    tasti = [{"text": f"🔗 {nome}", "url": url} for nome, url in fonti[:FONTI_MASSIME]]
    return {"inline_keyboard": [tasti[i:i + PULSANTI_PER_RIGA] for i in range(0, len(tasti), PULSANTI_PER_RIGA)]}


def link_testuali(fonti: list[tuple[str, str]], massimo: int = FONTI_MASSIME) -> str:
    """I link come testo: nel riepilogo, o se Telegram rifiuta i pulsanti (es. un indirizzo non valido)."""
    link = " · ".join(f'<a href="{html.escape(url, quote=True)}">{_e(nome)}</a>' for nome, url in fonti[:massimo])
    return f"\n🔗 {link}"


def invia(testo: str, token: str, chat_id: str, silenzioso: bool = False,
          tastiera: dict | None = None) -> bool:
    corpo = {"chat_id": chat_id, "text": testo, "parse_mode": "HTML",
             "disable_web_page_preview": True, "disable_notification": silenzioso}
    if tastiera:
        corpo["reply_markup"] = tastiera
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
            return True
        if r.status_code == 429:
            time.sleep(dati.get("parameters", {}).get("retry_after", 5))
            continue
        print(f"  ✗ Telegram ha rifiutato il messaggio: {dati.get('description')}")
        return False
    return False


def _e(testo: str) -> str:
    return html.escape(testo, quote=False)


def _visibile(testo_html: str) -> int:
    """Lunghezza del testo come la conta Telegram: senza i tag HTML."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", testo_html)))
