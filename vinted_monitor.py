#!/usr/bin/env python3
"""
Monitor annunci su Vinted, Subito ed eBay (MacBook Pro 16", iPad Pro 11" M4/M5, PS5 Pro).

Uso:
    python3 vinted_monitor.py            # un solo controllo (per launchd/cron)
    python3 vinted_monitor.py --loop     # gira in continuo, ogni 5-6 min
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
SUBITO_URL = "https://www.subito.it/annunci-italia/vendita/informatica/"  # tutta Italia
# Categoria Subito diversa per una ricerca: chiave "subito_url" nella ricerca
SUBITO_URL_CONSOLE = "https://www.subito.it/annunci-italia/vendita/videogiochi/"
CERCA_SU_SUBITO = True  # metti False per disattivare Subito
# eBay (API ufficiale): solo "Compralo Subito", usato + ricondizionato, "appena messi in vendita".
# Richiede i secrets EBAY_CLIENT_ID e EBAY_CLIENT_SECRET; senza, eBay viene saltato.
CERCA_SU_EBAY = True
EBAY_CONDIZIONI = "2000|2010|2020|2030|2500|3000"  # ricondizionati (vari gradi) + usato
# eBay: solo oggetti che si trovano in un paese UE (niente dogana, spedizioni ragionevoli)
EBAY_PAESI_OK = {"IT", "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR",
                 "HR", "HU", "IE", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}

# Esclusioni comuni a tutte le ricerche (regex sul titolo in minuscolo)
ESCLUDI_SEMPRE = [r"\bcerco\b", r"\bcompro\b"]

# Le ricerche. Per ognuna:
#   cerca     -> testo (o lista di testi) cercato su Vinted; cerca_subito su Subito ed eBay
#   min/max   -> prezzo in euro
#   richiedi  -> il titolo deve contenere TUTTE queste regex
#   almeno_una-> il titolo deve contenere ALMENO UNA di queste (lista vuota = nessun vincolo)
#   escludi   -> se il titolo contiene una di queste, l'annuncio è scartato
# Unità di misura dello schermo, anche in altre lingue (Vinted mostra annunci da Francia, Spagna, ecc.)
POLLICI = r"\s*(\"|”|″|''|pollici|pollic|inch|po\b|pouces?|zoll|pulgadas?|polegadas?)"
# Unità scritta PRIMA del numero ("POLLICI:13", "display 14", "pouces : 13")
UNITA_PRIMA = r"(pollici|inch(es)?|pouces?|zoll|pulgadas?|polegadas?|schermo|display)\s*[:=\-]?\s*"
# MacBook Pro di altre misure: "macbook pro 13/14/15" anche senza unità, e codici modello
MACBOOK_ALTRE_MISURE = [
    r"\b1[345]" + POLLICI,                                   # 13", 14 pollici, 13 pouces...
    UNITA_PRIMA + r"1[345]\b",                               # "pollici: 13", "schermo 14"
    r"mac\s*book\s*pro\s*(m\s*[1-5]\s*(pro|max)?\s*)?1[345]\b",  # "macbook pro 13", "macbook pro m1 14"
    r"\ba(1706|1708|1989|2159|2251|2289|2338|2442|2779|2918|2992)\b",  # codici modello 13"/14"
]
# ── Filtri anti-truffa (per ora usati solo dalla ricerca PS5 Pro) ──
# Controllati su titolo + descrizione, in italiano, inglese, francese, tedesco, spagnolo, portoghese.
# BASE: segnali quasi sempre di truffa/fuffa, usati su tutte le fonti (anche Subito).
ANTI_TRUFFA_BASE = [
    # vende solo la foto
    r"\bs[oó]l[oa]\s+(la\s+|le\s+|una\s+|un'|delle\s+)?fot[oa]\b", r"\b(foto|immagine|stampa)\s+stampat[ao]", r"\bprinted\s+(photo|picture|image)", r"\b(photo|image)\s+imprim[ée]e",
    r"\bgedruckte[sn]?\s+(foto|bild)", r"\b(foto|imagen)\s+impres[ao]", r"\b(foto|imagem)\s+impress[ao]", r"\b(only|just)\s+(a\s+|the\s+)?(photo|picture|image)s?\b",
    r"\b(photo|picture)\s+only\b", r"\bnur\s+(das\s+|ein\s+|die\s+)?(foto|bild)(s|er)?\b", r"\b(seulement|uniquement)\s+(la\s+|les\s+|une\s+|des\s+)?photos?\b",
    r"\bvends?\s+(que|seulement|uniquement)\s+(la\s+|les\s+|une\s+|des\s+)?photos?\b",
    r"\bs[oó]lo\s+(la\s+|una\s+)?imagen\b", r"\bapenas\s+(a\s+|uma\s+)?fotos?\b", r"\bnon\s+(verr[àa]|viene|sar[àa])\s+spedit[ao]",
    # dichiara che non vende la console ("NON È LA PS5 PRO"), ma non "non è la PS5 slim/normale"
    r"\bnon\s+[èe]\s+(la\s+|una\s+)?(ps\s*5|playstation(\s*5)?|console)(?!\s*(slim|normale|base|standard|fat|liscia|digital))",
    r"\b(is\s+)?not\s+(a|the)\s+(ps\s*5|playstation(\s*5)?|console)(?!\s*(slim|standard|base|fat|digital))",
    r"\bce\s+n'?\s*est\s+pas\s+(la|une)\s+(ps\s*5|playstation|console)", r"\b(nicht|keine)\s+(die\s+)?(ps\s*5|playstation|konsole)\b",
    r"\bno\s+es\s+(la|una)\s+(ps\s*5|playstation|consola)",
    # bonifico
    r"\bbonific", r"\bvirement", r"[üu]berweisung", r"\btransferencia", r"\b(bank|wire)\s*transfer", r"\biban\b",
    # chi cerca invece di vendere
    r"\bcerc(o|hiamo)\b", r"\bcompro\b", r"\b(re)?cherche\b", r"\bach[eè]te\b", r"\bbusco\b", r"\bsuche\b",
    r"\blooking\s+for\b", r"\bwtb\b", r"\bprocuro\b",
    # truffa
    r"\btruff[ae]\b", r"\bscams?\b", r"\barnaques?\b", r"\bbetrug\b", r"\bestafas?\b", r"\bfraudes?\b",
    # whatsapp
    r"whats\s*app", r"\bwa\.me\b",
    # scrivere / contattare prima (o in privato)
    r"\b(scriv|contatt)\w*\s+(\w+\s+){0,3}(prima|in\s+privato)\b", r"\bprima\s+di\s+(acquist|compr)",
    r"\b(contact|write|message|text|dm)\s+me\s+(first|before|privately)\b",
    r"\b(écri|ecri|contact)\w*[\s-]+moi\s+(avant|en\s+priv)", r"\b(schreib|kontaktier)\w*\s+mir\s+(vorher|zuerst|privat)",
    r"\b(escr[ií]b|contact|habl)\w*me\s+(antes|por\s+privado)",
]
# Solo DESCRIZIONE (e sua traduzione), tutte le fonti: "vendo + foto" a distanza di al massimo
# una parola ("vend photo de ps5", "vendo la foto", "je vends une photo", "selling a picture",
# "verkaufe das Bild"). "vendo la console, foto reali" non scatta: la virgola spezza la frase.
ANTI_TRUFFA_DESCRIZIONE = [
    r"\b(vend|verkauf|sell)\w*\s+(\w+\s+)?(photos?|fotos?|pictures?|images?|imagens?|imagen(es)?|bild(er)?|immagin[ei])\b",
]
# COMPLETO: in più, segnali di pagamento o contatto fuori piattaforma (Vinted ed eBay)
ANTI_TRUFFA_COMPLETO = ANTI_TRUFFA_BASE + [
    r"\bpay\s*pal\b", r"\beneba\b", r"\brevolut\b", r"\bpostepay\b", r"\bsatispay\b", r"\bricarica\b",
    r"\bamici\s+e\s+parenti\b", r"\bfriends\s*(and|&)\s*family\b", r"\btelegram\b",
    r"\bno\s+payment\s+(through|via|on)\b", r"\b(non|no)\s+(uso|utilizzo|accetto|passo\s+(da|per))\s+(il\s+)?(sistema|pagamento)",
    r"\bfuori\s+(da\s+)?(vinted|ebay|piattaforma)", r"\bsenza\s+(passare\s+(da|per)\s+)?(vinted|ebay)",
    r"\b(not|no)\s+(through|via)\s+(vinted|ebay|the\s+(system|platform))", r"\bhors\s+(de\s+)?vinted",
    r"\bohne\s+vinted", r"\bfuera\s+de\s+vinted",
    r"\b(vendita|ordine|acquisto)\s+((verr|sar)[àa]\s+)?annullat",
    # deposito / acconto pagato a parte
    r"\bdeposit[oi]?\b", r"\bacconto\b", r"\bcaparra\b", r"\banticipo\s+di\b", r"\bpagamento\s+anticipat",
    r"\bdown\s*payment\b", r"\bacompte\b", r"\banzahlung\b", r"\bdep[óo]sito\b", r"\bse[ñn]al\b",
]
# Concorsi / giveaway (solo Vinted): "PS5 Pro in palio, segui il profilo e metti mi piace"
CONCORSI = [
    r"\bconcors[oi]\b", r"\bin\s+palio\b", r"\bestrazion[ei]\b", r"\bsorteggi[oa]\b", r"\blotteri[ae]\b",
    r"\bvinc(i|ere|ita)\b", r"\bsegui(te)?\s+(il\s+|la\s+)?(mio\s+)?(profilo|pagina|account)",
    r"\bmett(i|ete)\s+(un\s+)?mi\s*piace", r"\blascia(te)?\s+(un\s+)?(mi\s*piace|like|cuore)",
    r"\bgive\s*aways?\b", r"\bcontests?\b", r"\braffles?\b", r"\bsweepstakes?\b", r"\b(to\s+)?win\b",
    r"\bfollow\s+(me|my|the|our)\b", r"\blike\s+(this|the)\s+(post|ad|item|listing)\b",
    r"\bconcours\b", r"\b[àa]\s+gagner\b", r"\btirage\s+au\s+sort\b", r"\babonne[zr]?[\s-]+(vous|toi)\b",
    r"\bgewinnspiel", r"\bverlosung", r"\bzu\s+gewinnen\b", r"\bfolg(t|e)\s+(mir|uns)\b",
    r"\bsorteo\b", r"\bconcurso\b", r"\bsorteio\b", r"\bpassatempo\b", r"\bs[ií]gue(me|nos)\b",
]

# Lingue "normali" per un annuncio sui siti europei (codici di Google Translate).
# Una descrizione lunga in un'altra lingua (es. afar, somalo) è un trucco tipico delle truffe:
# l'avviso "vendo solo la foto" scritto in modo che quasi nessuno lo capisca.
LINGUE_OK = {"it", "en", "fr", "de", "es", "pt", "nl", "pl", "cs", "sk", "lt", "lv", "et", "fi", "sv",
             "da", "no", "hu", "ro", "el", "hr", "sl", "bg", "ga", "lb", "mt", "ca", "gl", "eu", "uk", "ru"}
MIN_PAROLE_LINGUA = 5   # sotto questa lunghezza il rilevamento della lingua non è affidabile

RICERCHE = [
    {
        "nome": "MacBook Pro 16",
        "cerca": 'macbook pro 16"',
        "cerca_subito": "macbook pro 16",
        "min": 250, "max": 1750,
        # Tra 1600 e 1750 € segnala SOLO se titolo o descrizione citano
        # un chip Max oppure più di 32 GB di RAM
        "max_base": 1600,
        "sopra_max_base_se": [r"\bm\s*[1-5]\s*-?\s*max\b",
                              r"\b(36|48|64|96|128)\s*gb\b", r"\bram\s*:?\s*(36|48|64|96|128)\b"],
        "richiedi": [r"mac\s*bo+c?k"],          # accetta anche "mac book", "macboock"
        "almeno_una": [],
        "escludi": [
            r"\b201[5-9]\b", r"\b2020\b",            # anni pre-2021
            r"\bintel\b", r"\bi[579]\b",             # processori Intel
            r"\bair\b",                              # MacBook Air
            r"\bneo\b", r"\ba1[5-9]\b",               # MacBook Neo (chip A18 ecc.)
            r"touch\s*bar",                          # solo Intel
            *MACBOOK_ALTRE_MISURE,                   # 13", 14", 15" e codici modello
        ],
        # Regole sulla DESCRIZIONE: scarta se cita qualcosa in "escludi"
        # e allo stesso tempo NON cita niente in "salva".
        "regole_descrizione": [
            {   # misura sbagliata (13", 14", 15"), a meno che citi anche il 16"
                "escludi": MACBOOK_ALTRE_MISURE,
                "salva": [r"\b16" + POLLICI, UNITA_PRIMA + r"16\b"],
            },
            {   # modello Intel o MacBook Neo, a meno che citi anche un chip M (M1…M5, Pro/Max)
                "escludi": [r"\bintel\b", r"\bcore\s*i[3579]\b", r"\bi[3579]\b", r"touch\s*bar",
                            r"\bradeon\b", r"\b201[5-9]\b", r"\b2020\b",
                            r"macbook\s*neo", r"\ba1[5-9]\s*(pro|bionic)\b"],   # MacBook Neo
                "salva": [r"\bm\s*[1-5]\b", r"apple\s*silicon"],
            },
        ],
    },
    {
        "nome": "iPad Pro 11 M4/M5",
        "cerca": "ipad pro",
        "cerca_subito": "ipad pro",
        "min": 150, "max": 500,
        "richiedi": [r"i\s*pad", r"\bpro\b"],
        "almeno_una": [r"\bm\s*[45]\b", r"\b202[45]\b"],   # chip M4/M5 o anno 2024/2025
        "cerca_in_descrizione": True,   # se il titolo non lo dice, guarda nella descrizione...
        "in_descrizione": [r"\bm\s*[45]\b"],   # ...cercando solo "M4" o "M5" (l'anno lì è poco affidabile)
        "escludi": [
            r"\bair\b", r"\bmini\b",
            r"\b1[23](?:[.,]9)?" + POLLICI, r"12[.,]9",  # 12.9" e 13"
            UNITA_PRIMA + r"13\b",                       # "pollici: 13"
            # accessori venduti da soli: l'accessorio è nominato PRIMA di "iPad"
            # ("Custodia per iPad Pro M4", "Apple Magic Keyboard per iPad Pro" -> scartati;
            #  "iPad Pro M4 + custodia", "iPad Pro M4 con Magic Keyboard" -> passano)
            r"\b(custodia|cover|case|pellicola|vetro|protezione|magic\s*keyboard|keyboard|tastiera"
            r"|pencil|penna|stylus|smart\s*folio|folio|caricatore|cavo|supporto)\b.*\bi\s*pad",
        ],
        "regole_descrizione": [
            {   # 12.9"/13", a meno che citi anche l'11"
                "escludi": [r"\b1[23](?:[.,]9)?" + POLLICI, r"12[.,]9", UNITA_PRIMA + r"13\b"],
                "salva": [r"\b11" + POLLICI, UNITA_PRIMA + r"11\b"],
            },
        ],
    },
    {
        "nome": "PS5 Pro",
        # più testi = più ricerche con gli stessi filtri: "ps5 pro" non trova i titoli
        # scritti per esteso ("Playstation 5 Pro") e viceversa
        "cerca": ["ps5 pro", "playstation 5 pro"],
        "cerca_subito": ["ps5 pro", "playstation 5 pro"],
        "subito_url": SUBITO_URL_CONSOLE,
        # Su Vinted cerca solo nella categoria "Console" (niente controller,
        # accessori o annunci-truffa che vendono "la foto" della console)
        "vinted_categoria": 3025,
        "min": 100, "max": 500,
        # filtri anti-truffa su titolo + descrizione, diversi per fonte
        # traduce la descrizione in italiano e applica i filtri anche alla traduzione;
        # scarta le descrizioni scritte in lingue insolite (vedi LINGUE_OK)
        "traduci_descrizione": True,
        # descrizioni piene di hashtag o lunghissime: tipiche degli annunci-truffa (solo Vinted)
        "limiti_descrizione": {"Vinted": {"hashtag": 15, "caratteri": 2000}},
        "anti_truffa": {"Vinted": ANTI_TRUFFA_COMPLETO + CONCORSI,
                        "eBay": ANTI_TRUFFA_COMPLETO,
                        "Subito": ANTI_TRUFFA_BASE},
        "richiedi": [r"(\bps\s*5|play\s*station\s*5)", r"(\bpro\b|ps\s*5\s*pro\b)"],   # anche "PS5Pro"
        "almeno_una": [],
        "escludi": [
            r"\bps\s*4\b", r"play\s*station\s*4",               # PS4 Pro
            r"\bportal\b", r"\bvr\s*2?\b",                        # PlayStation Portal, PS VR2
            r"\b(solo\s+scatola|scatola\s+vuota|box\s+vuot[oa])\b",
            r"\b(per\s+(pezzi|ricambi)|non\s+funzionante|guast[oa]|rott[oa]|difettos[oa])\b",
            # accessori venduti da soli: l'accessorio è nominato PRIMA di "PS5"
            # ("Lettore disco per PS5 Pro" -> scartato; "PS5 Pro + lettore disco" -> passa).
            # Parole in italiano, inglese, francese, spagnolo, tedesco e portoghese, con i plurali.
            r"\b("
            # controller
            r"controller[s]?|joysticks?|gamepads?|pads?|dualsense|dual\s*sense|manett[ae]s?"
            r"|mand(o|os)|comand(o|os)|steuerung"
            # lettore disco
            r"|lettor[ei]|disc\s*drives?|lecteurs?|lector(es)?|laufwerke?|leitor(es)?"
            # cover e custodie
            r"|covers?|copertur[ae]|scocc(a|he)|pannell[oi]|faceplates?|plates?|custodi[ae]|cases?|housses?|[ée]tuis?|coques?|fundas?|h[üu]llen?|capas?"
            r"|skins?|stickers?|adesivi"
            # supporti
            r"|support[oi]?|supports|stands?|basi?|bases|soportes?|suportes?|halterung(en)?|st[äa]nder"
            # ventole
            r"|ventol[ae]|fans?|ventilateurs?|ventilador(es)?|l[üu]fter"
            # cuffie
            r"|cuffi[ae]|headsets?|headphones?|casques?|auriculares|cascos|kopfh[öo]rer|fones"
            # caricatori
            r"|caricator[ei]|chargers?|chargeurs?|cargador(es)?|carregador(es)?|ladestation(en)?|ladeger[äa]te?"
            # SSD e giochi
            r"|ssd|gioc(o|hi)|games?|jeux?|juegos?|spiele?|jogos?"
            r")\b.*(\bps\s*5|play\s*station\s*5)",
        ],
    },
]

INTERVALLO_MIN = 5 * 60    # per --loop
INTERVALLO_MAX = 6 * 60

BASE_DIR = Path(__file__).resolve().parent
SEEN_FILE = BASE_DIR / "vinted_seen.json"
PREZZI_FILE = BASE_DIR / "vinted_prezzi.json"   # ultimo prezzo visto per ogni annuncio
LOG_FILE = BASE_DIR / "vinted_monitor.log"

# ──────────────────────────────────────────────────────────────────



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
    marker = [x for x in seen if str(x).startswith("#")]
    ids = [x for x in seen if not str(x).startswith("#")]
    SEEN_FILE.write_text(json.dumps(marker + ids[-5000:]))  # tieni solo gli ultimi 5000 annunci


def load_prezzi() -> dict:
    try:
        return json.loads(PREZZI_FILE.read_text())
    except Exception:
        return {}


def save_prezzi(prezzi: dict, seen: list) -> None:
    tenuti = {str(x) for x in seen}   # stessi annunci di vinted_seen.json
    PREZZI_FILE.write_text(json.dumps({k: v for k, v in prezzi.items() if k in tenuti},
                                      separators=(",", ":")))


class Vinted:
    def __init__(self) -> None:
        self.new_session()

    def new_session(self) -> None:
        self.s = requests.Session(impersonate="chrome")
        self.s.headers.update({"Accept-Language": "it-IT,it;q=0.9"})

    def search(self, ric: dict) -> list:
        """Legge la pagina dei risultati di ricerca (gli annunci sono già nell'HTML)."""
        params = {
            "search_text": ric["cerca"],
            "order": "newest_first",
            "price_from": ric["min"],
            "price_to": ric["max"],
        }
        if ric.get("vinted_categoria"):
            params["catalog[]"] = ric["vinted_categoria"]
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


    def descrizione(self, url: str) -> str:
        """Scarica la pagina dell'annuncio e ne estrae la descrizione."""
        r = self.s.get(url, timeout=30)
        r.raise_for_status()
        m = re.search(r'"description":"((?:[^"\\]|\\.)*)"', r.text)
        if m:
            try:
                return json.loads('"' + m.group(1) + '"')
            except ValueError:
                return m.group(1)
        m = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', r.text)
        return html.unescape(m.group(1)) if m else ""


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


def cerca_subito(sessione, ric: dict) -> list:
    """Legge la pagina di ricerca di Subito (i dati sono nel JSON __NEXT_DATA__)."""
    params = {"q": ric["cerca_subito"], "ps": ric["min"], "pe": ric["max"], "order": "datedesc"}
    r = sessione.get(ric.get("subito_url", SUBITO_URL), params=params, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("pagina di Subito senza dati (struttura cambiata o richiesta bloccata)")
    lista = json.loads(m.group(1))["props"]["pageProps"]["initialState"]["items"]["originalList"]
    items = []
    for x in lista:
        it = x.get("item", x)
        url = (it.get("urls") or {}).get("default", "")
        iid = re.search(r"(\d+)\.htm", url) or re.search(r"(\d+)$", it.get("urn", ""))
        if not iid:
            continue
        prezzo_val = ((it.get("features") or {}).get("/price") or {}).get("values") or [{}]
        geo = it.get("geo") or {}
        citta = ((geo.get("town") or geo.get("city") or {}).get("value", ""))
        items.append({
            "id": "s" + iid.group(1),
            "title": it.get("subject", ""),
            "price": str(prezzo_val[0].get("key", "0")).replace(",", "."),
            "url": url,
            "dove": citta,
            "descrizione": it.get("body", ""),
        })
    return items


def traduci(testo: str) -> tuple:
    """Traduce in italiano con Google Translate (servizio gratuito non ufficiale).
    Ritorna (traduzione, lingua rilevata). Se non riesce ritorna ("", None)."""
    # toglie gli hashtag (riempitivo) e traduce fino a ~4500 caratteri, a blocchi da 1500
    testo = re.sub(r"\s+", " ", re.sub(r"#\w+", " ", testo or "")).strip()[:4500]
    if not testo:
        return "", None
    try:
        traduzioni, lingua = [], None
        for i in range(0, len(testo), 1500):
            r = requests.get("https://translate.googleapis.com/translate_a/single",
                             params={"client": "gtx", "sl": "auto", "tl": "it", "dt": "t",
                                     "q": testo[i:i + 1500]},
                             timeout=15)
            r.raise_for_status()
            dati = r.json()
            traduzioni.append("".join(parte[0] for parte in (dati[0] or []) if parte and parte[0]))
            if lingua is None and len(dati) > 2 and isinstance(dati[2], str):
                lingua = dati[2]
        return " ".join(traduzioni), lingua
    except Exception as e:
        log(f"Traduzione non riuscita: {e}")
        return "", None


def _testo(frammento_html: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", frammento_html)).strip()


class EbayAPI:
    """Accesso all'API ufficiale di eBay (Browse API) con le credenziali dell'app."""
    BASE = "https://api.ebay.com"

    def __init__(self, sessione) -> None:
        self.s = sessione
        self.token = None

    @staticmethod
    def configurata() -> bool:
        return bool(os.getenv("EBAY_CLIENT_ID") and os.getenv("EBAY_CLIENT_SECRET"))

    def _token(self) -> str:
        if self.token:
            return self.token
        import base64
        cred = base64.b64encode(
            f"{os.getenv('EBAY_CLIENT_ID')}:{os.getenv('EBAY_CLIENT_SECRET')}".encode()).decode()
        r = self.s.post(self.BASE + "/identity/v1/oauth2/token", timeout=30,
                        headers={"Authorization": f"Basic {cred}",
                                 "Content-Type": "application/x-www-form-urlencoded"},
                        data="grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope")
        r.raise_for_status()
        self.token = r.json()["access_token"]
        return self.token

    def _get(self, path: str, params=None) -> dict:
        r = self.s.get(self.BASE + path, params=params, timeout=30,
                       headers={"Authorization": f"Bearer {self._token()}",
                                "X-EBAY-C-MARKETPLACE-ID": "EBAY_IT",
                                "Accept-Language": "it-IT"})
        r.raise_for_status()
        return r.json()

    def cerca(self, ric: dict) -> list:
        filtro = (f"price:[{ric['min']}..{ric['max']}],priceCurrency:EUR,"
                  f"buyingOptions:{{FIXED_PRICE}},conditionIds:{{{EBAY_CONDIZIONI}}},"
                  f"deliveryCountry:IT")
        dati = self._get("/buy/browse/v1/item_summary/search",
                         {"q": ric["cerca_subito"], "sort": "newlyListed", "limit": 100, "filter": filtro})
        items = []
        for it in dati.get("itemSummaries", []):
            if "FIXED_PRICE" not in (it.get("buyingOptions") or ["FIXED_PRICE"]):
                continue
            num = it.get("legacyItemId") or it["itemId"].split("|")[1]
            luogo = it.get("itemLocation") or {}
            if (luogo.get("country") or "").upper() not in EBAY_PAESI_OK:
                continue   # fuori dall'UE (es. USA, UK, Cina): spedizione e dogana costose
            items.append({
                "id": "e" + str(num),
                "api_id": it["itemId"],
                "title": it.get("title", ""),
                "price": str((it.get("price") or {}).get("value", "0")),
                "url": f"https://www.ebay.it/itm/{num}",
                "dove": luogo.get("city") or luogo.get("country", ""),
            })
        return items

    def descrizione(self, item: dict) -> str:
        dati = self._get("/buy/browse/v1/item/" + requests_quote(item["api_id"]))
        testo = (dati.get("shortDescription") or "") + " " + (dati.get("description") or "")
        testo = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", testo, flags=re.S | re.I)
        return re.sub(r"\s+", " ", _testo(testo))


def requests_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def prezzo(item: dict) -> float:
    try:
        return float(item.get("price"))
    except (TypeError, ValueError):
        return 0.0


def motivo_esclusione(item: dict, ric: dict):
    """Ritorna il motivo per cui l'annuncio è scartato, oppure None se passa."""
    titolo = (item.get("title") or "").lower()
    for rx in ric["richiedi"]:
        if not re.search(rx, titolo, re.I):
            return f"manca '{rx}'"
    for rx in ric["escludi"] + ESCLUDI_SEMPRE:
        m = re.search(rx, titolo, re.I)
        if m:
            return f"contiene '{m.group(0)}'"
    p = prezzo(item)
    if not (ric["min"] <= p <= ric["max"]):
        return f"prezzo {p:.0f} € fuori range"
    if ric["almeno_una"] and not any(re.search(rx, titolo, re.I) for rx in ric["almeno_una"]):
        return MODELLO_NON_INDICATO
    if p > ric.get("max_base", ric["max"]) and not sopra_soglia_ok(titolo, ric):
        return SOPRA_SOGLIA
    return None


MODELLO_NON_INDICATO = "modello non indicato nel titolo"
SOPRA_SOGLIA = "prezzo sopra soglia senza chip Max / RAM > 32 GB nel titolo"


def sopra_soglia_ok(testo: str, ric: dict) -> bool:
    return any(re.search(rx, testo or "", re.I) for rx in ric.get("sopra_max_base_se", []))


def modello_in_descrizione(testo: str, ric: dict) -> bool:
    return any(re.search(rx, testo or "", re.I) for rx in ric.get("in_descrizione", ric["almeno_una"]))


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


def controlli_descrizione(it: dict, ric: dict, fonte: str, motivo, leggi_descrizione):
    """Controlli sulla descrizione (solo quando servono, per non appesantire)."""
    if not ((motivo == MODELLO_NON_INDICATO and ric.get("cerca_in_descrizione"))
            or motivo == SOPRA_SOGLIA
            or (motivo is None and (ric.get("regole_descrizione") or ric.get("anti_truffa", {}).get(fonte)))):
        return motivo
    try:
        if "descrizione" not in it:
            it["descrizione"] = leggi_descrizione(it["url"])
            time.sleep(random.uniform(1, 3))
        desc = it["descrizione"] or ""
        # modello non nel titolo: lo cerca nella descrizione
        if motivo == MODELLO_NON_INDICATO and modello_in_descrizione(desc, ric):
            motivo = None
        # prezzo sopra soglia: ok solo se la descrizione cita chip Max / RAM > 32 GB
        if motivo == SOPRA_SOGLIA and sopra_soglia_ok(desc, ric):
            motivo = None
        # regole sulla descrizione (misura o modello sbagliati)
        for regola in ric.get("regole_descrizione", []) if motivo is None else []:
            m = next((re.search(rx, desc, re.I) for rx in regola["escludi"]
                      if re.search(rx, desc, re.I)), None)
            salva = any(re.search(rx, desc, re.I) for rx in regola.get("salva", []))
            if m and not salva:
                motivo = f"descrizione cita '{m.group(0)}'"
                log(f"Scartato {ric['nome']} ({fonte}): {it.get('title')} – {motivo} – {it['url']}")
                break
        # filtri anti-truffa (titolo + descrizione)
        if motivo is None and ric.get("anti_truffa", {}).get(fonte):
            testo = f"{it.get('title', '')}\n{desc}"
            testo_desc = desc   # solo descrizione (+ traduzione), per ANTI_TRUFFA_DESCRIZIONE
            limiti = ric.get("limiti_descrizione", {}).get(fonte)
            if limiti:
                n_hashtag = len(re.findall(r"#\w+", desc))
                if n_hashtag > limiti.get("hashtag", 10**9):
                    motivo = f"possibile truffa: {n_hashtag} hashtag nella descrizione"
                elif len(desc) > limiti.get("caratteri", 10**9):
                    motivo = f"possibile truffa: descrizione molto lunga ({len(desc)} caratteri)"
                if motivo:
                    log(f"Scartato {ric['nome']} ({fonte}): {it.get('title')} – {motivo} – {it['url']}")
            if motivo is None and ric.get("traduci_descrizione") and desc.strip():
                traduzione, lingua = traduci(desc)
                testo += "\n" + traduzione
                testo_desc += "\n" + traduzione
                if lingua and lingua.lower() not in LINGUE_OK and len(desc.split()) >= MIN_PAROLE_LINGUA:
                    motivo = f"possibile truffa: descrizione in lingua insolita ({lingua})"
                    log(f"Scartato {ric['nome']} ({fonte}): {it.get('title')} – {motivo} – {it['url']}")
            for rx in ric.get("anti_truffa", {}).get(fonte, []) if motivo is None else []:
                m = re.search(rx, testo, re.I)
                if m:
                    motivo = f"possibile truffa: '{m.group(0).strip()}'"
                    log(f"Scartato {ric['nome']} ({fonte}): {it.get('title')} – {motivo} – {it['url']}")
                    break
            for rx in ANTI_TRUFFA_DESCRIZIONE if motivo is None else []:
                m = re.search(rx, testo_desc, re.I)
                if m:
                    motivo = f"possibile truffa: '{m.group(0).strip()}'"
                    log(f"Scartato {ric['nome']} ({fonte}): {it.get('title')} – {motivo} – {it['url']}")
                    break
    except Exception as e:
        log(f"Impossibile leggere la descrizione di {it['url']}: {e}")
    return motivo


def esamina(ric: dict, fonte: str, items: list, seen: list, visti: set, test: bool,
            leggi_descrizione=lambda url: "", prezzi: dict | None = None) -> int:
    """Notifica gli annunci nuovi (o ribassati) che passano i filtri. Ritorna quanti ne ha segnalati."""
    if prezzi is None:
        prezzi = {}
    # La prima volta che una ricerca gira su una fonte, gli annunci già online
    # vengono solo memorizzati, così non arrivano decine di notifiche insieme.
    marker = f"#init:{ric['nome']}:{fonte}"
    silenzioso = marker not in visti
    if silenzioso and ric is RICERCHE[0] and fonte in ("Vinted", "Subito"):
        # compatibilità: la ricerca MacBook era già attiva prima di questa versione
        prefisso_subito = fonte == "Subito"
        if any(isinstance(x, int) != prefisso_subito and not str(x).startswith("#") for x in seen):
            silenzioso = False
    if silenzioso and not test:
        log(f"Primo avvio di '{ric['nome']}' su {fonte}: memorizzo gli annunci attuali senza notificarli.")
    if not test and marker not in visti:
        seen.append(marker)
        visti.add(marker)

    nuovi = 0
    for it in items:
        iid, url, p = it["id"], it["url"], prezzo(it)
        motivo = motivo_esclusione(it, ric)

        if test:
            stato = "OK   " if motivo is None else f"NO   ({motivo})"
            print(f"[{ric['nome'][:12]:12}|{fonte:6}] {p:>6.0f} €  {stato:35}  {it.get('title')}  {url}")
            continue

        dove = f", {it['dove']}" if it.get("dove") else ""
        chiave = str(iid)
        prima = prezzi.get(chiave)
        if p > 0:
            prezzi[chiave] = p

        if iid in visti:
            # Annuncio già visto: notifica solo se il prezzo è sceso e ora passa i filtri.
            # (Senza prezzo salvato, cioè visto prima di questa versione, lo memorizza e basta.)
            if prima is None or p <= 0 or p >= prima - 0.5:
                continue
            motivo = controlli_descrizione(it, ric, fonte, motivo, leggi_descrizione)
            log(f"Ribasso {ric['nome']} ({fonte}): {prima:.0f} → {p:.0f} € – {it.get('title')} – "
                f"{'notificato' if motivo is None else motivo} – {url}")
            if motivo is None:
                nuovi += 1
                notifica(f"📉 {ric['nome']}: da {prima:.0f} a {p:.0f} € ({fonte}{dove})",
                         it.get("title", ""), url)
            continue

        seen.append(iid)
        visti.add(iid)
        if not silenzioso:
            motivo = controlli_descrizione(it, ric, fonte, motivo, leggi_descrizione)

        if motivo is None and not silenzioso:
            nuovi += 1
            log(f"NUOVO {ric['nome']} ({fonte}): {p:.0f} € – {it.get('title')} – {url}")
            notifica(f"{ric['nome']} a {p:.0f} € ({fonte}{dove})", it.get("title", ""), url)
    return nuovi


def cerca_tutti(ric: dict, chiave: str, funzione, fonte: str = "", seen=None, visti=None) -> list:
    """Esegue la ricerca per ogni testo in ric[chiave] (stringa o lista) e unisce i
    risultati senza doppioni. Filtri, prezzi e categoria restano quelli della ricerca.
    Un testo aggiunto dopo il primo avvio della ricerca viene "inizializzato" come al
    solito: la prima volta i suoi annunci già online vengono memorizzati senza notifiche."""
    testi = ric[chiave] if isinstance(ric[chiave], list) else [ric[chiave]]
    items, ids = [], set()
    for i, testo in enumerate(testi):
        if i:
            time.sleep(random.uniform(1, 3))
        trovati = funzione({**ric, chiave: testo})
        marker = f"#init:{ric['nome']}:{fonte}:{testo}"
        if i and seen is not None and marker not in visti:
            # prima volta per questo testo: segna come già visti gli annunci attuali
            # (solo se la ricerca era già attiva; altrimenti ci pensa esamina())
            if f"#init:{ric['nome']}:{fonte}" in visti:
                log(f"Primo avvio del testo '{testo}' per '{ric['nome']}' su {fonte}: "
                    f"memorizzo {len(trovati)} annunci senza notificarli.")
                for it in trovati:
                    if it["id"] not in visti and it["id"] not in ids:
                        seen.append(it["id"])
                        visti.add(it["id"])
            seen.append(marker)
            visti.add(marker)
        for it in trovati:
            if it["id"] not in ids:
                ids.add(it["id"])
                items.append(it)
    return items


def controllo(v: Vinted, seen: list, test: bool, prezzi: dict) -> None:
    visti = set(seen)
    riepilogo = []
    errori_vinted = 0
    ebay = EbayAPI(v.s)
    stato = (None, None) if test else (seen, visti)   # in modalità test non memorizza nulla

    for ric in RICERCHE:
        # Vinted
        try:
            items = cerca_tutti(ric, "cerca", v.search, "Vinted", *stato)
            if not items:
                raise RuntimeError("nessun annuncio ricevuto (possibile blocco)")
            n = esamina(ric, "Vinted", items, seen, visti, test, v.descrizione, prezzi)
            riepilogo.append(f"{ric['nome']} – Vinted: {len(items)} letti, {n} segnalati")
        except Exception as e:
            errori_vinted += 1
            log(f"Errore Vinted ({ric['nome']}): {e}")
            riepilogo.append(f"{ric['nome']} – Vinted: errore ({e})")
            v.new_session()

        # Subito: se fallisce lo segnala, ma non fa fallire l'esecuzione
        if CERCA_SU_SUBITO:
            try:
                items_s = cerca_tutti(ric, "cerca_subito", lambda r: cerca_subito(v.s, r), "Subito", *stato)
                n = esamina(ric, "Subito", items_s, seen, visti, test, prezzi=prezzi)
                riepilogo.append(f"{ric['nome']} – Subito: {len(items_s)} letti, {n} segnalati")
            except Exception as e:
                log(f"Errore Subito ({ric['nome']}): {e}")
                riepilogo.append(f"{ric['nome']} – Subito: errore ({e})")
        # eBay (API ufficiale): se fallisce lo segnala, ma non fa fallire l'esecuzione
        if CERCA_SU_EBAY and EbayAPI.configurata():
            try:
                items_e = cerca_tutti(ric, "cerca_subito", ebay.cerca, "eBay", *stato)
                per_url = {it["url"]: it for it in items_e}
                n = esamina(ric, "eBay", items_e, seen, visti, test,
                            lambda url: ebay.descrizione(per_url[url]), prezzi)
                riepilogo.append(f"{ric['nome']} – eBay: {len(items_e)} letti, {n} segnalati")
            except Exception as e:
                log(f"Errore eBay ({ric['nome']}): {e}")
                riepilogo.append(f"{ric['nome']} – eBay: errore ({e})")
        time.sleep(random.uniform(2, 5))  # piccola pausa tra una ricerca e l'altra

    if not test:
        save_seen(seen)
        save_prezzi(prezzi, seen)
        testo = "\n".join(riepilogo)
        log("Controllo completato:\n" + testo)
        # Se lanciato a mano da GitHub (Run workflow), manda una conferma su Telegram.
        # Gli avvii automatici da cron-job.org passano riepilogo=false e restano silenziosi.
        if (os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"
                and os.getenv("RIEPILOGO", "true").lower() != "false"):
            trad, lingua = traduci("Bonjour, je vends ma console")
            testo += "\nTraduzione automatica: " + (f"ok ({lingua} → it)" if trad else "non disponibile")
            notifica("✅ Monitor attivo", f"Controllo manuale completato.\n{testo}",
                     "https://github.com/" + os.getenv("GITHUB_REPOSITORY", "") + "/actions")

    if errori_vinted == len(RICERCHE):
        raise RuntimeError("Vinted non risponde per nessuna ricerca.")


def main() -> None:
    test = "--test" in sys.argv
    loop = "--loop" in sys.argv and not test
    seen = load_seen()
    prezzi = load_prezzi()
    if not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")) and sys.platform != "darwin":
        log("Attenzione: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID non impostati, nessuna notifica verrà inviata.")

    errore = False
    v = Vinted()

    while True:
        try:
            controllo(v, seen, test, prezzi)
            errore = False
        except Exception as e:
            errore = True
            log(f"Errore: {e}")
        if not loop:
            break
        attesa = random.uniform(INTERVALLO_MIN, INTERVALLO_MAX)
        log(f"Prossimo controllo tra {attesa / 60:.0f} minuti.")
        time.sleep(attesa)

    if errore:
        sys.exit(1)  # su GitHub l'esecuzione risulta fallita, così te ne accorgi


if __name__ == "__main__":
    main()
