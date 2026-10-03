"""Appuntamenti settimanali: riepilogo per il PSP e proposta di modifica al profilo.

- Riepilogo (sul canale, e salvato in riepiloghi/ nel repository): i numeri della settimana,
  i dati usciti e le notizie inviate, commentati per reparto, con frasi pronte per il commento
  di mercato della presentazione finale.
- Proposta (nella chat privata): in base ai voti 👍/👎 del team, l'IA propone come cambiare
  profilo.md; si applica o si ignora con un pulsante.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import re
from datetime import datetime, timedelta

import notifiche

ISTRUZIONI_RIEPILOGO = """Sei l'analista del team WhiteRock, che segue il Portfolio Simulation Project (PSP)
descritto nel profilo. Scrivi in italiano il riepilogo della settimana appena trascorsa.
Usa solo i numeri e i fatti forniti (numeri dei mercati, dati ufficiali usciti, notizie inviate):
non inventare dati, date o citazioni. Tono da commento di mercato professionale, frasi brevi.
- "in_sintesi": 3-4 frasi sul quadro della settimana;
- "reparti": per ciascun reparto del team, 2-4 frasi su cosa è successo e cosa significa per il
  portafoglio; se per un reparto non c'è nulla di rilevante, dillo in una frase;
- "da_guardare": 2-3 frasi sugli appuntamenti della prossima settimana più importanti per il PSP;
- "per_la_presentazione": 2-3 frasi pronte da usare nel commento di mercato della presentazione finale.
"""

ISTRUZIONI_PROPOSTA = """Aiuti a migliorare il profilo che guida un agente di notizie finanziarie.
Ricevi il profilo attuale e i giudizi del team (👍 apprezzata, 👎 non apprezzata) sulle notizie
inviate di recente. Proponi al massimo 3 modifiche al profilo che avvicinino le notizie future ai
gusti del team: per esempio dare più peso a un tema apprezzato o escludere un tipo di notizia
sempre bocciata. Se i giudizi non indicano nulla di chiaro, "proponi" deve essere false.
Mantieni struttura, tono e tutte le informazioni del profilo: cambia solo il necessario.
- "spiegazione": le modifiche proposte, una per riga, ciascuna di massimo 140 caratteri;
- "nuovo_profilo": il testo completo del profilo modificato, in Markdown.
"""

MIN_NOTIZIE_VOTATE = 4  # sotto questo numero i voti dicono troppo poco per proporre modifiche


def schema_riepilogo(reparti: list[str]) -> tuple[dict, dict]:
    """Struttura della risposta per Gemini, ed esempio equivalente per gli altri servizi."""
    testo = {"type": "STRING"}
    schema = {
        "type": "OBJECT",
        "properties": {
            "in_sintesi": testo,
            "reparti": {"type": "ARRAY", "items": {
                "type": "OBJECT",
                "properties": {"reparto": {"type": "STRING", "format": "enum", "enum": reparti}, "testo": testo},
                "required": ["reparto", "testo"]}},
            "da_guardare": testo,
            "per_la_presentazione": testo,
        },
        "required": ["in_sintesi", "reparti", "da_guardare", "per_la_presentazione"],
    }
    esempio = {"in_sintesi": "...", "reparti": [{"reparto": reparti[0] if reparti else "...", "testo": "..."}],
               "da_guardare": "...", "per_la_presentazione": "..."}
    return schema, esempio


SCHEMA_PROPOSTA = {
    "type": "OBJECT",
    "properties": {"proponi": {"type": "BOOLEAN"}, "spiegazione": {"type": "STRING"},
                   "nuovo_profilo": {"type": "STRING"}},
    "required": ["proponi", "spiegazione", "nuovo_profilo"],
}
ESEMPIO_PROPOSTA = {"proponi": True, "spiegazione": "...", "nuovo_profilo": "# Il mio profilo\n..."}


def materiale_riepilogo(numeri: str, copertura: str, dati_usciti: list[dict], inviate: list[dict],
                        prossima_settimana: list[str], profilo: str, reparti: list[str]) -> tuple[str, str]:
    """Istruzioni e testo da dare all'IA per il riepilogo."""
    usciti = "\n".join(f"- {d['nome']}: {d['testo']}" for d in dati_usciti) or "(nessuno)"
    notizie = "\n".join(f"- [{i.get('tema', '')} | {', '.join(i.get('reparti', []))}] {i['titolo']}"
                        + _giudizio(i) for i in inviate) or "(nessuna)"
    calendario = "\n".join(f"- {_testo_semplice(r)}" for r in prossima_settimana) or "(nessun appuntamento importante)"
    istruzioni = (ISTRUZIONI_RIEPILOGO + f"\nREPARTI DEL TEAM: {', '.join(reparti)}\n"
                  f"\nPROFILO DEL TEAM\n{profilo}\n")
    testo = (f"NUMERI DELLA SETTIMANA (valore e variazione sulla settimana)\n{numeri}\n{copertura}\n\n"
             f"DATI UFFICIALI USCITI IN SETTIMANA\n{usciti}\n\n"
             f"NOTIZIE INVIATE AL TEAM IN SETTIMANA (tra parentesi tema e reparti)\n{notizie}\n\n"
             f"CALENDARIO DELLA PROSSIMA SETTIMANA\n{calendario}")
    return istruzioni, testo


def componi_riepilogo(risposta: dict, dal: datetime, al: datetime, tabella: str, copertura: str,
                      reparti: list[str]) -> tuple[str, str]:
    """Il riepilogo in HTML per Telegram e in Markdown per l'archivio nel repository."""
    periodo = f"dal {dal.day} {notifiche.MESI[dal.month - 1]} al {al.day} {notifiche.MESI[al.month - 1]} {al.year}"
    per_reparto = {r.get("reparto"): r.get("testo", "") for r in risposta.get("reparti") or []}
    esc = notifiche.esc

    html = [f"🗓️ <b>Riepilogo della settimana · PSP</b>\n<i>{periodo}</i>",
            f"<b>In sintesi</b>\n{esc(risposta.get('in_sintesi', ''))}"]
    md = [f"# Riepilogo della settimana · PSP\n\n*{periodo}*", f"## In sintesi\n\n{risposta.get('in_sintesi', '')}"]
    if tabella:
        html.append(f"<b>📈 I numeri della settimana</b>\n<pre>{esc(tabella)}</pre>"
                    + (f"\n<i>{esc(copertura)}</i>" if copertura else "")
                    + ("\n<i>* dato non aggiornato di recente (fonte in ritardo)</i>" if " *" in tabella else ""))
        md.append(f"## I numeri della settimana\n\n```\n{tabella}\n```" + (f"\n\n*{copertura}*" if copertura else ""))
    for reparto in reparti:
        if per_reparto.get(reparto):
            html.append(f"{notifiche.hashtag(reparto)}\n{esc(per_reparto[reparto])}")
            md.append(f"## {reparto}\n\n{per_reparto[reparto]}")
    if risposta.get("da_guardare"):
        html.append(f"<b>📅 Da guardare la prossima settimana</b>\n{esc(risposta['da_guardare'])}")
        md.append(f"## Da guardare la prossima settimana\n\n{risposta['da_guardare']}")
    if risposta.get("per_la_presentazione"):
        html.append(f"<b>🎤 Per la presentazione</b>\n{esc(risposta['per_la_presentazione'])}")
        md.append(f"## Per la presentazione\n\n{risposta['per_la_presentazione']}")
    return "\n\n".join(html), "\n\n".join(md) + "\n"


def materiale_proposta(profilo: str, votate: list[dict]) -> str:
    righe = "\n".join(f"- {'👍' if sum(i['voti'].values()) > 0 else '👎'} [{i.get('tema', '')}] {i['titolo']}"
                      for i in votate)
    return f"PROFILO ATTUALE\n{profilo}\n\nGIUDIZI DEL TEAM SULLE NOTIZIE RECENTI\n{righe}"


def id_proposta(testo: str) -> str:
    return hashlib.sha256(testo.encode()).hexdigest()[:10]


def messaggio_proposta(spiegazione: str, votate: int, url_profilo: str) -> str:
    punti = "\n".join(f"• {notifiche.esc(r.strip(' -•'))}" for r in spiegazione.splitlines() if r.strip())
    return (f"🧭 <b>Proposta di modifica al profilo</b>\n"
            f"In base ai voti del team ({votate} notizie votate nelle ultime due settimane) propongo:\n\n"
            f"{punti}\n\nVuoi applicarla? Il profilo di adesso resta comunque nella "
            f"<a href=\"{notifiche.esc(url_profilo, virgolette=True)}\">cronologia di GitHub</a>.")


def tastiera_proposta(id_: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Applica", "callback_data": f"p+{id_}"},
                                 {"text": "❌ Ignora", "callback_data": f"p-{id_}"}]]}


def settimana_iso(giorno: datetime) -> str:
    anno, settimana, _ = giorno.isocalendar()
    return f"{anno}-W{settimana:02d}"


def _giudizio(notifica: dict) -> str:
    punteggio = sum((notifica.get("voti") or {}).values())
    return " (apprezzata dal team)" if punteggio > 0 else " (non apprezzata dal team)" if punteggio < 0 else ""


def _testo_semplice(riga_html: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", riga_html))


def prossimi_giorni(adesso: datetime, giorni: int = 7) -> tuple:
    domani = (adesso + timedelta(days=1)).date()
    return domani, domani + timedelta(days=giorni - 1)
