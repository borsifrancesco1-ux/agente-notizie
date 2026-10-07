"""Memoria tra un'esecuzione e l'altra, salvata in stato/memoria.json.

Su GitHub Actions la macchina riparte da zero a ogni giro: il file viene
salvato nel repository a fine esecuzione e ritrovato al giro successivo.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import archivio
from filtro import simili

GIORNI_DA_RICORDARE = 14
GIORNI_NOTIFICHE_VOTATE = 90   # le notizie votate servono più a lungo, come esempi per l'IA
ERRORI_PRIMA_DI_AVVISARE = 5   # giri consecutivi senza risposta prima di segnalare un feed
PARAMETRI_DI_TRACCIAMENTO = ("utm_", ".tsrc", "ncid", "cmpid", "mod")


def chiave(link: str) -> str:
    """Identificativo stabile di un articolo: il link senza parametri di tracciamento."""
    p = urlsplit(link.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query)
                       if not k.lower().startswith(PARAMETRI_DI_TRACCIAMENTO)])
    normale = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), query, ""))
    return hashlib.sha1(normale.encode()).hexdigest()[:16]


class Memoria:
    def __init__(self, percorso: Path):
        self.percorso = percorso
        dati = json.loads(percorso.read_text("utf-8")) if percorso.exists() else {}
        self.valutate: dict[str, str] = dati.get("valutate", {})   # chiave link -> quando
        self.inviate: list[dict] = dati.get("inviate", [])         # notifiche già mandate, con i voti
        self.feed_guasti: dict[str, int] = dati.get("feed_guasti", {})
        self.offset_telegram: int = dati.get("offset_telegram", 0)  # ultimo aggiornamento letto
        self.richieste_ia: dict[str, int] = dati.get("richieste_ia", {})  # giorno di quota -> richieste
        self.avviso_quota: str = dati.get("avviso_quota", "")   # giorno dell'ultimo avviso di quota finita
        # cosa è già stato fatto nella giornata: {"apertura": data, "ora_notizie": "data ora", "chiusura": data}
        self.giornata: dict[str, str] = dati.get("giornata", {})
        # ultimo valore visto di ogni indicatore ufficiale, per riconoscere i dati nuovi
        self.indicatori: dict[str, dict] = dati.get("indicatori", {})
        # proposta di modifica al profilo in attesa di risposta: {"id", "testo", "spiegazione", "quando"}
        self.proposta: dict = dati.get("proposta", {})
        # dati ufficiali usciti di recente, per il riepilogo settimanale: {"nome", "testo", "quando"}
        self.dati_usciti: list[dict] = dati.get("dati_usciti", [])
        self.pausa_fino: str = dati.get("pausa_fino", "")            # /pausa: niente invii fino a quest'ora
        self.ultima_modifica: dict = dati.get("ultima_modifica", {})  # /annulla: versione precedente
        self.da_archiviare: list[dict] = []                          # voci nuove per l'archivio mensile

    def gia_valutata(self, link: str) -> bool:
        return chiave(link) in self.valutate

    def segna_valutate(self, links) -> None:
        adesso = _adesso()
        for link in links:
            self.valutate[chiave(link)] = adesso

    def gia_inviata(self, impronta: frozenset[str]) -> bool:
        """Solo i doppioni quasi identici: i casi incerti (forse un aggiornamento) li valuta l'IA."""
        return any(simili(impronta, frozenset(i["impronta"]), contenimento=False) for i in self.inviate)

    def recenti(self, ore: int = 48) -> list[dict]:
        """Le notifiche inviate nelle ultime ore (per riconoscere doppioni e aggiornamenti)."""
        limite = datetime.now(timezone.utc) - timedelta(hours=ore)
        return [i for i in self.inviate if datetime.fromisoformat(i["quando"]) >= limite]

    def registra_invio(self, id_notifica: str, titolo: str, tema: str, reparti: list[str],
                       impronta: frozenset[str], link: str, messaggio: int | None = None,
                       riassunto: str = "", per_archivio: dict | None = None,
                       messaggi: dict[str, int] | None = None) -> None:
        """messaggio: numero del messaggio Telegram nel canale, per rispondergli con gli aggiornamenti;
        messaggi: lo stesso nel gruppo con gli argomenti, {"gruppo/argomento": numero};
        per_archivio: campi in più da salvare nell'archivio (fonti, perché conta, impatto)."""
        voce = {"id": id_notifica, "titolo": titolo, "tema": tema, "reparti": reparti,
                "impronta": sorted(impronta), "link": link, "quando": _adesso()}
        if messaggio:
            voce["messaggio"] = messaggio
        if messaggi:
            voce["messaggi"] = messaggi
        if riassunto:
            voce["riassunto"] = riassunto[:300]
        self.inviate.append(voce)
        self.da_archiviare.append({"tipo": "notizia", "quando": voce["quando"], "titolo": titolo,
                                   "riassunto": riassunto, "tema": tema, "reparti": reparti, "link": link,
                                   **(per_archivio or {})})

    def registra_dato(self, nome: str, testo: str, reparti: list[str]) -> None:
        voce = {"nome": nome, "testo": testo, "quando": _adesso()}
        self.dati_usciti.append(voce)
        self.da_archiviare.append({"tipo": "dato", "quando": voce["quando"], "titolo": f"{nome}: {testo}",
                                   "riassunto": testo, "tema": nome, "reparti": reparti})

    def in_pausa(self) -> bool:
        return bool(self.pausa_fino) and self.pausa_fino > _adesso()

    def inviate_dal(self, inizio: datetime) -> list[dict]:
        """Le notifiche inviate da un certo momento in poi (es. dall'inizio della giornata)."""
        limite = inizio.astimezone(timezone.utc).isoformat(timespec="seconds")
        return [i for i in self.inviate if i["quando"] >= limite]

    def vota(self, id_notifica: str, utente: str, voto: int) -> bool:
        """Registra il voto (+1 o -1) di un utente; vale l'ultimo voto dato."""
        for i in self.inviate:
            if i.get("id") == id_notifica:
                i.setdefault("voti", {})[utente] = voto
                return True
        return False

    def conteggio(self, id_notifica: str) -> tuple[int, int]:
        for i in self.inviate:
            if i.get("id") == id_notifica:
                voti = i.get("voti", {}).values()
                return sum(v > 0 for v in voti), sum(v < 0 for v in voti)
        return 0, 0

    def notifiche_votate(self, quante: int) -> list[dict]:
        """Le notifiche più recenti con un giudizio netto (più 👍 che 👎 o viceversa)."""
        return [i for i in reversed(self.inviate) if sum(i.get("voti", {}).values()) != 0][:quante]

    def conta_richieste(self, n: int) -> int:
        """Somma le richieste a Gemini del giorno di quota corrente e restituisce il totale."""
        giorno = giorno_quota()
        self.richieste_ia[giorno] = self.richieste_ia.get(giorno, 0) + n
        return self.richieste_ia[giorno]

    def aggiorna_feed(self, nomi: list[str], errori: dict[str, str]) -> list[str]:
        """Conta i giri consecutivi senza risposta. Restituisce i feed appena diventati guasti."""
        appena_guasti = []
        for nome in nomi:
            if nome in errori:
                self.feed_guasti[nome] = self.feed_guasti.get(nome, 0) + 1
                if self.feed_guasti[nome] == ERRORI_PRIMA_DI_AVVISARE:
                    appena_guasti.append(nome)
            else:
                self.feed_guasti.pop(nome, None)
        for nome in list(self.feed_guasti):  # feed tolti da config.yaml
            if nome not in nomi:
                del self.feed_guasti[nome]
        return appena_guasti

    def salva(self) -> None:
        adesso = datetime.now(timezone.utc)
        limite = (adesso - timedelta(days=GIORNI_DA_RICORDARE)).isoformat()
        limite_votate = (adesso - timedelta(days=GIORNI_NOTIFICHE_VOTATE)).isoformat()
        self.valutate = {k: v for k, v in self.valutate.items() if v >= limite}
        self.inviate = [i for i in self.inviate
                        if i["quando"] >= (limite_votate if i.get("voti") else limite)]
        self.richieste_ia = {g: n for g, n in self.richieste_ia.items() if g >= limite[:10]}
        self.percorso.parent.mkdir(parents=True, exist_ok=True)
        dati = {"valutate": self.valutate, "inviate": self.inviate, "feed_guasti": self.feed_guasti,
                "offset_telegram": self.offset_telegram, "richieste_ia": self.richieste_ia,
                "avviso_quota": self.avviso_quota, "giornata": self.giornata,
                "indicatori": self.indicatori, "proposta": self.proposta,
                "dati_usciti": [d for d in self.dati_usciti if d["quando"] >= limite],
                "pausa_fino": self.pausa_fino, "ultima_modifica": self.ultima_modifica}
        self.percorso.write_text(json.dumps(dati, ensure_ascii=False, indent=1), "utf-8")
        # l'archivio sta accanto alla cartella della memoria: archivio/AAAA-MM.jsonl
        archivio.aggiungi(self.percorso.parent.parent / "archivio", self.da_archiviare)
        self.da_archiviare = []


def giorno_quota() -> str:
    """Le quote gratuite di Google si rinnovano a mezzanotte del Pacifico (circa le 9 in Italia)."""
    return (datetime.now(timezone.utc) - timedelta(hours=8)).date().isoformat()


def _adesso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
