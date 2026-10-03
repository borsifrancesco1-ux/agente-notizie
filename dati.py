"""Dati ufficiali, calendario economico e numeri dei mercati.

- Calendario: gli eventi del giorno con orario, consensus e dato precedente (ForexFactory, gratuito)
  e le riunioni di BCE e Fed (date in config.yaml, verificate sui siti ufficiali).
- Indicatori ("dato uscito"): a ogni giro controlla le fonti ufficiali (FRED, BCE, Eurostat)
  e segnala i valori appena pubblicati, con il confronto sul precedente e sul consensus.
- Mercati: i valori di fine settimana e la variazione sulla settimana, per il riepilogo
  (FRED, BCE e, per gli indici azionari, Yahoo Finance).
"""
from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

import notifiche

UA = {"User-Agent": "agente-notizie/0.1 (lettore personale; github.com/borsifrancesco1-ux)"}
CALENDARIO = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
              "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
IMPATTO = {"Low": 1, "Medium": 2, "High": 3}
SIMBOLO_IMPATTO = {3: "🔴", 2: "🟠", 1: "⚪"}
BANDIERE = {"USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵", "CNY": "🇨🇳", "CHF": "🇨🇭"}
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
BCE_API = "https://data-api.ecb.europa.eu/service/data/{chiave}"
EUROSTAT_API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}"
YAHOO_API = "https://query1.finance.yahoo.com/v8/finance/chart/{simbolo}"


@dataclass
class Evento:
    quando: datetime
    paese: str
    titolo: str
    impatto: int
    atteso: str
    precedente: str


@dataclass
class Osservazione:
    periodo: str
    valore: float


# ---------------- calendario ----------------

_eventi: list[Evento] | None = None


def eventi_calendario() -> list[Evento]:
    """Gli eventi di questa settimana e, quando è già pubblicata, della prossima."""
    global _eventi
    if _eventi is None:
        _eventi = []
        for url in CALENDARIO:
            try:
                r = requests.get(url, headers=UA, timeout=30)
                if r.status_code == 404:  # la settimana successiva compare solo verso il weekend
                    continue
                r.raise_for_status()
                for e in r.json():
                    _eventi.append(Evento(datetime.fromisoformat(e["date"]), e.get("country", ""), e.get("title", ""),
                                          IMPATTO.get(e.get("impact"), 0), e.get("forecast") or "",
                                          e.get("previous") or ""))
            except (requests.RequestException, ValueError, KeyError) as e:
                print(f"  ✗ calendario non disponibile ({type(e).__name__})")
    return _eventi


def calendario(config: dict, dal: date, al: date, fuso, impatto_minimo: str | None = None) -> list[str]:
    """Le righe del calendario tra due date (incluse): riunioni delle banche centrali ed eventi."""
    opzioni = config.get("calendario") or {}
    valute = set(opzioni.get("valute", ["USD", "EUR"]))
    minimo = IMPATTO.get(impatto_minimo or opzioni.get("impatto_minimo", "Medium"), 2)
    voci: list[tuple[str, str]] = []
    for appuntamento in opzioni.get("appuntamenti") or []:
        giorno = date.fromisoformat(str(appuntamento["data"]))
        if dal <= giorno <= al:
            voci.append((f"{giorno} {appuntamento.get('ora', '00:00')}",
                         f"{appuntamento.get('icona', '🏛️')} {_giorno(giorno, dal != al)}{appuntamento.get('ora', '')} "
                         f"{notifiche.esc(appuntamento['descrizione'])}"))
    for e in eventi_calendario():
        locale = e.quando.astimezone(fuso)
        if dal <= locale.date() <= al and e.paese in valute and e.impatto >= minimo:
            dettagli = ", ".join(x for x in (f"atteso {all_italiana(e.atteso)}" if e.atteso else "",
                                             f"prec. {all_italiana(e.precedente)}" if e.precedente else "") if x)
            ora = "" if locale.strftime("%H:%M") == "00:00" else locale.strftime("%H:%M")
            voci.append((locale.strftime("%Y-%m-%d %H:%M"),
                         f"{SIMBOLO_IMPATTO[e.impatto]} {_giorno(locale.date(), dal != al)}{ora} "
                         f"{BANDIERE.get(e.paese, e.paese)} {notifiche.esc(e.titolo)}"
                         + (f" ({notifiche.esc(dettagli)})" if dettagli else "")))
    return [testo for _, testo in sorted(voci)]


def consensus(indicatore: dict, adesso: datetime) -> str:
    """Il valore atteso dal mercato per un indicatore uscito da poco, se il calendario lo riporta."""
    voce = indicatore.get("calendario") or {}
    titoli = set(voce.get("titoli") or [])
    for e in eventi_calendario():
        if e.paese == voce.get("paese") and e.titolo in titoli and timedelta(0) <= adesso - e.quando <= timedelta(days=3):
            return all_italiana(e.atteso)
    return ""


def all_italiana(valore: str) -> str:
    """'3.7%' -> '3,7%', '89K' -> '89 mila', '7.23M' -> '7,23 mln' (formato del calendario)."""
    valore = valore.strip()
    for sigla, parola in (("K", " mila"), ("M", " mln"), ("B", " mld"), ("T", " mila mld")):
        if valore.endswith(sigla):
            valore = valore[:-1] + parola
            break
    return valore.replace(".", ",")


# ---------------- indicatori: "dato uscito" ----------------

def nuovi_dati(config: dict, memoria) -> list[dict]:
    """Gli indicatori con un valore nuovo rispetto al giro precedente.
    La prima volta che vede un indicatore lo memorizza senza avvisare."""
    nuovi = []
    for indicatore in config.get("indicatori") or []:
        nome = indicatore["nome"]
        try:
            osservazioni = ultime_osservazioni(indicatore)
        except Exception as e:  # noqa: BLE001 — una fonte guasta non deve fermare le altre
            print(f"  ✗ {nome}: {type(e).__name__}")
            continue
        if not osservazioni:
            continue
        ultima = osservazioni[-1]
        prima = memoria.indicatori.get(nome)
        memoria.indicatori[nome] = {"periodo": ultima.periodo, "valore": ultima.valore}
        if prima is None:
            continue
        if indicatore.get("avvisa") == "se_cambia":  # tassi ufficiali: serie giornaliere, conta solo il cambio
            if round(ultima.valore, 4) != round(prima["valore"], 4):
                nuovi.append({"indicatore": indicatore, "ultima": ultima,
                              "precedente": Osservazione(prima["periodo"], prima["valore"])})
        elif ultima.periodo != prima["periodo"]:
            nuovi.append({"indicatore": indicatore, "ultima": ultima,
                          "precedente": osservazioni[-2] if len(osservazioni) > 1 else None})
    return nuovi


def messaggio_dato(nuovo: dict, atteso: str, etichetta_perche: str) -> str:
    ind, ultima, precedente = nuovo["indicatore"], nuovo["ultima"], nuovo["precedente"]
    unita, decimali = ind.get("unita", ""), ind.get("decimali", 1)
    righe = []
    if ind.get("reparti"):
        righe.append(" ".join(notifiche.hashtag(r) for r in ind["reparti"]))
    righe.append(f"📊 <b>Dato uscito · {notifiche.esc(ind['nome'])}: {_valore(ultima.valore, unita, decimali, ind)}</b>")
    dettagli = [_periodo(ultima.periodo, ind.get("trimestrale", False))]
    if precedente is not None:
        variazione = ultima.valore - precedente.valore
        dettagli.append(f"precedente {_valore(precedente.valore, unita, decimali, ind)}"
                        + (f" ({_numero(variazione, decimali, segno=True)} punti)" if unita == "%" else ""))
    if atteso:
        dettagli.append(f"atteso {notifiche.esc(atteso)}")
    righe += ["", " · ".join(dettagli)]
    if ind.get("perche"):
        righe += ["", f"🎯 <i>{notifiche.esc(etichetta_perche)}:</i> {notifiche.esc(ind['perche'])}"]
    righe += ["", f"🔗 <a href=\"{notifiche.esc(link_fonte(ind), virgolette=True)}\">{_nome_fonte(ind)}</a>"]
    return "\n".join(righe)


def riassunto_dato(nuovo: dict, atteso: str) -> str:
    """Il dato in una riga di testo semplice, per il riepilogo settimanale."""
    ind, ultima, precedente = nuovo["indicatore"], nuovo["ultima"], nuovo["precedente"]
    unita, decimali = ind.get("unita", ""), ind.get("decimali", 1)
    parti = [_periodo(ultima.periodo, ind.get("trimestrale", False))]
    if precedente is not None:
        parti.append(f"precedente {_valore(precedente.valore, unita, decimali, ind)}")
    if atteso:
        parti.append(f"atteso {atteso}")
    return f"{_valore(ultima.valore, unita, decimali, ind)} ({'; '.join(parti)})"


def ultime_osservazioni(voce: dict, quante: int = 2) -> list[Osservazione]:
    """Le ultime osservazioni (dalla più vecchia), già trasformate (es. variazione annua)."""
    trasformazione = voce.get("trasformazione", "livello")
    servono = quante + (12 if trasformazione == "variazione_annua" else 1 if trasformazione == "differenza" else 0)
    fonte = voce["fonte"]
    if fonte == "fred":
        grezze = _fred(voce["serie"], servono)
    elif fonte == "bce":
        grezze = _bce(voce["chiave"], servono)
    elif fonte == "eurostat":
        grezze = _eurostat(voce["dataset"], voce.get("filtri") or {}, servono)
    elif fonte == "yahoo":
        grezze = _yahoo(voce["simbolo"])[-servono:]
    else:
        raise ValueError(f"fonte sconosciuta: {fonte}")

    if trasformazione == "variazione_annua":
        trasformate = [Osservazione(grezze[i].periodo, (grezze[i].valore / grezze[i - 12].valore - 1) * 100)
                       for i in range(12, len(grezze))]
    elif trasformazione == "differenza":
        trasformate = [Osservazione(grezze[i].periodo, grezze[i].valore - grezze[i - 1].valore)
                       for i in range(1, len(grezze))]
    else:
        trasformate = grezze
    return trasformate[-quante:]


def link_fonte(voce: dict) -> str:
    if voce["fonte"] == "fred":
        return f"https://fred.stlouisfed.org/series/{voce['serie']}"
    if voce["fonte"] == "bce":
        dataset, chiave = voce["chiave"].split("/", 1)
        return f"https://data.ecb.europa.eu/data/datasets/{dataset}/{dataset}.{chiave}"
    if voce["fonte"] == "eurostat":
        return f"https://ec.europa.eu/eurostat/databrowser/view/{voce['dataset']}/default/table"
    return f"https://finance.yahoo.com/quote/{voce['simbolo']}"


# ---------------- mercati: numeri della settimana ----------------

def mercati_settimana(config: dict) -> list[dict]:
    """Valore più recente e variazione rispetto a una settimana prima, per ogni voce di config."""
    risultati = []
    oggi = datetime.now(timezone.utc).date()
    for voce in config.get("mercati") or []:
        try:
            serie = _storico(voce)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {voce['nome']}: {type(e).__name__}")
            continue
        if not serie:
            continue
        ultima = serie[-1]
        limite = (date.fromisoformat(ultima.periodo[:10]) - timedelta(days=7)).isoformat()
        prima = next((o for o in reversed(serie) if o.periodo[:10] <= limite), None)
        risultati.append({"voce": voce, "ultima": ultima, "prima": prima,
                          "vecchia": (oggi - date.fromisoformat(ultima.periodo[:10])).days > 10})
    return risultati


def tabella_mercati(righe: list[dict]) -> str:
    """Tabella a larghezza fissa, leggibile su Telegram dentro un blocco <pre>."""
    testo = []
    for r in righe:
        voce, ultima, prima = r["voce"], r["ultima"], r["prima"]
        valore = _valore(ultima.valore, voce.get("unita", ""), voce.get("decimali", 2), voce)
        if prima is None:
            variazione = "n.d."
        elif voce.get("tipo") == "tasso":  # tassi e spread: variazione in punti base
            variazione = f"{_numero((ultima.valore - prima.valore) * 100, 0, segno=True)} pb"
        else:
            variazione = f"{_numero((ultima.valore / prima.valore - 1) * 100, 1, segno=True)}%"
        testo.append(f"{voce['nome'][:24]:<24} {valore:>10} {variazione:>8}" + (" *" if r["vecchia"] else ""))
    return "\n".join(testo)


def costo_copertura(righe: list[dict], config: dict) -> str:
    """Costo annuo stimato della copertura del dollaro per un investitore in euro:
    con un forward il costo è circa il differenziale tra i tassi a breve USD ed EUR."""
    voci = {r["voce"]["nome"]: r["ultima"].valore for r in righe}
    opzioni = config.get("copertura") or {}
    usd, eur = voci.get(opzioni.get("tasso_usd", "")), voci.get(opzioni.get("tasso_eur", ""))
    if usd is None or eur is None:
        return ""
    return (f"Costo annuo stimato della copertura USD→EUR con forward (differenziale {opzioni['tasso_usd']} − "
            f"{opzioni['tasso_eur']}): {_numero(usd - eur, 2)}%")


def _storico(voce: dict) -> list[Osservazione]:
    if voce["fonte"] == "fred":
        return _fred(voce["serie"], 15)
    if voce["fonte"] == "bce":
        return _bce(voce["chiave"], 12)
    if voce["fonte"] == "yahoo":
        return _yahoo(voce["simbolo"])
    raise ValueError(f"fonte non adatta ai mercati: {voce['fonte']}")


# ---------------- accesso alle fonti ----------------

def _fred(serie: str, quante: int) -> list[Osservazione]:
    r = requests.get(FRED_API, timeout=30, params={"series_id": serie, "api_key": os.environ.get("FRED_KEY", ""),
                                                   "file_type": "json", "sort_order": "desc", "limit": quante + 10})
    r.raise_for_status()
    oss = [Osservazione(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] != "."]
    return list(reversed(oss))[-quante:]


def _bce(chiave: str, quante: int) -> list[Osservazione]:
    r = requests.get(BCE_API.format(chiave=chiave), headers=UA, timeout=60,
                     params={"lastNObservations": quante, "format": "csvdata"})
    r.raise_for_status()
    righe = csv.DictReader(io.StringIO(r.text))
    return [Osservazione(x["TIME_PERIOD"], float(x["OBS_VALUE"])) for x in righe if x.get("OBS_VALUE")]


def _eurostat(dataset: str, filtri: dict, quante: int) -> list[Osservazione]:
    r = requests.get(EUROSTAT_API.format(dataset=dataset), timeout=60,
                     params={**filtri, "lastTimePeriod": quante, "format": "JSON", "lang": "EN"})
    r.raise_for_status()
    dati = r.json()
    periodi = dati["dimension"]["time"]["category"]["index"]
    valori = dati.get("value", {})
    return [Osservazione(p, float(valori[str(i)])) for p, i in sorted(periodi.items(), key=lambda x: x[1])
            if str(i) in valori]


def _yahoo(simbolo: str) -> list[Osservazione]:
    r = requests.get(YAHOO_API.format(simbolo=simbolo), params={"range": "1mo", "interval": "1d"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    risultato = r.json()["chart"]["result"][0]
    chiusure = risultato["indicators"]["quote"][0]["close"]
    return [Osservazione(datetime.fromtimestamp(t, timezone.utc).date().isoformat(), c)
            for t, c in zip(risultato["timestamp"], chiusure) if c is not None]


# ---------------- formattazione all'italiana ----------------

MESI = notifiche.MESI


def _numero(x: float, decimali: int, segno: bool = False) -> str:
    testo = f"{x:{'+' if segno else ''},.{decimali}f}"
    return testo.replace(",", "§").replace(".", ",").replace("§", ".").replace("-", "−")


def _valore(x: float, unita: str, decimali: int, voce: dict) -> str:
    if voce.get("formato") == "migliaia":  # es. occupati: la serie è in migliaia di persone
        return f"{_numero(x, 0, segno=voce.get('trasformazione') == 'differenza')} mila"
    return f"{_numero(x, decimali)}{unita}"


def _periodo(periodo: str, trimestrale: bool = False) -> str:
    """'2026-09' -> 'settembre 2026', '2026-Q2' -> '2° trimestre 2026', '2026-10-02' -> '2 ottobre 2026'.
    trimestrale: per le serie FRED trimestrali, datate con il primo giorno del trimestre."""
    if "-Q" in periodo:
        anno, trimestre = periodo.split("-Q")
        return f"{trimestre}° trimestre {anno}"
    parti = periodo.split("-")
    if trimestrale and len(parti) >= 2:
        return f"{(int(parti[1]) - 1) // 3 + 1}° trimestre {parti[0]}"
    if len(parti) == 2:
        return f"{MESI[int(parti[1]) - 1]} {parti[0]}"
    if len(parti) == 3:
        if parti[2] == "01":  # FRED indica i dati mensili e trimestrali con il primo giorno del periodo
            return f"{MESI[int(parti[1]) - 1]} {parti[0]}"
        return f"{int(parti[2])} {MESI[int(parti[1]) - 1]} {parti[0]}"
    return periodo


def _giorno(giorno: date, mostra: bool) -> str:
    return f"{notifiche.GIORNI[giorno.weekday()][:3]} {giorno.day} · " if mostra else ""


def _nome_fonte(voce: dict) -> str:
    return {"fred": "FRED", "bce": "BCE", "eurostat": "Eurostat", "yahoo": "Yahoo Finance"}[voce["fonte"]]
