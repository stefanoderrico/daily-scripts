#!/usr/bin/env python3
"""
Vinted monitor – MacBook Pro 16" tra 250 e 1600 €.

Uso:
    python3 vinted_monitor.py            # un solo controllo (per launchd/cron)
    python3 vinted_monitor.py --loop     # gira in continuo, ogni 15-20 min
    python3 vinted_monitor.py --test     # stampa cosa passa e cosa viene escluso, senza notifiche

Dipendenze:
    pip3 install curl_cffi

Notifiche Telegram (facoltative): imposta le variabili d'ambiente
    TELEGRAM_BOT_TOKEN  e  TELEGRAM_CHAT_ID
"""

import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from curl_cffi import requests

# ───────────────────────── CONFIGURAZIONE ─────────────────────────

DOMAIN = "https://www.vinted.it"
RICERCA = 'macbook pro 16"'
PREZZO_MIN = 250
PREZZO_MAX = 1600

# Esclusioni: solo cose che indicano chiaramente un modello che non interessa.
# Sono regex, confrontate col titolo in minuscolo.
ESCLUDI = [
    r"\b201[5-9]\b", r"\b2020\b",          # anni pre-2021
    r"\bintel\b", r"\bi[579]\b",           # processori Intel
    r"\bair\b",                            # MacBook Air
    r"touch\s*bar",                        # solo Intel
    r"\b1[345]\s*(\"|”|''|pollici|inch)",  # 13", 14", 15"
    r"\bcerco\b", r"\bcompro\b",           # annunci di ricerca
]

INTERVALLO_MIN = 15 * 60   # per --loop
INTERVALLO_MAX = 20 * 60

BASE_DIR = Path(__file__).resolve().parent
SEEN_FILE = BASE_DIR / "vinted_seen.json"
LOG_FILE = BASE_DIR / "vinted_monitor.log"

# ──────────────────────────────────────────────────────────────────

ESCLUDI_RE = [re.compile(p, re.I) for p in ESCLUDI]


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_seen() -> list:
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return []


def save_seen(seen: list) -> None:
    SEEN_FILE.write_text(json.dumps(seen[-5000:]))  # tieni solo gli ultimi 5000


class Vinted:
    def __init__(self) -> None:
        self.new_session()

    def new_session(self) -> None:
        self.s = requests.Session(impersonate="chrome")
        self.s.headers.update({"Accept-Language": "it-IT,it;q=0.9"})

    def search(self) -> list:
        """Legge la pagina dei risultati di ricerca (gli annunci sono già nell'HTML)."""
        params = {
            "search_text": RICERCA,
            "order": "newest_first",
            "price_from": PREZZO_MIN,
            "price_to": PREZZO_MAX,
        }
        for _ in range(2):
            r = self.s.get(DOMAIN + "/catalog", params=params, timeout=30)
            if r.status_code in (401, 403, 429):
                log(f"Richiesta rifiutata ({r.status_code}), riprovo con una nuova sessione…")
                time.sleep(10)
                self.new_session()
                continue
            r.raise_for_status()
            return parse_catalogo(r.text)
        return []


# Ogni annuncio nella pagina ha un link così:
# <a href="/items/123-titolo" ... title="Titolo, Brand: Apple, Modello: ..., Condizioni: Ottime, 900.00 €, 945.70 €">
ITEM_RE = re.compile(r'<a href="(/items/(\d+)-[^"?]*)[^"]*"[^>]*?title="([^"]*)"')
PREZZI_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*€")


def parse_catalogo(page: str) -> list:
    items, visti = [], set()
    for path, iid, attr in ITEM_RE.findall(page):
        if iid in visti:
            continue
        visti.add(iid)
        attr = html.unescape(attr)
        titolo = re.split(r", (?:Brand|Modello|Condizioni|Taglia):", attr)[0]
        prezzi = PREZZI_RE.findall(attr)
        # gli ultimi due importi sono: prezzo articolo, prezzo con protezione acquisti
        prezzo_art = prezzi[-2] if len(prezzi) >= 2 else (prezzi[-1] if prezzi else "0")
        items.append({
            "id": int(iid),
            "title": titolo.strip(),
            "price": prezzo_art.replace(",", "."),
            "url": DOMAIN + path,
        })
    return items


def prezzo(item: dict) -> float:
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(p)
    except (TypeError, ValueError):
        return 0.0


def motivo_esclusione(item: dict):
    """Ritorna il motivo per cui l'annuncio è scartato, oppure None se passa."""
    titolo = (item.get("title") or "").lower()
    if not re.search(r"mac\s*bo+c?k", titolo):  # accetta anche "mac book", "macboock"
        return "non è un macbook"
    for rx in ESCLUDI_RE:
        m = rx.search(titolo)
        if m:
            return f"contiene '{m.group(0)}'"
    p = prezzo(item)
    if not (PREZZO_MIN <= p <= PREZZO_MAX):
        return f"prezzo {p:.0f} € fuori range"
    return None


def notifica(titolo: str, testo: str, url: str) -> None:
    if sys.platform == "darwin":
        if shutil.which("terminal-notifier"):  # notifica cliccabile che apre l'annuncio
            subprocess.run(["terminal-notifier", "-title", titolo, "-message", testo,
                            "-open", url, "-sound", "Glass"], check=False)
        else:
            safe = lambda s: s.replace('"', "'").replace("\\", "")
            subprocess.run(["osascript", "-e",
                            f'display notification "{safe(testo)}" with title "{safe(titolo)}" sound name "Glass"'],
                           check=False)

    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": f"{titolo}\n{testo}\n{url}"},
                          timeout=20)
        except Exception as e:
            log(f"Errore Telegram: {e}")


def controllo(v: Vinted, seen: list, test: bool, silenzioso: bool = False) -> None:
    items = v.search()
    if not items:
        raise RuntimeError("Nessun annuncio ricevuto: possibile blocco da parte di Vinted.")
    visti = set(seen)
    nuovi = 0

    for it in items:
        iid = it["id"]
        url = it.get("url") or f"{DOMAIN}/items/{iid}"
        if url.startswith("/"):
            url = DOMAIN + url
        p = prezzo(it)
        motivo = motivo_esclusione(it)

        if test:
            stato = "OK   " if motivo is None else f"NO   ({motivo})"
            print(f"{p:>6.0f} €  {stato:35}  {it.get('title')}  {url}")
            continue

        if iid in visti:
            continue
        seen.append(iid)
        visti.add(iid)

        if motivo is None and not silenzioso:
            nuovi += 1
            log(f"NUOVO: {p:.0f} € – {it.get('title')} – {url}")
            notifica(f"MacBook Pro 16 a {p:.0f} €", it.get("title", ""), url)

    if not test:
        save_seen(seen)
        log(f"Controllo completato: {len(items)} annunci letti, {nuovi} nuovi segnalati.")


def main() -> None:
    test = "--test" in sys.argv
    loop = "--loop" in sys.argv and not test
    primo_avvio = not SEEN_FILE.exists()
    seen = load_seen()
    if not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")) and sys.platform != "darwin":
        log("Attenzione: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID non impostati, nessuna notifica verrà inviata.")

    errore = False
    try:
        v = Vinted()
    except Exception as e:
        log(f"Impossibile aprire una sessione su Vinted: {e}")
        sys.exit(1)

    while True:
        try:
            # Al primo avvio gli annunci già online vengono solo memorizzati,
            # così non arrivano decine di notifiche tutte insieme.
            if primo_avvio and not test:
                log("Primo avvio: memorizzo gli annunci attuali senza notificarli.")
            controllo(v, seen, test, silenzioso=primo_avvio)
            primo_avvio = False
            errore = False
        except Exception as e:
            errore = True
            log(f"Errore: {e}")
            try:
                v.new_session()
            except Exception:
                pass
        if not loop:
            break
        attesa = random.uniform(INTERVALLO_MIN, INTERVALLO_MAX)
        log(f"Prossimo controllo tra {attesa / 60:.0f} minuti.")
        time.sleep(attesa)

    if errore:
        sys.exit(1)  # su GitHub l'esecuzione risulta fallita, così te ne accorgi


if __name__ == "__main__":
    main()
