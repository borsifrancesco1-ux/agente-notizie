"""Prova i motori di IA uno per uno con una vera scelta delle notizie, senza inviare né salvare nulla.

Serve a sapere subito se un motore non funziona (chiave scaduta, modello ritirato, quota finita,
server sovraccarico, risposta troncata) invece di accorgersene quando le notizie non arrivano.
La prova usa tutte le notizie delle ultime ore, anche quelle già valutate: è il carico più pesante
che un motore riceve (come il riepilogo della notte).

Uso, dalla cartella del progetto (chiavi nel file .env; su GitHub arrivano dai Secrets):
  python strumenti/prova_motori.py            prova i motori di config.yaml
  python strumenti/prova_motori.py --modelli  elenca anche i modelli disponibili su Gemini,
                                              OpenRouter (gratuiti) e GitHub Models, per scegliere
                                              un sostituto quando un motore smette di funzionare
  python strumenti/prova_motori.py --anche gemini:gemini-2.5-flash-lite openrouter:nvidia/x:free github:openai/gpt-4o-mini
                                              prova anche altri modelli, prima di metterli in config.yaml
                                              (stesso elenco nella variabile PROVA_ANCHE, separato da spazi)
Esce con errore se nessun motore di config.yaml risponde.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import load_dotenv

CARTELLA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CARTELLA))
import agente  # noqa: E402
import feedback  # noqa: E402
import ia  # noqa: E402
from memoria import Memoria  # noqa: E402

SU_GITHUB = bool(os.environ.get("GITHUB_ACTIONS"))
SERVIZI = {  # per --anche: servizio -> come si chiama
    "gemini": {"tipo": "gemini", "chiave": "GEMINI_API_KEY"},
    "openrouter": {"tipo": "openai", "url": "https://openrouter.ai/api/v1/chat/completions",
                   "chiave": "OPENROUTER_API_KEY"},
    "github": {"tipo": "openai", "url": "https://models.github.ai/inference/chat/completions",
               "chiave": "GITHUB_TOKEN", "max_notizie": 20},
}


def motori_in_piu() -> list[dict]:
    """I modelli da provare oltre a quelli di config.yaml: dopo --anche e nella variabile PROVA_ANCHE."""
    voci = os.environ.get("PROVA_ANCHE", "").split()
    if "--anche" in sys.argv:
        voci += [a for a in sys.argv[sys.argv.index("--anche") + 1:] if not a.startswith("--")]
    motori = []
    for voce in voci:
        servizio, _, modello = voce.partition(":")
        if servizio not in SERVIZI or not modello:
            print(f"«{voce}» non valido: scrivi servizio:modello, con servizio tra {', '.join(SERVIZI)}")
            continue
        motori.append({**SERVIZI[servizio], "modello": modello})
    return motori


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(CARTELLA / ".env")
    config = yaml.safe_load((CARTELLA / "config.yaml").read_text("utf-8"))
    profilo = (CARTELLA / "profilo.md").read_text("utf-8")
    memoria = Memoria(CARTELLA / "stato" / "memoria.json")  # solo in lettura: non viene salvata
    memoria.valutate.clear()  # tutte le notizie recenti, anche quelle già valutate
    fuso = ZoneInfo((config.get("orari") or {}).get("fuso_orario", "Europe/Rome"))
    giro = agente.Giro(config, profilo, memoria, True, datetime.now(fuso))
    candidati = giro.candidati()
    if not candidati:
        print("Nessuna notizia da far valutare: riprova più tardi")
        sys.exit(1)

    motori = list(giro.motori())
    settimanale = (config.get("ia") or {}).get("motore_settimanale")
    if settimanale and settimanale not in motori:
        motori.append(settimanale)
    in_config = len(motori)
    motori += [m for m in motori_in_piu() if m not in motori]
    esiti = []
    for posizione, motore in enumerate(motori):
        nome = f"{motore['tipo']} · {motore['modello']}" + ("" if posizione < in_config else " (prova)")
        print(f"\n===== {nome} =====")
        chiave = os.environ.get(motore["chiave"])
        if not chiave:
            esiti.append((nome, False, f"manca la chiave {motore['chiave']}"))
            continue
        inizio = time.monotonic()
        try:
            scelte = ia.seleziona(candidati, memoria.recenti(48), profilo, config, motore, chiave, 4,
                                  feedback.esempi_per_ia(memoria))
        except (ia.QuotaEsaurita, ia.ErroreIA) as e:
            esiti.append((nome, False, f"{type(e).__name__}: {e}"))
            continue
        except Exception as e:  # noqa: BLE001 — la prova deve arrivare in fondo e dire cosa è successo
            esiti.append((nome, False, f"errore inatteso {type(e).__name__}: {e}"))
            continue
        secondi = time.monotonic() - inizio
        for s in scelte:
            print(f"  {s['voto']}/10 · {s['tema']} · {s['titolo'][:90]}")
            print(f"    {s['riassunto'][:160]}")
        esiti.append((nome, True, f"{secondi:.0f} s, {min(len(candidati), motore.get('max_notizie') or 10**6)} "
                                  f"fatti valutati, {len(scelte)} notizie scelte"))

    print("\n===== RISULTATO =====")
    for posizione, (nome, ok, dettaglio) in enumerate(esiti):
        print(f"{'✓' if ok else '✗'} {nome}: {dettaglio}")
        if SU_GITHUB and not ok and posizione < in_config:
            print(f"::warning title=Motore IA non disponibile::{nome}: {dettaglio}")
    if "--modelli" in sys.argv:
        elenca_modelli()
    if not any(ok for _, ok, _ in esiti[:in_config]):
        print("Nessun motore ha risposto")
        sys.exit(1)


def elenca_modelli() -> None:
    """I modelli disponibili adesso presso ciascun servizio, per sostituire un motore che non va."""
    print("\n===== MODELLI DISPONIBILI =====")
    chiave = os.environ.get("GEMINI_API_KEY")
    if chiave:
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", timeout=30,
                             params={"pageSize": 200}, headers={"x-goog-api-key": chiave})
            nomi = [m["name"].removeprefix("models/") for m in r.json().get("models", [])
                    if "generateContent" in m.get("supportedGenerationMethods", [])]
            print("Gemini:", ", ".join(n for n in nomi if "flash" in n or "latest" in n) or f"HTTP {r.status_code}")
        except (requests.RequestException, ValueError) as e:
            print(f"Gemini: elenco non disponibile ({type(e).__name__})")
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
        gratuiti = sorted((m for m in r.json().get("data", []) if m["id"].endswith(":free")),
                          key=lambda m: -(m.get("context_length") or 0))
        print("OpenRouter gratuiti (contesto):",
              ", ".join(f"{m['id']} ({(m.get('context_length') or 0) // 1000}k)" for m in gratuiti[:30]))
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"OpenRouter: elenco non disponibile ({type(e).__name__})")
    chiave = os.environ.get("GITHUB_TOKEN")
    if chiave:
        r = None
        try:
            r = requests.get("https://models.github.ai/catalog/models", timeout=30,
                             headers={"Authorization": f"Bearer {chiave}", "Accept": "application/vnd.github+json"})
            modelli = r.json() if r.status_code == 200 else []
            print("GitHub Models (livello di limiti):",
                  ", ".join(f"{m['id']} ({m.get('rate_limit_tier', '?')})" for m in modelli
                            if isinstance(m, dict) and m.get("id")) or f"HTTP {r.status_code}")
        except (requests.RequestException, ValueError) as e:
            stato = f"HTTP {r.status_code}, " if r is not None else ""
            print(f"GitHub Models: elenco non disponibile ({stato}{type(e).__name__})")


if __name__ == "__main__":
    main()
