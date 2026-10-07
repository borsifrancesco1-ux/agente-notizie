"""Quello che arriva dal bot Telegram: voti 👍/👎 sulle notizie e comandi del proprietario.

Due modi, scelti in automatico:
- con il Worker di Cloudflare (WORKER_URL): Telegram consegna tutto al Worker, che risponde subito,
  conta i voti e mette i comandi in coda; l'agente li ritira a ogni giro;
- senza Worker: l'agente legge gli aggiornamenti da Telegram a ogni giro (getUpdates); i voti e i
  comandi funzionano lo stesso, ma la risposta arriva al giro successivo.

I voti servono anche da esempi per l'IA: le notizie apprezzate o bocciate dal team orientano i
giudizi successivi, senza modificare il modello.
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


def anonimo(id_utente: int | str) -> str:
    """Codice anonimo di chi vota: la memoria sta in un repository pubblico, gli ID Telegram no.
    Il Worker di Cloudflare calcola lo stesso codice."""
    return hashlib.sha256(f"voto-{id_utente}".encode()).hexdigest()[:12]


# ---------------- senza Worker: lettura diretta da Telegram ----------------

def leggi_telegram(memoria: Memoria, token: str, proprietario: str) -> list[dict]:
    """Legge voti e messaggi arrivati dall'ultimo giro. Restituisce i comandi del proprietario
    come [{"id": None, "testo": ...}] (le risposte ai pulsanti della proposta diventano /proposta)."""
    try:
        r = requests.get(API.format(token=token, metodo="getUpdates"), timeout=30,
                         params={"offset": memoria.offset_telegram,
                                 "allowed_updates": json.dumps(["callback_query", "message"])})
        risposta = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"Telegram non letto ({type(e).__name__})")
        return []
    if not risposta.get("ok"):  # es. 409: c'è un webhook attivo (Worker), quindi non si legge da qui
        print(f"Telegram non letto: {risposta.get('description')}")
        return []

    comandi, da_aggiornare, voti = [], {}, 0
    for u in risposta.get("result", []):
        memoria.offset_telegram = u["update_id"] + 1
        if "message" in u:
            m = u["message"]
            if str(m.get("chat", {}).get("id")) == str(proprietario) and m.get("text"):
                comandi.append({"id": None, "testo": m["text"]})
            continue
        q = u.get("callback_query") or {}
        dati = q.get("data", "")
        if dati.startswith(("p+", "p-")) and str(q.get("from", {}).get("id")) == str(proprietario):
            comandi.append({"id": None, "testo": f"/proposta {'applica' if dati[1] == '+' else 'ignora'} {dati[2:]}"})
        elif dati.startswith(("v+", "v-")):
            id_notifica = dati[2:]
            if memoria.vota(id_notifica, anonimo(q["from"]["id"]), 1 if dati[1] == "+" else -1):
                voti += 1
                if "message" in q:
                    m = q["message"]
                    da_aggiornare[(m["chat"]["id"], m["message_id"])] = (id_notifica, m.get("reply_markup") or {})

    for (chat_id, message_id), (id_notifica, tastiera_attuale) in da_aggiornare.items():
        _aggiorna_conteggio(token, chat_id, message_id, id_notifica, tastiera_attuale, memoria)
    print(f"Da Telegram: {voti} voti, {len(comandi)} comandi")
    return comandi


# ---------------- con il Worker di Cloudflare ----------------

def leggi_worker(memoria: Memoria, url: str, chiave: str) -> list[dict]:
    """Ritira dal Worker i voti (che sostituiscono quelli in memoria) e i comandi in coda."""
    intestazioni = {"Authorization": f"Bearer {chiave}"}
    try:
        voti = requests.get(f"{url}/agente/voti", headers=intestazioni, timeout=30).json()
        coda = requests.get(f"{url}/agente/coda", headers=intestazioni, timeout=30).json()
    except (requests.RequestException, ValueError) as e:
        print(f"Worker non raggiungibile ({type(e).__name__})")
        return []
    aggiornate = 0
    for notifica in memoria.inviate:
        if notifica.get("id") in voti and notifica.get("voti") != voti[notifica["id"]]:
            notifica["voti"] = voti[notifica["id"]]
            aggiornate += 1
    print(f"Dal Worker: voti aggiornati su {aggiornate} notizie, {len(coda)} comandi in coda")
    return coda


def iscrizioni_worker(url: str, chiave: str) -> dict[str, dict]:
    """Chi ha scelto con /iscrivimi di ricevere in privato le notizie di alcuni reparti.
    Le iscrizioni stanno sul Worker (non nel repository pubblico): {utente: {reparti, modo, chat}}."""
    try:
        r = requests.get(f"{url}/agente/iscrizioni", headers={"Authorization": f"Bearer {chiave}"}, timeout=30)
        return r.json() if r.ok else {}
    except (requests.RequestException, ValueError):
        return {}


def conferma_worker(url: str, chiave: str, ids: list[str]) -> None:
    """Toglie dalla coda del Worker i comandi eseguiti."""
    if not ids:
        return
    try:
        requests.delete(f"{url}/agente/coda", headers={"Authorization": f"Bearer {chiave}"},
                        json={"ids": ids}, timeout=30)
    except requests.RequestException as e:
        print(f"Coda del Worker non aggiornata ({type(e).__name__})")


# ---------------- esempi per l'IA ----------------

def esempi_per_ia(memoria: Memoria, quanti: int = 40) -> str:
    """Le ultime notizie votate, da dare all'IA come esempi di cosa piace al team."""
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


def _aggiorna_conteggio(token: str, chat_id: int, message_id: int, id_notifica: str,
                        tastiera_attuale: dict, memoria: Memoria) -> None:
    link = [riga for riga in tastiera_attuale.get("inline_keyboard", [])
            if not any("callback_data" in tasto for tasto in riga)]
    try:
        requests.post(API.format(token=token, metodo="editMessageReplyMarkup"), timeout=30, json={
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": {"inline_keyboard": [*link, riga_voti(id_notifica, *memoria.conteggio(id_notifica))]},
        })
    except requests.RequestException as e:
        # Mai stampare l'eccezione intera: contiene l'URL con il token del bot.
        print(f"  ✗ conteggio dei voti non aggiornato ({type(e).__name__})")
