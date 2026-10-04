"""Lettura dei feed RSS: generali, dedicati ai titoli e ricerche su Google News."""
from __future__ import annotations

import calendar
import html
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser
import requests

USER_AGENT = "agente-notizie/0.1 (lettore RSS personale; github.com/borsifrancesco1-ux)"
TIMEOUT = 20
MASSIMO_PER_RICERCA = 10   # risultati più recenti tenuti per ogni ricerca su Google News
                           # (pochi: altrimenti i siti minori trovati con le ricerche prevalgono)


@dataclass
class Feed:
    nome: str
    url: str
    fonte: str = ""             # nome della testata mostrato nelle notifiche
    titolo: str | None = None   # titolo seguito, se il feed è dedicato a un titolo
    tema: str | None = None     # tema, se il feed è una ricerca su un tema
    ufficiale: bool = False     # comunicati istituzionali (BCE, Fed): sempre pertinenti
    filtra: bool = False        # True: passano solo le notizie con parole chiave seguite

    @property
    def principale(self) -> bool:
        """Sito di notizie generale, non una ricerca o un feed dedicato a un titolo."""
        return not (self.titolo or self.tema)

    def __post_init__(self):
        self.fonte = self.fonte or self.nome


@dataclass
class Notizia:
    titolo: str
    link: str
    fonte: str
    descrizione: str = ""
    pubblicata: datetime | None = None
    feed: Feed | None = None
    titoli: set[str] = field(default_factory=set)  # titoli seguiti che la riguardano
    temi: set[str] = field(default_factory=set)    # temi seguiti che la riguardano


def elenco_feed(config: dict) -> list[Feed]:
    """Tutti i feed da leggere, costruiti a partire da config.yaml."""
    elenco = [Feed(f["nome"], f["url"], ufficiale=f.get("ufficiale", False), filtra=f.get("filtra", False))
              for f in config.get("feed_generali") or []]

    for t in config.get("titoli") or []:
        valori = {
            "ticker": t.get("ticker"),
            "ticker_usa": t.get("ticker_usa"),
            "query": urllib.parse.quote_plus(t.get("ricerca") or f'"{t["nome"]}"'),
        }
        for modello in config.get("feed_per_titolo") or []:
            segnaposti = re.findall(r"{(\w+)}", modello["url"])
            if all(valori.get(s) for s in segnaposti):
                elenco.append(Feed(f'{modello["nome"]} · {t["nome"]}',
                                   modello["url"].format(**valori),
                                   fonte=modello["nome"], titolo=t["nome"]))

    modelli = config.get("ricerca_google_news") or {}
    for tema in config.get("temi") or []:
        for lingua, modello in modelli.items():
            ricerche = tema.get(f"ricerca_{lingua}") or []
            for query in [ricerche] if isinstance(ricerche, str) else ricerche:
                url = modello.format(query=urllib.parse.quote_plus(query))
                elenco.append(Feed(f"Google News {lingua.upper()} · {query}", url,
                                   fonte="Google News", tema=tema["nome"]))
    return elenco


def scarica(feed: Feed) -> tuple[list[Notizia], str | None]:
    """Legge un feed. In caso di problemi restituisce l'errore invece di fermarsi."""
    try:
        risposta = requests.get(feed.url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        risposta.raise_for_status()
    except requests.RequestException as e:
        return [], _errore_breve(e)
    dati = feedparser.parse(risposta.content)
    if not dati.entries and dati.bozo:
        return [], f"non è un feed leggibile ({type(dati.bozo_exception).__name__})"
    notizie = [n for n in (_notizia(voce, feed) for voce in dati.entries) if n]
    if "news.google.com" in feed.url:  # le ricerche restituiscono fino a 100 risultati
        notizie.sort(key=lambda n: n.pubblicata.timestamp() if n.pubblicata else 0.0, reverse=True)
        notizie = notizie[:MASSIMO_PER_RICERCA]
    return notizie, None


def scarica_tutti(elenco: list[Feed]) -> tuple[list[Notizia], dict[str, str]]:
    """Legge tutti i feed in parallelo. Restituisce le notizie e gli errori per feed."""
    notizie, errori = [], {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        for feed, (trovate, errore) in zip(elenco, pool.map(scarica, elenco)):
            if errore:
                errori[feed.nome] = errore
            notizie.extend(trovate)
    return notizie, errori


def _notizia(voce, feed: Feed) -> Notizia | None:
    titolo = _testo(voce.get("title", ""), 300)
    link = voce.get("link", "")
    if not titolo or not link:
        return None

    fonte = feed.fonte
    sorgente = voce.get("source")  # Google News indica la testata originale
    if sorgente and sorgente.get("title"):
        fonte = sorgente["title"]
        if titolo.endswith(" - " + fonte):
            titolo = titolo[: -len(" - " + fonte)]

    descrizione = _testo(voce.get("summary", ""), 400)
    if descrizione.startswith(titolo[:40]):  # Google News ripete solo il titolo
        descrizione = ""

    return Notizia(titolo=titolo, link=link, fonte=fonte, descrizione=descrizione,
                   pubblicata=_data(voce), feed=feed)


def _data(voce) -> datetime | None:
    for chiave in ("published_parsed", "updated_parsed"):
        if voce.get(chiave):
            return datetime.fromtimestamp(calendar.timegm(voce[chiave]), tz=timezone.utc)
    return None


def _testo(s: str, massimo: int) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    return s if len(s) <= massimo else s[:massimo].rsplit(" ", 1)[0] + "…"


def _errore_breve(e: requests.RequestException) -> str:
    # Mai il messaggio completo: può contenere l'URL e, per alcune API, la chiave.
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    if isinstance(e, requests.Timeout):
        return "nessuna risposta (timeout)"
    if isinstance(e, requests.ConnectionError):
        return "connessione fallita"
    return type(e).__name__
