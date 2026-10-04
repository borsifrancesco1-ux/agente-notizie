"""Filtro a parole chiave e raggruppamento delle notizie sullo stesso fatto."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from fonti import Notizia

PAROLE_VUOTE = {
    # italiano
    "che", "del", "della", "delle", "dei", "degli", "dello", "nel", "nella", "nelle", "nei",
    "con", "per", "una", "uno", "sul", "sulla", "sui", "dal", "dalla", "dai", "alla", "alle",
    "agli", "non", "piu", "come", "dopo", "anche", "tra", "fra", "sono", "suo", "sua",
    # inglese
    "the", "and", "for", "with", "from", "that", "this", "its", "are", "was", "has", "have",
    "after", "over", "into", "says", "said", "amid", "will", "new", "how", "why", "what",
}


def _regola(parola: str) -> re.Pattern:
    """Parola chiave con maiuscole: confronto esatto. Tutta minuscola: indifferente."""
    flag = 0 if any(c.isupper() for c in parola) else re.IGNORECASE
    return re.compile(rf"(?<!\w){re.escape(parola)}(?!\w)", flag)


class Filtro:
    def __init__(self, config: dict):
        self.titoli = {
            t["nome"]: [_regola(p) for p in (t["nome"], t.get("ticker"), *(t.get("sinonimi") or [])) if p]
            for t in config.get("titoli") or []
        }
        self.temi = {
            tema["nome"]: [_regola(p) for p in tema.get("parole_chiave") or []]
            for tema in config.get("temi") or []
        }

    def pertinenti(self, notizie: list[Notizia]) -> list[Notizia]:
        """Annota i titoli e i temi toccati da ogni notizia. Tiene tutte quelle dei siti
        principali (le giudica l'IA) e, per gli altri feed, solo quelle con parole chiave."""
        risultato = []
        for n in notizie:
            testo = f"{n.titolo} {n.descrizione}"
            n.titoli = {nome for nome, regole in self.titoli.items() if any(r.search(testo) for r in regole)}
            n.temi = {nome for nome, regole in self.temi.items() if any(r.search(testo) for r in regole)}
            if n.feed and n.feed.titolo:
                n.titoli.add(n.feed.titolo)
            if n.feed and n.feed.tema:
                n.temi.add(n.feed.tema)
            if n.titoli or n.temi or (n.feed and (n.feed.ufficiale or not n.feed.filtra)):
                risultato.append(n)
        return risultato


def impronta(titolo: str) -> frozenset[str]:
    """Le parole significative di un titolo, senza accenti né maiuscole."""
    t = unicodedata.normalize("NFKD", titolo.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return frozenset(p for p in re.findall(r"[a-z0-9]+", t) if len(p) > 2 and p not in PAROLE_VUOTE)


def simili(a: frozenset[str], b: frozenset[str], soglia: float = 0.6, contenimento: bool = True) -> bool:
    """Titoli sullo stesso fatto: molte parole in comune oppure, con contenimento, uno quasi contenuto
    nell'altro (es. "AMAS - ANSA - Eni announces fuel cut" e "Eni announces fuel cut")."""
    if not (a and b):
        return False
    comuni = len(a & b)
    if comuni / len(a | b) >= soglia:
        return True
    return contenimento and min(len(a), len(b)) >= 4 and comuni / min(len(a), len(b)) >= 0.75


def autorevolezza(n: Notizia) -> int:
    """0 = istituzione, 1 = testata principale, 2 = feed dedicato a un titolo, 3 = ricerca sul web."""
    if n.feed and n.feed.ufficiale:
        return 0
    if n.feed and n.feed.principale:
        return 1
    if n.feed and "news.google.com" not in n.feed.url:
        return 2
    return 3


@dataclass
class Gruppo:
    """Notizie che riportano lo stesso fatto. La principale (titolo e primo link) è quella della
    fonte più autorevole e, a parità, la più recente."""
    notizie: list[Notizia]

    @property
    def principale(self) -> Notizia:
        return min(self.notizie, key=lambda n: (autorevolezza(n), -(n.pubblicata.timestamp() if n.pubblicata else 0.0)))

    @property
    def impronta(self) -> frozenset[str]:
        return impronta(self.principale.titolo)

    @property
    def titoli(self) -> set[str]:
        return set().union(*(n.titoli for n in self.notizie))

    @property
    def temi(self) -> set[str]:
        return set().union(*(n.temi for n in self.notizie))

    @property
    def ufficiale(self) -> bool:
        return any(n.feed and n.feed.ufficiale for n in self.notizie)

    def fonti(self) -> list[tuple[str, str]]:
        """(testata, link) senza testate ripetute, dalla più autorevole."""
        viste, elenco = set(), []
        for n in sorted(self.notizie, key=autorevolezza):
            if n.fonte not in viste:
                viste.add(n.fonte)
                elenco.append((n.fonte, n.link))
        return elenco

    def priorita(self) -> tuple:
        """Prima i titoli seguiti, poi i comunicati ufficiali, poi i siti principali,
        poi le ricerche su Google News; a parità, i più recenti."""
        dal_sito = any(n.feed and n.feed.principale for n in self.notizie)
        tipo = 0 if self.titoli else 1 if self.ufficiale else 2 if dal_sito else 3
        quando = self.principale.pubblicata
        return tipo, -(quando.timestamp() if quando else 0.0)

    @classmethod
    def unisci(cls, gruppi: list[Gruppo]) -> Gruppo:
        return cls([n for g in gruppi for n in g.notizie])


def raggruppa(notizie: list[Notizia]) -> list[Gruppo]:
    """Unisce le notizie con lo stesso link o con titoli quasi uguali."""
    gruppi: list[tuple[Gruppo, frozenset[str]]] = []
    recenti_prima = sorted(notizie, key=lambda n: n.pubblicata.timestamp() if n.pubblicata else 0.0, reverse=True)
    for n in recenti_prima:
        imp = impronta(n.titolo)
        for gruppo, imp_gruppo in gruppi:
            if any(n.link == altra.link for altra in gruppo.notizie) or simili(imp, imp_gruppo):
                gruppo.notizie.append(n)
                break
        else:
            gruppi.append((Gruppo([n]), imp))
    return [g for g, _ in gruppi]


def scelta_senza_ia(gruppi: list[Gruppo], massimo: int) -> list[Gruppo]:
    """Senza intelligenza artificiale passano solo comunicati ufficiali e notizie sui titoli
    seguiti da fonti affidabili: i siti principali e i feed dedicati, non i siti minori
    trovati con le ricerche su Google News. I temi da soli porterebbero troppo rumore."""
    affidabile = lambda g: any(n.feed and (n.feed.principale or "news.google.com" not in n.feed.url)  # noqa: E731
                               for n in g.notizie)
    return [g for g in sorted(gruppi, key=Gruppo.priorita)
            if g.ufficiale or (g.titoli and affidabile(g))][:massimo]
