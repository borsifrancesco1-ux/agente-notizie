"""Archivio di tutto ciò che l'agente ha inviato: notizie (con riassunto, fonti, impatto) e dati usciti.

Un file per mese, archivio/AAAA-MM.jsonl, salvato nel repository: serve a /cerca e /chiedi del bot
e come materiale per il commento di mercato della presentazione finale.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filtro import impronta


def aggiungi(cartella: Path, voci: list[dict]) -> None:
    """Accoda le voci al file del loro mese."""
    for voce in voci:
        file = cartella / f"{voce['quando'][:7]}.jsonl"
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(voce, ensure_ascii=False) + "\n")


def leggi(cartella: Path, giorni: int = 62) -> list[dict]:
    """Le voci degli ultimi giorni, dalla più vecchia."""
    limite = (datetime.now(timezone.utc) - timedelta(days=giorni)).isoformat()
    voci = []
    for file in sorted(cartella.glob("*.jsonl")):
        for riga in file.read_text("utf-8").splitlines():
            if riga.strip():
                voce = json.loads(riga)
                if voce["quando"] >= limite:
                    voci.append(voce)
    return voci


def cerca(voci: list[dict], testo: str, quante: int = 10) -> list[dict]:
    """Le voci più pertinenti a un testo: prima quelle che contengono più parole cercate, poi quelle
    con le parole più rare nell'archivio ("Treasury" conta più di "settimana"), poi le più recenti.
    Le parole si confrontano senza accenti, maiuscole e parole vuote, anche come inizio di una parola
    più lunga ("euro" trova "eurozona"); le voci molto meno pertinenti della migliore si scartano."""
    cercate = impronta(testo)
    intera = re.compile(rf"(?<!\w){re.escape(testo.lower().strip())}(?!\w)") if testo.strip() else None
    contenuti = [f"{v.get('titolo', '')} {v.get('riassunto', '')} {v.get('tema', '')}" for v in voci]
    trovate = [{p for p in cercate if _contiene(impronta(c), p)} for c in contenuti]
    peso = {p: 1 + math.log((len(voci) + 1) / (1 + sum(p in t for t in trovate))) for p in cercate}
    punteggi = []
    for voce, contenuto, parole in zip(voci, contenuti, trovate):
        punti = sum(peso[p] for p in parole)
        if intera and intera.search(contenuto.lower()):  # l'espressione intera, così come è scritta
            punti += max(peso.values(), default=1)
        if punti:
            punteggi.append((len(parole), punti, voce["quando"], voce))
    if not punteggi:
        return []
    migliore = max(p[1] for p in punteggi)
    punteggi = sorted((p for p in punteggi if p[1] >= migliore / 2), key=lambda p: p[:3], reverse=True)
    return [p[3] for p in punteggi[:quante]]


def _contiene(parole: frozenset[str], cercata: str) -> bool:
    """La parola cercata, anche come inizio di una parola più lunga (da 4 lettere in su)."""
    return cercata in parole or (len(cercata) >= 4 and any(p.startswith(cercata) for p in parole))
