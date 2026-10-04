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
import re
import statistics
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

import notifiche

UA = {"User-Agent": "agente-notizie/0.1 (lettore personale; github.com/borsifrancesco1-ux)"}
CALENDARIO = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
              "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]
IMPATTO = {"Low": 1, "Medium": 2, "High": 3}
STORICO = 14   # osservazioni lette per ogni indicatore: servono a capire se una variazione è insolita
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


def evento(indicatore: dict, adesso: datetime) -> Evento | None:
    """L'evento del calendario corrispondente a un indicatore uscito da poco (con orario e consensus)."""
    voce = indicatore.get("calendario") or {}
    titoli = set(voce.get("titoli") or [])
    for e in eventi_calendario():
        if e.paese == voce.get("paese") and e.titolo in titoli and timedelta(0) <= adesso - e.quando <= timedelta(days=3):
            return e
    return None


def consensus(indicatore: dict, adesso: datetime) -> str:
    """Il valore atteso dal mercato per un indicatore uscito da poco, se il calendario lo riporta."""
    e = evento(indicatore, adesso)
    return all_italiana(e.atteso) if e else ""


# ---------------- reazione dei mercati ----------------

_barre: dict[str, list[tuple[datetime, float]]] = {}
BARRA = timedelta(minutes=15)   # ogni quotazione infragiornaliera è la chiusura di 15 minuti di scambi


def reazione(config: dict, gruppo: str, dal: datetime) -> str:
    """Come si sono mosse le quotazioni del gruppo indicato (sezione reazione_mercati di config)
    da 'dal' a adesso. Vuoto se i mercati sono chiusi o mancano le quotazioni."""
    quotazioni = config.get("quotazioni") or {}
    adesso = datetime.now(timezone.utc)
    parti = []
    for nome in (config.get("reazione_mercati") or {}).get(gruppo) or []:
        voce = quotazioni.get(nome)
        if not voce or not voce.get("simbolo"):  # solo le quotazioni di Yahoo hanno l'andamento nella giornata
            continue
        try:
            barre = _infragiornaliere(voce["simbolo"])
        except Exception:  # noqa: BLE001 — senza quella quotazione la riga resta più corta
            continue
        # il prezzo di partenza è quello di una barra chiusa entro 'dal' (Yahoo data le barre dall'inizio)
        prima = next((b for b in reversed(barre) if b[0] + BARRA <= dal), None)
        ultima = barre[-1] if barre else None
        if not prima or not ultima or ultima[0] <= prima[0] or adesso - ultima[0] > timedelta(hours=2):
            continue  # mercato chiuso, o nessuna quotazione dopo l'uscita
        if voce.get("tipo") == "tasso":
            parti.append(f"{nome} {numero((ultima[1] - prima[1]) * 100, 0, segno=True)} pb")
        else:
            parti.append(f"{nome} {numero((ultima[1] / prima[1] - 1) * 100, 2, segno=True)}%")
    return " · ".join(parti)


def _infragiornaliere(simbolo: str) -> list[tuple[datetime, float]]:
    """Quotazioni a 15 minuti degli ultimi giorni (Yahoo Finance), lette una volta per esecuzione."""
    if simbolo not in _barre:
        r = requests.get(YAHOO_API.format(simbolo=simbolo), params={"range": "5d", "interval": "15m"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        risultato = r.json()["chart"]["result"][0]
        chiusure = risultato["indicators"]["quote"][0]["close"]
        _barre[simbolo] = [(datetime.fromtimestamp(t, timezone.utc), c)
                           for t, c in zip(risultato["timestamp"], chiusure) if c is not None]
    return _barre[simbolo]


# ---------------- valori per le notizie ----------------

SIMBOLO_VALIDO = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^]{0,14}$")   # es. AAPL, ENI.MI, BRK-B, ^GSPC
SUFFISSI_SOCIETARI = re.compile(r",?\s+(inc\.?|corp\.?|corporation|co\.?|company|s\.?p\.?a\.?|plc|n\.?v\.?|se|ag|"
                                r"sa|ltd\.?|limited|holdings?|group)$", re.IGNORECASE)
_quotazioni: dict[str, dict | None] = {}


def nome_breve(nome: str) -> str:
    """'JPMORGAN CHASE & CO.' -> 'Jpmorgan Chase', 'Apple Inc.' -> 'Apple': senza la forma societaria."""
    nome = nome.title() if nome.isupper() else nome
    return re.sub(r"[\s,&]+$", "", SUFFISSI_SOCIETARI.sub("", nome.strip())) or nome


def quotazione(simbolo: str) -> dict | None:
    """Prezzo attuale (o dell'ultima chiusura, a mercato chiuso) da Yahoo Finance:
    {"prezzo", "variazione" (% sulla chiusura precedente), "valuta", "nome", "quando"}. None se non c'è."""
    if simbolo not in _quotazioni:
        try:
            r = requests.get(YAHOO_API.format(simbolo=simbolo), params={"range": "1d", "interval": "1d"},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            meta = r.json()["chart"]["result"][0]["meta"]
            prezzo, prima = meta["regularMarketPrice"], meta.get("chartPreviousClose") or meta.get("previousClose")
            nome = meta.get("shortName") or meta.get("longName") or simbolo
            _quotazioni[simbolo] = {
                "prezzo": prezzo, "precedente": prima, "valuta": meta.get("currency") or "",
                "variazione": (prezzo / prima - 1) * 100 if prima else None,
                "nome": nome_breve(nome),
                "quando": datetime.fromtimestamp(meta["regularMarketTime"], timezone.utc),
            }
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
            _quotazioni[simbolo] = None
    return _quotazioni[simbolo]


def valore(voce: dict, fuso) -> str:
    """Livello attuale di una voce di "quotazioni" (config.yaml), con la variazione:
    '1,1257 (+0,11%)', '5,28% (+4 pb)'; per i dati ufficiali giornalieri anche la data: '3,88% (1/10)'."""
    tasso = voce.get("tipo") == "tasso"
    if voce.get("fonte", "yahoo") == "yahoo":
        q = quotazione(voce["simbolo"])
        if not q:
            return ""
        livello, prima, quando = q["prezzo"], q["precedente"], q["quando"]
    else:
        oss = ultime_osservazioni(voce, 2)
        if not oss:
            return ""
        livello, prima = oss[-1].valore, (oss[-2].valore if len(oss) > 1 else None)
        quando = datetime.fromisoformat(oss[-1].periodo).replace(tzinfo=timezone.utc)
    decimali = voce.get("decimali", 2 if tasso or livello < 1000 else 0)
    testo = f"{numero(livello, decimali)}{'%' if tasso else voce.get('unita', '')}"
    dettagli = []
    variazione = ((livello - prima) * 100 if tasso else (livello / prima - 1) * 100) if prima else 0
    if round(variazione, 0 if tasso else 2):  # una variazione nulla non si scrive
        dettagli.append(f"{numero(variazione, 0, segno=True)} pb" if tasso else f"{numero(variazione, 2, segno=True)}%")
    if quando.astimezone(fuso).date() != datetime.now(fuso).date():  # mercato chiuso o dato del giorno prima
        dettagli.append(giorno(quando.astimezone(fuso)))
    return f"{testo} ({', '.join(dettagli)})" if dettagli else testo


def giorno(d: date | datetime) -> str:
    """'2/10': la data breve all'italiana."""
    return f"{d.day}/{d.month}"


PAROLE_GENERICHE = {"the", "and", "inc", "corp", "company", "group", "holding", "holdings", "international",
                    "spa", "plc", "ltd", "global", "industries", "technologies", "energy", "financial", "bank",
                    "general", "american", "national", "first", "united", "new", "capital", "systems",
                    "resources", "partners"}


def stessa_societa(nome: str, altro: str) -> bool:
    """Se due nomi indicano la stessa società ('JPMorgan' e 'JP Morgan Chase & Co.'): una parola
    significativa dell'uno compare nell'altro. Serve perché i ticker passano ad altre società
    (es. PARA, che era di Paramount)."""
    def parole(testo: str) -> list[str]:
        return [p for p in re.findall(r"[a-z0-9]+", testo.lower()) if len(p) >= 3 and p not in PAROLE_GENERICHE]

    def unito(testo: str) -> str:
        return re.sub(r"[^a-z0-9]", "", testo.lower())

    return any(p in unito(altro) for p in parole(nome)) or any(p in unito(nome) for p in parole(altro))


def valori_notizia(config: dict, aziende: list[dict], valori: list[str], fuso) -> str:
    """La riga dei valori sotto una notizia: prezzo attuale delle società citate (per quelle USA anche
    capitalizzazione e P/E, da defeatbeta) e livello dei tassi, cambi o materie prime indicati dall'IA.
    aziende: [{"nome", "simbolo"}]; il prezzo si scrive solo se il nome del titolo corrisponde."""
    import aziende as piattaforma  # importato qui: la libreria defeatbeta serve solo se ci sono società USA

    parti = []
    for societa in aziende[:3]:
        simbolo = (societa.get("simbolo") or "").strip().upper()
        q = quotazione(simbolo) if SIMBOLO_VALIDO.match(simbolo) else None
        if not q:
            continue
        if not stessa_societa(societa.get("nome") or "", q["nome"]):
            print(f"  {simbolo} è {q['nome']}, non {societa.get('nome')}: prezzo non scritto")
            continue
        valuta = {"USD": " $", "EUR": " €", "GBP": " £", "GBp": " p", "JPY": " ¥"}.get(q["valuta"], f" {q['valuta']}")
        dettagli = [f"{numero(q['variazione'], 1, segno=True)}%"] if q["variazione"] is not None else []
        if q["quando"].astimezone(fuso).date() != datetime.now(fuso).date():
            dettagli.append(f"chiusura {giorno(q['quando'].astimezone(fuso))}")
        try:
            fondamentali = piattaforma.capitalizzazione_e_pe(simbolo, q["prezzo"]) if q["valuta"] == "USD" else ""
        except Exception as e:  # noqa: BLE001 — senza la piattaforma resta il prezzo
            print(f"  defeatbeta non disponibile per {simbolo} ({type(e).__name__})")
            fondamentali = ""
        if fondamentali:
            dettagli.append(fondamentali)
        parti.append(f"{q['nome']} {numero(q['prezzo'], 2)}{valuta}" + (f" ({'; '.join(dettagli)})" if dettagli else ""))
    quotazioni = config.get("quotazioni") or {}
    for nome in valori[:3]:
        if nome in quotazioni:
            try:
                testo = valore(quotazioni[nome], fuso)
            except Exception as e:  # noqa: BLE001 — una fonte che non risponde toglie solo quel valore
                print(f"  valore di {nome} non disponibile ({type(e).__name__})")
                testo = ""
            if testo:
                parti.append(f"{nome} {testo}")
    return " · ".join(parti)


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
            osservazioni = ultime_osservazioni(indicatore, STORICO)
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
                nuovi.append({"indicatore": indicatore, "ultima": ultima, "storico": osservazioni,
                              "precedente": Osservazione(prima["periodo"], prima["valore"])})
        elif ultima.periodo != prima["periodo"]:
            nuovi.append({"indicatore": indicatore, "ultima": ultima, "storico": osservazioni,
                          "precedente": osservazioni[-2] if len(osservazioni) > 1 else None})
    return nuovi


def anomalia(nuovo: dict, atteso: str, sigma: float) -> str:
    """Perché un dato è anomalo, oppure "" se non lo è. Due prove:
    - sorpresa: distanza dal consensus oltre la soglia dell'indicatore (soglia_sorpresa in config);
    - variazione insolita: variazione sul periodo prima oltre 'sigma' volte la sua variabilità tipica."""
    ind, ultima = nuovo["indicatore"], nuovo["ultima"]
    unita, decimali = ind.get("unita", ""), ind.get("decimali", 1)
    motivi = []
    valore_atteso = _numero_da_testo(atteso)
    soglia = ind.get("soglia_sorpresa", 0.3 if unita == "%" else 75 if ind.get("formato") == "migliaia" else None)
    if valore_atteso is not None and soglia is not None and abs(ultima.valore - valore_atteso) >= soglia:
        distanza = ultima.valore - valore_atteso
        misura = "punti" if unita == "%" else "mila" if ind.get("formato") == "migliaia" else ""
        motivi.append(f"{numero(distanza, 0 if misura == 'mila' else decimali, segno=True)} {misura} "
                      f"{'sopra' if distanza > 0 else 'sotto'} le attese".replace("  ", " "))
    storico = [o.valore for o in nuovo.get("storico") or []]
    if ind.get("trasformazione") == "differenza":
        # la serie è già una variazione (es. nuovi occupati): conta quanto si allontana dalla sua media
        passate, ultimo = storico[:-1], storico[-1] if storico else 0.0
        scarto = ultimo - statistics.fmean(passate) if len(passate) >= 6 else 0.0
    else:
        # livelli e tassi (es. inflazione, disoccupazione): conta la variazione sul periodo prima
        variazioni = [b - a for a, b in zip(storico, storico[1:])]
        passate, scarto = variazioni[:-1], variazioni[-1] if variazioni else 0.0
    if len(passate) >= 6 and ind.get("avvisa") != "se_cambia":
        tipica = statistics.pstdev(passate)
        if tipica > 0 and abs(scarto) >= sigma * tipica:
            motivi.append(f"movimento {abs(scarto) / tipica:.1f} volte più ampio del solito "
                          f"rispetto agli ultimi {len(passate)} periodi".replace(".", ","))
    return "; ".join(motivi)


def messaggio_dato(nuovo: dict, atteso: str, etichetta_perche: str, motivo_anomalia: str = "",
                   mercati: str = "") -> str:
    ind, ultima, precedente = nuovo["indicatore"], nuovo["ultima"], nuovo["precedente"]
    unita, decimali = ind.get("unita", ""), ind.get("decimali", 1)
    righe = []
    if motivo_anomalia:
        righe.append(f"🚨 <b>Dato anomalo</b>: {notifiche.esc(motivo_anomalia)}")
    if ind.get("reparti"):
        righe.append(" ".join(notifiche.hashtag(r) for r in ind["reparti"]))
    righe.append(f"📊 <b>Dato uscito · {notifiche.esc(ind['nome'])}: {_valore(ultima.valore, unita, decimali, ind)}</b>")
    dettagli = [_periodo(ultima.periodo, ind.get("trimestrale", False))]
    if precedente is not None:
        variazione = ultima.valore - precedente.valore
        dettagli.append(f"precedente {_valore(precedente.valore, unita, decimali, ind)}"
                        + (f" ({numero(variazione, decimali, segno=True)} punti)" if unita == "%" else ""))
    if atteso:
        dettagli.append(f"atteso {notifiche.esc(atteso)}")
    righe += ["", " · ".join(dettagli)]
    if ind.get("perche"):
        righe += ["", f"🎯 <i>{notifiche.esc(etichetta_perche)}:</i> {notifiche.esc(ind['perche'])}"]
    if mercati:
        righe.append(f"📈 <i>Reazione dei mercati:</i> {notifiche.esc(mercati)}")
    righe += ["", f"🔗 <a href=\"{notifiche.esc(link_fonte(ind), virgolette=True)}\">{_nome_fonte(ind)}</a>"]
    return "\n".join(righe)


def riassunto_dato(nuovo: dict, atteso: str, motivo_anomalia: str = "") -> str:
    """Il dato in una riga di testo semplice, per il riepilogo settimanale."""
    ind, ultima, precedente = nuovo["indicatore"], nuovo["ultima"], nuovo["precedente"]
    unita, decimali = ind.get("unita", ""), ind.get("decimali", 1)
    parti = [_periodo(ultima.periodo, ind.get("trimestrale", False))]
    if precedente is not None:
        parti.append(f"precedente {_valore(precedente.valore, unita, decimali, ind)}")
    if atteso:
        parti.append(f"atteso {atteso}")
    if motivo_anomalia:
        parti.append(f"DATO ANOMALO: {motivo_anomalia}")
    return f"{_valore(ultima.valore, unita, decimali, ind)} ({'; '.join(parti)})"


def cambio_del_giorno(config: dict) -> str:
    """Una riga per il buongiorno: EUR/USD di riferimento BCE e costo stimato della copertura."""
    opzioni = config.get("copertura") or {}
    voci = {v["nome"]: v for v in config.get("mercati") or []}
    try:
        cambio = _storico(voci[opzioni.get("cambio", "EUR/USD")])
    except Exception as e:  # noqa: BLE001 — il buongiorno parte comunque, senza questa riga
        print(f"  ✗ cambio del giorno: {type(e).__name__}")
        return ""
    if len(cambio) < 2:
        return ""
    ultima, prima = cambio[-1], cambio[-2]
    testo = (f"<b>EUR/USD</b> {numero(ultima.valore, 4)} "
             f"({numero((ultima.valore / prima.valore - 1) * 100, 2, segno=True)}% sul giorno prima, "
             f"riferimento BCE del {_periodo(ultima.periodo)})")
    righe_tassi = []
    for nome in (opzioni.get("tasso_usd"), opzioni.get("tasso_eur")):
        if nome in voci:
            try:
                righe_tassi.append({"voce": voci[nome], "ultima": _storico(voci[nome])[-1]})
            except Exception:  # noqa: BLE001
                pass
    copertura = costo_copertura(righe_tassi, config)
    if copertura:
        testo += f"\n{notifiche.esc(copertura)}"
    return testo + " " + notifiche.hashtag("Copertura")


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
            variazione = f"{numero((ultima.valore - prima.valore) * 100, 0, segno=True)} pb"
        else:
            variazione = f"{numero((ultima.valore / prima.valore - 1) * 100, 1, segno=True)}%"
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
            f"{opzioni['tasso_eur']}): {numero(usd - eur, 2)}%")


def _storico(voce: dict) -> list[Osservazione]:
    if voce["fonte"] == "fred":
        return _fred(voce["serie"], 15)
    if voce["fonte"] == "bce":
        return _bce(voce["chiave"], 12)
    if voce["fonte"] == "yahoo":
        return _yahoo(voce["simbolo"])
    raise ValueError(f"fonte non adatta ai mercati: {voce['fonte']}")


# ---------------- accesso alle fonti ----------------

def _scarica(url: str, params: dict, timeout: int = 60, headers: dict | None = None) -> requests.Response:
    """GET con un secondo tentativo: le API pubbliche (BCE in particolare) a volte rispondono
    "504" o vanno in timeout per pochi secondi."""
    for tentativo in range(2):
        try:
            r = requests.get(url, params=params, headers=headers or UA, timeout=timeout)
            if r.status_code < 500:
                r.raise_for_status()
                return r
        except (requests.Timeout, requests.ConnectionError):
            if tentativo:
                raise
        time.sleep(5)
    r.raise_for_status()
    return r


def _fred(serie: str, quante: int) -> list[Osservazione]:
    r = _scarica(FRED_API, {"series_id": serie, "api_key": os.environ.get("FRED_KEY", ""),
                            "file_type": "json", "sort_order": "desc", "limit": quante + 10}, timeout=30)
    oss = [Osservazione(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] != "."]
    return list(reversed(oss))[-quante:]


def _bce(chiave: str, quante: int) -> list[Osservazione]:
    r = _scarica(BCE_API.format(chiave=chiave), {"lastNObservations": quante, "format": "csvdata"})
    righe = csv.DictReader(io.StringIO(r.text))
    return [Osservazione(x["TIME_PERIOD"], float(x["OBS_VALUE"])) for x in righe if x.get("OBS_VALUE")]


def _eurostat(dataset: str, filtri: dict, quante: int) -> list[Osservazione]:
    r = _scarica(EUROSTAT_API.format(dataset=dataset),
                 {**filtri, "lastTimePeriod": quante, "format": "JSON", "lang": "EN"})
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


def _numero_da_testo(testo: str) -> float | None:
    """'3,7%' -> 3.7, '89 mila' -> 89 (le serie degli occupati sono in migliaia), '' -> None."""
    trovato = re.search(r"-?\d+(?:[.,]\d+)?", testo or "")
    if not trovato:
        return None
    valore = float(trovato.group().replace(",", "."))
    return valore * 1000 if "mln" in testo else valore


def numero(x: float, decimali: int, segno: bool = False) -> str:
    testo = f"{x:{'+' if segno else ''},.{decimali}f}"
    return testo.replace(",", "§").replace(".", ",").replace("§", ".").replace("-", "−")


def _valore(x: float, unita: str, decimali: int, voce: dict) -> str:
    if voce.get("formato") == "migliaia":  # es. occupati: la serie è in migliaia di persone
        return f"{numero(x, 0, segno=voce.get('trasformazione') == 'differenza')} mila"
    return f"{numero(x, decimali)}{unita}"


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
