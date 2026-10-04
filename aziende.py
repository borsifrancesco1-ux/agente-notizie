"""Dati delle società quotate negli USA dalla piattaforma defeatbeta-api.

defeatbeta-api (https://github.com/defeat-beta/defeatbeta-api) pubblica su Hugging Face i dati di
Yahoo Finance sulle società quotate negli USA, in file Parquet aggiornati ogni notte (verso le 5 UTC):
prezzi giornalieri, bilanci, multipli, redditività, crescita, beta e WACC, dividendi, ricavi per
segmento, trascrizioni delle conference call, notizie, valutazione DCF e report grafico. DuckDB legge
solo i pezzi di file che servono: niente chiavi e niente limiti di richieste. I dati arrivano fino al
giorno prima: il prezzo del momento è quello di Yahoo (dati.quotazione). Le società estere ci sono se
quotate anche a New York (es. ASML, Stellantis, Eni con l'ADR "E").

Qui: capitalizzazione e P/E al prezzo attuale per le notizie, e il comando /azienda, dove l'IA sceglie
quali dati chiedere alla piattaforma e poi risponde alla domanda con quei dati.
"""
from __future__ import annotations

import contextlib
import io
import logging
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import requests

import dati
import ia
import notifiche

if TYPE_CHECKING:
    from agente import Giro

BASE = "https://huggingface.co/datasets/defeatbeta/yahoo-finance-data/resolve/main"
VALUTE = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
MAX_TRASCRIZIONE = 40_000   # caratteri della conference call passati all'IA (circa 10.000 parole)
MAX_DATI = 6                # dati chiesti alla piattaforma per ogni domanda
MAX_SOCIETA = 3

_aperti: dict = {}
_elenco: list[dict] | None = None
_paesi: dict[str, str] = {}


class NonTrovata(Exception):
    """La piattaforma non ha dati per questa società."""


# ---------------- accesso alla piattaforma ----------------

@contextlib.contextmanager
def _in_silenzio():
    """All'avvio la libreria stampa un'insegna e scarica dati linguistici: niente nel registro dell'agente."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def _ticker(simbolo: str):
    if simbolo not in _aperti:
        with _in_silenzio():
            from defeatbeta_api.data.ticker import Ticker  # pesante: si carica solo quando serve
            _aperti[simbolo] = Ticker(simbolo, log_level=logging.WARNING)
    return _aperti[simbolo]


def _chiedi(simbolo: str, metodo: str, *argomenti):
    with _in_silenzio():
        return getattr(_ticker(simbolo), metodo)(*argomenti)


def elenco() -> list[dict]:
    """Le società della piattaforma, dalla più grande: [{"ticker", "title", "financial_currency"}]."""
    global _elenco
    if _elenco is None:
        r = requests.get(f"{BASE}/data/US/company_tickers.json", timeout=30)
        r.raise_for_status()
        _elenco = list(r.json().values())
    return _elenco


def trova(testo: str, solo_simbolo: bool = False) -> dict | None:
    """La società indicata da un ticker USA ("AAPL") o da un nome ("Apple"): {"simbolo", "nome", "valuta"}.
    Tra più società con lo stesso nome vince la più grande (l'elenco è in ordine di capitalizzazione)."""
    cercato = testo.strip()
    voci = elenco()
    trovata = next((v for v in voci if v["ticker"].upper() == cercato.upper()), None)
    if not trovata and not solo_simbolo and len(cercato) > 2:
        regola = re.compile(rf"(?<!\w){re.escape(cercato.lower())}(?!\w)")
        trovata = next((v for v in voci if regola.search(v["title"].lower())), None)
    if not trovata:
        return None
    return {"simbolo": trovata["ticker"].upper(), "nome": dati.nome_breve(trovata["title"]),
            "valuta": trovata.get("financial_currency") or "USD"}


def aggiornata_al() -> str:
    """Quando la piattaforma ha aggiornato i dati, es. '4/10 alle 6:52' (ora italiana)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        quando = requests.get(f"{BASE}/spec.json", timeout=20).json()["files"]["US/stock_prices.parquet"]
        d = datetime.fromisoformat(quando.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Rome"))
        return f"{dati.giorno(d)} alle {d:%H:%M}".replace(" 0", " ")
    except Exception:  # noqa: BLE001 — l'informazione è accessoria
        return ""


# ---------------- per le notizie ----------------

def capitalizzazione_e_pe(simbolo: str, prezzo: float) -> str:
    """'cap. 4.875 mld $, P/E 38' al prezzo attuale, per le società americane della piattaforma.
    Per quelle estere (ADR) le azioni in circolazione non corrispondono alle ricevute quotate: niente."""
    societa = trova(simbolo, solo_simbolo=True)
    if not societa or societa["valuta"] != "USD" or _paese(societa["simbolo"]) != "United States":
        return ""
    parti = []
    azioni = _ultimo(_chiedi(societa["simbolo"], "shares"), "shares_outstanding")
    if azioni:
        parti.append(f"cap. {_importo(prezzo * azioni, 'USD')}")
    utile = _ultimo(_chiedi(societa["simbolo"], "ttm_eps"), "tailing_eps")
    if utile and utile > 0:
        parti.append(f"P/E {dati.numero(prezzo / utile, 0)}")
    return ", ".join(parti)


def _paese(simbolo: str) -> str:
    if simbolo not in _paesi:
        info = _chiedi(simbolo, "info")
        _paesi[simbolo] = str(info["country"].iloc[0]) if len(info) else ""
    return _paesi[simbolo]


# ---------------- dati per /azienda ----------------
# Ogni dato è un testo per l'IA, con i numeri già formattati. f(societa, richiesta) -> (testo, allegato)

def _profilo(s: dict, _r: dict):
    info = _chiedi(s["simbolo"], "info")
    if not len(info):
        raise NonTrovata
    i = info.iloc[0]
    dipendenti = f"{dati.numero(float(i['full_time_employees']), 0)}" if _numero(i["full_time_employees"]) else "n.d."
    return (f"Settore: {i['sector']} · industria: {i['industry']} · paese: {i['country']} · dipendenti: {dipendenti} · "
            f"sito: {i['web_site']}\nAttività: {str(i['long_business_summary'])[:900]}"), None


def _prezzo(s: dict, _r: dict):
    righe = []
    q = dati.quotazione(s["simbolo"])
    if q:
        righe.append(f"Prezzo attuale (Yahoo): {dati.numero(q['prezzo'], 2)} {q['valuta']} "
                     f"({dati.numero(q['variazione'] or 0, 2, segno=True)}% sulla chiusura precedente)")
    cap = _chiedi(s["simbolo"], "market_capitalization").dropna(subset=["close_price"])
    if len(cap):
        ultima = cap.iloc[-1]
        prezzi = cap.set_index("report_date")["close_price"]
        righe.append(f"Ultima chiusura nella piattaforma: {dati.numero(ultima['close_price'], 2)} $ ({_data(ultima['report_date'])}); "
                     f"capitalizzazione {_importo(ultima['market_capitalization'], 'USD')}")
        variazioni = []
        for etichetta, giorni in (("1 mese", 21), ("3 mesi", 63), ("1 anno", 252), ("3 anni", 756)):
            if len(prezzi) > giorni:
                variazioni.append(f"{etichetta} {_pct(prezzi.iloc[-1] / prezzi.iloc[-1 - giorni] - 1)}")
        anno = prezzi.iloc[-252:]
        righe.append("Andamento: " + ", ".join(variazioni) +
                     f"; massimo a 52 settimane {dati.numero(anno.max(), 2)} $, minimo {dati.numero(anno.min(), 2)} $")
    return "\n".join(righe), None


def _valutazione(s: dict, _r: dict):
    righe = []
    pe = _chiedi(s["simbolo"], "ttm_pe").dropna(subset=["ttm_pe"])
    if len(pe):
        serie = pe.set_index("report_date")["ttm_pe"]
        cinque = serie.iloc[-1260:]
        righe.append(f"P/E sugli utili degli ultimi 12 mesi: {dati.numero(serie.iloc[-1], 1)} (un anno fa "
                     f"{dati.numero(serie.iloc[-253], 1) if len(serie) > 252 else 'n.d.'}; negli ultimi 5 anni tra "
                     f"{dati.numero(cinque.min(), 1)} e {dati.numero(cinque.max(), 1)}, mediana {dati.numero(cinque.median(), 1)})")
    for metodo, colonna, nome in (("pb_ratio", "pb_ratio", "P/B (prezzo / patrimonio netto)"),
                                  ("ps_ratio", "ps_ratio", "P/S (prezzo / ricavi)"),
                                  ("enterprise_to_ebitda", "ev_to_ebitda", "EV/EBITDA")):
        valore = _ultimo(_chiedi(s["simbolo"], metodo), colonna)
        if valore is not None:
            righe.append(f"{nome}: {dati.numero(valore, 1)}")
    return "\n".join(righe), None


def _conti(s: dict, r: dict):
    df = _chiedi(s["simbolo"], "quarterly_income_statement").df()
    colonne = [c for c in df.columns if re.match(r"\d{4}-\d{2}-\d{2}$", str(c))]
    colonna = _colonna_trimestre(colonne, r)
    prima = next((c for c in colonne if c[:4] == str(int(colonna[:4]) - 1) and c[5:] == colonna[5:]), None)
    valuta = s["valuta"]
    righe = [f"Conto economico del trimestre chiuso il {_data(colonna)} (in {valuta}; confronto con lo stesso "
             f"trimestre dell'anno prima e totale degli ultimi 12 mesi)"]
    for etichette, nome in ((["Total Revenue"], "Ricavi"), (["Gross Profit"], "Utile lordo"),
                            (["Operating Income"], "Utile operativo"), (["EBITDA", "Normalized EBITDA"], "EBITDA"),
                            (["Net Income Common Stockholders"], "Utile netto"), (["Diluted EPS"], "EPS diluito")):
        valore = _voce(df, etichette, colonna)
        if valore is None:
            continue
        eps = nome.startswith("EPS")
        testo = f"{nome}: {dati.numero(valore, 2) if eps else _importo(valore, valuta)}"
        precedente = _voce(df, etichette, prima) if prima else None
        if precedente is not None and precedente > 0:
            testo += f" ({_pct(valore / precedente - 1)} a/a)"
        elif precedente is not None:  # da una perdita la variazione percentuale non ha senso
            testo += f" (un anno prima {dati.numero(precedente, 2) if eps else _importo(precedente, valuta)})"
        ttm = _voce(df, etichette, "TTM")
        if ttm is not None and not eps:
            testo += f"; 12 mesi {_importo(ttm, valuta)}"
        righe.append(testo)
    ricavi = _voce(df, ["Total Revenue"], colonna)
    margini = [f"{nome} {_pct(v / ricavi, segno=False)}" for etichette, nome in
               ((["Gross Profit"], "lordo"), (["Operating Income"], "operativo"), (["Net Income Common Stockholders"], "netto"))
               if ricavi and (v := _voce(df, etichette, colonna)) is not None]
    if margini:
        righe.append("Margini del trimestre: " + ", ".join(margini))
    return "\n".join(righe), None


def _bilancio(s: dict, _r: dict):
    valuta = s["valuta"]
    sp = _chiedi(s["simbolo"], "quarterly_balance_sheet").df()
    colonna = next(c for c in sp.columns if re.match(r"\d{4}-\d{2}-\d{2}$", str(c)))
    righe = [f"Stato patrimoniale al {_data(colonna)} (in {valuta})"]
    for etichette, nome in ((["Total Assets"], "Totale attivo"), (["Cash, Cash Equivalents & Short Term Investments",
                             "Cash And Cash Equivalents"], "Liquidità e investimenti a breve"),
                            (["Total Liabilities", "Total Liabilities Net Minority Interest"], "Totale passività"),
                            (["Stockholders' Equity", "Total Equity"], "Patrimonio netto")):
        valore = _voce(sp, etichette, colonna)
        if valore is not None:
            righe.append(f"{nome}: {_importo(valore, valuta)}")
    debito = _chiedi(s["simbolo"], "debt_to_equity").dropna(subset=["debt_to_equity"])
    if len(debito):
        d = debito.iloc[-1]
        righe.append(f"Debito finanziario: {_importo(d['total_debt'], valuta)}; debito / patrimonio {dati.numero(d['debt_to_equity'], 2)}")
    fc = _chiedi(s["simbolo"], "quarterly_cash_flow").df()
    righe.append("Flussi di cassa degli ultimi 12 mesi (somma degli ultimi 4 trimestri):")
    colonne_fc = [c for c in fc.columns if re.match(r"\d{4}-\d{2}-\d{2}$", str(c))][:4]
    for etichette, nome in ((["Operating Cash Flow"], "Cassa dalla gestione"), (["Capital Expenditure (CapEx)",
                             "Capital Expenditure"], "Investimenti (capex)"), (["Free Cash Flow"], "Free cash flow"),
                            (["Cash Dividends Paid", "Common Stock Dividend Paid"], "Dividendi pagati"),
                            (["Common Stock Payments", "Repurchase Of Capital Stock"], "Riacquisto di azioni proprie")):
        valori = [v for c in colonne_fc if (v := _voce(fc, etichette, c)) is not None]
        if len(valori) == len(colonne_fc) == 4:
            righe.append(f"{nome}: {_importo(sum(valori), valuta)}")
    return "\n".join(righe), None


def _crescita(s: dict, _r: dict):
    righe = ["Crescita sullo stesso trimestre dell'anno prima (ultimi 4 trimestri, dal più vecchio):"]
    for metodo, nome in (("quarterly_revenue_yoy_growth", "Ricavi"), ("quarterly_net_income_yoy_growth", "Utile netto"),
                         ("quarterly_eps_yoy_growth", "EPS")):
        df = _chiedi(s["simbolo"], metodo).dropna(subset=["yoy_growth"]).tail(4)
        if len(df):
            righe.append(f"{nome}: " + ", ".join(f"{_data(r['report_date'])} {_pct(r['yoy_growth'])}" for _, r in df.iterrows()))
    return "\n".join(righe), None


def _redditivita(s: dict, _r: dict):
    righe = ["Margini degli ultimi 4 trimestri (dal più vecchio):"]
    for metodo, colonna, nome in (("quarterly_gross_margin", "gross_margin", "Lordo"),
                                  ("quarterly_operating_margin", "operating_margin", "Operativo"),
                                  ("quarterly_net_margin", "net_margin", "Netto")):
        df = _chiedi(s["simbolo"], metodo).dropna(subset=[colonna]).tail(4)
        if len(df):
            righe.append(f"{nome}: " + ", ".join(f"{_data(r['report_date'])} {_pct(r[colonna], segno=False)}"
                                                 for _, r in df.iterrows()))
    for metodo, colonna, nome in (("roe", "roe", "ROE (rendimento del patrimonio)"),
                                  ("roic", "roic", "ROIC (rendimento del capitale investito)")):
        df = _chiedi(s["simbolo"], metodo).dropna(subset=[colonna]).tail(4)
        if len(df) == 4:  # i valori sono trimestrali: la somma dà quello degli ultimi 12 mesi
            righe.append(f"{nome} ultimi 12 mesi: {_pct(df[colonna].sum(), segno=False)}")
    return "\n".join(righe), None


def _dividendi(s: dict, _r: dict):
    df = _chiedi(s["simbolo"], "dividends")
    if not len(df):
        return "La società non ha pagato dividendi negli ultimi anni.", None
    ultimi = df.tail(8)
    righe = ["Dividendi per azione (ultimi pagamenti, data di stacco): " +
             ", ".join(f"{_data(r['report_date'])} {dati.numero(r['amount'], 3)} $" for _, r in ultimi.iterrows())]
    from datetime import date, timedelta
    anno_fa = (date.today() - timedelta(days=365)).isoformat()
    totale = df[df["report_date"].astype(str) >= anno_fa]["amount"].sum()
    q = dati.quotazione(s["simbolo"])
    if totale and q:
        righe.append(f"Ultimi 12 mesi: {dati.numero(totale, 2)} $ per azione, rendimento {_pct(totale / q['prezzo'], segno=False)} "
                     f"al prezzo attuale")
    return "\n".join(righe), None


def _rischio(s: dict, _r: dict):
    righe = []
    beta = _chiedi(s["simbolo"], "beta")
    if len(beta):
        righe.append(f"Beta a 5 anni rispetto all'S&P 500: {dati.numero(beta['beta'].iloc[-1], 2)} "
                     "(1 = si muove come il mercato; più alto = più volatile)")
    wacc = _chiedi(s["simbolo"], "wacc").dropna(subset=["wacc"])
    if len(wacc):
        w = wacc.iloc[-1]
        righe.append(f"Costo del capitale (WACC): {_pct(w['wacc'], segno=False)}; costo del capitale proprio "
                     f"{_pct(w['cost_of_equity'], segno=False)}, costo del debito {_pct(w['cost_of_debt'], segno=False)}; "
                     f"peso del debito {_pct(w['weight_of_debt'], segno=False)}")
    debito = _ultimo(_chiedi(s["simbolo"], "debt_to_equity"), "debt_to_equity")
    if debito is not None:
        righe.append(f"Debito / patrimonio netto: {dati.numero(debito, 2)}")
    return "\n".join(righe), None


def _segmenti(s: dict, _r: dict):
    df = _chiedi(s["simbolo"], "trailing_revenue_by_breakdown")
    if not len(df):
        return "La società non pubblica i ricavi per segmento.", None
    righe = []
    for nome, gruppo in df.groupby("breakdown_name"):
        ultimo = gruppo[gruppo["report_date"] == gruppo["report_date"].max()]
        totale = ultimo["value"].sum()
        voci = sorted(ultimo.itertuples(), key=lambda r: -r.value)[:8]
        righe.append(f"{nome} (ultimi 12 mesi al {_data(ultimo['report_date'].iloc[0])}): " + ", ".join(
            f"{v.series_name} {_importo(v.value, v.currency)} ({_pct(v.value / totale, segno=False)})" for v in voci))
    return "\n".join(righe), None


def _call(s: dict, r: dict):
    trascrizioni = _chiedi(s["simbolo"], "earning_call_transcripts")
    with _in_silenzio():
        elenco_call = trascrizioni.get_transcripts_list()
    if not len(elenco_call):
        return "Nessuna trascrizione disponibile.", None
    scelta = elenco_call.iloc[-1]
    if r.get("anno"):
        richieste = elenco_call[(elenco_call["fiscal_year"] == r["anno"]) &
                                ((elenco_call["fiscal_quarter"] == r["trimestre"]) if r.get("trimestre") else True)]
        if len(richieste):
            scelta = richieste.iloc[-1]
    with _in_silenzio():
        testo = trascrizioni.get_transcript(int(scelta["fiscal_year"]), int(scelta["fiscal_quarter"]))
    righe = "\n".join(f"{p.speaker}: {p.content}" for p in testo.itertuples())
    if len(righe) > MAX_TRASCRIZIONE:  # l'inizio (i numeri del management) e la fine (le domande degli analisti)
        righe = righe[:MAX_TRASCRIZIONE * 2 // 3] + "\n[…]\n" + righe[-MAX_TRASCRIZIONE // 3:]
    return (f"Conference call del trimestre fiscale {int(scelta['fiscal_quarter'])} del {int(scelta['fiscal_year'])} "
            f"({_data(scelta['report_date'])}):\n{righe}"), None


def _settore(s: dict, _r: dict):
    df = _chiedi(s["simbolo"], "industry_ttm_pe").dropna(subset=["industry_pe"])
    if not len(df):
        return "Confronto con il settore non disponibile.", None
    u = df.iloc[-1]
    return (f"Settore {u['industry']}: P/E medio {dati.numero(u['industry_pe'], 1)} "
            f"(capitalizzazione complessiva {_importo(u['total_market_cap'], 'USD')})"), None


def _notizie(s: dict, _r: dict):
    df = _chiedi(s["simbolo"], "news").get_news_list().drop_duplicates("uuid").tail(12)
    return "Ultime notizie (dalla più vecchia):\n" + "\n".join(
        f"- {_data(r['report_date'])} · {r['publisher']}: {r['title']}" for _, r in df.iterrows()), None


def _dcf(s: dict, _r: dict):
    with _in_silenzio():
        t = _ticker(s["simbolo"])
        d = t.dcf_data()
        file = t.dcf()["file_path"]
    v, sconto, crescita = d["dcf_value"], d["discount_rate"], d["dcf_template"]
    testo = (f"Valutazione DCF automatica della piattaforma: prezzo equo stimato {dati.numero(v['fair_price'], 2)} $ "
             f"contro un prezzo di {dati.numero(v['current_price'], 2)} $ (margine di sicurezza {_pct(v['margin_of_safety'])}); "
             f"valore d'impresa {_importo(v['enterprise_value'], 'USD')}; tasso di sconto {_pct(crescita['discount_rate'], segno=False)}; "
             f"crescita stimata {_pct(crescita['growth_rate_1_5y'], segno=False)} negli anni 1-5, "
             f"{_pct(crescita['growth_rate_6_10y'], segno=False)} negli anni 6-10, {_pct(crescita['growth_rate_terminal'], segno=False)} "
             f"in seguito; beta {dati.numero(sconto['beta_5y'], 2)}. Il foglio Excel con tutti i passaggi è allegato.")
    return testo, (Path(file), f"💎 Valutazione DCF di {s['nome']} ({s['simbolo']}): foglio Excel modificabile")


def _report(s: dict, _r: dict):
    with _in_silenzio():
        from defeatbeta_api.reports import tearsheet
        file = Path(tempfile.gettempdir()) / f"Report_{s['simbolo']}.html"
        tearsheet.html(_ticker(s["simbolo"]), output=str(file))
    return ("Report grafico allegato (P/E nel tempo, margini e crescita trimestrali).",
            (file, f"📑 Report di {s['nome']} ({s['simbolo']}): aprilo nel browser"))


DATI = {  # nome: (cosa contiene, per l'IA che sceglie; funzione)
    "profilo": ("settore, attività, paese, dipendenti", _profilo),
    "prezzo": ("prezzo attuale, andamento a 1 mese, 3 mesi, 1 e 3 anni, massimo e minimo annuo, capitalizzazione", _prezzo),
    "valutazione": ("multipli: P/E oggi e negli ultimi 5 anni, P/B, P/S, EV/EBITDA", _valutazione),
    "conti": ("conto economico di un trimestre (ricavi, utili, EBITDA, EPS, margini) con il confronto annuo", _conti),
    "bilancio": ("stato patrimoniale e flussi di cassa: liquidità, debito, patrimonio, cassa operativa, capex, "
                 "dividendi e riacquisti pagati", _bilancio),
    "crescita": ("crescita annua di ricavi, utile netto ed EPS negli ultimi 4 trimestri", _crescita),
    "redditivita": ("margini lordo, operativo e netto degli ultimi trimestri, ROE e ROIC", _redditivita),
    "dividendi": ("dividendi pagati e rendimento al prezzo attuale", _dividendi),
    "rischio": ("beta rispetto all'S&P 500, costo del capitale (WACC), leva finanziaria", _rischio),
    "segmenti": ("ricavi per segmento di attività e per area geografica", _segmenti),
    "call": ("trascrizione della conference call sui risultati: commenti del management, guidance, prospettive, "
             "domande degli analisti", _call),
    "settore": ("P/E medio del settore, per il confronto (lento)", _settore),
    "notizie": ("titoli delle ultime notizie sulla società", _notizie),
    "dcf": ("valutazione con i flussi di cassa scontati (prezzo equo stimato), con il foglio Excel da scaricare", _dcf),
    "report": ("report grafico in HTML (P/E, margini e crescita nel tempo) da scaricare", _report),
}
PANORAMICA = ["profilo", "prezzo", "valutazione", "crescita", "redditivita", "rischio"]

ISTRUZIONI_PIANO = """Sei l'assistente di un team universitario che gestisce un portafoglio simulato. L'utente
chiede informazioni su una o più società quotate. Prepara la ricerca nella piattaforma di dati finanziari,
che contiene le società quotate negli USA (comprese le estere quotate anche a New York):
- "societa": le società citate (massimo 3), ognuna con "nome", "simbolo_usa" (il ticker della borsa USA,
  es. Apple = AAPL, ASML = ASML, Stellantis = STLA; vuoto se non è quotata negli USA) e "simbolo_yahoo"
  (il simbolo su Yahoo Finance nella borsa principale, es. AAPL, ISP.MI, MC.PA);
- "dati": da 1 a {massimo} dati della piattaforma che servono per rispondere, scelti tra:
{elenco}
  Per una richiesta generica o una panoramica: {panoramica}. Per domande sui risultati o sul trimestre:
  conti, crescita e call. "dcf" e "report" solo se l'utente chiede una valutazione o un report;
- "anno" e "trimestre": l'anno e il trimestre fiscale chiesti (0 se non indicati: l'ultimo disponibile);
- "domanda": la richiesta dell'utente riscritta in modo chiaro (per una richiesta generica: "panoramica
  della società").
"""
SCHEMA_PIANO = {
    "type": "OBJECT",
    "properties": {
        "societa": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"nome": {"type": "STRING"}, "simbolo_usa": {"type": "STRING"}, "simbolo_yahoo": {"type": "STRING"}},
            "required": ["nome", "simbolo_usa", "simbolo_yahoo"]}},
        "dati": {"type": "ARRAY", "items": {"type": "STRING", "format": "enum", "enum": list(DATI)}},
        "anno": {"type": "INTEGER"}, "trimestre": {"type": "INTEGER"}, "domanda": {"type": "STRING"},
    },
    "required": ["societa", "dati", "anno", "trimestre", "domanda"],
}
ESEMPIO_PIANO = {"societa": [{"nome": "Apple", "simbolo_usa": "AAPL", "simbolo_yahoo": "AAPL"}],
                 "dati": ["conti", "crescita", "call"], "anno": 0, "trimestre": 0, "domanda": "..."}

ISTRUZIONI_RISPOSTA = """Rispondi in italiano alla domanda del team usando SOLO i dati qui sotto (piattaforma
defeatbeta: dati di Yahoo Finance aggiornati al giorno prima; il prezzo attuale è di Yahoo). Il team è di
studenti di finanza: risposta concreta, da 4 a 8 frasi, con i numeri, il confronto con i periodi
precedenti e, se servono, brevi spiegazioni dei concetti. Se i dati non bastano per rispondere, dillo.
Valuta i fatti senza dare consigli di investimento.
- "risposta": il testo della risposta;
- "punti": da 0 a 5 numeri chiave, ognuno in una riga breve (es. "Ricavi del trimestre: 94 mld $, +8% a/a").
"""
SCHEMA_RISPOSTA = {
    "type": "OBJECT",
    "properties": {"risposta": {"type": "STRING"}, "punti": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["risposta", "punti"],
}
ESEMPIO_RISPOSTA = {"risposta": "...", "punti": ["..."]}


def domanda(giro: Giro, testo: str) -> tuple[str, list[tuple[Path, str]]]:
    """/azienda: l'IA sceglie quali dati chiedere alla piattaforma, l'agente li recupera e l'IA risponde.
    Restituisce il messaggio in HTML e gli allegati (foglio del DCF, report)."""
    elenco_dati = "\n".join(f"  - {nome}: {descrizione}" for nome, (descrizione, _) in DATI.items())
    istruzioni = ISTRUZIONI_PIANO.format(massimo=MAX_DATI, elenco=elenco_dati, panoramica=", ".join(PANORAMICA))
    piano = giro.con_motori(lambda m, k: ia.genera(m, k, istruzioni, testo, SCHEMA_PIANO, ESEMPIO_PIANO), giro.motori())
    if not piano or not piano.get("societa"):  # senza IA: il testo è il nome della società, e una panoramica
        piano = {"societa": [{"nome": testo, "simbolo_usa": testo, "simbolo_yahoo": ""}], "dati": PANORAMICA,
                 "anno": 0, "trimestre": 0, "domanda": "panoramica della società"}
    richiesta = {"anno": piano.get("anno") or 0, "trimestre": piano.get("trimestre") or 0}
    nomi_dati = [d for d in dict.fromkeys(piano.get("dati") or PANORAMICA) if d in DATI][:MAX_DATI]
    print(f"/azienda: {[s.get('nome') for s in piano['societa']]} · dati: {nomi_dati}")

    trovate, materiale, allegati, mancanti = [], [], [], []
    for voce in piano["societa"][:MAX_SOCIETA]:
        nome, simbolo = (voce.get("nome") or "").strip(), (voce.get("simbolo_usa") or "").strip()
        societa = trova(simbolo, solo_simbolo=True) if simbolo else None
        if societa and nome and not dati.stessa_societa(nome, societa["nome"]):
            societa = None  # il ticker ora è di un'altra società
        societa = societa or (trova(nome) if nome else None)
        if not societa:
            mancanti.append(voce)
            continue
        trovate.append(societa)
        blocchi = [f"===== {societa['nome']} ({societa['simbolo']}) ====="]
        for nome in nomi_dati:
            try:
                testo_dato, allegato = DATI[nome][1](societa, richiesta)
            except Exception as e:  # noqa: BLE001 — un dato che manca non ferma gli altri
                print(f"  {societa['simbolo']} · {nome}: non disponibile ({type(e).__name__})")
                testo_dato, allegato = "non disponibile", None
            blocchi.append(f"--- {nome.upper()} ---\n{testo_dato}")
            if allegato:
                allegati.append(allegato)
        materiale.append("\n".join(blocchi))

    avvisi = [f"{v.get('nome')} non è nella piattaforma"
              + (f" (prezzo attuale {_prezzo_breve(v['simbolo_yahoo'])})"
                 if v.get("simbolo_yahoo") and _prezzo_breve(v["simbolo_yahoo"]) else "") for v in mancanti]
    if not trovate:
        return notifiche.esc("; ".join(avvisi) + ". La piattaforma contiene solo le società quotate negli USA, "
                             "comprese le estere quotate anche a New York (es. ASML, Stellantis): se la società "
                             "lo è, prova con il suo ticker USA, es. /azienda STLA."), []

    risposta = giro.con_motori(
        lambda m, k: ia.genera(m, k, ISTRUZIONI_RISPOSTA, f"DOMANDA: {piano.get('domanda') or testo}\n\n" + "\n\n".join(materiale),
                               SCHEMA_RISPOSTA, ESEMPIO_RISPOSTA), giro.motori())
    intestazione = " · ".join(f"<b>{notifiche.esc(s['nome'])}</b> ({s['simbolo']})"
                              + (f" {notifiche.esc(p)}" if (p := _prezzo_breve(s['simbolo'])) else "") for s in trovate)
    richiesta_chiara = (piano.get("domanda") or testo).strip()
    righe = [f"🏢 {intestazione}", f"<i>{notifiche.esc(richiesta_chiara[:1].upper() + richiesta_chiara[1:])}</i>", ""]
    if risposta and risposta.get("risposta"):
        righe.append(notifiche.esc(risposta["risposta"]))
        punti = [p for p in risposta.get("punti") or [] if p.strip()][:5]
        if punti:
            righe += [""] + [f"• {notifiche.esc(p)}" for p in punti]
    else:  # senza IA: i dati così come sono
        righe.append(notifiche.esc("\n\n".join(materiale))[:3000])
    if avvisi:
        righe += ["", notifiche.esc("⚠️ " + "; ".join(avvisi) + ".")]
    aggiornamento = aggiornata_al()
    righe += ["", f"<i>Dati: {', '.join(nomi_dati)} · fonte defeatbeta-api (Yahoo Finance"
                  f"{', aggiornati il ' + aggiornamento if aggiornamento else ''}), prezzo attuale da Yahoo</i>"]
    return "\n".join(righe), allegati


# ---------------- strumenti ----------------

def _prezzo_breve(simbolo: str) -> str:
    q = dati.quotazione(simbolo)
    if not q:
        return ""
    variazione = f" ({dati.numero(q['variazione'], 1, segno=True)}%)" if q["variazione"] is not None else ""
    return f"{dati.numero(q['prezzo'], 2)} {VALUTE.get(q['valuta'], q['valuta'])}{variazione}"


def _colonna_trimestre(colonne: list[str], richiesta: dict) -> str:
    """La colonna del trimestre chiesto (anno e trimestre dell'anno solare di chiusura), altrimenti l'ultima."""
    if richiesta.get("anno") and richiesta.get("trimestre"):
        mesi = {1: "03", 2: "06", 3: "09", 4: "12"}.get(richiesta["trimestre"])
        trovata = next((c for c in colonne if c.startswith(f"{richiesta['anno']}-{mesi}")), None)
        if trovata:
            return trovata
    return colonne[0]


def _voce(df, etichette: list[str], colonna: str) -> float | None:
    """Il valore di una riga di un bilancio (per nome, in ordine di preferenza) in una colonna."""
    if colonna not in df.columns:
        return None
    for etichetta in etichette:
        righe = df[df["Breakdown"].str.lower() == etichetta.lower()]
        if len(righe):
            return _numero(righe.iloc[0][colonna])
    return None


def _ultimo(df, colonna: str) -> float | None:
    if df is None or not len(df) or colonna not in df.columns:
        return None
    valori = df[colonna].dropna()
    return _numero(valori.iloc[-1]) if len(valori) else None


def _numero(x) -> float | None:
    """I bilanci segnano '*' i valori mancanti."""
    try:
        valore = float(x)
    except (TypeError, ValueError):
        return None
    return None if valore != valore else valore  # NaN


def _importo(x: float, valuta: str) -> str:
    """'4.875 mld $', '22,3 mld €', '650 mln $'."""
    simbolo = VALUTE.get(valuta, valuta)
    for soglia, sigla in ((1e9, "mld"), (1e6, "mln")):
        if abs(x) >= soglia:
            return f"{dati.numero(x / soglia, 0 if abs(x) >= 100 * soglia else 1)} {sigla} {simbolo}"
    return f"{dati.numero(x, 0)} {simbolo}"


def _pct(x: float, segno: bool = True) -> str:
    return f"{dati.numero(x * 100, 1, segno=segno)}%"


def _data(valore) -> str:
    """'2026-06-30' -> '30/06/2026'."""
    testo = str(valore)[:10]
    return f"{testo[8:10]}/{testo[5:7]}/{testo[:4]}" if re.match(r"\d{4}-\d{2}-\d{2}", testo) else testo
