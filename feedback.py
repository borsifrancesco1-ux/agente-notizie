"""Voti 👍/👎 del team sulle notifiche.

I pulsanti di voto stanno sotto ogni notizia. A ogni giro l'agente legge i voti arrivati,
aggiorna il conteggio sui pulsanti e passa le notizie votate a Gemini come esempi:
così il giudizio si adatta ai gusti del team senza modificare il modello.
"""
from __future__ import annotations

import hashlib
import json

import requests

import notifiche
from memoria import Memoria

API = "https://api.telegram.org/bot{token}/{metodo}"


def riga_voti(id_notifica: str, su: int = 0, giu: int = 0) -> list[dict]:
    return [{"text": f"👍 {su}" if su else "👍", "callback_data": f"v+{id_notifica}"},
            {"text": f"👎 {giu}" if giu else "👎", "callback_data": f"v-{id_notifica}"}]


def tastiera(id_notifica: str, fonti: list[tuple[str, str]], con_link: bool = True) -> dict:
    """Pulsanti delle fonti (se con_link) e, sotto, i pulsanti di voto."""
    righe = notifiche.pulsanti(fonti)["inline_keyboard"] if con_link else []
    return {"inline_keyboard": [*righe, riga_voti(id_notifica)]}


def leggi_voti(memoria: Memoria, token: str) -> int:
    """Legge i voti arrivati dall'ultimo giro e aggiorna i contatori sui pulsanti.
    Restituisce il numero di voti letti."""
    try:
        r = requests.get(API.format(token=token, metodo="getUpdates"), timeout=30,
                         params={"offset": memoria.offset_telegram,
                                 "allowed_updates": json.dumps(["callback_query"])})
        aggiornamenti = r.json().get("result", [])
    except (requests.RequestException, ValueError) as e:
        print(f"Voti non letti ({type(e).__name__})")
        return 0

    voti, da_aggiornare = 0, {}
    for u in aggiornamenti:
        memoria.offset_telegram = u["update_id"] + 1
        q = u.get("callback_query") or {}
        dati = q.get("data", "")
        if not dati.startswith(("v+", "v-")):
            continue
        id_notifica = dati[2:]
        if memoria.vota(id_notifica, anonimo(q["from"]["id"]), 1 if dati[1] == "+" else -1):
            voti += 1
            if "message" in q:
                m = q["message"]
                da_aggiornare[(m["chat"]["id"], m["message_id"])] = (id_notifica, m.get("reply_markup") or {})

    for (chat_id, message_id), (id_notifica, tastiera_attuale) in da_aggiornare.items():
        link = [riga for riga in tastiera_attuale.get("inline_keyboard", [])
                if not any("callback_data" in tasto for tasto in riga)]
        _chiama(token, "editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": [*link, riga_voti(id_notifica, *memoria.conteggio(id_notifica))]},
        })
    return voti


def anonimo(id_utente: int | str) -> str:
    """Codice anonimo di chi vota: la memoria sta in un repository pubblico, gli ID Telegram no."""
    return hashlib.sha256(f"voto-{id_utente}".encode()).hexdigest()[:12]


def esempi_per_ia(memoria: Memoria, quanti: int = 40) -> str:
    """Le ultime notizie votate, da dare a Gemini come esempi di cosa piace al team."""
    votate = memoria.notifiche_votate(quanti)
    if not votate:
        return ""
    riga = lambda i: f"- [{i.get('tema') or '?'}] {i['titolo']}"  # noqa: E731
    piaciute = [riga(i) for i in votate if sum(i["voti"].values()) > 0]
    scartate = [riga(i) for i in votate if sum(i["voti"].values()) < 0]
    testo = ("\nGIUDIZI DEL TEAM SULLE NOTIFICHE PASSATE. Usali come esempi: dai voti più alti ai fatti "
             "simili a quelli apprezzati e più bassi a quelli simili ai non apprezzati.\n")
    if piaciute:
        testo += "Apprezzate (👍):\n" + "\n".join(piaciute) + "\n"
    if scartate:
        testo += "Non apprezzate (👎):\n" + "\n".join(scartate) + "\n"
    return testo


def _chiama(token: str, metodo: str, corpo: dict) -> None:
    try:
        requests.post(API.format(token=token, metodo=metodo), json=corpo, timeout=30)
    except requests.RequestException as e:
        # Mai stampare l'eccezione intera: contiene l'URL con il token del bot.
        print(f"  ✗ Telegram {metodo} non riuscito ({type(e).__name__})")
