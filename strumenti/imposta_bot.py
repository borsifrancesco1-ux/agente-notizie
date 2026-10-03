"""Collega il bot Telegram al Worker di Cloudflare (o lo scollega) e imposta il menu dei comandi.

Uso, dalla cartella del progetto:
  python strumenti/imposta_bot.py            collega il bot al Worker (WORKER_URL nel file .env)
  python strumenti/imposta_bot.py --rimuovi  scollega: l'agente torna a leggere Telegram a ogni giro
  python strumenti/imposta_bot.py --stato    mostra dove il bot consegna gli aggiornamenti
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

COMANDI = [
    ("segui", "Aggiungi un titolo da seguire"),
    ("tema", "Aggiungi un tema da seguire"),
    ("smetti", "Smetti di seguire un titolo o un tema"),
    ("soglia", "Cambia la soglia di rilevanza (1-10)"),
    ("profilo", "Aggiungi un'indicazione al profilo"),
    ("notizie", "Fai subito un giro di notizie"),
    ("stato", "Com'è andata oggi"),
    ("aiuto", "Elenco dei comandi"),
]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rimuovi", action="store_true")
    parser.add_argument("--stato", action="store_true")
    args = parser.parse_args()
    api = f"https://api.telegram.org/bot{os.environ['TELEGRAM_TOKEN']}"

    if args.rimuovi:
        print("Scollegato:", requests.post(f"{api}/deleteWebhook", timeout=30).json().get("ok"))
    elif not args.stato:
        r = requests.post(f"{api}/setWebhook", timeout=30, json={
            "url": os.environ["WORKER_URL"].rstrip("/") + "/telegram",
            "secret_token": os.environ["WEBHOOK_SECRET"],
            "allowed_updates": ["message", "callback_query"],
        }).json()
        print("Collegato al Worker:", r.get("ok"), r.get("description", ""))
        r = requests.post(f"{api}/setMyCommands", timeout=30, json={
            "commands": [{"command": c, "description": d} for c, d in COMANDI]}).json()
        print("Menu dei comandi:", r.get("ok"))

    info = requests.get(f"{api}/getWebhookInfo", timeout=30).json().get("result", {})
    print("Stato:", json.dumps({k: info.get(k) for k in ("url", "pending_update_count", "last_error_message")},
                               ensure_ascii=False))


if __name__ == "__main__":
    main()
