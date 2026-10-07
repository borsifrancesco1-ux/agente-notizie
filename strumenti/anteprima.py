"""Manda nella chat privata una notizia di esempio, per vedere com'è fatto il messaggio
senza far girare la selezione delle notizie.

Uso, dalla cartella del progetto:
  python strumenti/anteprima.py           la invia (TELEGRAM_TOKEN e TELEGRAM_CHAT_ID nel file .env)
  python strumenti/anteprima.py --prova   la stampa e basta
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

CARTELLA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CARTELLA))
import feedback  # noqa: E402
import notifiche  # noqa: E402


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(CARTELLA / ".env")
    fonti = [("Reuters", "https://www.reuters.com/markets/"), ("Il Sole 24 Ore", "https://www.ilsole24ore.com/")]
    testo = notifiche.componi(
        "ECB holds rates steady, signals patience as inflation nears target",
        "La BCE ha lasciato invariato al 2% il tasso sui depositi, come atteso. Lagarde ha detto che "
        "l'inflazione è vicina all'obiettivo e che il Consiglio non ha fretta di muoversi. I rendimenti "
        "del Bund sono scesi di 5 punti base e l'euro si è rafforzato sul dollaro.",
        "Tassi fermi più a lungo sostengono la parte obbligazionaria e riducono il costo della copertura "
        "del cambio, che dipende dal differenziale con i tassi USA.",
        "🇪🇺", "BCE", 9, ["Obbligazionario", "Macroeconomia", "Copertura"], "Perché conta per il PSP",
        nota="messaggio di prova", impatto="Bund ↑ · BTP ↑ · euro ↑ · dollaro ↓", valori=[("Tasso BCE sui depositi", "2,00%", "", ""), ("EUR/USD", "1,1720", "+0,35%", "")])
    tastiera = feedback.tastiera("anteprima", fonti)
    if "--prova" in sys.argv:
        print(testo)
        return
    numero = notifiche.invia(testo, os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], tastiera=tastiera)
    print("Inviata" if numero else "Non inviata")


if __name__ == "__main__":
    main()
