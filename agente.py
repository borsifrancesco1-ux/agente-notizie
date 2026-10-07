"""Agente notizie finanziarie: legge fonti e dati ufficiali, sceglie ciò che conta e lo invia su Telegram.

Uso:
  python agente.py                  un giro di notizie, subito
  python agente.py --automatico     fa quello che prevede l'orario (sezione "orari" di config.yaml):
                                    buongiorno con calendario e riepilogo della notte, dati appena escono,
                                    notizie ogni due ore, riepilogo della domenica, chiusura, silenzio la notte.
                                    È il comando di GitHub Actions
  python agente.py --apertura       buongiorno con il calendario del giorno e il riepilogo della notte
  python agente.py --dati           controlla subito se sono usciti dati ufficiali nuovi
  python agente.py --settimanale    riepilogo della settimana e proposta di modifica al profilo
  python agente.py --chiusura       messaggio di fine giornata
  python agente.py --comando "..."  esegue un comando come se arrivasse dal bot (es. "/segui Mediobanca")
  python agente.py --prova          stampa i messaggi invece di inviarli e non salva nulla
                                    (si combina con le altre opzioni)
  python agente.py --rivaluta       riesamina anche le notizie recenti già valutate (non quelle già
                                    inviate): per vedere subito l'effetto di modifiche a profilo e config
  python agente.py --max 1          limita il numero di notifiche del giro
  python agente.py --controlla-feed verifica che ogni feed risponda
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

import comandi
import dati
import feedback
import fonti
import ia
import notifiche
import settimana
from filtro import Filtro, Gruppo, raggruppa, scelta_senza_ia
from fonti import Notizia
from memoria import ERRORI_PRIMA_DI_AVVISARE, Memoria, chiave, giorno_quota

CARTELLA = Path(__file__).resolve().parent
MAX_CANDIDATI = 150        # fatti al massimo passati all'IA in un giro
ICONA_PREDEFINITA = "📰"
APERTURA_ENTRO = "12:00"   # se il buongiorno non è partito entro quest'ora (es. GitHub fermo), si salta


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(CARTELLA / ".env")  # in locale; su GitHub le chiavi arrivano dai Secrets

    parser = argparse.ArgumentParser(description="Agente notizie finanziarie")
    azione = parser.add_mutually_exclusive_group()
    azione.add_argument("--automatico", action="store_true", help="fa quello che prevede l'orario")
    azione.add_argument("--apertura", action="store_true", help="buongiorno con calendario e riepilogo della notte")
    azione.add_argument("--dati", action="store_true", help="controlla i dati ufficiali usciti")
    azione.add_argument("--settimanale", action="store_true", help="riepilogo e proposta di profilo della settimana")
    azione.add_argument("--chiusura", action="store_true", help="messaggio di fine giornata")
    azione.add_argument("--comando", help="esegue un comando come se arrivasse dal bot")
    azione.add_argument("--controlla-feed", action="store_true", help="verifica che ogni feed risponda")
    parser.add_argument("--prova", action="store_true", help="stampa invece di inviare, non salva nulla")
    parser.add_argument("--rivaluta", action="store_true",
                        help="riesamina anche le notizie recenti già valutate, non quelle già inviate")
    parser.add_argument("--max", type=int, help="numero massimo di notifiche in questo giro")
    args = parser.parse_args()

    config = yaml.safe_load((CARTELLA / "config.yaml").read_text("utf-8"))
    if args.max:
        config.setdefault("notifiche", {})["max_per_giro"] = args.max
        config.setdefault("orari", {})["max_riepilogo_notte"] = args.max
    if args.controlla_feed:
        controlla_feed(config)
        return

    percorso_profilo = CARTELLA / "profilo.md"
    profilo = percorso_profilo.read_text("utf-8") if percorso_profilo.exists() else ""
    memoria = Memoria(CARTELLA / "stato" / "memoria.json")
    if args.rivaluta:
        memoria.valutate.clear()

    orari = config.get("orari") or {}
    adesso = datetime.now(ZoneInfo(orari.get("fuso_orario", "Europe/Rome")))
    giro = Giro(config, profilo, memoria, args.prova, adesso)
    giro.carica_iscrizioni()
    # prima i comandi, poi il programma: un /pausa o un /riprendi vale già per questo giro
    if args.comando:
        giro.esegui_comandi([{"id": None, "testo": args.comando}])
    elif not args.prova:
        giro.esegui_comandi(giro.leggi_telegram())

    if args.automatico:
        azioni = piano_della_giornata(adesso, orari, memoria)
    elif args.comando:
        azioni = []
    else:
        azioni = [a for a in ("apertura", "dati", "settimanale", "chiusura") if getattr(args, a)] or ["notizie"]
    if not args.comando:
        print(f"{adesso:%d/%m %H:%M} · azioni: {', '.join(azioni) or 'nessuna'}")
    for nome in [*azioni, *[a for a in giro.azioni_extra if a not in azioni]]:
        getattr(giro, nome)()
    if not args.prova:
        memoria.salva()


def piano_della_giornata(adesso: datetime, orari: dict, memoria: Memoria) -> list[str]:
    """Le azioni previste a quest'ora. Tollera i ritardi di GitHub Actions: un'azione saltata
    si recupera al giro successivo e nessuna si ripete nello stesso giorno (o nella stessa ora)."""
    oggi, ora = adesso.date().isoformat(), adesso.strftime("%H:%M")
    apertura = orari.get("apertura", "07:30")
    prima, ultima = orari.get("prima_notizia", "08:00"), orari.get("ultima_notizia", "22:00")
    fatto = memoria.giornata
    if ora < apertura or fatto.get("chiusura") == oggi:
        print("Silenzio notturno")
        return []
    if memoria.in_pausa():
        print(f"In pausa fino a {memoria.pausa_fino} (comando /pausa)")
        return []
    azioni = []
    if fatto.get("apertura") != oggi and ora < APERTURA_ENTRO:
        azioni.append("apertura")
    azioni.append("dati")  # i dati ufficiali si controllano a ogni giro della giornata, cioè ogni ora
    # notizie una volta per fascia di "ogni_ore" ore, dall'ora della prima fino all'ora dell'ultima compresa
    # (un giro in ritardo dopo le 23, es. alla fine di una pausa, fa solo la chiusura)
    if prima <= ora and ora[:2] <= ultima[:2] and fatto.get("ora_notizie") != fascia_notizie(adesso, orari):
        azioni.append("notizie")
    settimanale = orari.get("riepilogo_settimanale") or {}
    if (settimanale and adesso.weekday() == notifiche.GIORNI.index(settimanale.get("giorno", "domenica"))
            and ora >= settimanale.get("ora", "18:00")
            and fatto.get("settimanale") != settimana.settimana_iso(adesso)):
        azioni.append("settimanale")
    if ora >= ultima:
        azioni.append("chiusura")
    return azioni


def fascia_notizie(adesso: datetime, orari: dict) -> str:
    """La fascia del giro di notizie a cui appartiene quest'ora: con ogni_ore 2 e la prima notizia alle 8,
    le fasce iniziano alle 8, 10, 12... e un giro delle 9 rientra in quella delle 8 (niente notizie,
    o recupera quella delle 8 se quel giro è saltato)."""
    ogni = max(1, int(orari.get("ogni_ore", 1)))
    prima = int(orari.get("prima_notizia", "08:00")[:2])
    inizio = prima + (adesso.hour - prima) // ogni * ogni
    return f"{adesso.date().isoformat()} {inizio:02d}"


class Giro:
    """Un'esecuzione dell'agente: raccoglie notizie e dati, li fa scegliere e li invia."""

    def __init__(self, config: dict, profilo: str, memoria: Memoria, prova: bool, adesso: datetime):
        self.config, self.profilo, self.memoria, self.prova, self.adesso = config, profilo, memoria, prova, adesso
        self.cartella = CARTELLA
        self.fuso = adesso.tzinfo
        self.oggi = adesso.date().isoformat()
        self.token = os.environ.get("TELEGRAM_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self.canale = os.environ.get("TELEGRAM_CANALE") or self.chat_id  # il canale del team, se c'è
        self.worker_url = (os.environ.get("WORKER_URL") or "").rstrip("/")
        self.worker_chiave = os.environ.get("AGENTE_KEY", "")
        self.azioni_extra: list[str] = []  # azioni chieste dai comandi (es. /notizie)
        self.iscrizioni: dict[str, dict] = {}  # /iscrivimi: chi riceve le notizie in privato (dal Worker)
        self._scaricate: list[Notizia] | None = None
        self.aggiorna_da_config()

    def aggiorna_da_config(self) -> None:
        """Rilegge le impostazioni che dipendono da config.yaml (anche dopo un comando)."""
        self.opzioni = self.config.get("notifiche") or {}
        self.orari = self.config.get("orari") or {}
        temi = (self.config.get("titoli") or []) + (self.config.get("temi") or [])
        self.icone = {t["nome"]: t.get("icona") or ICONA_PREDEFINITA for t in temi}
        self.reparti_dei_temi = {t["nome"]: t.get("reparti") or [] for t in temi}
        self.nomi_reparti = [r["nome"] for r in self.config.get("reparti") or []]
        self.etichetta_perche = self.opzioni.get("etichetta_perche", "Perché conta")
        self.etichetta_impatto = self.opzioni.get("etichetta_impatto", "Impatto atteso")

    # ---------- azioni ----------

    def notizie(self) -> None:
        """Un giro di notizie: una notifica per ogni fatto scelto, con i pulsanti di voto.
        Le novità su un fatto già inviato arrivano come aggiornamento, in risposta al messaggio originale."""
        scelte = self.scegli(self.opzioni.get("max_per_giro", 5))
        print(f"Notifiche da inviare: {len(scelte)}")
        suonato = False  # il telefono suona al massimo una volta per giro, solo per le notizie sopra la soglia
        for s in scelte:
            g, originale = s["gruppo"], s.get("aggiorna")
            rispondi_a = (originale or {}).get("messaggio")
            mercati = []
            if (s["voto"] or 0) >= (self.config.get("reazione_mercati") or {}).get("voto_minimo_notizie", 9):
                mercati = dati.reazione(self.config, "notizie", datetime.now(timezone.utc) - timedelta(hours=1))
            # prezzo attuale delle società citate e livello di tassi, cambi o materie prime toccati
            valori = dati.valori_notizia(self.config, s.get("aziende") or [], s.get("valori") or [], self.fuso)
            if s.get("aziende") or s.get("valori"):
                print(f"  valori per «{s['titolo'][:50]}»: {s.get('aziende')} {s.get('valori')}")
            testo = notifiche.componi(s["titolo"], s["riassunto"], s["perche_conta"],
                                      self.icone.get(s["tema"], ICONA_PREDEFINITA), s["tema"], s["voto"],
                                      s["reparti"], self.etichetta_perche, s.get("nota", ""),
                                      s.get("impatto", ""), self.etichetta_impatto, aggiornamento=bool(originale),
                                      mercati=mercati, valori=valori,
                                      aggiorna_titolo="" if rispondi_a else (originale or {}).get("titolo", ""))
            silenzioso = suonato or not s["voto"] or s["voto"] < self.opzioni.get("con_suono_da", 9)
            id_notifica = chiave(g.principale.link)
            tastiera = feedback.tastiera(id_notifica, g.fonti())
            messaggio = self.invia(testo, silenzioso, tastiera, rispondi_a=rispondi_a)
            if not messaggio:  # es. un link rifiutato come pulsante: riprovo con i link nel testo
                testo += notifiche.link_testuali(g.fonti())
                tastiera = feedback.tastiera(id_notifica, g.fonti(), con_link=False)
                messaggio = self.invia(testo, silenzioso, tastiera, rispondi_a=rispondi_a)
            if messaggio:
                suonato = suonato or not silenzioso
                self.registra(s, id_notifica, messaggio)
                self.invia_iscritti(testo, s["reparti"], silenzioso, tastiera)
        self.memoria.giornata["ora_notizie"] = fascia_notizie(self.adesso, self.orari)

    def apertura(self) -> None:
        """Buongiorno in un unico messaggio: calendario del giorno e riepilogo della notte."""
        calendario = dati.calendario(self.config, self.adesso.date(), self.adesso.date(), self.fuso)
        scelte = self.scegli(self.orari.get("max_riepilogo_notte", 6))
        voci = [{"titolo": s["titolo"], "riassunto": s["riassunto"], "reparti": s["reparti"],
                 "icona": self.icone.get(s["tema"], ICONA_PREDEFINITA), "fonti": s["gruppo"].fonti()}
                for s in scelte]
        testo = notifiche.riepilogo_notte(voci, self.adesso, self.orari.get("prima_notizia", "08:00"), calendario,
                                          dati.cambio_del_giorno(self.config), self.orari.get("ogni_ore", 1),
                                          dati.quadro_mercati(self.config, "apertura", self.fuso))
        if self.invia(testo):
            for s in scelte:
                self.registra(s, chiave(s["gruppo"].principale.link))
        self.memoria.giornata["apertura"] = self.oggi

    def dati(self) -> None:
        """Dato uscito: pubblica i valori ufficiali appena usciti, con precedente e consensus."""
        for nuovo in dati.nuovi_dati(self.config, self.memoria):
            ind = nuovo["indicatore"]
            atteso = dati.consensus(ind, self.adesso)
            motivo = dati.anomalia(nuovo, atteso, self.opzioni.get("anomalia_sigma", 2.5))
            if motivo:
                print(f"Dato anomalo: {ind['nome']} ({motivo})")
            # reazione dei mercati dall'ora di uscita (dal calendario), altrimenti nell'ultima ora
            uscita = dati.evento(ind, self.adesso)
            dal = uscita.quando if uscita else datetime.now(timezone.utc) - timedelta(hours=1)
            area = ind.get("area") or ("usa" if ind["fonte"] == "fred" else "euro")
            mercati = dati.reazione(self.config, area, dal)
            periodo = f"dalle {dal.astimezone(self.fuso):%H:%M}" if uscita else "nell'ultima ora"
            testo = dati.messaggio_dato(nuovo, atteso, self.etichetta_perche, motivo, mercati, periodo)
            if self.invia(testo):
                self.memoria.registra_dato(ind["nome"], dati.riassunto_dato(nuovo, atteso, motivo), ind.get("reparti") or [])
                self.invia_iscritti(testo, ind.get("reparti") or [], silenzioso=False)

    def settimanale(self) -> None:
        """Riepilogo della settimana sul canale e proposta di modifica al profilo in privato."""
        self.riepilogo_settimanale()
        self.proposta_profilo()
        self.memoria.giornata["settimanale"] = settimana.settimana_iso(self.adesso)

    def chiusura(self) -> None:
        """Fine delle comunicazioni, con il conto delle notizie del giorno per reparto."""
        inizio_giornata = self.adesso.replace(hour=0, minute=0, second=0, microsecond=0)
        inviate_oggi = self.memoria.inviate_dal(inizio_giornata)
        testo = notifiche.chiusura(inviate_oggi, self.orari.get("apertura", "07:30"),
                                   dati.quadro_mercati(self.config, "chiusura", self.fuso))
        self.invia(testo, silenzioso=True)
        # chi è iscritto in modalità "sera" riceve in privato le notizie del giorno dei suoi reparti
        for iscritto in self.iscrizioni.values():
            if iscritto.get("modo") == "sera" and iscritto.get("chat"):
                mie = [n for n in inviate_oggi if set(n.get("reparti") or []) & set(iscritto.get("reparti") or [])]
                self.invia(notifiche.riepilogo_serale(mie, iscritto.get("reparti") or []), silenzioso=True,
                           chat=iscritto["chat"])
        self.memoria.giornata["chiusura"] = self.oggi

    # ---------- settimana ----------

    def riepilogo_settimanale(self) -> None:
        righe = dati.mercati_settimana(self.config)
        tabella, copertura = dati.tabella_mercati(righe), dati.costo_copertura(righe, self.config)
        inizio = (self.adesso - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        limite = inizio.astimezone(timezone.utc).isoformat(timespec="seconds")
        usciti = [d for d in self.memoria.dati_usciti if d["quando"] >= limite]
        dal, al = settimana.prossimi_giorni(self.adesso)
        prossima = dati.calendario(self.config, dal, al, self.fuso, impatto_minimo="High")
        istruzioni, testo = settimana.materiale_riepilogo(tabella, copertura, usciti, self.memoria.inviate_dal(inizio),
                                                          prossima, self.profilo, self.nomi_reparti)
        schema, esempio = settimana.schema_riepilogo(self.nomi_reparti)
        risposta = self.con_motori(lambda m, k: ia.genera(m, k, istruzioni, testo, schema, esempio),
                                   self.motori_settimanali(), avvisa_quota=False)
        if not risposta:
            risposta = {"in_sintesi": "L'IA non era disponibile: qui sotto i numeri della settimana.", "reparti": []}
        html, markdown = settimana.componi_riepilogo(risposta, inizio, self.adesso, tabella, copertura, self.nomi_reparti)
        for numero, parte in enumerate(notifiche.dividi(html)):
            self.invia(parte, silenzioso=numero > 0)
        archivio = self.cartella / "riepiloghi" / f"{settimana.settimana_iso(self.adesso)}.md"
        if self.prova:
            print(f"(prova) {archivio.name} non salvato")
        else:
            archivio.parent.mkdir(exist_ok=True)
            archivio.write_text(markdown, "utf-8")

    def proposta_profilo(self) -> None:
        limite = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
        votate = [i for i in self.memoria.notifiche_votate(100) if i["quando"] >= limite]
        if len(votate) < settimana.MIN_NOTIZIE_VOTATE:
            print(f"Proposta di profilo saltata: solo {len(votate)} notizie votate nelle ultime due settimane")
            return
        testo = settimana.materiale_proposta(self.profilo, votate)
        risposta = self.con_motori(lambda m, k: ia.genera(m, k, settimana.ISTRUZIONI_PROPOSTA, testo,
                                                          settimana.SCHEMA_PROPOSTA, settimana.ESEMPIO_PROPOSTA),
                                   self.motori_settimanali(), avvisa_quota=False)
        if not risposta or not risposta.get("proponi") or not (risposta.get("nuovo_profilo") or "").strip():
            print("Nessuna modifica al profilo da proporre")
            return
        id_ = settimana.id_proposta(risposta["nuovo_profilo"])
        self.memoria.proposta = {"id": id_, "testo": risposta["nuovo_profilo"], "spiegazione": risposta["spiegazione"],
                                 "quando": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        url = f"https://github.com/{comandi.repository()}/commits/main/profilo.md"
        self.invia(settimana.messaggio_proposta(risposta["spiegazione"], len(votate), url),
                   tastiera=settimana.tastiera_proposta(id_), privato=True)

    # ---------- comandi dal bot ----------

    def leggi_telegram(self) -> list[dict]:
        """Voti e comandi arrivati dal bot: dal Worker di Cloudflare se c'è, altrimenti da Telegram."""
        if self.worker_url:
            return feedback.leggi_worker(self.memoria, self.worker_url, self.worker_chiave)
        return feedback.leggi_telegram(self.memoria, self.token, self.chat_id)

    def carica_iscrizioni(self) -> None:
        """Chi ha chiesto con /iscrivimi le notizie di alcuni reparti in privato (le gestisce il Worker)."""
        if self.worker_url:
            self.iscrizioni = feedback.iscrizioni_worker(self.worker_url, self.worker_chiave)
            if self.iscrizioni:
                print(f"Iscritti in privato: {len(self.iscrizioni)}")

    def esegui_comandi(self, coda: list[dict]) -> None:
        eseguiti = []
        for voce in coda:
            print(f"Comando: {voce['testo']}")
            try:
                risposta = comandi.esegui(self, voce["testo"])
            except Exception as e:  # noqa: BLE001 — un comando sbagliato non deve fermare il giro
                risposta = f"⚠️ Non sono riuscito a eseguire «{voce['testo']}» ({type(e).__name__})."
            # le risposte con link (/chiedi, /oggi, /cerca, /azienda) sono già in HTML
            self.invia(risposta if isinstance(risposta, comandi.Html) else notifiche.esc(risposta), privato=True)
            for percorso, didascalia in getattr(risposta, "allegati", []):  # es. il foglio Excel del DCF
                self.invia_file(percorso, didascalia, privato=True)
            if voce.get("id"):
                eseguiti.append(voce["id"])
        if self.worker_url and not self.prova:
            feedback.conferma_worker(self.worker_url, self.worker_chiave, eseguiti)

    # ---------- passaggi comuni ----------

    def scegli(self, massimo: int) -> list[dict]:
        """Raccoglie le notizie nuove e sceglie quelle da inviare, completando tema e reparti."""
        candidati = self.candidati()
        if not candidati:
            return []
        scelte = self.con_motori(
            lambda m, k: ia.seleziona(candidati, self.memoria.recenti(48), self.profilo, self.config,
                                      m, k, massimo, feedback.esempi_per_ia(self.memoria)), self.motori())
        if scelte is None:
            print("Nessun motore disponibile: uso il filtro a parole chiave")
            scelte = [{"gruppo": g, "voto": None, "tema": min(g.titoli or g.temi or {""}), "reparti": [],
                       "titolo": g.principale.titolo, "riassunto": "", "perche_conta": "", "impatto": "",
                       "aggiorna": None, "nota": "filtro a parole chiave"}
                      for g in scelta_senza_ia(candidati, massimo)]
        for s in scelte:
            g = s["gruppo"]
            if s["tema"] not in self.icone:  # tema inventato dal modello: uso quello delle parole chiave
                s["tema"] = min(g.titoli or g.temi or {s["tema"]})
            if not s["reparti"]:  # senza IA, o se il modello non li ha indicati: dai temi
                s["reparti"] = self.reparti_da_temi([s["tema"], *sorted(g.titoli), *sorted(g.temi)])
        self.memoria.segna_valutate(n.link for g in candidati for n in g.notizie)
        return scelte

    def candidati(self) -> list[Gruppo]:
        """Le notizie nuove e pertinenti, raggruppate per fatto e messe in ordine di priorità."""
        if self._scaricate is None:  # i feed si leggono una volta sola per esecuzione
            elenco = fonti.elenco_feed(self.config)
            notizie, errori = fonti.scarica_tutti(elenco)
            print(f"Feed: {len(elenco) - len(errori)}/{len(elenco)} ok, {len(notizie)} notizie lette")
            for nome, errore in errori.items():
                print(f"  ✗ {nome}: {errore}")
            guasti = self.memoria.aggiorna_feed([f.nome for f in elenco], errori)
            if guasti:
                elenco_guasti = "\n".join(f"• {notifiche.esc(nome)}: {errori[nome]}" for nome in guasti)
                self.invia(f"⚠️ Questi feed non rispondono da {ERRORI_PRIMA_DI_AVVISARE} giri:\n"
                           f"{elenco_guasti}\nControlla config.yaml.", silenzioso=True, privato=True)
            escluse = {f.lower() for f in self.config.get("fonti_escluse") or []}
            self._scaricate = [n for n in notizie if n.fonte.lower() not in escluse]

        adesso = datetime.now(timezone.utc)
        limite = adesso - timedelta(hours=self.opzioni.get("finestra_ore", 12))
        # Le istituzioni pubblicano di rado (e mai nel weekend): per loro la finestra è più lunga
        limite_ufficiali = adesso - timedelta(hours=self.opzioni.get("finestra_ore_ufficiali", 72))
        nuove = [n for n in self._scaricate
                 if (n.pubblicata is None or n.pubblicata >= (limite_ufficiali if n.feed and n.feed.ufficiale else limite))
                 and not self.memoria.gia_valutata(n.link)]
        pertinenti = Filtro(self.config).pertinenti(nuove)
        gruppi = sorted((g for g in raggruppa(pertinenti) if not self.memoria.gia_inviata(g.impronta)),
                        key=Gruppo.priorita)
        print(f"Nuove: {len(nuove)} · pertinenti: {len(pertinenti)} · fatti distinti: {len(gruppi)}")
        return gruppi[:MAX_CANDIDATI]

    def motori(self) -> list[dict]:
        return (self.config.get("ia") or {}).get("motori") or []

    def motori_settimanali(self) -> list[dict]:
        """Per i testi della settimana: prima il motore più accurato, poi quelli di tutti i giorni."""
        primo = (self.config.get("ia") or {}).get("motore_settimanale")
        return [primo, *[m for m in self.motori() if m != primo]] if primo else self.motori()

    def con_motori(self, lavoro, motori: list[dict], avvisa_quota: bool = True):
        """Prova i motori in ordine finché uno risponde: lavoro(motore, chiave) -> risultato.
        Restituisce None se falliscono tutti. Conta le richieste e avvisa se finisce la quota."""
        risultato, quota_finita = None, False
        for posizione, motore in enumerate(motori):
            nome = f"{motore['tipo']} · {motore['modello']}"
            chiave_ia = os.environ.get(motore["chiave"])
            if not chiave_ia:
                print(f"{nome}: manca {motore['chiave']}, passo al successivo")
                continue
            try:
                risultato = lavoro(motore, chiave_ia)
                print(f"Fatto con {nome}")
                break
            except ia.QuotaEsaurita as e:
                print(f"Quota esaurita ({e}), passo al successivo")
                quota_finita = quota_finita or posizione == 0
            except ia.ErroreIA as e:
                print(f"{nome} non disponibile ({e}), passo al successivo")

        if ia.richieste_fatte:
            totale = self.memoria.conta_richieste(ia.richieste_fatte)
            print(f"Richieste all'IA: {ia.richieste_fatte} adesso, {totale} oggi")
            ia.richieste_fatte = 0
            if avvisa_quota and quota_finita and self.memoria.avviso_quota != giorno_quota():
                self.memoria.avviso_quota = giorno_quota()
                self.invia(f"⚠️ Quota gratuita del motore principale finita per oggi ({totale} richieste). "
                           "Fino al rinnovo, verso le 9 del mattino, uso i motori di riserva "
                           "o, se mancano anche quelli, il filtro a parole chiave.", silenzioso=True, privato=True)
        return risultato

    def reparti_da_temi(self, nomi: list[str]) -> list[str]:
        """I reparti collegati in config.yaml ai temi e titoli indicati, al massimo tre."""
        reparti: list[str] = []
        for nome in nomi:
            reparti += [r for r in self.reparti_dei_temi.get(nome, []) if r not in reparti]
        return reparti[:3]

    def registra(self, s: dict, id_notifica: str, messaggio: int | None = None) -> None:
        g = s["gruppo"]
        self.memoria.registra_invio(id_notifica, s["titolo"], s["tema"], s["reparti"], g.impronta, g.principale.link,
                                    messaggio, s.get("riassunto", ""),
                                    {"perche": s.get("perche_conta", ""), "impatto": s.get("impatto", ""),
                                     "voto": s.get("voto"), "fonti": g.fonti()[:6]})

    def invia_iscritti(self, testo: str, reparti: list[str], silenzioso: bool, tastiera: dict | None = None) -> None:
        """Copia in privato a chi è iscritto in modalità "subito" ad almeno uno dei reparti."""
        for iscritto in self.iscrizioni.values():
            chat = iscritto.get("chat")
            if (iscritto.get("modo", "subito") == "subito" and chat and str(chat) != str(self.canale)
                    and set(iscritto.get("reparti") or []) & set(reparti)):
                self.invia(testo, silenzioso, tastiera, chat=chat)

    def invia(self, testo: str, silenzioso: bool = False, tastiera: dict | None = None,
              privato: bool = False, rispondi_a: int | None = None, chat: str | int | None = None) -> int:
        """Al canale del team; se privato, alla chat dell'utente; se chat, a quella chat (iscritti).
        Restituisce il numero del messaggio (0 se non è partito). In prova stampa e basta."""
        destinazione = chat or (self.chat_id if privato else self.canale)
        if self.prova:
            tasti = [t["text"] for riga in (tastiera or {}).get("inline_keyboard", []) for t in riga]
            dove = "iscritto in privato" if chat else "chat privata" if privato else "canale"
            print(f"\n----- {dove}, {'senza suono' if silenzioso else 'con suono'}"
                  f"{f', in risposta al messaggio {rispondi_a}' if rispondi_a else ''} -----"
                  f"\n{testo}" + (f"\n[{']  ['.join(tasti)}]" if tasti else ""))
            return 1
        return notifiche.invia(testo, self.token, destinazione, silenzioso, tastiera, rispondi_a)

    def invia_file(self, percorso: Path, didascalia: str = "", privato: bool = False) -> bool:
        """Un file come documento Telegram (es. il foglio Excel del DCF). In prova stampa e basta."""
        if self.prova:
            print(f"\n----- file in {'chat privata' if privato else 'canale'}: {percorso} "
                  f"({percorso.stat().st_size / 1000:.0f} KB) · {didascalia}")
            return True
        return notifiche.invia_file(percorso, self.token, self.chat_id if privato else self.canale,
                                    notifiche.esc(didascalia))


def controlla_feed(config: dict) -> None:
    elenco = fonti.elenco_feed(config)
    with ThreadPoolExecutor(max_workers=12) as pool:
        risultati = list(pool.map(fonti.scarica, elenco))
    for feed, (notizie, errore) in zip(elenco, risultati):
        if errore:
            print(f"✗ {feed.nome}: {errore}")
            continue
        date = [n.pubblicata for n in notizie if n.pubblicata]
        ultima = max(date).strftime("%d/%m %H:%M UTC") if date else "senza date"
        print(f"✓ {feed.nome}: {len(notizie)} notizie, la più recente {ultima}")


if __name__ == "__main__":
    main()
