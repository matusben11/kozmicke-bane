"""
KOZMICKÉ BANE v5.3 — Web Server
Spustenie: python web_server.py
           alebo cez game_login_system.py → [2] Web
"""

import hashlib
import json
import math
import os
import pathlib
import random
import smtplib
import time
import urllib.request
import urllib.error
from collections import Counter
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, request, redirect, session, make_response

BASE_DIR  = pathlib.Path(__file__).parent.resolve()
HTML_FILE = BASE_DIR / "kozmicke_bane.html"

# Dáta: na Render v /opt/render/project/data (oddelené od kódu), lokálne v BASE_DIR
_RENDER_DATA = pathlib.Path("/opt/render/project/data")
DATA_DIR  = _RENDER_DATA if os.environ.get("RENDER") else BASE_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATA_FILE = DATA_DIR / "game_users.json"
KB_CAREER = DATA_DIR / "kb_career.json"
KB_SAVES  = DATA_DIR / "kb_saves.json"
KB_LB     = DATA_DIR / "kb_leaderboard.json"
KB_ENERGY     = DATA_DIR / "kb_energy.json"
KB_MARKET     = DATA_DIR / "kb_market.json"
KB_AUCTIONS   = DATA_DIR / "kb_auctions.json"
KB_COUNTRIES    = DATA_DIR / "kb_countries.json"
KB_COUNCIL      = DATA_DIR / "kb_council.json"
KB_INVESTMENTS  = DATA_DIR / "kb_investments.json"

# ── Filesystem helper (musí byť pred KV sekciou) ───────────────────────────
def _atomic_write(path, text):
    """Write text to a temp file then rename. Returns True on success."""
    try:
        path = pathlib.Path(path)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        tmp.replace(path)
        return True
    except Exception as e:
        print(f"[FS] zápis zlyhal pre '{path.name}': {e}")
        return False


# ── Upstash Redis — voliteľné perzistentné KV úložisko ─────────────────────
# Nastav UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN v Render env vars.
# Ak nie sú nastavené, používajú sa lokálne súbory (pre vývoj).
_KV_URL = next(
    (os.environ.get(k, "").strip().rstrip("/")
     for k in ["UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_URL",
                "KV_REST_API_URL", "UPSTASH_URL"]
     if os.environ.get(k, "").strip()),
    ""
)
_KV_TOKEN = next(
    (os.environ.get(k, "").strip()
     for k in ["UPSTASH_REDIS_REST_TOKEN", "UPSTASH_REDIS_TOKEN",
                "KV_REST_API_TOKEN", "UPSTASH_TOKEN"]
     if os.environ.get(k, "").strip()),
    ""
)
print(f"[KV] URL set: {bool(_KV_URL)} | TOKEN set: {bool(_KV_TOKEN)} | URL prefix: {_KV_URL[:40] if _KV_URL else 'NONE'}")

# Mapovanie cesty súboru → Redis kľúč
_KV_KEYS = {
    DATA_FILE:   "game_users",
    KB_CAREER:   "kb_career",
    KB_SAVES:    "kb_saves",
    KB_LB:       "kb_leaderboard",
    KB_ENERGY:   "kb_energy",
    KB_MARKET:    "kb_market",
    KB_AUCTIONS:  "kb_auctions",
    KB_COUNTRIES:   "kb_countries",
    KB_COUNCIL:     "kb_council",
    KB_INVESTMENTS: "kb_investments",
}


def _kv_request(path, body_obj=None):
    """
    Nízkoúrovňový Upstash REST request.
    path  = napr. '/get/mykey' alebo '/pipeline'
    body  = Python objekt → JSON; None = GET request
    Vráti parsed JSON alebo None pri chybe.
    """
    if not _KV_URL:
        return None
    try:
        url = f"{_KV_URL}{path}"
        data = json.dumps(body_obj).encode("utf-8") if body_obj is not None else None
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Authorization": f"Bearer {_KV_TOKEN}",
                "Content-Type": "application/json",
            }
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")
        print(f"[KV] HTTP {e.code} pre '{path}': {body_txt[:200]}")
    except Exception as e:
        print(f"[KV] chyba pri '{path}': {e}")
    return None


def _kv_get(key):
    """Načítaj JSON hodnotu z Upstash. Vráti Python objekt alebo None."""
    resp = _kv_request(f"/get/{key}")
    if resp is None:
        return None
    result = resp.get("result")
    if result is None:
        return None          # kľúč neexistuje
    try:
        return json.loads(result)
    except Exception:
        return result        # hodnota nebola JSON-enkódovaná (napr. starý záznam)


def _kv_set(key, value):
    """Ulož Python objekt do Upstash ako JSON string. Vráti True ak OK."""
    val_str = json.dumps(value, ensure_ascii=False)
    # Pipeline endpoint: POST /pipeline s [["SET", key, val]]
    resp = _kv_request("/pipeline", [["SET", key, val_str]])
    if resp is None:
        return False
    # Pipeline vracia list: [{"result": "OK"}]
    if isinstance(resp, list) and resp and resp[0].get("result") == "OK":
        return True
    print(f"[KV] set '{key}' neočakávaná odpoveď: {resp}")
    return False


def _kv_ping():
    """Otestuj spojenie s Upstash. Vráti True/False."""
    resp = _kv_request("/ping")
    return isinstance(resp, dict) and resp.get("result") == "PONG"


def _kv_load_all():
    """
    Startup warm-up: načítaj všetky kľúče z Upstash do lokálnych súborov.
    Lokálne súbory slúžia ako cache — čítanie beží vždy z lokálneho disku.
    """
    if not _KV_URL:
        print("[KV] Upstash nie je nakonfigurovaný — používajú sa lokálne súbory.")
        return
    if not _kv_ping():
        print("[KV] VAROVANIE: Upstash nedostupný pri štarte — používajú sa lokálne súbory.")
        return
    print("[KV] Upstash dostupný. Načítavam dáta...")
    loaded = 0
    for path, key in _KV_KEYS.items():
        data = _kv_get(key)
        if data is not None:
            try:
                _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
                print(f"[KV] ✓ {key}")
                loaded += 1
            except Exception as e:
                print(f"[KV] ✗ {key} zápis zlyhal: {e}")
        else:
            print(f"[KV] - {key} (prázdne v Upstash)")
    print(f"[KV] Warm-up hotový: {loaded}/{len(_KV_KEYS)} kľúčov načítaných.")

# Startup: načítaj dáta z Upstash do lokálnych súborov (pred migráciami a seedmi)
_kv_load_all()

# Migrácia zo starého disk path (pred zmenou mountPath)
_OLD_SRC = pathlib.Path("/opt/render/project/src")
for _fname in ["game_users.json", "kb_career.json", "kb_saves.json", "kb_leaderboard.json"]:
    _old = _OLD_SRC / _fname
    _new = DATA_DIR / _fname
    if _old.exists() and not _new.exists():
        import shutil
        shutil.copy2(_old, _new)
        print(f"[migrate] {_fname}: {_old} -> {_new}")

print(f"[startup] DATA_DIR={DATA_DIR} | RENDER={bool(os.environ.get('RENDER'))} | users exists={DATA_FILE.exists()}")

PORT       = int(os.environ.get("PORT", 5000))
OWNER_CODE = os.environ.get("OWNER_CODE", os.environ.get("ADMIN_CODE", ""))  # nastav OWNER_CODE v env premenných na Render

# Zoznam beta funkcií.
# public=False  → viditeľné len testerom
# public=True   → dostupné všetkým (release bez zmeny iného kódu)
BETA_FEATURES = [
    {
        "id": "omega7",
        "public": False,
        "name_sk": "Planéta Omega-VII",
        "desc_sk": "Prístup k planéte Omega-VII od štartu novej hry",
        "name_en": "Planet Omega-VII",
        "desc_en": "Access to planet Omega-VII from the start of a new game",
    },
    {
        "id": "countries",
        "public": False,
        "name_sk": "Medzigalaktická rada — krajiny a roly",
        "desc_sk": "Systém krajín, rolí a medzigalaktickej rady bezpečnosti",
        "name_en": "Intergalactic Council — countries and roles",
        "desc_en": "Country system, roles and intergalactic security council",
    },
    {
        "id": "energy_minigame",
        "public": True,
        "name_sk": "Energetická minihra",
        "desc_sk": "Stav elektrárne, vyrábaj energiu — základ pre budúci trh",
        "name_en": "Energy minigame",
        "desc_en": "Build power plants, produce energy — foundation for the future market",
    },
]

# ── Energetická minihra — konštanty ─────────────────────────────────────────

# ── Medzigalaktická rada — krajiny, roly, zbrane ────────────────────────────

COUNTRY_ROLES = [
    {"id": "president",     "name_sk": "Prezident",              "name_en": "President",              "power": 10, "icon": "👑"},
    {"id": "pm",            "name_sk": "Predseda vlády",         "name_en": "Prime Minister",         "power": 9,  "icon": "🏛"},
    {"id": "def_minister",  "name_sk": "Minister obrany",        "name_en": "Minister of Defense",    "power": 8,  "icon": "⚔"},
    {"id": "fin_minister",  "name_sk": "Minister financií",      "name_en": "Minister of Finance",    "power": 7,  "icon": "💰"},
    {"id": "foreign_min",   "name_sk": "Minister zahraničia",    "name_en": "Foreign Minister",       "power": 7,  "icon": "🌐"},
    {"id": "general",       "name_sk": "Generál",                "name_en": "General",                "power": 6,  "icon": "🎖"},
    {"id": "spy_chief",     "name_sk": "Šéf rozviedky",          "name_en": "Intelligence Chief",     "power": 6,  "icon": "🕵"},
    {"id": "senator",       "name_sk": "Senátor",                "name_en": "Senator",                "power": 3,  "icon": "📜"},
    {"id": "ambassador",    "name_sk": "Veľvyslanec",            "name_en": "Ambassador",             "power": 4,  "icon": "🤝"},
    {"id": "council_rep",   "name_sk": "Zástupca v Rade",        "name_en": "Council Representative", "power": 5,  "icon": "🏢"},
    {"id": "council_seat",  "name_sk": "Člen Rady — Ženeva",     "name_en": "Council Member — Geneva","power": 8,  "icon": "🕊",
     "neutral_only": True},  # iba pre Švajčiarsko
]
ROLE_BY_ID = {r["id"]: r for r in COUNTRY_ROLES}

# Švajčiarsko = neutrálne sídlo Rady — nikdy nemôže byť vo vojne
COUNCIL_HQ_COUNTRY = "switzerland"

COUNTRIES = [
    # Severná Amerika
    {"id": "usa",    "name": "United States",    "flag": "🇺🇸", "region": "North America"},
    {"id": "canada", "name": "Canada",           "flag": "🇨🇦", "region": "North America"},
    {"id": "mexico", "name": "Mexico",           "flag": "🇲🇽", "region": "North America"},
    # Európa
    {"id": "uk",     "name": "United Kingdom",   "flag": "🇬🇧", "region": "Europe"},
    {"id": "france", "name": "France",           "flag": "🇫🇷", "region": "Europe"},
    {"id": "germany","name": "Germany",          "flag": "🇩🇪", "region": "Europe"},
    {"id": "russia", "name": "Russia",           "flag": "🇷🇺", "region": "Europe"},
    {"id": "italy",  "name": "Italy",            "flag": "🇮🇹", "region": "Europe"},
    {"id": "spain",  "name": "Spain",            "flag": "🇪🇸", "region": "Europe"},
    {"id": "poland", "name": "Poland",           "flag": "🇵🇱", "region": "Europe"},
    {"id": "ukraine","name": "Ukraine",          "flag": "🇺🇦", "region": "Europe"},
    {"id": "czechia","name": "Czech Republic",   "flag": "🇨🇿", "region": "Europe"},
    {"id": "slovakia","name":"Slovakia",         "flag": "🇸🇰", "region": "Europe"},
    {"id": "hungary","name": "Hungary",          "flag": "🇭🇺", "region": "Europe"},
    {"id": "sweden", "name": "Sweden",           "flag": "🇸🇪", "region": "Europe"},
    {"id": "norway", "name": "Norway",           "flag": "🇳🇴", "region": "Europe"},
    {"id": "finland","name": "Finland",          "flag": "🇫🇮", "region": "Europe"},
    {"id": "switzerland","name":"Switzerland",   "flag": "🇨🇭", "region": "Europe"},
    {"id": "austria","name": "Austria",          "flag": "🇦🇹", "region": "Europe"},
    {"id": "netherlands","name":"Netherlands",   "flag": "🇳🇱", "region": "Europe"},
    {"id": "belgium","name": "Belgium",          "flag": "🇧🇪", "region": "Europe"},
    {"id": "portugal","name":"Portugal",         "flag": "🇵🇹", "region": "Europe"},
    {"id": "greece", "name": "Greece",           "flag": "🇬🇷", "region": "Europe"},
    {"id": "romania","name": "Romania",          "flag": "🇷🇴", "region": "Europe"},
    {"id": "serbia", "name": "Serbia",           "flag": "🇷🇸", "region": "Europe"},
    {"id": "croatia","name": "Croatia",          "flag": "🇭🇷", "region": "Europe"},
    # Ázia
    {"id": "china",  "name": "China",            "flag": "🇨🇳", "region": "Asia"},
    {"id": "japan",  "name": "Japan",            "flag": "🇯🇵", "region": "Asia"},
    {"id": "india",  "name": "India",            "flag": "🇮🇳", "region": "Asia"},
    {"id": "skorea", "name": "South Korea",      "flag": "🇰🇷", "region": "Asia"},
    {"id": "nkorea", "name": "North Korea",      "flag": "🇰🇵", "region": "Asia"},
    {"id": "iran",   "name": "Iran",             "flag": "🇮🇷", "region": "Asia"},
    {"id": "turkey", "name": "Turkey",           "flag": "🇹🇷", "region": "Asia"},
    {"id": "israel", "name": "Israel",           "flag": "🇮🇱", "region": "Asia"},
    {"id": "saudi",  "name": "Saudi Arabia",     "flag": "🇸🇦", "region": "Asia"},
    {"id": "pakistan","name":"Pakistan",         "flag": "🇵🇰", "region": "Asia"},
    {"id": "vietnam","name": "Vietnam",          "flag": "🇻🇳", "region": "Asia"},
    {"id": "thailand","name":"Thailand",         "flag": "🇹🇭", "region": "Asia"},
    {"id": "indonesia","name":"Indonesia",       "flag": "🇮🇩", "region": "Asia"},
    # Afrika
    {"id": "nigeria","name": "Nigeria",          "flag": "🇳🇬", "region": "Africa"},
    {"id": "egypt",  "name": "Egypt",            "flag": "🇪🇬", "region": "Africa"},
    {"id": "safrica","name": "South Africa",     "flag": "🇿🇦", "region": "Africa"},
    {"id": "ethiopia","name":"Ethiopia",         "flag": "🇪🇹", "region": "Africa"},
    {"id": "kenya",  "name": "Kenya",            "flag": "🇰🇪", "region": "Africa"},
    # Južná Amerika
    {"id": "brazil", "name": "Brazil",           "flag": "🇧🇷", "region": "South America"},
    {"id": "argentina","name":"Argentina",       "flag": "🇦🇷", "region": "South America"},
    {"id": "colombia","name":"Colombia",         "flag": "🇨🇴", "region": "South America"},
    {"id": "chile",  "name": "Chile",            "flag": "🇨🇱", "region": "South America"},
    # Oceánia
    {"id": "australia","name":"Australia",       "flag": "🇦🇺", "region": "Oceania"},
    {"id": "nzealand","name":"New Zealand",      "flag": "🇳🇿", "region": "Oceania"},
]
COUNTRY_BY_ID = {c["id"]: c for c in COUNTRIES}

# Rola v Medzigalaktickej rade bezpečnosti (stály člen = veto právo)
COUNCIL_PERMANENT = {"usa", "russia", "china", "uk", "france"}

# Zbrane — typy
WEAPON_TYPES = {
    "conventional": {"name_sk": "Konvenčné sily",   "name_en": "Conventional forces", "icon": "🪖"},
    "nuclear":      {"name_sk": "Jadrové hlavice",   "name_en": "Nuclear warheads",     "icon": "☢"},
    "missiles":     {"name_sk": "Balistické rakety", "name_en": "Ballistic missiles",   "icon": "🚀"},
    "cyber":        {"name_sk": "Kybernetické zbrane","name_en": "Cyber weapons",        "icon": "💻"},
}

# Rezolúcie Rady bezpečnosti
RES_TYPES = {
    "nuclear_approve": {
        "name_sk": "Schváliť jadrový program",
        "name_en": "Approve nuclear program",
        "icon": "☢", "needs_veto_check": True,
        "desc_sk": "Krajina smie legálne vlastniť jadrové zbrane.",
        "desc_en": "Country may legally possess nuclear weapons.",
    },
    "nuclear_ban": {
        "name_sk": "Zakázať jadrový program",
        "name_en": "Ban nuclear program",
        "icon": "🚫☢", "needs_veto_check": True,
        "desc_sk": "Krajina musí odovzdať jadrové zbrane. Odmietnutie = sankcie.",
        "desc_en": "Country must surrender nuclear weapons. Refusal = sanctions.",
    },
    "sanctions": {
        "name_sk": "Ekonomické sankcie",
        "name_en": "Economic sanctions",
        "icon": "📉", "needs_veto_check": True,
        "desc_sk": "Obmedzenie obchodu a finančného styku.",
        "desc_en": "Restriction of trade and financial contacts.",
    },
    "embargo": {
        "name_sk": "Zbrojné embargo",
        "name_en": "Arms embargo",
        "icon": "🚫🪖", "needs_veto_check": True,
        "desc_sk": "Zákaz dovozu a vývozu zbraní.",
        "desc_en": "Ban on arms import and export.",
    },
    "war_auth": {
        "name_sk": "Autorizovať vojenskú akciu",
        "name_en": "Authorize military action",
        "icon": "⚔", "needs_veto_check": True,
        "desc_sk": "Rada povolí vojenskú intervenciu inej krajine.",
        "desc_en": "Council authorizes military intervention.",
    },
    "ceasefire": {
        "name_sk": "Požadovať prímerie",
        "name_en": "Demand ceasefire",
        "icon": "🕊", "needs_veto_check": False,
        "desc_sk": "Všetky bojujúce strany musia zastaviť paľbu.",
        "desc_en": "All belligerents must cease fire.",
    },
}

RES_VOTE_HOURS  = 48    # rezolúcia platí 48 hodín na hlasovanie
RES_QUOTA       = 3     # potrebný počet hlasov ZA (okrem stálych členov)
NUCLEAR_HEAT_THRESHOLD = 60

# ── Vojny ────────────────────────────────────────────────────────────────────
WAR_ROLES = {"president", "pm", "def_minister", "general"}

# Ceny výroby zbraní (CR za 1 jednotku)
WEAPON_BUILD_COSTS = {
    "conventional": 500,    # 1 tis. vojakov = 500 CR
    "missiles":     8000,   # 1 raketa = 8 000 CR
    "cyber":        3000,   # 1 kyber jednotka = 3 000 CR
    # warheads → len cez Pu (nie CR)
}

# Vojenské zbrane — výkon na jednotku + straty útočníka + nahnevanie sveta
# dmg_*: škoda nepriateľovi na každú použitú jednotku
# loss_rate: podiel vlastných strát útočníka (0.0 = žiadne, 0.3 = 30%)
# anger_neutral: naštvanosť neutrálnych krajín za 1 jednotku
# anger_perm: naštvanosť stálych členov RB za 1 jednotku
WEAPON_STATS = {
    "conventional": {
        "icon": "🪖", "name_sk": "Konvenčné sily",
        "dmg_conventional": 1.5,   # 1 tis. útočníka zničí 1.5 tis. obrancu
        "dmg_missiles": 0, "dmg_cyber": 0, "dmg_warheads": 0,
        "loss_rate": 0.30,         # útočník stratí 30 % nasadených
        "anger_neutral": 1, "anger_perm": 3,
    },
    "missiles": {
        "icon": "🚀", "name_sk": "Balistické rakety",
        "dmg_conventional": 8.0,   # 1 raketa ničí 8 tis. konvenčných
        "dmg_missiles": 0.5, "dmg_cyber": 0, "dmg_warheads": 0,
        "loss_rate": 0.0,          # rakety sú spotrebné
        "anger_neutral": 8, "anger_perm": 20,
    },
    "cyber": {
        "icon": "💻", "name_sk": "Kybernetická zbraň",
        "dmg_conventional": 0, "dmg_missiles": 0,
        "dmg_cyber": 3.0,          # 1 kyber jednotka ničí 3 kyber nepriateľa
        "dmg_warheads": 0,
        "loss_rate": 0.0,
        "anger_neutral": 3, "anger_perm": 8,
    },
    "warheads": {
        "icon": "☢", "name_sk": "Jadrová hlavica",
        "dmg_conventional": 300,
        "dmg_missiles": 5, "dmg_cyber": 5, "dmg_warheads": 0,
        "loss_rate": 1.0,          # každá použitá hlavica je spotrebená
        "anger_neutral": 80, "anger_perm": 100,
    },
}

# Dôsledky naštvanosti (anger body od jednej krajiny)
ANGER_SANCTIONS_THRESHOLD  = 50   # Rada automaticky navrhne sankcie
ANGER_WAR_AUTH_THRESHOLD   = 90   # Rada automaticky navrhne vojenskú intervenciu

# ── Investície ────────────────────────────────────────────────────────────────
INV_DURATION_H   = 24     # investícia trvá 24 hodín
INV_RETURN_RATE  = 1.25   # investor dostane 125 % späť (25 % výnos)
INV_MAX_ACTIVE   = 5      # max aktívnych investícií pre jedného hráča
INV_MIN_CR       = 500    # minimálna investícia v CR
INV_MIN_FUEL     = 5      # minimálna investícia v palive (energie)

def _countries_allowed():
    if "username" not in session:
        return False
    u = load_users().get(session["username"], {})
    if u.get("is_tester"):
        return True
    return any(f["id"] == "countries" and f.get("public") for f in BETA_FEATURES)

def _get_country_data():
    """Načíta kb_countries.json — {country_id: {roles, at_war, sanctions, weapons}}"""
    data = load_jf(KB_COUNTRIES, {})
    for c in COUNTRIES:
        cd = data.setdefault(c["id"], {})
        cd.setdefault("roles", {})
        cd.setdefault("at_war", [])
        cd.setdefault("sanctions", [])
        cd.setdefault("weapons", {
            "nuclear_approved": c["id"] in COUNCIL_PERMANENT,
            "warheads": 0,
            "missiles": 0,
            "conventional": 0,
            "cyber": 0,
        })
    return data

def _get_council_data():
    """Načíta kb_council.json — {resolutions: [...], alerts: [...]}"""
    data = load_jf(KB_COUNCIL, {})
    data.setdefault("resolutions", [])
    data.setdefault("alerts", [])
    return data

def _council_members(cdata):
    """
    Vráti dict {username: {"country": cid, "role": rid, "is_permanent": bool, "is_geneva": bool}}
    pre všetkých hráčov s relevantnou rolou v Rade.
    Geneva seats (council_seat v Švajčiarsku) sú neutrálni mediátori.
    """
    members = {}
    council_roles = {"president", "pm", "def_minister", "council_rep", "foreign_min", "council_seat"}
    for cid, cd in cdata.items():
        is_perm   = cid in COUNCIL_PERMANENT
        is_geneva = cid == COUNCIL_HQ_COUNTRY
        for rid, users in cd.get("roles", {}).items():
            if rid not in council_roles:
                continue
            if rid == "council_seat" and not is_geneva:
                continue  # council_seat platí iba pre Švajčiarsko
            if isinstance(users, str):
                users = [users] if users else []
            for u in users:
                if u and u not in members:
                    members[u] = {
                        "country": cid, "role": rid,
                        "is_permanent": is_perm,
                        "is_geneva": is_geneva,
                    }
    return members

def _player_countries(uname, cdata):
    """Vráti zoznam (cid, rid) — všetky krajiny kde má hráč nejakú rolu."""
    result = []
    for cid, cd in cdata.items():
        for rid, users in cd.get("roles", {}).items():
            ul = users if isinstance(users, list) else ([users] if users else [])
            if uname in ul:
                result.append((cid, rid))
    return result

def _resolve_resolution(res, cdata):
    """Aplikuj schválenú rezolúciu na krajinu."""
    cid    = res.get("target_country")
    rtype  = res.get("type")
    cd     = cdata.get(cid, {})
    w      = cd.setdefault("weapons", {})

    if rtype == "nuclear_approve":
        w["nuclear_approved"] = True
    elif rtype == "nuclear_ban":
        w["nuclear_approved"] = False
        w["warheads"] = 0
    elif rtype == "sanctions":
        target_c = res.get("target_country")
        proposer_c = res.get("proposed_by_country")
        if proposer_c and target_c:
            cd.setdefault("sanctions", [])
            if proposer_c not in cd["sanctions"]:
                cd["sanctions"].append(proposer_c)
    elif rtype == "embargo":
        cd.setdefault("sanctions", [])
        if "ISGC_embargo" not in cd["sanctions"]:
            cd["sanctions"].append("ISGC_embargo")
    elif rtype == "war_auth":
        pass  # vojenská akcia — len notifikácia
    elif rtype == "ceasefire":
        cd["at_war"] = []

    cdata[cid] = cd
    return cdata

PLANT_TYPES = {
    "solar": {
        "id": "solar", "icon": "☀",
        "name_sk": "Solárna elektráreň", "name_en": "Solar power plant",
        "desc_sk": "Bez paliva. Produkuje 8 energie/hod.",
        "desc_en": "No fuel needed. Produces 8 energy/hr.",
        "build_cost": 3000,
        "fuel_type": None, "fuel_per_hr": 0,
        "energy_per_hr": 8, "max_count": 5,
    },
    "coal": {
        "id": "coal", "icon": "🏭",
        "name_sk": "Uhoľná elektráreň", "name_en": "Coal power plant",
        "desc_sk": "Spotrebuje 1 tonu uhlia/hod. Produkuje 40 energie/hod.",
        "desc_en": "Consumes 1 coal ton/hr. Produces 40 energy/hr.",
        "build_cost": 8000,
        "fuel_type": "coal", "fuel_per_hr": 1,
        "energy_per_hr": 40, "max_count": 3,
    },
    "nuclear": {
        "id": "nuclear", "icon": "⚛",
        "name_sk": "Jadrová elektráreň", "name_en": "Nuclear power plant",
        "desc_sk": "Spotrebuje 1 palivový článok/hod. Produkuje 200 energie/hod.",
        "desc_en": "Consumes 1 fuel rod/hr. Produces 200 energy/hr.",
        "build_cost": 50000,
        "fuel_type": "uranium", "fuel_per_hr": 1,
        "energy_per_hr": 200, "max_count": 1,
    },
    "wind": {
        "id": "wind", "icon": "🌬",
        "name_sk": "Veterná farma", "name_en": "Wind farm",
        "desc_sk": "Bez paliva. Produkuje 20 energie/hod. Max 4.",
        "desc_en": "No fuel needed. Produces 20 energy/hr. Max 4.",
        "build_cost": 12000,
        "fuel_type": None, "fuel_per_hr": 0,
        "energy_per_hr": 20, "max_count": 4,
    },
    "gas": {
        "id": "gas", "icon": "🔥",
        "name_sk": "Plynová elektráreň", "name_en": "Gas power plant",
        "desc_sk": "Spotrebuje 2 tony plynu/hod. Produkuje 75 energie/hod.",
        "desc_en": "Consumes 2 gas tons/hr. Produces 75 energy/hr.",
        "build_cost": 22000,
        "fuel_type": "gas", "fuel_per_hr": 2,
        "energy_per_hr": 75, "max_count": 2,
    },
    "geothermal": {
        "id": "geothermal", "icon": "🌋",
        "name_sk": "Geotermálna elektráreň", "name_en": "Geothermal plant",
        "desc_sk": "Bez paliva. Produkuje 55 energie/hod. Max 2.",
        "desc_en": "No fuel needed. Produces 55 energy/hr. Max 2.",
        "build_cost": 40000,
        "fuel_type": None, "fuel_per_hr": 0,
        "energy_per_hr": 55, "max_count": 2,
    },
}

# ── Jadrová vetva — Fáza 1 ──────────────────────────────────────────────────
MINE_TYPES = {
    "uranium_mine": {
        "id":         "uranium_mine",
        "icon":       "⛏☢",
        "name_sk":    "Uránová baňa",
        "name_en":    "Uranium mine",
        "desc_sk":    "Ťaží 2 t surového uránu/hod. pasívne.",
        "desc_en":    "Passively mines 2 t raw uranium/hr.",
        "build_cost": 15000,
        "rate_per_hr": 2.0,
        "produces":   "uranium_raw",
        "max_count":  3,
    },
}
ENRICH_RATIO = 10   # legacy: /energy/enrich button (LEU-3 quick convert)

# ── Jadrová vetva — Fáza 4: Hazard mechanika ────────────────────────────────
PLANT_TYPES["rbmk"] = {
    "id": "rbmk", "icon": "⚠☢",
    "name_sk": "RBMK reaktor", "name_en": "RBMK reactor",
    "desc_sk": "300 E/hod. Lacný ale pozitívny dutinový koef. = havárne riziko.",
    "desc_en": "300 E/hr. Cheap but positive void coeff. = accident risk.",
    "build_cost": 35000,
    "fuel_type": "uranium", "fuel_per_hr": 1,
    "energy_per_hr": 300, "max_count": 1,
}

# Pravdepodobnosť havárie za jeden energy_tick podľa safety level 0-3
HAZARD_PROBS = {
    "rbmk":    [0.07, 0.028, 0.008, 0.001],
    "breeder": [0.04, 0.015, 0.005, 0.001],
}

SAFETY_UPGRADES = [
    {"level": 1, "cost": 5000,
     "name_sk": "Bezpečnostný stupeň 1", "name_en": "Safety Level 1",
     "desc_sk": "Základné SCRAM systémy. Riziko havárie ÷2.5.",
     "desc_en": "Basic SCRAM systems. Accident risk ÷2.5."},
    {"level": 2, "cost": 15000,
     "name_sk": "Bezpečnostný stupeň 2", "name_en": "Safety Level 2",
     "desc_sk": "Pokročilé ochranné okruhy. Riziko ÷8.75.",
     "desc_en": "Advanced protection circuits. Risk ÷8.75."},
    {"level": 3, "cost": 40000,
     "name_sk": "Bezpečnostný stupeň 3", "name_en": "Safety Level 3",
     "desc_sk": "Pasívna bezpečnosť. Riziko minimálne.",
     "desc_en": "Passive safety systems. Risk minimal."},
]

REPAIR_COSTS = {
    "rbmk":    8000,
    "breeder": 20000,
}
DAMAGE_OFFLINE_H = 10

# ── Jadrová vetva — Fáza 5: Xenón + Dispatch ────────────────────────────────
# Xenón-135 sa hromadí pri prevádzke RBMK — pri vysokých hodnotách znižuje výkon
# a zvyšuje riziko havárie (ako pri Černobyle)
XENON_RATE_RUN  =  3.0   # xenón/hod keď RBMK beží
XENON_RATE_IDLE =  0.5   # xenón/hod keď RBMK je nečinný
XENON_DECAY     =  1.5   # prirodzený pokles/hod
XENON_PURGE_RATE = 18.0  # xenón/hod počas aktívneho purge (draho palivo)
XENON_WARN      =  60    # od tejto hodnoty varuj hráča
XENON_REDUCE    =  75    # od tejto hodnoty RBMK výkon × 0.55
XENON_DANGER    =  88    # od tejto hodnoty hazard pravdepodobnosť × 2.5

# Dispatch pressure — dispečing volá a žiada výkon
DISPATCH_PROB   = 0.12   # šanca dispatch eventu na tick (ak má RBMK a žiadny aktívny)
DISPATCH_OPTS   = [
    {"id": "accept",  "cr": 8000,  "hazard_mult": 3.0, "hours": 4,
     "label_sk": "Prijať kontrakt (+8000 CR, riziko ×3 na 4h)",
     "label_en": "Accept contract (+8000 CR, risk ×3 for 4h)"},
    {"id": "partial", "cr": 2500,  "hazard_mult": 1.5, "hours": 2,
     "label_sk": "Čiastočne (+2500 CR, riziko ×1.5 na 2h)",
     "label_en": "Partial (+2500 CR, risk ×1.5 for 2h)"},
    {"id": "refuse",  "cr": -1000, "hazard_mult": 1.0, "hours": 0,
     "label_sk": "Odmietnuť (−1000 CR, žiadne riziko)",
     "label_en": "Refuse (−1000 CR, no extra risk)"},
]
DAMAGE_OFFLINE_H = 10

# ── Jadrová vetva — Fáza 6: Spent fuel + Palivový cyklus ────────────────────
SPENT_FUEL_RATE   = 0.15   # vyhorených článkov/hod na jadrový/RBMK reaktor
SPENT_FUEL_MAX    = 20     # max kapacita — pri prekročení: regulatory shutdown
PUREX_RATIO       = 5      # 5 spent fuel rods → 1 Pu-239
PUREX_HEAT        = 18     # proliferačná horúčava za každý Pu rod z PUREX
PUREX_CR_PER_ROD  = 3000   # CR za reprocesing 1 Pu rodu
MOX_PU_PER_ROD    = 0.5    # Pu-239 na 1 MOX rod
MOX_U238_PER_ROD  = 3.0    # t U-238 na 1 MOX rod
MOX_ENERGY_MULT   = 1.35   # MOX efektivita oproti LEU-3
DISPOSAL_CR       = 400    # CR na likvidáciu 1 spent fuel rodu

# ── Jadrová vetva — Fáza 7: BN-800, RBMK online refueling, WG-Pu ───────────
WG_PU_PER_HR_BN800      = 0.05    # zbraňové Pu/hod z BN-800
WG_PU_PER_HR_RBMK_OL    = 0.05    # zbraňové Pu/hod z RBMK online refuelingu
WG_PU_HEAT_PER_UNIT     = 50.0    # proliferačná horúčava za predaj 1 WG-Pu
WG_PU_SELL_CR           = 25000   # CR za predaj 1 WG-Pu (čierny trh)
ONLINE_REFUEL_HEAT_HR   = 5.0     # extra horúčava/hod pri RBMK online refuelingu
SOVIET_EVENT_PROB       = 0.04    # šanca sovietskeho eventu na tick
SOVIET_OPTS = [
    {"id": "accept",  "cr": 20000, "wg_pu": 5.0, "heat": 40, "hazard_mult": 4.0, "hours": 8,
     "label_sk": "Prijať — tajný reaktor (+20 000 CR, +5 WG-Pu, riziko ×4 na 8h)",
     "label_en": "Accept — secret reactor (+20,000 CR, +5 WG-Pu, risk ×4 for 8h)"},
    {"id": "partial", "cr":  5000, "wg_pu": 1.0, "heat": 15, "hazard_mult": 2.0, "hours": 4,
     "label_sk": "Čiastočne — minimálna spolupráca (+5 000 CR, +1 WG-Pu, riziko ×2 na 4h)",
     "label_en": "Partial — minimal cooperation (+5,000 CR, +1 WG-Pu, risk ×2 for 4h)"},
    {"id": "refuse",  "cr":     0, "wg_pu": 0.0, "heat":  0, "hazard_mult": 1.0, "hours": 0,
     "label_sk": "Odmietnuť — bez odmeny, bez rizika",
     "label_en": "Refuse — no reward, no risk"},
]

# ── Kaskádová havária (RBMK) ─────────────────────────────────────────────────
# Kombinácia faktorov: únava operátora + nestabilita + tlak dispečingu
CASCADE_FACTORS = {
    "xenon_high":    {"threshold": 75,  "weight": 3},  # xenón > 75 = kumuluje sa
    "dispatch_on":   {"weight": 2},                     # aktívny dispatch kontrakt
    "online_refuel": {"weight": 2},                     # RBMK online refueling zapnutý
    "safety_low":    {"threshold": 1,   "weight": 2},  # safety < 1 = nechránený
    "heat_high":     {"threshold": 70,  "weight": 2},  # prolif heat > 70
}
CASCADE_THRESHOLD = 5   # suma váh pre spustenie kaskády (max ~11)
CASCADE_OFFLINE_H = 24  # poškodenie kaskády = 24h offline (vs normálnych 10h)
CASCADE_HEAT_DUMP = 45  # kaskáda dumpa +45 proliferačnej horúčavy

# ── Viacúrovňový sodíkový únik (Breeder / BN-800) ────────────────────────────
SODIUM_LEVELS = [
    {"name": "minor",  "prob_mult": 0.6, "offline_h": 6,  "heat_add": 15,
     "name_sk": "Menší sodíkový únik", "name_en": "Minor sodium leak"},
    {"name": "major",  "prob_mult": 0.3, "offline_h": 18, "heat_add": 40,
     "name_sk": "Veľký sodíkový únik — POŽIAR", "name_en": "Major sodium leak — FIRE"},
    {"name": "explosion", "prob_mult": 0.1, "offline_h": 48, "heat_add": 60,
     "name_sk": "EXPLÓZIA SODÍKA — TOTÁLNA HAVÁRIA", "name_en": "SODIUM EXPLOSION — TOTAL LOSS"},
]

# ── Fáza 2 — obohacovanie minihra ───────────────────────────────────────────
# feed_per_rod = t surového uránu na 1 palivový článok
# energy_mult  = koľkonásobok energie oproti štandardu dáva každý článok
# cr_cost      = CR za celú dávku (separačná práca)
# u238_per_rod = t ochudobneného U-238 ako vedľajší produkt
ENRICHMENT_GRADES = [
    {
        "id": "leu3",  "fuel_key": "uranium",
        "name_sk": "LEU-3  (štandard)",    "name_en": "LEU-3  (standard)",
        "u235_pct": 3,  "feed_per_rod": 10, "energy_mult": 1.0,
        "cr_cost": 0,   "stages": 4,        "u238_per_rod": 9.0,
        "desc_sk": "Bežné reaktorové palivo. Jednoduchá kaskáda 4 stupní.",
        "desc_en": "Standard reactor fuel. Simple 4-stage cascade.",
    },
    {
        "id": "leu5",  "fuel_key": "uranium_leu5",
        "name_sk": "LEU-5  (vylepšené)",   "name_en": "LEU-5  (enhanced)",
        "u235_pct": 5,  "feed_per_rod": 17, "energy_mult": 1.5,
        "cr_cost": 600, "stages": 8,        "u238_per_rod": 15.5,
        "desc_sk": "Každý článok dá 1.5× viac energie. 8-stupňová kaskáda.",
        "desc_en": "Each rod yields 1.5× more energy. 8-stage cascade.",
    },
    {
        "id": "heu20", "fuel_key": "uranium_heu20",
        "name_sk": "HEU-20 (výskumný)",   "name_en": "HEU-20 (research)",
        "u235_pct": 20, "feed_per_rod": 70, "energy_mult": 3.0,
        "cr_cost": 7000,"stages": 25,       "u238_per_rod": 68.0,
        "desc_sk": "3× energia. Vysoký proliferačný dopad. 25 stupní.",
        "desc_en": "3× energy. High proliferation impact. 25 stages.",
    },
]
_GRADE_BY_ID  = {g["id"]: g for g in ENRICHMENT_GRADES}
_GRADE_BY_KEY = {g["fuel_key"]: g for g in ENRICHMENT_GRADES}
# Poradie tried pre jadrové elektrárne: najlepší stupeň sa spotrebuje prvý
_NUCLEAR_GRADE_ORDER = ["heu20", "leu5", "leu3"]

PLANT_TYPES["breeder"] = {
    "id": "breeder", "icon": "♻☢",
    "name_sk": "Breeder reaktor", "name_en": "Breeder reactor",
    "desc_sk": "Spotrebuje 2 t U-238/hod. Vyrába Pu-239 + 120 energie/hod.",
    "desc_en": "Consumes 2 t U-238/hr. Breeds Pu-239 + 120 energy/hr.",
    "build_cost": 120000,
    "fuel_type": "u238_breed",   # špeciálny typ — spracovaný nižšie v tiku
    "fuel_per_hr": 2.0,
    "energy_per_hr": 120,
    "pu_per_hr": 0.30,           # plutónia za hodinu
    "max_count": 1,
}

PLANT_TYPES["bn800"] = {
    "id": "bn800", "icon": "⚡☢",
    "name_sk": "BN-800 (pokročilý breeder)", "name_en": "BN-800 (advanced breeder)",
    "desc_sk": "Spotrebuje 3 t U-238/hod. Vyrába Pu-239 + WG-Pu + 180 E/hod. Vyžaduje Breeder.",
    "desc_en": "Consumes 3 t U-238/hr. Breeds Pu-239 + WG-Pu + 180 E/hr. Requires Breeder.",
    "build_cost": 200000,
    "fuel_type": "u238_breed",
    "fuel_per_hr": 3.0,
    "energy_per_hr": 180,
    "pu_per_hr": 0.50,
    "max_count": 1,
}
HAZARD_PROBS["bn800"] = [0.03, 0.012, 0.004, 0.001]
REPAIR_COSTS["bn800"] = 25000

PLANT_TYPES["fusion"] = {
    "id": "fusion", "icon": "🌟",
    "name_sk": "Fúzny reaktor", "name_en": "Fusion reactor",
    "desc_sk": "Spotrebuje 1 hélium-3/hod. Produkuje 800 energie/hod.",
    "desc_en": "Consumes 1 helium-3/hr. Produces 800 energy/hr.",
    "build_cost": 300000,
    "fuel_type": "helium", "fuel_per_hr": 1,
    "energy_per_hr": 800, "max_count": 1,
}

FUEL_SHOP = [
    {
        "id": "coal", "icon": "⛏",
        "name_sk": "Uhlie", "name_en": "Coal",
        "pack_qty": 20, "pack_cost": 800,
        "unit_sk": "ton", "unit_en": "tons",
    },
    {
        "id": "gas", "icon": "🔥",
        "name_sk": "Zemný plyn", "name_en": "Natural gas",
        "pack_qty": 30, "pack_cost": 1800,
        "unit_sk": "ton", "unit_en": "tons",
    },
    {
        "id": "uranium", "icon": "☢",
        "name_sk": "Urán (palivové články)", "name_en": "Uranium (fuel rods)",
        "pack_qty": 5, "pack_cost": 6000,
        "unit_sk": "ks", "unit_en": "rods",
    },
    {
        "id": "helium", "icon": "🌟",
        "name_sk": "Hélium-3 (fúzne palivá)", "name_en": "Helium-3 (fusion fuel)",
        "pack_qty": 3, "pack_cost": 30000,
        "unit_sk": "ks", "unit_en": "cells",
    },
]

MAX_ENERGY        = 150000  # maximálna kapacita zásobníka energie
BANKRUPT_GRACE_H  = 8     # hodín mŕtvej firmy pred auto-bankrotom
BANKRUPT_SHARE    = 0.60  # podiel výťažku aukcie pre bankrotujúceho hráča
BANKRUPT_DUR_H    = 2     # trvanie bankrotovej aukcie (hodiny)

# ── Fáza 6 — Eventy a dráma ─────────────────────────────────────────────────
EVENT_PROB       = 0.20   # pravdepodobnosť eventu pri každej návšteve /energy
EVENT_COOLDOWN_H = 3.0    # minimálna pauza medzi eventmi (hodiny)

# type: "positive" | "negative"
# effect: čo sa stane
#   fuel_gift    → pridaj palivo (value = qty, fuel = "coal"|"uranium"|"random")
#   solar_boost  → solárna produkcia ×value po dobu duration_h hodín
#   sell_bonus   → predaj energie za ×value po dobu duration_h hodín
#   fuel_leak    → stratíš value% paliva (oboch druhov)
#   plant_fail   → náhodná elektráreň offline duration_h hodín
#   energy_drain → stratíš value% zásobníka energie
ENERGY_EVENTS = [
    {"id": "coal_gift",    "type": "pos", "weight": 25,
     "effect": "fuel_gift", "fuel": "coal",    "value": 20,
     "name_sk": "⛏ Nečakaná dodávka uhlia!",      "name_en": "⛏ Surprise coal delivery!",
     "desc_sk": "+20 ton uhlia zadarmo.",           "desc_en": "+20 tons of coal for free."},
    {"id": "uranium_gift", "type": "pos", "weight": 10,
     "effect": "fuel_gift", "fuel": "uranium",  "value": 3,
     "name_sk": "☢ Urán od výskumníkov!",          "name_en": "☢ Uranium from researchers!",
     "desc_sk": "+3 palivové články zadarmo.",       "desc_en": "+3 fuel rods for free."},
    {"id": "solar_boost",  "type": "pos", "weight": 20, "duration_h": 6,
     "effect": "solar_boost", "value": 2.0,
     "name_sk": "☀ Solárny prielom!",              "name_en": "☀ Solar breakthrough!",
     "desc_sk": "Solárna produkcia ×2 na 6 hodín.", "desc_en": "Solar output ×2 for 6 hours."},
    {"id": "sell_bonus",   "type": "pos", "weight": 15, "duration_h": 4,
     "effect": "sell_bonus", "value": 2.0,
     "name_sk": "⚡ Špičkový dopyt po energii!",   "name_en": "⚡ Peak energy demand!",
     "desc_sk": "Predaj energie za 2× cenu 4 hodiny.","desc_en": "Sell energy at 2× price for 4 hours."},
    {"id": "fuel_leak",    "type": "neg", "weight": 20,
     "effect": "fuel_leak", "value": 0.30,
     "name_sk": "💧 Únik paliva!",                 "name_en": "💧 Fuel leak!",
     "desc_sk": "Stratíš 30% zásob paliva.",        "desc_en": "You lose 30% of fuel stocks."},
    {"id": "plant_fail",   "type": "neg", "weight": 15, "duration_h": 6,
     "effect": "plant_fail", "value": 1,
     "name_sk": "🔧 Porucha elektrárne!",           "name_en": "🔧 Plant malfunction!",
     "desc_sk": "Jedna elektráreň offline 6 hodín.", "desc_en": "One plant offline for 6 hours."},
    {"id": "energy_drain", "type": "neg", "weight": 15,
     "effect": "energy_drain", "value": 0.35,
     "name_sk": "⚠ Výpadok siete!",               "name_en": "⚠ Grid blackout!",
     "desc_sk": "Zásobník energie klesne o 35%.",   "desc_en": "Energy storage drops by 35%."},
    # ── Fáza 7 eventy ───────────────────────────────────────────
    {"id": "helium_find",  "type": "pos", "weight": 6,
     "effect": "fuel_gift", "fuel": "helium", "value": 2,
     "name_sk": "🌟 Héliové nálezisko!",            "name_en": "🌟 Helium-3 deposit found!",
     "desc_sk": "+2 hélium-3 palivá zadarmo.",       "desc_en": "+2 helium-3 cells for free."},
    {"id": "plasma_boost", "type": "pos", "weight": 8, "duration_h": 4,
     "effect": "plasma_boost", "value": 2.0,
     "name_sk": "🌟 Plazmový prielom!",             "name_en": "🌟 Plasma breakthrough!",
     "desc_sk": "Fúzny reaktor ×2 na 4 hodiny.",    "desc_en": "Fusion reactor ×2 for 4 hours."},
]
_EVENT_WEIGHTS   = [e["weight"] for e in ENERGY_EVENTS]
_EVENT_TOTAL_W   = sum(_EVENT_WEIGHTS)

# ── Fáza 3 — statický NPC trh ───────────────────────────────────────────────
# npc_buys  = CR ktoré NPC zaplatí hráčovi za 1 jednotku (hráč predáva)
# npc_sells = CR ktoré NPC pýta za 1 jednotku (hráč kupuje)
# None      = NPC tento smer neobchoduje
NPC_MARKET = [
    {
        "id": "energy", "icon": "⚡",
        "name_sk": "Energia", "name_en": "Energy",
        "unit_sk": "jednotiek", "unit_en": "units",
        "npc_buys": 8, "npc_sells": None,
        "min_qty": 10, "step": 10,
        "source": "energy",
        "note_sk": "Predaj energiu ktorú vyrobíš elektrárňami.",
        "note_en": "Sell energy produced by your power plants.",
    },
    {
        "id": "coal", "icon": "⛏",
        "name_sk": "Uhlie", "name_en": "Coal",
        "unit_sk": "ton", "unit_en": "tons",
        "npc_buys": None, "npc_sells": 45,
        "min_qty": 5, "step": 5,
        "source": "fuel_coal",
        "note_sk": "Palivo pre uhoľné elektrárne.", "note_en": "Fuel for coal plants.",
    },
    {
        "id": "uranium", "icon": "☢",
        "name_sk": "Urán (palivové články)", "name_en": "Uranium (fuel rods)",
        "unit_sk": "ks", "unit_en": "rods",
        "npc_buys": None, "npc_sells": 1300,
        "min_qty": 1, "step": 1,
        "source": "fuel_uranium",
        "note_sk": "Palivo pre jadrové elektrárne.", "note_en": "Fuel for nuclear plants.",
    },
    {
        "id": "oil", "icon": "🛢",
        "name_sk": "Ropa", "name_en": "Oil",
        "unit_sk": "barelov", "unit_en": "barrels",
        "npc_buys": 50, "npc_sells": 65,
        "min_qty": 10, "step": 10,
        "source": "commodity_oil",
        "note_sk": "Komodita. Plynové elektrárne v budúcnosti.",
        "note_en": "Commodity. Gas plants in the future.",
    },
    {
        "id": "gold", "icon": "🥇",
        "name_sk": "Zlato", "name_en": "Gold",
        "unit_sk": "oz", "unit_en": "oz",
        "npc_buys": 480, "npc_sells": 520,
        "min_qty": 1, "step": 1,
        "source": "commodity_gold",
        "note_sk": "Uchovávateľ hodnoty. Cena stabilná (zatiaľ).",
        "note_en": "Store of value. Price stable (for now).",
    },
    {
        "id": "platinum", "icon": "💎",
        "name_sk": "Platina", "name_en": "Platinum",
        "unit_sk": "oz", "unit_en": "oz",
        "npc_buys": 1200, "npc_sells": 1350,
        "min_qty": 1, "step": 1,
        "source": "commodity_platinum",
        "note_sk": "Endgame komodita. Vzácnejšia ako zlato.",
        "note_en": "Endgame commodity. Rarer than gold.",
    },
    {
        "id": "pu239", "icon": "☣",
        "name_sk": "Plutónium-239", "name_en": "Plutonium-239",
        "unit_sk": "ks", "unit_en": "rods",
        "npc_buys": 8000, "npc_sells": None,
        "min_qty": 1, "step": 1,
        "source": "fuel_pu239",
        "note_sk": "NPC kupuje. Vysoká proliferačná horúčava pri predaji.",
        "note_en": "NPC buys. High proliferation heat when selling.",
    },
]

# ── Fáza 4 — dynamický trh ──────────────────────────────────────────────────
# liq     = likvidita (vyššia = menší dopad obchodu na cenu)
# rev     = rýchlosť reverzie k základnej cene (exp. útlm za hodinu)
# min_b/max_b = hranice nákupnej ceny (NPC kúpi od hráča)
# min_s/max_s = hranice predajnej ceny (NPC predá hráčovi)
MARKET_DYN = {
    "energy":  {"liq": 300, "rev": 0.25, "min_b": 2,   "max_b": 30},
    "coal":    {"liq": 150, "rev": 0.20,                "min_s": 15,  "max_s": 180},
    "uranium": {"liq": 20,  "rev": 0.15,                "min_s": 600, "max_s": 4000},
    "oil":     {"liq": 100, "rev": 0.20, "min_b": 15,  "max_b": 250, "min_s": 20,  "max_s": 300},
    "gold":    {"liq": 25,  "rev": 0.10, "min_b": 150, "max_b": 3000, "min_s": 160, "max_s": 3200},
    "platinum":{"liq": 12,  "rev": 0.08, "min_b": 400,  "max_b": 8000,  "min_s": 450,  "max_s": 9000},
    "pu239":   {"liq": 5,   "rev": 0.05, "min_b": 3000, "max_b": 25000},
}

# ── Fáza 5a — Komoditné aukcie ──────────────────────────────────────────────
# Každý lot je dražba fixného balíka komodity. Víťaz platí pri zbere.
# duration_min = ako dlho lot trvá; MAX_ACTIVE_LOTS = koľko lotov súčasne
AUCTION_LOTS_CFG = [
    {"commodity": "coal",    "icon": "⛏", "name_sk": "Uhlie",
     "name_en": "Coal",    "unit_sk": "ton",    "unit_en": "tons",
     "qty": 150, "start_bid": 4500,  "source": "fuel_coal",      "duration_min": 30},
    {"commodity": "uranium", "icon": "☢", "name_sk": "Urán",
     "name_en": "Uranium", "unit_sk": "ks",     "unit_en": "rods",
     "qty": 20,  "start_bid": 18000, "source": "fuel_uranium",   "duration_min": 45},
    {"commodity": "oil",     "icon": "🛢", "name_sk": "Ropa",
     "name_en": "Oil",     "unit_sk": "barelov","unit_en": "barrels",
     "qty": 200, "start_bid": 7500,  "source": "commodity_oil",  "duration_min": 30},
    {"commodity": "gold",    "icon": "🥇", "name_sk": "Zlato",
     "name_en": "Gold",    "unit_sk": "oz",     "unit_en": "oz",
     "qty": 5,   "start_bid": 1800,  "source": "commodity_gold",    "duration_min": 60},
    {"commodity": "helium",   "icon": "🌟", "name_sk": "Hélium-3",
     "name_en": "Helium-3", "unit_sk": "ks",    "unit_en": "cells",
     "qty": 8,   "start_bid": 60000, "source": "fuel_helium",       "duration_min": 90},
    {"commodity": "platinum", "icon": "💎", "name_sk": "Platina",
     "name_en": "Platinum", "unit_sk": "oz",    "unit_en": "oz",
     "qty": 3,   "start_bid": 3200,  "source": "commodity_platinum", "duration_min": 75},
]
MAX_ACTIVE_LOTS = 2  # koľko lotov môže bežať súčasne

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "kb-web-secret-xyrax9-2024")


# ── Language helpers ────────────────────────────────────────────────────────

def L(sk_text, en_text):
    """Return en_text if session lang is 'en', otherwise sk_text."""
    return en_text if session.get('lang') == 'en' else sk_text


# ── Seed default user from env vars ────────────────────────────────────────
def _seed_default_user():
    """
    Ak sú nastavené env premenné DEFAULT_USER + DEFAULT_PASS,
    automaticky vytvorí účet pri štarte servera (iba ak ešte neexistuje).
    Použitie na Render: nastav tieto premenné v Environment sekcii.
    """
    username = os.environ.get("DEFAULT_USER", "").strip()
    password = os.environ.get("DEFAULT_PASS", "")
    if not username or not password:
        return
    users = load_users() if DATA_FILE.exists() else {}
    if username in users:
        return  # účet už existuje, nič nerob
    users[username] = {
        "password": password,
        "registered": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "last_login": None,
        "last_web_login": None,
        "score": 0, "games_played": 0, "kb_sessions": 0,
    }
    save_users(users)
    print(f"[seed] Účet '{username}' vytvorený z DEFAULT_USER env var.")


# ── Helpers ────────────────────────────────────────────────────────────────

def _uname():
    return session["username"].upper()

def load_users():
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[WARN] game_users.json corrupted ({e}), starting fresh.")
    return {}

def save_users(u):
    if _KV_URL:
        _kv_set("game_users", u)          # primárne úložisko
        _atomic_write(DATA_FILE, json.dumps(u, indent=4, ensure_ascii=False))  # best-effort cache
    else:
        _atomic_write(DATA_FILE, json.dumps(u, indent=4, ensure_ascii=False))

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def check_pw(stored, entered):
    """Podporuje plaintext (nové) aj SHA-256 hash (staré účty)."""
    if stored == entered:
        return True
    if len(stored) == 64 and stored == hash_pw(entered):
        return True
    return False

def check_ban(user):
    """Vráti (is_banned, správa). banned_until: None=nie, -1=permanent, timestamp=čas."""
    bu = user.get("banned_until")
    if bu is None:
        return False, ""
    if bu == -1:
        return True, L("Tvoj účet bol trvalo zablokovaný administrátorom.",
                        "Your account has been permanently banned by an administrator.")
    remaining = bu - datetime.now().timestamp()
    if remaining <= 0:
        return False, ""
    if remaining < 3600:
        t = L(f"{int(remaining/60)+1} minút", f"{int(remaining/60)+1} minutes")
    elif remaining < 86400:
        t = L(f"{int(remaining/3600)+1} hodín", f"{int(remaining/3600)+1} hours")
    else:
        t = L(f"{int(remaining/86400)+1} dní", f"{int(remaining/86400)+1} days")
    return True, L(f"Tvoj účet je zablokovaný ešte {t}.", f"Your account is banned for another {t}.")

def validate_pw(pw):
    if len(pw) < 4:
        return False, L("Heslo musí mať aspoň 4 znaky.", "Password must be at least 4 characters.")
    return True, "OK"

def load_jf(path, default=None):
    """
    Načítaj dáta. Primárne z lokálneho súboru (cache po warm-up).
    Ak lokálny súbor chýba alebo je prázdny → fallback na Upstash.
    """
    try:
        p = pathlib.Path(path)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data:  # neprázdny → vráť
                return data
    except Exception:
        pass
    # Lokálny súbor chýba alebo je prázdny — skús Upstash
    if _KV_URL:
        key = _KV_KEYS.get(pathlib.Path(path), pathlib.Path(path).stem)
        kv_data = _kv_get(key)
        if kv_data is not None:
            # Obnov lokálny cache
            _atomic_write(path, json.dumps(kv_data, ensure_ascii=False, indent=2))
            return kv_data
    return default if default is not None else {}

def save_jf(path, data):
    """Zapiš dáta. Ak je Upstash nakonfigurovaný = primárne; lokálny súbor = cache."""
    key = _KV_KEYS.get(pathlib.Path(path), pathlib.Path(path).stem)
    if _KV_URL:
        _kv_set(key, data)                                                   # primárne
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))  # best-effort cache
    else:
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))  # jediné úložisko


# ── Energetická minihra — helpers ───────────────────────────────────────────

_DEFAULT_PROFILE = {
    "plants": [], "energy": 0.0,
    "fuel": {"coal": 0.0, "uranium": 0.0},
    "commodities": {"oil": 0.0, "gold": 0.0},
    "last_tick": 0.0,
}


def _ensure_profile_fields(profile):
    profile.setdefault("plants", [])
    profile.setdefault("energy", 0.0)
    profile.setdefault("fuel", {})
    profile["fuel"].setdefault("coal", 0.0)
    profile["fuel"].setdefault("gas", 0.0)
    profile["fuel"].setdefault("uranium", 0.0)       # LEU-3
    profile["fuel"].setdefault("uranium_leu5", 0.0)   # LEU-5
    profile["fuel"].setdefault("uranium_heu20", 0.0)  # HEU-20
    profile["fuel"].setdefault("pu239", 0.0)          # plutónium
    profile["fuel"].setdefault("helium", 0.0)
    profile["fuel"].setdefault("wg_pu", 0.0)          # zbraňové plutónium (WG-Pu)
    profile["raw_materials"].setdefault("u238", 0.0)   # ochudobnený urán
    profile.setdefault("proliferation_heat", 0.0)      # 0.0–100.0
    profile.setdefault("safety_level", 0)
    profile.setdefault("damaged_plants", [])
    profile.setdefault("xenon_level", 0.0)            # 0–100, RBMK xenón-135
    profile.setdefault("xenon_purge", False)          # aktívny purge (spaľuje palivo 2×)
    profile.setdefault("dispatch_pending", None)      # čakajúca dispatch ponuka
    profile.setdefault("hazard_mult_expires", 0.0)    # timestamp kedy expiruje zvýšené riziko
    profile.setdefault("hazard_mult_val", 1.0)        # aktuálny multiplikátor rizika
    profile.setdefault("kalkar_converted", False)
    profile["raw_materials"].setdefault("spent_fuel", 0.0)  # vyhorené palivové články
    profile["fuel"].setdefault("mox", 0.0)                  # MOX palivové články
    profile.setdefault("commodities", {})
    profile["commodities"].setdefault("oil", 0.0)
    profile["commodities"].setdefault("gold", 0.0)
    profile["commodities"].setdefault("platinum", 0.0)
    profile.setdefault("mines", [])
    profile.setdefault("raw_materials", {})
    profile["raw_materials"].setdefault("uranium_raw", 0.0)
    profile.setdefault("active_events", [])
    profile.setdefault("last_event", None)
    profile.setdefault("last_event_at", 0.0)
    profile.setdefault("rbmk_online_refuel", False)    # Fáza 7: RBMK online refueling
    profile.setdefault("soviet_event_pending", None)   # Fáza 7: sovietský event
    profile.setdefault("cascade_score", 0.0)           # kaskádová havária: akumulovaný risk score
    return profile


def _apply_energy_event(ev_cfg, profile, now):
    """Aplikuj efekt eventu na profil hráča. Upravuje profil in-place."""
    effect = ev_cfg["effect"]
    val = ev_cfg["value"]
    dur_h = ev_cfg.get("duration_h", 0)
    expires_at = now + dur_h * 3600 if dur_h else 0

    if effect == "fuel_gift":
        fuel_key = ev_cfg.get("fuel", "coal")
        profile.setdefault("fuel", {})[fuel_key] = round(
            profile["fuel"].get(fuel_key, 0) + val, 2)

    elif effect in ("solar_boost", "sell_bonus", "plasma_boost"):
        profile.setdefault("active_events", []).append({
            "id": ev_cfg["id"], "effect": effect,
            "value": val, "expires_at": expires_at,
            "name_sk": ev_cfg["name_sk"], "name_en": ev_cfg["name_en"],
        })

    elif effect == "fuel_leak":
        fuel = profile.get("fuel", {})
        for k in fuel:
            fuel[k] = round(fuel[k] * (1.0 - val), 2)
        profile["fuel"] = fuel

    elif effect == "plant_fail":
        plants = profile.get("plants", [])
        if plants:
            idx = random.randint(0, len(plants) - 1)
            profile.setdefault("active_events", []).append({
                "id": ev_cfg["id"], "effect": effect,
                "value": val, "expires_at": expires_at,
                "plant_idx": idx,
                "plant_type": plants[idx],
                "name_sk": ev_cfg["name_sk"], "name_en": ev_cfg["name_en"],
            })

    elif effect == "energy_drain":
        profile["energy"] = round(profile.get("energy", 0) * (1.0 - val), 1)

    # Zaznamenaj posledný event pre flash správu
    profile["last_event"] = {
        "name_sk": ev_cfg["name_sk"], "name_en": ev_cfg["name_en"],
        "desc_sk": ev_cfg["desc_sk"], "desc_en": ev_cfg["desc_en"],
        "type":    ev_cfg["type"],
        "ts":      now,
    }


def _energy_tick(uname_upper):
    """Vypočítaj offline produkciu a ulož. Vráti aktualizovaný profil."""
    data = load_jf(KB_ENERGY, {})
    profile = _ensure_profile_fields(
        data.get(uname_upper, {"last_tick": datetime.now().timestamp()})
    )
    now = datetime.now().timestamp()
    elapsed_hrs = min((now - profile.get("last_tick", now)) / 3600.0, 72.0)

    fuel = {k: float(v) for k, v in profile["fuel"].items()}
    energy = float(profile["energy"])

    # Aktívne eventy — vymaž expirované, zozbieraj efekty
    active_events = [e for e in profile.get("active_events", []) if e["expires_at"] > now]
    solar_mult  = 1.0
    plasma_mult = 1.0
    failed_plant_idx = None
    for ae in active_events:
        if ae["effect"] == "solar_boost":
            solar_mult = max(solar_mult, ae["value"])
        elif ae["effect"] == "plasma_boost":
            plasma_mult = max(plasma_mult, ae["value"])
        elif ae["effect"] == "plant_fail":
            failed_plant_idx = ae.get("plant_idx")
    profile["active_events"] = active_events

    damaged_ids_tick = {d["plant_id"] for d in profile.get("damaged_plants", [])
                        if d["expires_at"] > now}

    for idx, plant_id in enumerate(profile["plants"]):
        if idx == failed_plant_idx:
            continue
        if plant_id in damaged_ids_tick:
            continue  # poškodený reaktor neprodukuje
        pt = PLANT_TYPES.get(plant_id)
        if not pt:
            continue
        if pt["fuel_type"] is None:
            energy += pt["energy_per_hr"] * elapsed_hrs * solar_mult
        elif pt["fuel_type"] == "uranium":
            # Jadrové elektrárne — poradie: Pu-239, MOX, HEU-20, LEU-5, LEU-3
            spent_produced = 0.0
            pu_avail  = fuel.get("pu239", 0.0)
            mox_avail = fuel.get("mox", 0.0)
            if pu_avail > 0:
                hrs = min(elapsed_hrs, pu_avail / pt["fuel_per_hr"])
                energy += pt["energy_per_hr"] * hrs * 1.8
                fuel["pu239"] = max(0.0, pu_avail - hrs * pt["fuel_per_hr"])
                spent_produced = hrs * pt["fuel_per_hr"]
            elif mox_avail > 0:
                hrs = min(elapsed_hrs, mox_avail / pt["fuel_per_hr"])
                energy += pt["energy_per_hr"] * hrs * MOX_ENERGY_MULT
                fuel["mox"] = max(0.0, mox_avail - hrs * pt["fuel_per_hr"])
                spent_produced = hrs * pt["fuel_per_hr"]
            else:
                for gid in _NUCLEAR_GRADE_ORDER:
                    g = _GRADE_BY_ID[gid]
                    avail = fuel.get(g["fuel_key"], 0.0)
                    if avail <= 0:
                        continue
                    hrs = min(elapsed_hrs, avail / pt["fuel_per_hr"])
                    energy += pt["energy_per_hr"] * hrs * g["energy_mult"]
                    fuel[g["fuel_key"]] = max(0.0, avail - hrs * pt["fuel_per_hr"])
                    spent_produced = hrs * pt["fuel_per_hr"]
                    break
            # Generuj vyhorené palivo
            if spent_produced > 0:
                raw = profile.get("raw_materials", {})
                raw["spent_fuel"] = round(raw.get("spent_fuel", 0.0) +
                                          SPENT_FUEL_RATE * spent_produced, 3)
                profile["raw_materials"] = raw
        elif pt["fuel_type"] == "u238_breed":
            # Breeder / BN-800 — spotrebuje U-238, vyrobí Pu-239 + energiu
            raw = profile.get("raw_materials", {})
            u238_avail = raw.get("u238", 0.0)
            if u238_avail > 0:
                hrs = min(elapsed_hrs, u238_avail / pt["fuel_per_hr"])
                energy += pt["energy_per_hr"] * hrs
                pu_produced = round(pt["pu_per_hr"] * hrs, 3)
                fuel["pu239"] = round(fuel.get("pu239", 0.0) + pu_produced, 3)
                raw["u238"] = round(u238_avail - hrs * pt["fuel_per_hr"], 2)
                profile["raw_materials"] = raw
                # Proliferačná horúčava rastie s produkciou Pu
                heat = profile.get("proliferation_heat", 0.0)
                profile["proliferation_heat"] = min(100.0, round(heat + pu_produced * 8, 2))
                # BN-800: produkuje tiež zbraňové plutónium
                if plant_id == "bn800":
                    wg_produced = round(WG_PU_PER_HR_BN800 * hrs, 3)
                    fuel["wg_pu"] = round(fuel.get("wg_pu", 0.0) + wg_produced, 3)
                    # WG-Pu produkcia zvyšuje proliferačnú horúčavu viac
                    profile["proliferation_heat"] = min(100.0, round(
                        profile["proliferation_heat"] + wg_produced * WG_PU_HEAT_PER_UNIT * 0.3, 2))
        else:
            avail = fuel.get(pt["fuel_type"], 0.0)
            if avail <= 0:
                continue
            hrs = min(elapsed_hrs, avail / pt["fuel_per_hr"])
            mult = plasma_mult if plant_id == "fusion" else 1.0
            energy += pt["energy_per_hr"] * hrs * mult
            fuel[pt["fuel_type"]] = max(0.0, avail - hrs * pt["fuel_per_hr"])

    profile["energy"] = round(min(energy, MAX_ENERGY), 1)
    profile["fuel"] = {k: round(v, 2) for k, v in fuel.items()}

    # ── Bane — pasívna ťažba surovín ────────────────────────────
    raw = profile.get("raw_materials", {})
    for mine_id in profile.get("mines", []):
        mt = MINE_TYPES.get(mine_id)
        if not mt:
            continue
        key = mt["produces"]
        raw[key] = round(raw.get(key, 0.0) + mt["rate_per_hr"] * elapsed_hrs, 2)
    profile["raw_materials"] = raw

    # ── Regulatory shutdown (spent fuel) ────────────────────────
    spent = profile.get("raw_materials", {}).get("spent_fuel", 0.0)
    if spent > SPENT_FUEL_MAX:
        nuclear_types = {"nuclear", "rbmk"}
        damaged_ids_sf = {d["plant_id"] for d in profile.get("damaged_plants", [])}
        for ntype in nuclear_types:
            if ntype in profile.get("plants", []) and ntype not in damaged_ids_sf:
                profile.setdefault("damaged_plants", []).append({
                    "plant_id": ntype, "damage_type": "regulatory_shutdown",
                    "expires_at": now + 6 * 3600,
                })
        profile["last_event"] = {
            "name_sk": "🔒 REGULAČNÝ SHUTDOWN — Príliš veľa vyhorelého paliva!",
            "name_en": "🔒 REGULATORY SHUTDOWN — Too much spent fuel!",
            "desc_sk": "Jadrové elektrárne odstavené na 6h. Zlikviduj alebo reprocesuj palivo.",
            "desc_en": "Nuclear plants shutdown for 6h. Dispose or reprocess spent fuel.",
            "type": "neg", "ts": now,
        }

    # ── Xenón-135 (RBMK) ────────────────────────────────────────
    has_rbmk_running = (
        "rbmk" in profile.get("plants", []) and
        "rbmk" not in {d["plant_id"] for d in profile.get("damaged_plants", [])}
    )
    xenon = profile.get("xenon_level", 0.0)
    purge = profile.get("xenon_purge", False)
    if has_rbmk_running:
        if purge:
            xenon = max(0.0, xenon - XENON_PURGE_RATE * elapsed_hrs)
            # Purge spaľuje palivo 2× rýchlejšie — reducujeme energiu ako náklad
            energy = max(0.0, energy - 40 * elapsed_hrs)
        else:
            xenon = min(100.0, xenon + XENON_RATE_RUN * elapsed_hrs)
    else:
        xenon = max(0.0, xenon - XENON_DECAY * elapsed_hrs +
                   XENON_RATE_IDLE * elapsed_hrs if "rbmk" in profile.get("plants", []) else
                   xenon - XENON_DECAY * elapsed_hrs)
    profile["xenon_level"] = round(xenon, 2)

    # Xenón redukuje výkon RBMK (aplikované spätne na energiu)
    if has_rbmk_running and xenon >= XENON_REDUCE and not purge:
        rbmk_pt = PLANT_TYPES.get("rbmk", {})
        base_output = rbmk_pt.get("energy_per_hr", 300) * elapsed_hrs
        energy = max(0.0, energy - base_output * 0.45)  # odober 45% výkonu

    profile["energy"] = round(min(energy, MAX_ENERGY), 1)
    profile["fuel"] = {k: round(v, 2) for k, v in fuel.items()}

    # ── RBMK online refueling — generuje WG-Pu + extra horúčava ────
    if has_rbmk_running and profile.get("rbmk_online_refuel", False):
        wg_ol = round(WG_PU_PER_HR_RBMK_OL * elapsed_hrs, 3)
        fuel["wg_pu"] = round(fuel.get("wg_pu", 0.0) + wg_ol, 3)
        heat_ol = profile.get("proliferation_heat", 0.0)
        profile["proliferation_heat"] = min(100.0, round(
            heat_ol + ONLINE_REFUEL_HEAT_HR * elapsed_hrs, 2))

    # Dispatch pressure event
    has_pending = profile.get("dispatch_pending") is not None
    if has_rbmk_running and not has_pending and random.random() < DISPATCH_PROB:
        profile["dispatch_pending"] = {"ts": now}

    # Soviet event (tajný sovietský reaktor) — len ak má RBMK a žiadny aktívny
    has_soviet = profile.get("soviet_event_pending") is not None
    if has_rbmk_running and not has_soviet and random.random() < SOVIET_EVENT_PROB:
        profile["soviet_event_pending"] = {"ts": now}

    # Proliferačná horúčava — prirodzený pokles 1.5 bodu/hod
    heat = profile.get("proliferation_heat", 0.0)
    profile["proliferation_heat"] = max(0.0, round(heat - 1.5 * elapsed_hrs, 2))

    # ── Hazard check ─────────────────────────────────────────────
    safety = profile.get("safety_level", 0)
    damaged = profile.get("damaged_plants", [])
    # Vymaž expirované poškodenia (oprava prebehla alebo čas uplynul)
    active_dmg = [d for d in damaged if d["expires_at"] > now]
    # Reaktory ktoré sú momentálne poškodené
    damaged_ids = {d["plant_id"] for d in active_dmg}
    hazard_event = None

    # Hazard multiplikátor (dispatch, xenón)
    haz_mult = profile.get("hazard_mult_val", 1.0)
    if profile.get("hazard_mult_expires", 0) <= now:
        haz_mult = 1.0
        profile["hazard_mult_val"] = 1.0
    if "rbmk" in profile.get("plants", []) and profile.get("xenon_level", 0) >= XENON_DANGER:
        haz_mult = max(haz_mult, 2.5)

    # ── Kaskádová havária (RBMK) ─────────────────────────────────────
    has_rbmk_plant = "rbmk" in profile.get("plants", [])
    if has_rbmk_plant and "rbmk" not in damaged_ids:
        cs = 0.0
        if profile.get("xenon_level", 0) >= CASCADE_FACTORS["xenon_high"]["threshold"]:
            cs += CASCADE_FACTORS["xenon_high"]["weight"]
        if profile.get("dispatch_pending") is not None:
            cs += CASCADE_FACTORS["dispatch_on"]["weight"]
        if profile.get("rbmk_online_refuel", False):
            cs += CASCADE_FACTORS["online_refuel"]["weight"]
        if safety < CASCADE_FACTORS["safety_low"]["threshold"]:
            cs += CASCADE_FACTORS["safety_low"]["weight"]
        if profile.get("proliferation_heat", 0) >= CASCADE_FACTORS["heat_high"]["threshold"]:
            cs += CASCADE_FACTORS["heat_high"]["weight"]
        cs *= haz_mult
        profile["cascade_score"] = round(min(15.0, cs), 2)
        if cs >= CASCADE_THRESHOLD:
            active_dmg.append({
                "plant_id": "rbmk", "damage_type": "cascade_accident",
                "expires_at": now + CASCADE_OFFLINE_H * 3600,
            })
            damaged_ids.add("rbmk")
            profile["energy"] = round(profile.get("energy", 0) * 0.20, 1)
            profile["proliferation_heat"] = min(100.0, round(
                profile.get("proliferation_heat", 0) + CASCADE_HEAT_DUMP, 2))
            profile["xenon_level"] = 0.0
            profile["rbmk_online_refuel"] = False
            profile["cascade_score"] = 0.0
            hazard_event = "cascade"

    for haz_type in ("rbmk", "breeder", "bn800"):
        if hazard_event:
            break
        if haz_type in profile.get("plants", []) and haz_type not in damaged_ids:
            prob = HAZARD_PROBS.get(haz_type, [0])[min(safety, 3)] * haz_mult
            if random.random() < prob:
                if haz_type in ("breeder", "bn800"):
                    # Viacúrovňový sodíkový únik — náhodná závažnosť
                    r = random.random()
                    lvl = SODIUM_LEVELS[0]
                    acc = 0.0
                    for sl in SODIUM_LEVELS:
                        acc += sl["prob_mult"]
                        if r <= acc:
                            lvl = sl
                            break
                    offline_h = lvl["offline_h"]
                    heat_add  = lvl["heat_add"]
                    dmg_type  = lvl["name"]
                    profile["proliferation_heat"] = min(100.0, round(
                        profile.get("proliferation_heat", 0) + heat_add, 2))
                    profile["raw_materials"]["u238"] = round(
                        profile.get("raw_materials", {}).get("u238", 0) * 0.60, 2)
                    sodium_lvl_sk = lvl["name_sk"]
                    sodium_lvl_en = lvl["name_en"]
                else:
                    offline_h = DAMAGE_OFFLINE_H
                    dmg_type  = "meltdown"
                    sodium_lvl_sk = sodium_lvl_en = ""
                active_dmg.append({
                    "plant_id":    haz_type,
                    "damage_type": dmg_type,
                    "expires_at":  now + offline_h * 3600,
                })
                damaged_ids.add(haz_type)
                profile["energy"] = round(profile.get("energy", 0) * 0.50, 1)
                hazard_event = haz_type

    profile["damaged_plants"] = active_dmg
    if hazard_event:
        dn_map = {"rbmk": "RBMK", "breeder": "Breeder", "bn800": "BN-800", "cascade": "RBMK"}
        dmg_name = dn_map.get(hazard_event, hazard_event)
        if hazard_event == "cascade":
            profile["last_event"] = {
                "name_sk": "💥 KASKÁDOVÁ HAVÁRIA — RBMK!",
                "name_en": "💥 CASCADE ACCIDENT — RBMK!",
                "desc_sk": f"Kombinácia faktorov spôsobila kaskádu. Offline {CASCADE_OFFLINE_H}h. Energia −80%.",
                "desc_en": f"Combined factors caused cascade. Offline {CASCADE_OFFLINE_H}h. Energy −80%.",
                "type": "neg", "ts": now,
            }
        elif hazard_event in ("breeder", "bn800"):
            h_str = str(int(active_dmg[-1]["expires_at"] - now) // 3600) + "h"
            profile["last_event"] = {
                "name_sk": f"💥 {sodium_lvl_sk} — {dmg_name}!",
                "name_en": f"💥 {sodium_lvl_en} — {dmg_name}!",
                "desc_sk": f"Reaktor offline {h_str}. Oprav cez /energy.",
                "desc_en": f"Reactor offline {h_str}. Repair via /energy.",
                "type": "neg", "ts": now,
            }
        else:
            profile["last_event"] = {
                "name_sk": f"💥 HAVÁRIA — {dmg_name}!",
                "name_en": f"💥 ACCIDENT — {dmg_name}!",
                "desc_sk": f"Reaktor poškodený. Offline {DAMAGE_OFFLINE_H}h. Oprav ho cez /{'/energy'}.",
                "desc_en": f"Reactor damaged. Offline {DAMAGE_OFFLINE_H}h. Repair via /energy.",
                "type": "neg", "ts": now,
            }

    profile["last_tick"] = now

    # ── Event trigger ────────────────────────────────────────────
    new_event = None
    if profile.get("plants"):
        last_event_t = profile.get("last_event_at", 0)
        if (now - last_event_t) > EVENT_COOLDOWN_H * 3600:
            if random.random() < EVENT_PROB:
                r = random.uniform(0, _EVENT_TOTAL_W)
                acc = 0
                for ev_cfg in ENERGY_EVENTS:
                    acc += ev_cfg["weight"]
                    if r <= acc:
                        new_event = ev_cfg
                        break
            if new_event:
                profile["last_event_at"] = now
                _apply_energy_event(new_event, profile, now)

    # ── Detekcia bankrotu ────────────────────────────────────────
    fuel_plants = [p for p in profile["plants"]
                   if PLANT_TYPES.get(p, {}).get("fuel_type")]
    all_fuel_zero = all(profile["fuel"].get(ft, 0) == 0
                        for ft in {"coal", "uranium"})
    if fuel_plants and all_fuel_zero and profile["energy"] == 0:
        if not profile.get("bankrupt_at"):
            profile["bankrupt_at"] = now
        elif now - profile["bankrupt_at"] > BANKRUPT_GRACE_H * 3600:
            _trigger_bankruptcy(uname_upper, profile)
    else:
        profile.pop("bankrupt_at", None)

    data[uname_upper] = profile
    save_jf(KB_ENERGY, data)
    return profile


def _get_commodity_stock(profile, source):
    """Vráti zásobu komodity podľa source kľúča."""
    if source == "energy":
        return float(profile.get("energy", 0.0))
    if source.startswith("fuel_"):
        return float(profile.get("fuel", {}).get(source[5:], 0.0))
    if source.startswith("commodity_"):
        return float(profile.get("commodities", {}).get(source[10:], 0.0))
    return 0.0


def _set_commodity_stock(profile, source, value):
    """Nastav zásobu komodity podľa source kľúča."""
    value = max(0.0, round(float(value), 2))
    if source == "energy":
        profile["energy"] = min(value, MAX_ENERGY)
    elif source.startswith("fuel_"):
        profile.setdefault("fuel", {})[source[5:]] = value
    elif source.startswith("commodity_"):
        profile.setdefault("commodities", {})[source[10:]] = value
    return profile

def _get_market_prices():
    """
    Načítaj kb_market.json, aplikuj exponenciálnu reverziu k základnej cene
    a ulož aktualizované ceny. Vráti dict {item_id: {b, s, base_b, base_s}}.
    """
    now = time.time()
    raw = load_jf(KB_MARKET, {})
    prices = {}
    new_raw = {}
    for item in NPC_MARKET:
        cid = item["id"]
        dyn = MARKET_DYN.get(cid, {})
        entry = raw.get(cid, {})
        ts = entry.get("ts", now)
        elapsed_hrs = max(0.0, (now - ts) / 3600.0)
        decay = math.exp(-dyn.get("rev", 0.1) * elapsed_hrs)
        base_b = item["npc_buys"]
        base_s = item["npc_sells"]

        if base_b is not None:
            cur_b = float(entry.get("b") or base_b)
            new_b = round(
                max(dyn.get("min_b", 1),
                    min(dyn.get("max_b", base_b * 5),
                        base_b + (cur_b - base_b) * decay)), 1)
        else:
            new_b = None

        if base_s is not None:
            cur_s = float(entry.get("s") or base_s)
            new_s = round(
                max(dyn.get("min_s", 1),
                    min(dyn.get("max_s", base_s * 5),
                        base_s + (cur_s - base_s) * decay)), 1)
        else:
            new_s = None

        new_raw[cid] = {"b": new_b, "s": new_s, "ts": now}
        prices[cid] = {"b": new_b, "s": new_s, "base_b": base_b, "base_s": base_s}

    save_jf(KB_MARKET, new_raw)
    return prices


def _apply_price_impact(item_id, qty, direction):
    """
    Zaznamenaj cenový dopad obchodu do kb_market.json.
    direction='sell' → hráč predáva (prebytok ponuky → cena klesá)
    direction='buy'  → hráč kupuje (dopyt → cena stúpa)
    """
    now = time.time()
    dyn = MARKET_DYN.get(item_id, {})
    liq = max(1, dyn.get("liq", 100))
    raw = load_jf(KB_MARKET, {})
    entry = dict(raw.get(item_id, {}))
    item = next((i for i in NPC_MARKET if i["id"] == item_id), {})
    base_b = item.get("npc_buys")
    base_s = item.get("npc_sells")
    impact = qty / liq  # bezrozmerný tlak

    if direction == "sell":
        # predaj → ponuka rastie → kúpna cena klesá, predajná mierne klesá
        if base_b is not None:
            cur = float(entry.get("b") or base_b)
            entry["b"] = round(max(dyn.get("min_b", 1), cur * (1.0 - impact * 0.30)), 1)
        if base_s is not None:
            cur = float(entry.get("s") or base_s)
            entry["s"] = round(max(dyn.get("min_s", 1), cur * (1.0 - impact * 0.10)), 1)
    else:
        # nákup → dopyt rastie → predajná cena stúpa, kúpna mierne stúpa
        if base_s is not None:
            cur = float(entry.get("s") or base_s)
            entry["s"] = round(min(dyn.get("max_s", base_s * 5), cur * (1.0 + impact * 0.30)), 1)
        if base_b is not None:
            cur = float(entry.get("b") or base_b)
            entry["b"] = round(min(dyn.get("max_b", base_b * 5), cur * (1.0 + impact * 0.10)), 1)

    entry["ts"] = now
    raw[item_id] = entry
    save_jf(KB_MARKET, raw)


def _estimate_company_value(profile):
    """Odhadne hodnotu firmy pre bankrotovú ponuku."""
    total = sum(PLANT_TYPES.get(p, {}).get("build_cost", 0) for p in profile.get("plants", []))
    total += sum(MINE_TYPES.get(m, {}).get("build_cost", 0) for m in profile.get("mines", []))
    fuel = profile.get("fuel", {})
    comm = profile.get("commodities", {})
    raw  = profile.get("raw_materials", {})
    total += int(fuel.get("coal", 0)) * 45
    total += int(fuel.get("uranium", 0)) * 1300
    total += int(fuel.get("helium", 0)) * 10000
    total += int(comm.get("oil", 0)) * 58
    total += int(comm.get("gold", 0)) * 500
    total += int(comm.get("platinum", 0)) * 1200
    total += int(raw.get("uranium_raw", 0)) * 45
    total += int(profile.get("energy", 0)) * 8
    return max(500, total)


def _trigger_bankruptcy(uname_upper, profile):
    """
    Automaticky vytvorí bankrotovú aukciu pre mŕtvu firmu.
    Vymaže energetický profil hráča, vloží snímku do bankrupt_lots.
    """
    now = time.time()
    auc = load_jf(KB_AUCTIONS, {"lots": [], "pending": {}, "company_lots": [],
                                 "company_pending": {}, "bankrupt_lots": [], "bankrupt_pending": {}})
    blots = auc.get("bankrupt_lots", [])

    # Nespúšťaj ak už má aktívny bankrot
    if any(l["seller"] == uname_upper for l in blots):
        return

    snap = {
        "plants":      list(profile.get("plants", [])),
        "energy":      round(profile.get("energy", 0), 1),
        "fuel":        dict(profile.get("fuel", {})),
        "commodities": dict(profile.get("commodities", {})),
    }
    val = _estimate_company_value(profile)
    start_bid = max(500, round(val * 0.40))
    lot_id = f"bankrupt_{uname_upper}_{int(now*1000) % 10**9}"
    blots.append({
        "id":          lot_id,
        "seller":      uname_upper,
        "snapshot":    snap,
        "start_bid":   start_bid,
        "current_bid": start_bid,
        "bidder":      None,
        "ends_at":     now + BANKRUPT_DUR_H * 3600,
        "est_value":   val,
    })
    auc["bankrupt_lots"] = blots
    save_jf(KB_AUCTIONS, auc)

    # Reset energetického profilu hráča
    profile["plants"] = []
    profile["energy"] = 0.0
    profile["fuel"] = {"coal": 0.0, "uranium": 0.0}
    profile["commodities"] = {"oil": 0.0, "gold": 0.0}
    profile.pop("bankrupt_at", None)
    print(f"[bankrot] {uname_upper} — auto-bankrotová aukcia vytvorená, štartovacia ponuka {start_bid:,} CR")


def _bankrupt_tick():
    """Expirácia bankrotových lotov → bankrupt_pending."""
    now = time.time()
    data = load_jf(KB_AUCTIONS, {"lots": [], "pending": {}, "company_lots": [],
                                  "company_pending": {}, "bankrupt_lots": [], "bankrupt_pending": {}})
    blots = data.get("bankrupt_lots", [])
    bpending = data.get("bankrupt_pending", {})
    changed = False
    active = []
    for lot in blots:
        if now < lot["ends_at"]:
            active.append(lot)
            continue
        bidder = lot.get("bidder")
        if bidder:
            bpending.setdefault(bidder, []).append({
                "lot_id":    lot["id"],
                "seller":    lot["seller"],
                "snapshot":  lot["snapshot"],
                "paid":      lot["current_bid"],
                "est_value": lot.get("est_value", lot["start_bid"]),
            })
        changed = True
    data["bankrupt_lots"] = active
    data["bankrupt_pending"] = bpending
    if changed:
        save_jf(KB_AUCTIONS, data)
    return data


def _auction_tick():
    """
    Expirácia ukončených lotov → pending výhry, generovanie nových lotov.
    Vráti {'lots': [...], 'pending': {UNAME: [...]}}.
    """
    now = time.time()
    data = load_jf(KB_AUCTIONS, {"lots": [], "pending": {}})
    lots = data.get("lots", [])
    pending = data.get("pending", {})
    changed = False

    # 1. Spracuj expirované loty
    active = []
    for lot in lots:
        if now < lot["ends_at"]:
            active.append(lot)
            continue
        # lot skončil
        bidder = lot.get("bidder")
        if bidder:
            pending.setdefault(bidder, []).append({
                "lot_id":    lot["id"],
                "commodity": lot["commodity"],
                "icon":      lot["icon"],
                "name_sk":   lot["name_sk"],
                "name_en":   lot["name_en"],
                "unit_sk":   lot["unit_sk"],
                "unit_en":   lot["unit_en"],
                "qty":       lot["qty"],
                "source":    lot["source"],
                "paid":      lot["current_bid"],
            })
        changed = True  # lot zmiznul z active → potrebujeme uložiť

    # 2. Doplň loty do MAX_ACTIVE_LOTS
    active_commodities = {l["commodity"] for l in active}
    for cfg in AUCTION_LOTS_CFG:
        if len(active) >= MAX_ACTIVE_LOTS:
            break
        if cfg["commodity"] in active_commodities:
            continue
        uid = f"{cfg['commodity']}_{int(now*1000) % 10**9}"
        active.append({
            "id":        uid,
            "commodity": cfg["commodity"],
            "icon":      cfg["icon"],
            "name_sk":   cfg["name_sk"],
            "name_en":   cfg["name_en"],
            "unit_sk":   cfg["unit_sk"],
            "unit_en":   cfg["unit_en"],
            "qty":       cfg["qty"],
            "source":    cfg["source"],
            "start_bid": cfg["start_bid"],
            "current_bid": cfg["start_bid"],
            "bidder":    None,
            "ends_at":   now + cfg["duration_min"] * 60,
        })
        active_commodities.add(cfg["commodity"])
        changed = True

    data["lots"] = active
    data["pending"] = pending
    if changed:
        save_jf(KB_AUCTIONS, data)
    return data


_USER_FIELD_DEFAULTS = {
    "is_admin":     False,
    "is_tester":    False,
    "banned_until": None,
    "special_ranks": [],
    "score":        0,
    "games_played": 0,
    "kb_sessions":  0,
}

def _migrate_users():
    """Pridaj chýbajúce polia starým účtom (z v1/v2 keď polia ešte neexistovali)."""
    users = load_users()
    changed = False
    for u in users.values():
        for field, default in _USER_FIELD_DEFAULTS.items():
            if field not in u:
                u[field] = default
                changed = True
    if changed:
        save_users(users)
        print("[migrate] user fields doplnené pre staré účty")


def _migrate_career_keys():
    """Normalizuj kľúče kb_career.json na UPPERCASE (staré verzie mohli ukladať inak)."""
    career = load_jf(KB_CAREER, {})
    new_career = {}
    changed = False
    for key, val in career.items():
        up = key.upper()
        if up != key:
            changed = True
        if up not in new_career:
            new_career[up] = val
        else:
            # zlúč: zachovaj vyššie career_cr
            if val.get("career_cr", 0) > new_career[up].get("career_cr", 0):
                new_career[up] = val
                changed = True
    if changed:
        save_jf(KB_CAREER, new_career)
        print("[migrate] kb_career kľúče normalizované na uppercase")


def _migrate_saves_keys():
    """Normalizuj používateľské kľúče v kb_saves.json na UPPERCASE."""
    saves = load_jf(KB_SAVES, {})
    new_saves = {}
    changed = False
    for key, slots in saves.items():
        up = key.upper()
        if up != key:
            changed = True
        if up not in new_saves:
            new_saves[up] = slots
        else:
            # zlúč sloty — zachovaj novší ts
            for slot, data in slots.items():
                existing = new_saves[up].get(slot)
                if not existing or (data.get("ts", 0) > existing.get("ts", 0)):
                    new_saves[up][slot] = data
                    changed = True
    if changed:
        save_jf(KB_SAVES, new_saves)
        print("[migrate] kb_saves kľúče normalizované na uppercase")


def _migrate_saves():
    saves = load_jf(KB_SAVES, {})
    if not saves or any(not k.isdigit() for k in saves.keys()):
        return
    new_saves = {}
    for slot, data in saves.items():
        uname = data.get("username", "UNKNOWN").upper()
        new_saves.setdefault(uname, {})[slot] = data
    save_jf(KB_SAVES, new_saves)
    print(f"[migrate] kb_saves.json migrated: {list(new_saves.keys())}")

_migrate_saves()
_migrate_users()
_migrate_career_keys()
_migrate_saves_keys()
_seed_default_user()

def _seed_admin_users():
    """
    Env premenná ADMIN_USERS = čiarkou oddelené mená userov, ktorí majú mať is_admin=True.
    Napr.: ADMIN_USERS=matus,tomas
    Spúšťa sa pri každom štarte — takto admini pretrvajú aj po redeploy.
    """
    raw = os.environ.get("ADMIN_USERS", "").strip()
    if not raw:
        return
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        return
    users = load_users()
    changed = False
    for name in names:
        match = next((k for k in users if k.lower() == name.lower()), None)
        if match and not users[match].get("is_admin"):
            users[match]["is_admin"] = True
            changed = True
            print(f"[seed] is_admin=True nastavené pre '{match}' (z ADMIN_USERS env var).")
    if changed:
        save_users(users)

_seed_admin_users()


def _seed_tester_users():
    """
    Env premenná TESTER_USERS = čiarkou oddelené mená testerov (is_tester=True).
    Napr.: TESTER_USERS=matus,beta1
    Spúšťa sa pri každom štarte — flag pretrváva aj po redeploy bez persistent disku.
    """
    raw = os.environ.get("TESTER_USERS", "").strip()
    if not raw:
        return
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        return
    users = load_users()
    changed = False
    for name in names:
        match = next((k for k in users if k.lower() == name.lower()), None)
        if match and not users[match].get("is_tester"):
            users[match]["is_tester"] = True
            changed = True
            print(f"[seed] is_tester=True nastavené pre '{match}' (z TESTER_USERS env var).")
    if changed:
        save_users(users)

_seed_tester_users()


def _seed_special_ranks():
    """
    Env premenná SPECIAL_RANKS = username:Rank1,Rank2;username2:Rank3
    Napr.: SPECIAL_RANKS=matus:Owner,Pro;beta:Tester
    Vždy nastaví presne tieto ranky — prepisuje existujúce.
    """
    raw = os.environ.get("SPECIAL_RANKS", "").strip()
    if not raw:
        return
    users = load_users()
    changed = False
    for entry in raw.split(";"):
        entry = entry.strip()
        if ":" not in entry:
            continue
        uname_raw, ranks_raw = entry.split(":", 1)
        uname_raw = uname_raw.strip()
        ranks = [r.strip() for r in ranks_raw.split(",") if r.strip()][:2]
        match = next((k for k in users if k.lower() == uname_raw.lower()), None)
        if match and users[match].get("special_ranks") != ranks:
            users[match]["special_ranks"] = ranks
            users[match].pop("special_rank", None)
            changed = True
            print(f"[seed] special_ranks={ranks} pre '{match}' (z SPECIAL_RANKS env var).")
    if changed:
        save_users(users)


def _seed_energy_profile():
    """
    Env premenná ENERGY_STARTER = username:plant1,plant2;fuel_coal:N;fuel_uranium:N
    Napr.: ENERGY_STARTER=matus:solar,solar,coal;fuel_coal:80;fuel_uranium:5
    Seed sa aplikuje IBA ak má hráč prázdny profil (žiadne elektrárne).
    """
    raw = os.environ.get("ENERGY_STARTER", "").strip()
    if not raw:
        return
    parts = [p.strip() for p in raw.split(";")]
    if not parts or ":" not in parts[0]:
        return

    uname_raw, plants_raw = parts[0].split(":", 1)
    uname_raw = uname_raw.strip()
    users = load_users()
    match = next((k for k in users if k.lower() == uname_raw.lower()), None)
    if not match:
        return

    uname_upper = match.upper()
    energy_data = load_jf(KB_ENERGY, {})
    profile = energy_data.get(uname_upper, {})

    if profile.get("plants"):
        return  # Profil existuje, nič nemeníme

    plants = [p.strip() for p in plants_raw.split(",") if p.strip() in PLANT_TYPES]
    fuel = {"coal": 0.0, "uranium": 0.0}
    for part in parts[1:]:
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k = k.strip()
        if k == "fuel_coal":
            fuel["coal"] = float(v.strip())
        elif k == "fuel_uranium":
            fuel["uranium"] = float(v.strip())

    profile["plants"] = plants
    profile["fuel"] = fuel
    profile.setdefault("energy", 0.0)
    profile.setdefault("commodities", {"oil": 0.0, "gold": 0.0})
    profile.setdefault("last_tick", time.time())
    profile.setdefault("active_events", [])
    profile.setdefault("last_event", None)
    profile.setdefault("last_event_at", 0.0)
    energy_data[uname_upper] = profile
    save_jf(KB_ENERGY, energy_data)
    print(f"[seed] energy profil pre '{match}': plants={plants}, fuel={fuel}")


_seed_special_ranks()
_seed_energy_profile()


def get_sp_ranks(user_dict):
    """Vráti list špeciálnych rankov (max 2). Kompatibilné so starým special_rank stringom."""
    sr = user_dict.get("special_ranks")
    if isinstance(sr, list):
        return [s for s in sr if s][:2]
    old = user_dict.get("special_rank")
    return [old] if old else []

RANKS = [
    (1,  "Baník",        0),
    (2,  "Prospektér",   100_000),
    (3,  "Veterán",      500_000),
    (4,  "Veliteľ",      2_000_000),
    (5,  "Legenda",      10_000_000),
    (6,  "Elita",        25_000_000),
    (7,  "Majster",      75_000_000),
    (8,  "Hrdina",       200_000_000),
    (9,  "Šampión",      500_000_000),
    (10, "Vesmírny Boh", 1_000_000_000),
]

# Špeciálne tituly, ktoré môže nastaviť IBA owner (nie admin)
OWNER_ONLY_RANKS = {"Owner", "Creator", "Dev", "God", "Zakladateľ"}
# Tituly, ktoré môžu nastavovať admini (ale nie owner-only tituly)
ADMIN_RANKS_HINT = ["Tester", "VIP", "Veteran", "Pilot"]

def kb_rank(cr):
    for thr, r, name in [
        (10_000_000, 5, "Legenda"),
        (2_000_000,  4, "Veliteľ"),
        (500_000,    3, "Veterán"),
        (100_000,    2, "Prospektér"),
        (0,          1, "Baník"),
    ]:
        if cr >= thr:
            return r, name
    return 1, "Baník"


def send_notification(uname, text, from_role="owner"):
    """Uloží notifikáciu do users[uname]['notifications']. Ak má email, pošle aj mail."""
    users = load_users()
    # Presná zhoda, potom case-insensitive fallback
    key = uname if uname in users else next(
        (k for k in users if k.lower() == uname.lower()), None)
    if key is None:
        print(f"[notify] uname '{uname}' not found in users")
        return
    notifs = users[key].setdefault("notifications", [])
    notifs.append({"text": text, "from": from_role,
                   "ts": datetime.now().strftime("%Y-%m-%d %H:%M"), "read": False})
    save_users(users)
    email = users[key].get("email", "").strip()
    if email:
        _send_email(email, f"[Kozmické Bane] Správa od {from_role}", text)


def _send_email(to, subject, body):
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", 587))
    user = os.environ.get("SMTP_USER", "")
    pw   = os.environ.get("SMTP_PASS", "")
    frm  = os.environ.get("SMTP_FROM", user)
    if not host or not user or not pw:
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = frm
        msg["To"]      = to
        with smtplib.SMTP(host, port, timeout=8) as s:
            s.starttls()
            s.login(user, pw)
            s.sendmail(frm, [to], msg.as_string())
    except Exception as ex:
        print(f"[email] chyba pri odoslaní: {ex}")


# ── Login page HTML ────────────────────────────────────────────────────────

LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="__LANG__">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KOZMICKÉ BANE v4.7 — Login</title>
<style>html,body{background:#000;}</style>
<style>
@import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{
  background:#000;
  color:#ffb000;
  font-family:'VT323',monospace;
  min-height:100vh;
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  padding:20px;
  background-image:
    radial-gradient(ellipse at 20% 20%, rgba(255,176,0,0.04) 0%, transparent 50%),
    radial-gradient(ellipse at 80% 80%, rgba(56,209,255,0.03) 0%, transparent 50%);
}
pre.logo{
  color:#ffb000;
  font-size:0.52em;
  line-height:1.25;
  margin-bottom:6px;
  text-align:center;
  text-shadow:0 0 12px #ffb000aa;
}
.subtitle{
  color:#a07000;
  font-size:1.1em;
  margin-bottom:28px;
  letter-spacing:0.12em;
}
.card{
  background:#0b0900;
  border:1px solid #ffb000;
  box-shadow:0 0 30px rgba(255,176,0,0.15);
  width:100%;
  max-width:440px;
  padding:26px 30px 30px;
}
.tabs{display:flex;gap:6px;margin-bottom:22px;}
.tab{
  flex:1;
  padding:7px 4px;
  background:#0b0900;
  border:1px solid #3a2800;
  color:#a07000;
  cursor:pointer;
  font-family:'VT323',monospace;
  font-size:1em;
  transition:all 0.15s;
}
.tab:hover,.tab.on{border-color:#ffb000;color:#ffb000;background:#1a1200;}
.panel{display:none;}
.panel.on{display:block;}
label{display:block;font-size:0.9em;color:#a07000;margin-bottom:3px;}
input[type=text],input[type=password]{
  width:100%;
  padding:7px 10px;
  background:#000;
  border:1px solid #3a2800;
  color:#fff8e0;
  font-family:'VT323',monospace;
  font-size:1.05em;
  outline:none;
  margin-bottom:12px;
  transition:border-color 0.15s;
}
input:focus{border-color:#ffb000;}
.btn{
  width:100%;
  padding:9px;
  background:#0f0c00;
  border:1px solid #ffb000;
  color:#ffb000;
  cursor:pointer;
  font-family:'VT323',monospace;
  font-size:1.1em;
  font-weight:bold;
  margin-top:4px;
  transition:all 0.15s;
  letter-spacing:0.05em;
}
.btn:hover{background:#3a2800;color:#fff8e0;}
.err{color:#ff3a3a;font-size:0.9em;margin-bottom:14px;border-left:2px solid #ff3a3a;padding-left:8px;}
.ok{color:#39ff6a;font-size:0.9em;margin-bottom:14px;border-left:2px solid #39ff6a;padding-left:8px;}
.hint{color:#3a2800;font-size:0.8em;margin-top:18px;text-align:center;}
.lang-toggle{position:fixed;top:10px;right:14px;font-family:'VT323',monospace;font-size:1em;}
.lang-toggle a{color:#a07000;text-decoration:none;padding:2px 6px;border:1px solid #3a2800;}
.lang-toggle a:hover,.lang-toggle a.active{color:#ffb000;border-color:#ffb000;}
</style>
</head>
<body>
<div class="lang-toggle">
  <a href="/lang/sk"__SK_ACTIVE__>SK</a><a href="/lang/en"__EN_ACTIVE__>EN</a>
</div>
<pre class="logo">
 ██╗  ██╗ ██████╗ ███████╗███╗   ███╗██╗ ██████╗██╗  ██╗███████╗
 ██║ ██╔╝██╔═══██╗╚══███╔╝████╗ ████║██║██╔════╝██║ ██╔╝██╔════╝
 █████╔╝ ██║   ██║  ███╔╝ ██╔████╔██║██║██║     █████╔╝ █████╗
 ██╔═██╗ ██║   ██║ ███╔╝  ██║╚██╔╝██║██║██║     ██╔═██╗ ██╔══╝
 ██║  ██╗╚██████╔╝███████╗██║ ╚═╝ ██║██║╚██████╗██║  ██╗███████╗
 ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝     ╚═╝╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝</pre>
<div class="subtitle">B A N E &nbsp; v4.7 &mdash; WEB EDITION</div>

<div class="card">
  <div class="tabs">
    <button class="tab __ON_LOGIN__" onclick="show('login',this)">&#128272; __TAB_LOGIN__</button>
    <button class="tab __ON_REG__"   onclick="show('register',this)">&#128221; __TAB_REG__</button>
    <button class="tab __ON_RESET__" onclick="show('reset',this)">&#128273; __TAB_RESET__</button>
  </div>

  <div id="login" class="panel __ON_LOGIN__">
    __FLASH_LOGIN__
    <form method="POST" action="/login">
      <label>__LBL_USERNAME__</label>
      <input type="text" name="username" autocomplete="username" autofocus>
      <label>__LBL_PASSWORD__</label>
      <input type="password" name="password" autocomplete="current-password">
      <button class="btn" type="submit">&#9654; &nbsp; __BTN_SIGNIN__</button>
    </form>
  </div>

  <div id="register" class="panel __ON_REG__">
    __FLASH_REG__
    <form method="POST" action="/register">
      <label>__LBL_USERNAME__</label>
      <input type="text" name="username">
      <label>__LBL_PASSWORD_HINT__</label>
      <input type="password" name="password">
      <label>__LBL_CONFIRM_PW__</label>
      <input type="password" name="password2">
      <button class="btn" type="submit">&#10003; &nbsp; __BTN_REGISTER__</button>
    </form>
  </div>

  <div id="reset" class="panel __ON_RESET__">
    __FLASH_RESET__
    <form method="POST" action="/reset">
      <label>__LBL_USERNAME__</label>
      <input type="text" name="username">
      <label>__LBL_REGDATE__</label>
      <input type="text" name="reg_date" placeholder="__PLACEHOLDER_DATE__">
      <label>__LBL_NEWPW__</label>
      <input type="password" name="new_password">
      <label>__LBL_CONFIRM_PW__</label>
      <input type="password" name="new_password2">
      <button class="btn" type="submit">&#128273; &nbsp; __BTN_CHANGEPW__</button>
    </form>
  </div>

  <p class="hint">KOZMICKÉ BANE v4.7 &mdash; Web Edition &mdash; localhost:__PORT__</p>
</div>

<script>
function show(id, btn) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('on'));
  document.getElementById(id).classList.add('on');
  btn.classList.add('on');
}
</script>
</body>
</html>
"""


def render_login(tab="login", err_login="", err_reg="", err_reset="",
                 ok_login="", ok_reg=""):
    def flash(err, ok):
        if err:
            return f'<p class="err">&#10007; {err}</p>'
        if ok:
            return f'<p class="ok">&#10003; {ok}</p>'
        return ""

    on_login = "on" if tab == "login"    else ""
    on_reg   = "on" if tab == "register" else ""
    on_reset = "on" if tab == "reset"    else ""

    lang = session.get('lang', 'sk')
    sk_active = ' class="active"' if lang == 'sk' else ''
    en_active = ' class="active"' if lang == 'en' else ''

    return (LOGIN_HTML
        .replace("__LANG__",        lang)
        .replace("__SK_ACTIVE__",   sk_active)
        .replace("__EN_ACTIVE__",   en_active)
        .replace("__ON_LOGIN__",    on_login)
        .replace("__ON_REG__",      on_reg)
        .replace("__ON_RESET__",    on_reset)
        .replace("__FLASH_LOGIN__", flash(err_login, ok_login))
        .replace("__FLASH_REG__",   flash(err_reg, ok_reg))
        .replace("__FLASH_RESET__", flash(err_reset, ""))
        .replace("__PORT__",        str(PORT))
        .replace("__TAB_LOGIN__",   L("Prihlásenie", "Login"))
        .replace("__TAB_REG__",     L("Registrácia", "Registration"))
        .replace("__TAB_RESET__",   L("Reset hesla", "Reset Password"))
        .replace("__LBL_USERNAME__",    L("MENO", "USERNAME"))
        .replace("__LBL_PASSWORD__",    L("HESLO", "PASSWORD"))
        .replace("__LBL_PASSWORD_HINT__",
                 L('HESLO &nbsp;<span style="color:#555;font-size:0.85em">(min. 6 znakov, aspoň 1 číslica)</span>',
                   'PASSWORD &nbsp;<span style="color:#555;font-size:0.85em">(min. 6 chars, at least 1 digit)</span>'))
        .replace("__LBL_CONFIRM_PW__",  L("POTVRĎ HESLO", "CONFIRM PASSWORD"))
        .replace("__BTN_SIGNIN__",      L("PRIHLÁSIŤ SA", "SIGN IN"))
        .replace("__BTN_REGISTER__",    L("VYTVORIŤ ÚČET", "CREATE ACCOUNT"))
        .replace("__LBL_REGDATE__",
                 L('DÁTUM REGISTRÁCIE &nbsp;<span style="color:#555;font-size:0.85em">(YYYY-MM-DD)</span>',
                   'REGISTRATION DATE &nbsp;<span style="color:#555;font-size:0.85em">(YYYY-MM-DD)</span>'))
        .replace("__PLACEHOLDER_DATE__", L("napr. 2024-03-10", "e.g. 2024-03-10"))
        .replace("__LBL_NEWPW__",       L("NOVÉ HESLO", "NEW PASSWORD"))
        .replace("__BTN_CHANGEPW__",    L("ZMENIŤ HESLO", "CHANGE PASSWORD"))
    )


# ── Web bridge JS injected into kozmicke_bane.html ────────────────────────

WEB_BRIDGE = """\
<script>
(function(){
  // Flask-backed API — replaces pywebview.api / pyapi
  var webApi = {
    save_game: function(slot, data_json) {
      return fetch('/api/save_game', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({slot: slot, data: JSON.parse(data_json)})
      }).then(function(){return true;});
    },
    load_game: function(slot) {
      return fetch('/api/load_game', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({slot: slot})
      }).then(function(r){return r.json();})
        .then(function(d){return d ? JSON.stringify(d) : 'null';});
    },
    delete_save: function(slot) {
      return fetch('/api/delete_save', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({slot: slot})
      }).then(function(){return true;});
    },
    get_startup_data: function() {
      return fetch('/api/get_startup_data')
        .then(function(r){return r.text();});
    },
    add_leaderboard: function(entry_json) {
      return fetch('/api/add_leaderboard', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: entry_json
      }).then(function(){return true;});
    },
    get_leaderboard: function() {
      return fetch('/api/get_leaderboard').then(function(r){return r.text();});
    },
    clear_leaderboard: function() {
      return fetch('/api/clear_leaderboard', {method:'POST',
        headers:{'Content-Type':'application/json'}, body:'{}'
      }).then(function(){return true;});
    },
    report_session_end: function(credits_earned, mined, win) {
      return fetch('/api/report_session_end', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({credits_earned: credits_earned, mined: mined, win: win})
      }).then(function(r){return r.text();});
    },
    get_career: function() {
      return fetch('/api/get_career').then(function(r){return r.text();});
    },
    minimize_window: function(){},
    toggle_fullscreen: function(){}
  };

  window.pyapi = webApi;
  window.pywebview = {api: webApi};

  // Bridge — rovnaka logika ako BRIDGE_JS v app.py
  if (!window.__KB_BRIDGE_LOADED__) {
    window.__KB_BRIDGE_LOADED__ = true;

    function getApi() {
      if (window.pywebview && window.pywebview.api) return window.pywebview.api;
      if (window.pyapi) return window.pyapi;
      return null;
    }

    var _t = 0;
    function tryInit() {
      var api = getApi();
      if (api) {
        api.get_startup_data().then(function(raw) {
          try {
            var d = JSON.parse(raw);
            if (d.saves && Object.keys(d.saves).length > 0)
              localStorage.setItem('kb_saves', JSON.stringify(d.saves));
            if (d.leaderboard && d.leaderboard.length > 0)
              localStorage.setItem('kb_leaderboard', JSON.stringify(d.leaderboard));
          } catch(e) {}
        });
      } else if (_t++ < 50) { setTimeout(tryInit, 100); }
    }
    setTimeout(tryInit, 200);

    var _origSet = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) {
      _origSet.call(this, key, value);
      var api = getApi(); if (!api) return;
      if (key === 'kb_saves') {
        try {
          var saves = JSON.parse(value);
          Object.keys(saves).forEach(function(s) {
            api.save_game(s, JSON.stringify(saves[s]));
          });
        } catch(e) {}
      }
      if (key === 'kb_leaderboard') {
        try {
          JSON.parse(value).forEach(function(e) {
            api.add_leaderboard(JSON.stringify(e));
          });
        } catch(e) {}
      }
    };

    var _origRemove = Storage.prototype.removeItem;
    Storage.prototype.removeItem = function(key) {
      _origRemove.call(this, key);
      var api = getApi(); if (!api) return;
      if (key === 'kb_saves') { for (var i=1;i<=4;i++) api.delete_save(i); }
      if (key === 'kb_leaderboard') { api.clear_leaderboard(); }
    };
  }

  console.log('[KB Web Bridge v3] Active.');

  // Tlacidlo "← Lobby" fixne v pravom hornom rohu
  var lobbyBtn = document.createElement('a');
  lobbyBtn.href = '/lobby';
  lobbyBtn.innerText = '\u2190 Lobby';
  lobbyBtn.style.cssText = (
    'position:fixed;top:8px;left:8px;z-index:99999;' +
    'background:#0b0900;border:1px solid #a07000;' +
    'color:#a07000;font-family:"VT323",monospace;font-size:1rem;' +
    'padding:3px 10px;text-decoration:none;cursor:pointer;' +
    'transition:all 0.15s;'
  );
  lobbyBtn.onmouseover = function() {
    this.style.borderColor = '#ffb000';
    this.style.color = '#ffb000';
  };
  lobbyBtn.onmouseout = function() {
    this.style.borderColor = '#a07000';
    this.style.color = '#a07000';
  };
  document.addEventListener('DOMContentLoaded', function() {
    document.body.appendChild(lobbyBtn);
  });
})();
</script>
"""


# ── Lobby HTML ────────────────────────────────────────────────────────────

LOBBY_CSS = """
<style>html,body{background:#000;}</style>
<style>
@import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#000;color:#ffb000;font-family:'VT323',monospace;min-height:100vh;
  display:flex;flex-direction:column;align-items:center;padding:20px 20px 40px;}
pre.logo{color:#ffb000;font-size:0.52em;line-height:1.25;margin:18px 0 4px;
  text-align:center;text-shadow:0 0 12px #ffb000aa;}
.subtitle{color:#a07000;font-size:1em;margin-bottom:6px;letter-spacing:0.1em;text-align:center;}
.pilot{color:#fff8e0;font-size:1.3em;margin-bottom:18px;text-align:center;letter-spacing:0.08em;}
.card{background:#0b0900;border:1px solid #ffb000;box-shadow:0 0 25px rgba(255,176,0,0.12);
  width:100%;max-width:700px;padding:20px 26px 24px;margin-bottom:14px;}
.card-title{color:#a07000;font-size:1em;border-bottom:1px solid #3a2800;
  padding-bottom:6px;margin-bottom:14px;letter-spacing:0.08em;}
.btn{display:block;width:100%;padding:11px;background:#0f0c00;border:1px solid #ffb000;
  color:#ffb000;cursor:pointer;font-family:'VT323',monospace;font-size:1.2em;
  font-weight:bold;text-align:center;text-decoration:none;letter-spacing:0.06em;
  transition:all 0.15s;margin-bottom:6px;}
.btn:hover{background:#3a2800;color:#fff8e0;}
.btn-green{border-color:#39ff6a;color:#39ff6a;}
.btn-green:hover{background:#003a10;}
.btn-dim{border-color:#3a2800;color:#3a2800;cursor:default;}
.btn-dim:hover{background:#0b0900;color:#3a2800;}
.btn-red{border-color:#ff3a3a;color:#ff3a3a;font-size:1em;padding:6px 12px;
  width:auto;display:inline-block;}
.btn-red:hover{background:#3a0000;}
.btn-logout{border-color:#555;color:#555;font-size:0.95em;}
.btn-logout:hover{background:#1a1a1a;color:#aaa;}
.slot-row{display:flex;gap:8px;align-items:center;margin-bottom:6px;}
.slot-info{flex:1;padding:8px 10px;background:#0f0c00;border:1px solid #3a2800;
  color:#a07000;font-family:'VT323',monospace;font-size:0.95em;}
.slot-info.used{border-color:#a07000;color:#fff8e0;}
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px 20px;}
.stat{font-size:0.95em;color:#a07000;} .stat span{color:#fff8e0;}
.lb-row{font-size:0.9em;color:#a07000;padding:2px 0;}
.lb-row span{color:#fff8e0;} .lb-row.me{color:#ffb000;}
.sep{border:none;border-top:1px solid #3a2800;margin:10px 0;}
</style>
"""

def fmt_date_ts(ts):
    try:
        from datetime import datetime as dt
        return dt.fromtimestamp(ts / 1000).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "–"

DEPTHS = {1: "Povrch", 2: "Litosféra", 3: "Hlboká", 4: "Magma", 5: "Jadro"}
DEPTHS_EN = {1: "Surface", 2: "Lithosphere", 3: "Deep", 4: "Magma", 5: "Core"}

def render_lobby(pilot):
    all_saves = load_jf(KB_SAVES, {})
    saves  = all_saves.get(pilot.upper(), {})
    career = load_jf(KB_CAREER, {})
    kb     = career.get(pilot.upper(), {})
    cr     = kb.get("career_cr", 0)
    r, rname = kb_rank(cr)
    rank_title = kb.get("rank_title", "")
    display_rank = rank_title if rank_title else rname
    users_db = load_users()
    u_data   = users_db.get(pilot, {})
    sp_ranks = get_sp_ranks(u_data)
    sp_stars = " ".join(f'<span style="color:#ffd700;text-shadow:0 0 8px #ffd700">&#9733;&nbsp;{s}</span>' for s in sp_ranks)

    # ── Notifikácie (správy od admina/ownera)
    notifs_html = ""
    raw_notifs = [n for n in u_data.get("notifications", []) if not n.get("read")]
    if raw_notifs:
        notif_items = "".join(
            f'<div style="margin-bottom:4px"><span style="color:#888;font-size:.82em">'
            f'[{n["ts"]} — {n["from"]}]</span><br>{n["text"]}</div>'
            for n in raw_notifs
        )
        notifs_html = (
            f'<div style="width:100%;max-width:700px;margin-bottom:10px;'
            f'background:#1a1200;border:1px solid #ffb000;padding:10px 14px;'
            f'font-family:\'VT323\',monospace;font-size:1em;color:#fff8e0">'
            f'<strong style="color:#ffb000">&#128276; {L("SPRÁVY","MESSAGES")} ({len(raw_notifs)})</strong>'
            f'<div style="margin-top:6px">{notif_items}</div>'
            f'<button onclick="fetch(\'/api/notifications_read\',{{method:\'POST\'}}).then(()=>location.reload())" '
            f'style="margin-top:8px;background:#3a2800;border:1px solid #ffb000;color:#ffb000;'
            f'font-family:\'VT323\',monospace;font-size:.95em;padding:2px 10px;cursor:pointer">'
            f'{L("Označiť ako prečítané","Mark as read")}</button>'
            f'</div>'
        )

    lang = session.get('lang', 'sk')
    depth_map = DEPTHS_EN if lang == 'en' else DEPTHS
    sk_active = ' class="active"' if lang == 'sk' else ''
    en_active = ' class="active"' if lang == 'en' else ''
    lang_toggle = (
        f'<div style="position:fixed;top:10px;right:14px;font-family:\'VT323\',monospace;font-size:1em;z-index:9999">'
        f'<a href="/lang/sk"{sk_active} style="color:#a07000;text-decoration:none;padding:2px 6px;border:1px solid #3a2800;">SK</a>'
        f'<a href="/lang/en"{en_active} style="color:#a07000;text-decoration:none;padding:2px 6px;border:1px solid #3a2800;">EN</a>'
        f'</div>'
    )

    # ── Header
    html  = f"<!DOCTYPE html><html lang='{lang}'><head><meta charset='UTF-8'>"
    html += f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
    html += f"<title>KOZMICKÉ BANE — Lobby</title>{LOBBY_CSS}"
    html += "<style>.lang-toggle a.active{color:#ffb000!important;border-color:#ffb000!important;}</style>"
    html += "</head><body>"
    html += lang_toggle
    html += """<pre class="logo">
 ██╗  ██╗ ██████╗ ███████╗███╗   ███╗██╗ ██████╗██╗  ██╗███████╗
 ██║ ██╔╝██╔═══██╗╚══███╔╝████╗ ████║██║██╔════╝██║ ██╔╝██╔════╝
 █████╔╝ ██║   ██║  ███╔╝ ██╔████╔██║██║██║     █████╔╝ █████╗
 ██╔═██╗ ██║   ██║ ███╔╝  ██║╚██╔╝██║██║██║     ██╔═██╗ ██╔══╝
 ██║  ██╗╚██████╔╝███████╗██║ ╚═╝ ██║██║╚██████╗██║  ██╗███████╗
 ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝     ╚═╝╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝</pre>"""
    html += f'<div class="subtitle">B A N E &nbsp; v4.7 &mdash; CAREER EDITION</div>'
    pilot_line = f'PILOT: {pilot.upper()} &nbsp;|&nbsp; {L("RANG","RANK")}: {display_rank} &nbsp;|&nbsp; {cr:,} CR'
    if sp_stars:
        pilot_line += f' &nbsp;|&nbsp; {sp_stars}'
    html += f'<div class="pilot">{pilot_line}</div>'
    if notifs_html:
        html += notifs_html

    # ── Rank milestone bar
    _rank_thresholds = [(t, name, min_cr) for t, name, min_cr in [
        (1,"Baník",0),(2,"Prospektér",100_000),(3,"Veterán",500_000),
        (4,"Veliteľ",2_000_000),(5,"Legenda",10_000_000),(6,"Elita",25_000_000),
        (7,"Majster",75_000_000),(8,"Hrdina",200_000_000),(9,"Šampión",500_000_000),
        (10,"Vesmírny Boh",1_000_000_000),
    ]]
    _next = next(((name, min_cr) for _, name, min_cr in _rank_thresholds if min_cr > cr), None)
    _cur_thresh = next((min_cr for _, name, min_cr in reversed(_rank_thresholds) if min_cr <= cr), 0)
    if _next:
        _next_name, _next_cr = _next
        _needed = _next_cr - cr
        _span = _next_cr - _cur_thresh
        _pct = min(100, round((_span - _needed) / _span * 100)) if _span > 0 else 100
        _milestone_lbl = L(f"Ďalší rank: {_next_name} za {_needed:,} CR", f"Next rank: {_next_name} in {_needed:,} CR")
    else:
        _pct = 100
        _milestone_lbl = L("MAX RANK — Vesmírny Boh", "MAX RANK — Vesmírny Boh")
    html += (
        f'<div style="width:100%;max-width:700px;margin-bottom:8px;'
        f'font-family:\'VT323\',monospace;font-size:.9em">'
        f'<div style="display:flex;justify-content:space-between;color:#888;margin-bottom:3px">'
        f'<span style="color:#ffb000">{display_rank}{"&nbsp;&#9733;" if rank_title else ""}</span>'
        f'<span style="color:#666">{_milestone_lbl}</span>'
        f'</div>'
        f'<div style="width:100%;height:6px;background:#0d0d00;border:1px solid #3a2800;overflow:hidden">'
        f'<div style="width:{_pct}%;height:100%;background:linear-gradient(90deg,#7a4800,#ffb000);'
        f'box-shadow:0 0 6px #ffb000aa;transition:width .4s"></div>'
        f'</div></div>'
    )

    # ── Career stats
    html += '<div class="card">'
    html += f'<div class="card-title">&#128202; {L("KARIÉRA","CAREER")}</div>'
    html += '<div class="stats-grid">'
    html += f'<div class="stat">{L("Kariérne CR","Career CR")}: <span>{cr:,}</span></div>'
    html += f'<div class="stat">{L("Rang","Rank")}: <span>{display_rank}{(" ★" if rank_title else "")}</span></div>'
    if sp_ranks:
        html += f'<div class="stat">{L("Spec. ranky","Spec. ranks")}: <span style="color:#ffd700">' + " | ".join(f"&#9733; {s}" for s in sp_ranks) + '</span></div>'
    html += f'<div class="stat">{L("Sessioni","Sessions")}: <span>{kb.get("sessions", 0)}</span></div>'
    html += f'<div class="stat">{L("Najlepší run","Best run")}: <span>{kb.get("best_session", 0):,} CR</span></div>'
    html += f'<div class="stat">{L("Celkom ťažby","Total mined")}: <span>{kb.get("total_mined", 0):,} {L("ks","pcs")}</span></div>'
    html += f'<div class="stat">{L("Posledná hra","Last game")}: <span>{kb.get("last_seen", "–")}</span></div>'
    html += '</div></div>'

    # ── Mini games
    html += '<div class="card">'
    html += f'<div class="card-title">&#127918; {L("MINI HRY","MINI GAMES")}</div>'
    html += f'<a href="/mini/cislo" class="btn">&#128290; &nbsp; {L("HÁDANIE ČÍSLA","NUMBER GUESSING")} &nbsp; <span style="color:#a07000;font-size:0.85em">(1&ndash;100, 7 {L("pokusov","attempts")})</span></a>'
    html += f'<a href="/mini/obesenec" class="btn">&#128279; &nbsp; {L("OBESENEC","HANGMAN")} &nbsp; <span style="color:#a07000;font-size:0.85em">({L("hádaj slovo","guess the word")})</span></a>'
    html += '</div>'

    # ── New game KB
    html += '<div class="card">'
    html += f'<div class="card-title">&#128640; KOZMICK&#201; BANE v4.7</div>'
    html += f'<a href="/game" class="btn btn-green">&#9654; &nbsp; {L("NOVÁ HRA &mdash; Začni od nuly","NEW GAME &mdash; Start fresh")}</a>'
    html += '</div>'

    # ── Save slots
    html += '<div class="card">'
    html += f'<div class="card-title">&#128193; {L("POKRAČOVAŤ &mdash; Vyber uloženie","CONTINUE &mdash; Choose save")}</div>'
    for s in range(1, 5):
        d = saves.get(str(s))
        if not d:
            html += f'<div class="slot-row"><div class="slot-info">#{s} &nbsp; &ndash; {L("prázdny slot","empty slot")} &ndash;</div></div>'
        else:
            dep  = depth_map.get(d.get("depth", 1), "?")
            crs  = d.get("credits", 0)
            goal = max(1, d.get("goal", 15000))
            pct  = min(100, round(crs / goal * 100))
            date = fmt_date_ts(d.get("ts", 0))
            uname = d.get("username", "?")
            turn_lbl = L("Táh","Turn")
            lbl  = f"#{s} &nbsp; {uname} &nbsp; {crs:,} CR ({pct}%) &nbsp; {turn_lbl} {d.get('turn',0)} &nbsp; [{dep}] &nbsp; {date}"
            html += f'<div class="slot-row">'
            html += f'<a href="/game?slot={s}" class="btn" style="margin:0;flex:1">{lbl}</a>'
            confirm_msg = L(f"Vymazať slot #{s}?", f"Delete slot #{s}?")
            del_js = (
                f"if(confirm('{confirm_msg}'))"
                "{var sv=JSON.parse(localStorage.getItem('kb_saves')||'{}');"
                f"delete sv['{s}'];"
                "localStorage.setItem('kb_saves',JSON.stringify(sv));"
                f"window.location='/delete_save/{s}';"
                "}return false;"
            )
            html += f'<a href="#" class="btn btn-red" style="margin:0" onclick="{del_js}">&#128465;</a>'
            html += f'</div>'
    html += '</div>'

    # ── Leaderboard top 5
    entries = sorted(career.items(), key=lambda x: -x[1].get("career_cr", 0))
    html += '<div class="card">'
    html += f'<div class="card-title">&#127942; {L("KARIÉRA &mdash; TOP HRÁČI","CAREER &mdash; TOP PLAYERS")}</div>'
    medals = ["&#129351;", "&#129352;", "&#129353;"]
    shown = 0
    users_for_lb = load_users()
    for i, (uname, d) in enumerate(entries[:5]):
        c = d.get("career_cr", 0)
        if c == 0:
            continue
        rn = d.get("rank_title") or kb_rank(c)[1]
        m   = medals[i] if i < 3 else f"{i+1}."
        cls = "lb-row me" if uname.upper() == pilot.upper() else "lb-row"
        u_lb = next((v for k, v in users_for_lb.items() if k.upper() == uname), {})
        spr  = get_sp_ranks(u_lb)
        sp_tag = (" " + " ".join(f'<span style="color:#ffd700;font-size:.85em">&#9733;{s}</span>' for s in spr)) if spr else ""
        sess_lbl = L("sess.", "sess.")
        html += f'<div class="{cls}">{m} &nbsp; <span>{uname}</span>{sp_tag} &nbsp; {c:,} CR &nbsp; [{rn}] &nbsp; {d.get("sessions",0)} {sess_lbl}</div>'
        shown += 1
    if shown == 0:
        html += f'<div class="lb-row">&ndash; {L("zatiaľ žiadne záznamy","no records yet")} &ndash;</div>'
    html += '</div>'

    # ── Import / Export
    html += '<div class="card">'
    html += f'<div class="card-title">&#128228; {L("PRENOS DÁT &mdash; Import / Export","DATA TRANSFER &mdash; Import / Export")}</div>'
    html += f'<a href="/import_data" class="btn" style="text-align:center">&#8597; {L("Preniesť dáta z PC na server (alebo naopak)","Transfer data between PC and server")}</a>'
    html += '</div>'

    # ── Restore from browser
    html += '<div style="width:100%;max-width:700px;margin-bottom:6px">'
    reg_info = u_data.get("created_at") or u_data.get("registered", "")
    restore_lbl = L("OBNOVIŤ DÁTA Z PREHLIADAČA", "RESTORE DATA FROM BROWSER")
    html += (f'<button onclick="window._forceSync(true)" '
             f'style="background:#000;border:1px solid #3a2800;color:#a07000;'
             f'font-family:\'VT323\',monospace;font-size:1em;padding:7px 14px;'
             f'cursor:pointer;width:100%;letter-spacing:.05em">'
             f'&#8635; {restore_lbl}'
             f'{"  |  Reg: " + reg_info if reg_info else ""}'
             f'</button></div>')

    # ── Kontaktovať admin/owner (dostupné pre všetkých hráčov)
    admin_list = sorted(k for k, v in users_db.items()
                        if v.get("is_admin") and k != pilot)
    if admin_list:
        admin_opts = "".join(f'<option value="{a}">{a}</option>' for a in admin_list)
        html += '<div class="card">'
        html += f'<div class="card-title">&#9993; {L("NAPÍSAŤ ADMINOVI","CONTACT ADMIN")}</div>'
        html += (
            f'<form method="POST" action="/api/message_admin" style="display:flex;flex-direction:column;gap:6px">'
            f'<div style="display:flex;gap:8px;align-items:center">'
            f'<label style="color:#888;font-size:.9em">{L("Komu:","To:")}</label>'
            f'<select name="to" style="background:#000;border:1px solid #3a2800;color:#fff8e0;'
            f'font-family:\'VT323\',monospace;font-size:1em;padding:3px 8px;flex:1;outline:none">'
            f'{admin_opts}</select></div>'
            f'<textarea name="msg" rows="2" placeholder="{L("Tvoja správa...","Your message...")}" '
            f'style="background:#000;border:1px solid #3a2800;color:#fff8e0;resize:vertical;'
            f'font-family:\'VT323\',monospace;font-size:1em;padding:6px 10px;outline:none"></textarea>'
            f'<button type="submit" style="background:#000;border:1px solid #a07000;color:#ffb000;'
            f'font-family:\'VT323\',monospace;font-size:1em;padding:6px 16px;cursor:pointer;'
            f'letter-spacing:.05em">{L("Odoslať","Send")}</button>'
            f'</form>'
        )
        html += '</div>'

    # ── TESTER — Beta features card (len nepublic funkcie)
    if u_data.get("is_tester") is True:
        tester_only = [f for f in BETA_FEATURES if not f.get("public", False)]
        if tester_only:
            beta_items = "".join(
                f'<div style="padding:4px 0;border-bottom:1px solid #0d1a0d;font-size:.9em">'
                f'<span style="color:#39ff6a;margin-right:6px">▶</span>'
                f'<span style="color:#cfffcf">{L(f["name_sk"], f["name_en"])}</span>'
                f'<span style="color:#556655;margin-left:8px;font-size:.88em">— {L(f["desc_sk"], f["desc_en"])}</span>'
                f'</div>'
                for f in tester_only
            )
            html += (
                f'<div class="card" style="border-color:#39ff6a44;background:#010d01">'
                f'<div class="card-title" style="color:#39ff6a">&#946; {L("BETA PRÍSTUP","BETA ACCESS")}</div>'
                f'<div style="color:#556655;font-size:.82em;margin-bottom:8px">'
                f'{L("Máš prístup k funkciám ktoré ešte nie sú verejné. Hláš chyby adminovi.", "You have access to features not yet public. Report bugs to an admin.")}'
                f'</div>'
                f'{beta_items}'
                f'</div>'
            )

    # ── Energetická minihra (tester alebo public)
    _energy_public = next((f for f in BETA_FEATURES if f["id"] == "energy_minigame"), {}).get("public", False)
    if u_data.get("is_tester") is True or _energy_public:
        html += '<div style="width:100%;max-width:700px;margin-bottom:6px">'
        _beta_tag = ' &nbsp;<span style="font-size:.75em;opacity:.6">[BETA]</span>' if not _energy_public else ""
        html += (f'<a href="/energy" style="display:block;background:#010d01;border:1px solid #39ff6a;'
                 f'color:#39ff6a;font-family:\'VT323\',monospace;font-size:1.15em;padding:9px 14px;'
                 f'text-align:center;text-decoration:none;letter-spacing:.06em">'
                 f'&#9889; {L("ENERGETICKÁ MINIHRA","ENERGY MINIGAME")}{_beta_tag}'
                 f'</a></div>')

    # ── Krajiny (tester only pre teraz)
    _countries_public = next((f for f in BETA_FEATURES if f["id"] == "countries"), {}).get("public", False)
    if u_data.get("is_tester") is True or _countries_public:
        # Zisti či hráč má nejakú rolu — ak áno, zobraz "Moja krajina", inak "Rada"
        _cdata_lb = _get_country_data()
        _my_pairs = _player_countries(session["username"], _cdata_lb)
        _country_href = "/my_country" if _my_pairs else "/countries"
        _country_label = L("MOJA KRAJINA","MY COUNTRY") if _my_pairs else L("MEDZIGALAKTICKÁ RADA","INTERGALACTIC COUNCIL")
        html += '<div style="width:100%;max-width:700px;margin-bottom:6px">'
        html += (f'<a href="{_country_href}" style="display:block;background:#010808;border:1px solid #38d1ff;'
                 f'color:#38d1ff;font-family:\'VT323\',monospace;font-size:1.15em;padding:9px 14px;'
                 f'text-align:center;text-decoration:none;letter-spacing:.06em">'
                 f'🌍 {_country_label}'
                 f' &nbsp;<span style="font-size:.75em;opacity:.6">[BETA]</span>'
                 f'</a></div>')

    # ── Admin Panel (for is_admin players)
    if u_data.get("is_admin"):
        html += '<div style="width:100%;max-width:700px;margin-bottom:6px">'
        html += '<a href="/adminpanel" style="display:block;background:#000;border:1px solid #00ccff;'
        html += 'color:#00ccff;font-family:\'VT323\',monospace;font-size:1.1em;padding:7px 14px;'
        html += 'text-align:center;text-decoration:none;letter-spacing:.05em">&#9733; ADMIN PANEL</a>'
        html += '</div>'

    # ── Owner access (kept in SK — admin-only)
    html += '<div style="width:100%;max-width:700px;margin-bottom:6px">'
    html += '<details style="border:1px solid #2a1500;padding:8px 14px;background:#0b0900">'
    html += '<summary style="cursor:pointer;color:#555;font-size:0.92em;letter-spacing:0.06em;list-style:none">&#128081; OWNER PR&#205;STUP</summary>'
    html += '<form method="POST" action="/owner" style="margin-top:10px;display:flex;gap:8px;align-items:center">'
    html += '<input type="password" name="code" placeholder="Owner k&#243;d" autocomplete="off" '
    html += 'style="background:#000;border:1px solid #3a2800;color:#fff8e0;font-family:\'VT323\',monospace;'
    html += 'font-size:1.1em;padding:6px 10px;flex:1;outline:none;">'
    html += '<button type="submit" style="background:#1a0000;border:1px solid #ff4444;color:#ff4444;'
    html += 'padding:6px 16px;cursor:pointer;font-family:\'VT323\',monospace;font-size:1.1em;white-space:nowrap">Vstúpiť</button>'
    html += '</form></details></div>'

    # ── Logout
    html += '<div style="width:100%;max-width:700px">'
    html += f'<a href="/logout" class="btn btn-logout">&#10007; &nbsp; {L("Odhlásiť sa","Log out")}</a>'
    html += '</div>'
    # ── Auto-sync script (localStorage → server on every lobby open)
    msg_no_data   = L("Ziadne lokalne data na obnovenie.", "No local data to restore.")
    msg_restored  = L("Obnovene", "Restored")
    msg_slots     = L("slotov z prehliadaca!", "slot(s) from browser!")
    msg_sync_err  = L("Sync chyba: ", "Sync error: ")
    html += f"""<script>
(function(){{
  try{{
    function _doSync(showMsg){{
      var lsCareer=JSON.parse(localStorage.getItem('kb_career')||'null');
      if(lsCareer&&lsCareer.career_cr>0){{
        fetch('/api/sync_career',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify(lsCareer)}}).then(function(){{
          if(showMsg)window.location.reload();
        }}).catch(function(){{}});
      }}
      var saves=JSON.parse(localStorage.getItem('kb_saves')||'{{}}');
      var lb=JSON.parse(localStorage.getItem('kb_leaderboard')||'[]');
      var hasData=Object.keys(saves).length>0||lb.length>0;
      if(!hasData){{if(showMsg)alert('{msg_no_data}');return;}}
      fetch('/api/sync_local_saves',{{method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{saves:saves,leaderboard:lb}})
      }}).then(function(r){{return r.json();}}).then(function(d){{
        if(d.synced>0||showMsg){{
          if(showMsg)alert('{msg_restored} '+d.synced+' {msg_slots}');
          window.location.reload();
        }}
      }}).catch(function(e){{if(showMsg)alert('{msg_sync_err}'+e);}});
    }}
    window._forceSync=_doSync;
    _doSync(false);
  }}catch(e){{console.warn('[sync] error:',e);}}
}})();
</script>"""

    html += '</body></html>'
    return html


# ── Mini-hry HTML ─────────────────────────────────────────────────────────

MINI_BASE_CSS = """
<style>html,body{background:#000;}</style>
<style>
@import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#000;color:#ffb000;font-family:'VT323',monospace;min-height:100vh;
  display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px;}
.card{background:#0b0900;border:1px solid #ffb000;box-shadow:0 0 25px rgba(255,176,0,0.12);
  width:100%;max-width:520px;padding:22px 28px 26px;}
h2{font-size:1.4em;border-bottom:1px solid #3a2800;padding-bottom:8px;margin-bottom:18px;letter-spacing:0.06em;}
.info{color:#a07000;font-size:0.95em;margin-bottom:14px;}
input[type=number],input[type=text]{
  background:#000;border:1px solid #3a2800;color:#fff8e0;
  font-family:'VT323',monospace;font-size:1.2em;padding:6px 10px;
  outline:none;transition:border-color 0.15s;width:100%;margin-bottom:10px;}
input:focus{border-color:#ffb000;}
.btn{display:inline-block;padding:9px 18px;background:#0f0c00;border:1px solid #ffb000;
  color:#ffb000;cursor:pointer;font-family:'VT323',monospace;font-size:1.1em;
  font-weight:bold;text-decoration:none;transition:all 0.15s;letter-spacing:0.05em;width:100%;margin-top:4px;}
.btn:hover{background:#3a2800;color:#fff8e0;}
.btn-back{border-color:#555;color:#555;font-size:0.9em;}
.btn-back:hover{background:#1a1a1a;color:#aaa;}
.msg{font-size:1.1em;margin:12px 0;padding:8px 10px;border-left:3px solid #ffb000;}
.msg.ok{color:#39ff6a;border-color:#39ff6a;}
.msg.err{color:#ff3a3a;border-color:#ff3a3a;}
.msg.hint{color:#ffb000;}
pre.hang{font-size:1.1em;line-height:1.3;color:#a07000;margin-bottom:12px;}
.word{font-size:2em;letter-spacing:0.25em;color:#fff8e0;margin:14px 0;text-align:center;}
.wrong{color:#ff3a3a;font-size:1em;margin-bottom:10px;}
.letters{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:14px;}
.lbtn{padding:5px 10px;background:#0b0900;border:1px solid #3a2800;color:#a07000;
  cursor:pointer;font-family:'VT323',monospace;font-size:1em;transition:all 0.12s;}
.lbtn:hover:not(:disabled){border-color:#ffb000;color:#ffb000;}
.lbtn:disabled{opacity:0.3;cursor:default;}
.score-msg{font-size:1.2em;color:#39ff6a;margin-top:10px;}
</style>
"""

def build_cislo_html():
    title   = L("HÁDANIE ČÍSLA", "NUMBER GUESSING")
    info    = L("Hádám číslo od 1 do 100. Máš 7 pokusov.", "I'm thinking of a number from 1 to 100. You have 7 tries.")
    ph      = L("Zadaj číslo 1-100", "Enter number 1-100")
    btn_g   = L("▶ HÁDAJ", "▶ GUESS")
    btn_ag  = L("↺ Hrať znova", "↺ Play again")
    btn_lb  = L("← Lobby", "← Lobby")
    msg_inv = L("Zadaj číslo 1-100!", "Enter a number 1-100!")
    msg_ok  = L("🎉 Správne za {a} pokusov! +{sc} bodov", "🎉 Correct in {a} tries! +{sc} points")
    msg_lo  = L("💀 Číslo bolo {s}. Nahráš znova?", "💀 The number was {s}. Try again?")
    hint_hi = L("📈 Viac!", "📈 Higher!")
    hint_lo = L("📉 Menej!", "📉 Lower!")
    att_lbl = L("Pokus {a}/7 — {h}", "Attempt {a}/7 — {h}")
    end_ok  = L("+{sc} bodov pridaných!", "+{sc} points added!")
    end_no  = L("Žiadne body tentokrát.", "No points this time.")
    return (
        MINI_BASE_CSS
        + f"""
<div class="card">
  <h2>&#128290; {title}</h2>
  <div class="info" id="info">{info}</div>
  <div id="msg"></div>
  <div id="gameArea">
    <input type="number" id="inp" min="1" max="100" placeholder="{ph}" onkeydown="if(event.key==='Enter')guess()">
    <button class="btn" onclick="guess()">{btn_g}</button>
  </div>
  <div id="result" style="display:none">
    <div class="score-msg" id="scoreMsg"></div>
    <a href="/mini/cislo" class="btn" style="margin-top:14px">{btn_ag}</a>
    <a href="/lobby" class="btn btn-back" style="margin-top:6px">{btn_lb}</a>
  </div>
</div>
<script>
const secret = Math.floor(Math.random()*100)+1;
let attempts = 0, done = false;
const MSG_INV={_json.dumps(msg_inv)}, MSG_OK={_json.dumps(msg_ok)}, MSG_LO={_json.dumps(msg_lo)};
const HINT_HI={_json.dumps(hint_hi)}, HINT_LO={_json.dumps(hint_lo)};
const ATT_LBL={_json.dumps(att_lbl)}, END_OK={_json.dumps(end_ok)}, END_NO={_json.dumps(end_no)};
function fmt(s,v){{for(const[k,val]of Object.entries(v))s=s.replaceAll('{{'+k+'}}',val);return s;}}
function guess() {{
  if (done) return;
  const v = parseInt(document.getElementById('inp').value);
  if (!v || v<1 || v>100) {{ setMsg(MSG_INV,'err'); return; }}
  attempts++;
  if (v === secret) {{
    const sc = Math.max(100-(attempts-1)*12, 10);
    done = true;
    setMsg(fmt(MSG_OK,{{a:attempts,sc}}),'ok');
    endGame(sc);
  }} else if (attempts >= 7) {{
    done = true;
    setMsg(fmt(MSG_LO,{{s:secret}}),'err');
    endGame(0);
  }} else {{
    const h = v < secret ? HINT_HI : HINT_LO;
    setMsg(fmt(ATT_LBL,{{a:attempts,h}}),'hint');
  }}
  document.getElementById('inp').value='';
  document.getElementById('inp').focus();
}}
function setMsg(t,cls){{const m=document.getElementById('msg');m.innerHTML='<div class="msg '+cls+'">'+t+'</div>';}}
function endGame(sc){{
  document.getElementById('gameArea').style.display='none';
  const r=document.getElementById('result'); r.style.display='block';
  document.getElementById('scoreMsg').innerHTML=(sc>0?fmt(END_OK,{{sc}}):END_NO);
  if(sc>0) fetch('/api/update_score',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{score:sc,game:'cislo'}})}});
}}
document.getElementById('inp').focus();
</script>
"""
    )

HANGMAN_STAGES = [
    "   \u250c\u2500\u2500\u2500\u2510\n   \u2502   \u2502\n       \u2502\n       \u2502\n   \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550",
    "   \u250c\u2500\u2500\u2500\u2510\n   \u2502   \u2502\n   O   \u2502\n       \u2502\n   \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550",
    "   \u250c\u2500\u2500\u2500\u2510\n   \u2502   \u2502\n   O   \u2502\n   |   \u2502\n   \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550",
    "   \u250c\u2500\u2500\u2500\u2510\n   \u2502   \u2502\n   O   \u2502\n  /|   \u2502\n   \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550",
    "   \u250c\u2500\u2500\u2500\u2510\n   \u2502   \u2502\n   O   \u2502\n  /|\u005c  \u2502\n   \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550",
    "   \u250c\u2500\u2500\u2500\u2510\n   \u2502   \u2502\n   O   \u2502\n  /|\u005c  \u2502\n  /    \u2502\n   \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550",
    "   \u250c\u2500\u2500\u2500\u2510\n   \u2502   \u2502\n   O   \u2502\n  /|\u005c  \u2502\n  / \u005c  \u2502\n   \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550",
]

HANGMAN_WORDS = [
    ("python",     "Programovaci jazyk",      "Programming language"),
    ("astronaut",  "Cestovatel vesmírom",      "Space traveler"),
    ("planety",    "Kozmicke telesa",          "Cosmic bodies"),
    ("detektor",   "Hladacie zariadenie",      "Scanning device"),
    ("robotika",   "Veda o robotoch",          "Science of robots"),
    ("gravitacia", "Pritazlivost",             "Force of attraction"),
    ("kristal",    "Priehladny mineral",       "Transparent mineral"),
    ("vrtacka",    "Tazobne zariadenie",       "Mining equipment"),
    ("asteroid",   "Kozmicka hornina",         "Space rock"),
    ("laser",      "Svetelna zbran",           "Light weapon"),
    ("reaktor",    "Zdroj energie",            "Energy source"),
    ("mineral",    "Hornina z bane",           "Rock from mine"),
]

import json as _json


def build_obesenec_html():
    import random
    lang = session.get('lang', 'sk')
    word, hint_sk, hint_en = random.choice(HANGMAN_WORDS)
    hint = hint_en if lang == 'en' else hint_sk
    stages_js = _json.dumps(HANGMAN_STAGES)
    title    = L("OBESENEC", "HANGMAN")
    hint_lbl = L("Nápoveda", "Hint")
    err_lbl  = L("Chyby", "Errors")
    btn_ag   = L("↺ Hrať znova", "↺ Play again")
    btn_lb   = L("← Lobby", "← Lobby")
    msg_win  = L("🎉 {w}! +{sc} bodov", "🎉 {w}! +{sc} points")
    msg_lose = L("💀 Slovo bolo: {w}", "💀 The word was: {w}")
    end_ok   = L("+{sc} bodov pridaných!", "+{sc} points added!")
    end_no   = L("Žiadne body tentokrát.", "No points this time.")
    abc      = 'abcdefghijklmnopqrstuvwxyz' if lang == 'en' else 'aábcčdďeéfghiíjklľmnňoópqrŕsštťuúvwxyýzž'
    return (
        MINI_BASE_CSS
        + f"""
<div class="card">
  <h2>&#128279; {title}</h2>
  <pre class="hang" id="hang"></pre>
  <div class="word" id="word"></div>
  <div class="info">{hint_lbl}: <span style="color:#a07000">{hint}</span></div>
  <div class="wrong" id="wrong">{err_lbl} (0/6): &mdash;</div>
  <div id="msg"></div>
  <div class="letters" id="letters"></div>
  <div id="result" style="display:none">
    <div class="score-msg" id="scoreMsg"></div>
    <a href="/mini/obesenec" class="btn" style="margin-top:14px">{btn_ag}</a>
    <a href="/lobby" class="btn btn-back" style="margin-top:6px">{btn_lb}</a>
  </div>
</div>
<script>
const WORD = {_json.dumps(word)};
const STAGES = {stages_js};
const ERR_LBL = {_json.dumps(err_lbl)};
const MSG_WIN = {_json.dumps(msg_win)}, MSG_LOSE = {_json.dumps(msg_lose)};
const END_OK = {_json.dumps(end_ok)}, END_NO = {_json.dumps(end_no)};
function fmt(s,v){{for(const[k,val]of Object.entries(v))s=s.replaceAll('{{'+k+'}}',val);return s;}}
let guessed = new Set(), wrong = new Set(), done = false;

function render() {{
  document.getElementById('hang').textContent = STAGES[wrong.size];
  const disp = WORD.split('').map(c => guessed.has(c) ? c : '_').join(' ');
  document.getElementById('word').textContent = disp;
  document.getElementById('wrong').innerHTML =
    ERR_LBL + ' (' + wrong.size + '/6): ' + (wrong.size ? [...wrong].sort().join(' ') : '&mdash;');
}}

function guess(letter) {{
  if (done) return;
  const btn = document.getElementById('btn_' + letter);
  if (btn) btn.disabled = true;
  if (WORD.includes(letter)) {{
    guessed.add(letter);
    render();
    if (WORD.split('').every(c => guessed.has(c))) {{
      const sc = Math.max(80 - wrong.size * 10, 10);
      done = true;
      setMsg(fmt(MSG_WIN,{{w:WORD.toUpperCase(),sc}}),'ok');
      endGame(sc);
    }}
  }} else {{
    wrong.add(letter);
    render();
    if (wrong.size >= 6) {{
      done = true;
      setMsg(fmt(MSG_LOSE,{{w:WORD.toUpperCase()}}),'err');
      endGame(0);
    }}
  }}
}}

function setMsg(t,cls){{const m=document.getElementById('msg');m.innerHTML='<div class="msg '+cls+'">'+t+'</div>';}}
function endGame(sc){{
  document.getElementById('letters').style.display='none';
  const r=document.getElementById('result'); r.style.display='block';
  document.getElementById('scoreMsg').innerHTML=(sc>0?fmt(END_OK,{{sc}}):END_NO);
  if(sc>0) fetch('/api/update_score',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{score:sc,game:'obesenec'}})}});
}}

const abc = {_json.dumps(abc)};
const lb = document.getElementById('letters');
[...new Set(abc)].forEach(function(l){{
  const b = document.createElement('button');
  b.className = 'lbtn'; b.id = 'btn_' + l; b.textContent = l;
  b.onclick = function(){{ guess(l); }};
  lb.appendChild(b);
}});

render();
</script>
"""
    )


# ── Routes — Auth ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_login()


@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    users = load_users()
    if username not in users:
        return render_login(tab="login", err_login=L(f"Používateľ '{username}' neexistuje.", f"User '{username}' does not exist."))
    banned, ban_msg = check_ban(users[username])
    if banned:
        return render_login(tab="login", err_login=ban_msg)
    if not check_pw(users[username]["password"], password):
        return render_login(tab="login", err_login=L("Nesprávne heslo.", "Incorrect password."))
    session["username"] = username
    users[username]["last_web_login"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    save_users(users)
    return redirect("/lobby")


@app.route("/register", methods=["POST"])
def register():
    username  = request.form.get("username", "").strip()
    password  = request.form.get("password", "")
    password2 = request.form.get("password2", "")
    users = load_users()
    if not username:
        return render_login(tab="register", err_reg=L("Meno nemôže byť prázdne.", "Username cannot be empty."))
    if username in users:
        return render_login(tab="register", err_reg=L(f"Meno '{username}' je obsadené.", f"Username '{username}' is already taken."))
    ok, msg = validate_pw(password)
    if not ok:
        return render_login(tab="register", err_reg=msg)
    if password != password2:
        return render_login(tab="register", err_reg=L("Heslá sa nezhodujú.", "Passwords do not match."))
    users[username] = {
        "password":   password,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "score": 0, "games_played": 0, "kb_sessions": 0,
    }
    save_users(users)
    return render_login(tab="login",
                        ok_login=L(f"Účet '{username}' vytvorený! Prihlás sa.", f"Account '{username}' created! Sign in."))


@app.route("/reset", methods=["POST"])
def reset():
    username  = request.form.get("username", "").strip()
    reg_date  = request.form.get("reg_date", "").strip()
    new_pw    = request.form.get("new_password", "")
    new_pw2   = request.form.get("new_password2", "")
    users = load_users()
    if username not in users:
        return render_login(tab="reset", err_reset=L("Používateľ neexistuje.", "User does not exist."))
    u = users[username]
    date_field = u.get("created_at", "") or u.get("registered", "")
    if not reg_date or reg_date not in date_field:
        return render_login(tab="reset", err_reset=L("Nesprávny dátum registrácie (napr. 2024-01-15).", "Incorrect registration date (e.g. 2024-01-15)."))
    ok, msg = validate_pw(new_pw)
    if not ok:
        return render_login(tab="reset", err_reset=msg)
    if new_pw != new_pw2:
        return render_login(tab="reset", err_reset=L("Heslá sa nezhodujú.", "Passwords do not match."))
    users[username]["password"] = new_pw
    save_users(users)
    return render_login(tab="login", ok_login=L("Heslo bolo zmenené. Prihlás sa.", "Password changed. Sign in."))


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/lang/<code>")
def set_lang(code):
    if code in ('sk', 'en'):
        session['lang'] = code
    return redirect(request.referrer or '/')


# ── Routes — Mini hry ─────────────────────────────────────────────────────

@app.route("/mini/cislo")
def mini_cislo():
    if "username" not in session:
        return redirect("/")
    return make_response(build_cislo_html())


@app.route("/mini/obesenec")
def mini_obesenec():
    if "username" not in session:
        return redirect("/")
    return make_response(build_obesenec_html())


@app.route("/api/notifications_read", methods=["POST"])
def api_notifications_read():
    if "username" not in session:
        return "", 401
    users = load_users()
    uname = session["username"]
    if uname in users:
        for n in users[uname].get("notifications", []):
            n["read"] = True
        save_users(users)
    return "", 204


@app.route("/api/update_score", methods=["POST"])
def api_update_score():
    if "username" not in session:
        return "", 401
    score = int(request.json.get("score", 0))
    pilot = session["username"]
    users = load_users()
    if pilot in users:
        users[pilot]["score"] = users[pilot].get("score", 0) + score
        users[pilot]["games_played"] = users[pilot].get("games_played", 0) + 1
        save_users(users)
    return json.dumps({"total": users.get(pilot, {}).get("score", 0)})


# ── Routes — Lobby & Game ──────────────────────────────────────────────────

@app.route("/lobby")
def lobby():
    if not _require_session():
        return redirect("/")
    return render_lobby(session["username"])


@app.route("/delete_save/<int:slot>")
def delete_save_route(slot):
    if not _require_session():
        return redirect("/")
    saves = load_jf(KB_SAVES, {})
    uname = _uname()
    if uname in saves:
        saves[uname].pop(str(slot), None)
    save_jf(KB_SAVES, saves)
    # Zapamätaj vymazaný slot — auto-sync ho nesmie znovu pridať
    deleted = list(session.get("deleted_slots", []))
    if str(slot) not in deleted:
        deleted.append(str(slot))
    session["deleted_slots"] = deleted
    return redirect("/lobby")


@app.route("/game")
def game():
    if not _require_session():
        return redirect("/")
    pilot = session["username"]
    # Hra číta pilot z URL – ak tam nie je, presmeruj
    if "pilot" not in request.args:
        slot = request.args.get("slot", "")
        url = f"/game?pilot={pilot}"
        if slot:
            url += f"&slot={slot}"
        return redirect(url)
    if not HTML_FILE.exists():
        return ("<h2 style='color:red;font-family:monospace'>"
                "kozmicke_bane.html nenájdený!</h2>"), 404
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()
    # Inject server-side saves + career so any device gets data immediately (no async needed)
    user_saves  = load_jf(KB_SAVES,  {}).get(_uname(), {})
    all_career  = load_jf(KB_CAREER, {})
    my_career   = all_career.get(_uname(), {})
    _u = load_users().get(session["username"], {})
    _spr = get_sp_ranks(_u)
    if _spr:
        my_career = dict(my_career, special_ranks=_spr)
    _is_tester = _u.get("is_tester") is True
    # Pre každý feature: True ak je tester ALEBO feature je public
    _beta_flags = {
        f["id"]: (_is_tester or f.get("public", False))
        for f in BETA_FEATURES
    }
    lb_rows = []
    for u, d in all_career.items():
        if d.get("career_cr", 0) > 0:
            lb_rows.append({"username": u, "career_cr": d.get("career_cr", 0),
                "rank_name": d.get("rank_name", "Baník"), "rank": d.get("rank", 1),
                "sessions": d.get("sessions", 0), "wins": d.get("wins", 0),
                "best_session": d.get("best_session", 0)})
    lb_rows.sort(key=lambda x: -x["career_cr"])
    server_inject = (
        f"<script>"
        f"window.__SERVER_SAVES__={json.dumps(user_saves)};"
        f"window.__MY_CAREER__={json.dumps(my_career)};"
        f"window.__GLOBAL_LB__={json.dumps(lb_rows)};"
        f"window.__IS_TESTER__={'true' if _is_tester else 'false'};"
        f"window.__BETA_FLAGS__={json.dumps(_beta_flags)};"
        f"window.__SESSION_USER__={json.dumps(session['username'].lower())};"
        f"</script>\n"
    )
    html = html.replace("<head>", "<head>\n" + server_inject + WEB_BRIDGE, 1)
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


# ── Routes — Game API ──────────────────────────────────────────────────────

def _require_session():
    if "username" not in session:
        return False
    # Ak účet neexistuje alebo je banned, zruš session
    users = load_users()
    uname = session["username"]
    u = users.get(uname)
    if u is None:
        session.clear()
        return False
    banned, _ = check_ban(u)
    if banned:
        session.clear()
        return False
    return True

@app.route("/api/save_game", methods=["POST"])
def api_save_game():
    if not _require_session():
        return "", 401
    d = request.json
    saves = load_jf(KB_SAVES, {})
    saves.setdefault(_uname(), {})[str(d["slot"])] = d["data"]
    save_jf(KB_SAVES, saves)
    return "true"

@app.route("/api/load_game", methods=["POST"])
def api_load_game():
    if not _require_session():
        return "null", 401
    slot = str(request.json.get("slot"))
    saves = load_jf(KB_SAVES, {})
    user_saves = saves.get(_uname(), {})
    d = user_saves.get(slot)
    return json.dumps(d) if d else "null"

@app.route("/api/delete_save", methods=["POST"])
def api_delete_save():
    if not _require_session():
        return "", 401
    slot = str(request.json.get("slot"))
    saves = load_jf(KB_SAVES, {})
    uname = _uname()
    if uname in saves:
        saves[uname].pop(slot, None)
    save_jf(KB_SAVES, saves)
    return "true"

@app.route("/api/get_startup_data")
def api_startup_data():
    if not _require_session():
        return "{}", 401
    all_saves = load_jf(KB_SAVES, {})
    user_saves = all_saves.get(_uname(), {})
    return json.dumps({
        "saves":       user_saves,
        "leaderboard": load_jf(KB_LB, []),
        "pilot":       session["username"],
    })

@app.route("/api/add_leaderboard", methods=["POST"])
def api_add_lb():
    lb = load_jf(KB_LB, [])
    lb.append(request.json)
    lb.sort(key=lambda x: -x.get("score", 0))
    save_jf(KB_LB, lb[:20])
    return "true"

@app.route("/api/get_leaderboard")
def api_get_lb():
    """Vráti globálne kariéry zoradené podľa career_cr pre in-game leaderboard."""
    if not _require_session():
        return "[]", 401
    career = load_jf(KB_CAREER, {})
    rows = []
    for uname, d in career.items():
        if d.get("career_cr", 0) > 0:
            rows.append({"username": uname, "career_cr": d.get("career_cr", 0),
                "rank_name": d.get("rank_name", "Baník"), "rank": d.get("rank", 1),
                "sessions": d.get("sessions", 0), "wins": d.get("wins", 0),
                "best_session": d.get("best_session", 0)})
    rows.sort(key=lambda x: -x["career_cr"])
    return json.dumps(rows)

@app.route("/api/clear_leaderboard", methods=["POST"])
def api_clear_lb():
    save_jf(KB_LB, [])
    return "true"

@app.route("/api/report_session_end", methods=["POST"])
def api_session_end():
    if not _require_session():
        return "{}", 401
    d      = request.json
    pilot  = session["username"]
    career = load_jf(KB_CAREER, {})
    key    = pilot.upper()
    e = career.get(key, {
        "career_cr": 0, "sessions": 0, "best_session": 0,
        "total_mined": 0, "wins": 0, "last_seen": "–"
    })
    earned = int(d.get("credits_earned", 0))
    e["career_cr"]    += earned
    e["sessions"]     += 1
    e["total_mined"]  += int(d.get("mined", 0))
    e["best_session"]  = max(e["best_session"], earned)
    e["last_seen"]     = datetime.now().strftime("%Y-%m-%d %H:%M")
    if d.get("win"):
        e["wins"] = e.get("wins", 0) + 1
    r, rname = kb_rank(e["career_cr"])
    e["rank"] = r; e["rank_name"] = rname
    career[key] = e
    save_jf(KB_CAREER, career)
    # Sync skóre do login systému
    users = load_users()
    if pilot in users:
        users[pilot]["games_played"] = users[pilot].get("games_played", 0) + 1
        users[pilot]["kb_sessions"]  = users[pilot].get("kb_sessions", 0) + 1
        users[pilot]["score"]        = users[pilot].get("score", 0) + max(0, earned // 100)
        save_users(users)
    return json.dumps(e)

@app.route("/api/get_career")
def api_get_career():
    if not _require_session():
        return "{}", 401
    career = load_jf(KB_CAREER, {})
    return json.dumps(career.get(_uname(), {}))

@app.route("/api/sync_career", methods=["POST"])
def api_sync_career():
    """Záloha kariéry z localStorage → server (iba ak je server nižší alebo prázdny)."""
    if not _require_session():
        return "{}", 401
    d = request.get_json(force=True, silent=True) or {}
    client_cr = int(d.get("career_cr", 0))
    if client_cr <= 0:
        return "{}", 200
    career = load_jf(KB_CAREER, {})
    key = _uname()
    server_cr = career.get(key, {}).get("career_cr", 0)
    if client_cr > server_cr:
        e = career.get(key, {"career_cr": 0, "sessions": 0, "best_session": 0,
                              "total_mined": 0, "wins": 0, "last_seen": "–"})
        e["career_cr"]   = client_cr
        e["sessions"]    = max(e.get("sessions", 0),    int(d.get("sessions", 0)))
        e["wins"]        = max(e.get("wins", 0),        int(d.get("wins", 0)))
        e["total_mined"] = max(e.get("total_mined", 0), int(d.get("total_mined", 0)))
        e["best_session"]= max(e.get("best_session", 0),int(d.get("best_session", 0)))
        r, rname = kb_rank(client_cr)
        e["rank"] = r; e["rank_name"] = rname
        career[key] = e
        save_jf(KB_CAREER, career)
        return json.dumps(e)
    return json.dumps(career.get(key, {}))

@app.route("/api/energy")
def api_energy_status():
    """Vráti aktuálnu energiu hráča z energetickej minihry."""
    if not _require_session():
        return '{"error":"unauthorized"}', 401
    profile = _energy_tick(_uname())
    return json.dumps({"energy": round(profile.get("energy", 0.0), 1),
                        "max_energy": MAX_ENERGY})


@app.route("/api/energy_use", methods=["POST"])
def api_energy_use():
    """Spotrebuje N energie z minihry. Vracia {ok, energy}."""
    if not _require_session():
        return '{"ok":false,"energy":0}', 401
    try:
        amount = float((request.json or {}).get("amount", 0))
    except (ValueError, TypeError):
        return '{"ok":false,"energy":0}', 400
    uname   = _uname()
    profile = _energy_tick(uname)
    current = float(profile.get("energy", 0.0))
    ok = current >= amount
    new_val = max(0.0, round(current - amount, 1)) if ok else current
    profile["energy"] = new_val
    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)
    return json.dumps({"ok": ok, "energy": new_val})


@app.route("/api/get_all_careers")
def api_get_all_careers():
    """Verejný globálny leaderboard — všetky účty zoradené podľa career_cr."""
    if not _require_session():
        return "[]", 401
    career = load_jf(KB_CAREER, {})
    rows = []
    for uname, d in career.items():
        if d.get("career_cr", 0) > 0:
            rows.append({
                "username": uname,
                "career_cr": d.get("career_cr", 0),
                "rank_name": d.get("rank_name", "Baník"),
                "rank": d.get("rank", 1),
                "sessions": d.get("sessions", 0),
                "wins": d.get("wins", 0),
                "best_session": d.get("best_session", 0),
            })
    rows.sort(key=lambda x: -x["career_cr"])
    return json.dumps(rows)


# ── Export / Import dát ────────────────────────────────────────────────────

@app.route("/api/sync_local_saves", methods=["POST"])
def sync_local_saves():
    """Auto-sync: prehliadač pošle localStorage dáta → server ich uloží."""
    if "username" not in session:
        return {"ok": False}, 401
    uname = _uname()
    body = request.get_json(force=True, silent=True) or {}
    synced = 0

    # Uloženia (kb_saves) — localStorage formát: {slot: saveData}
    raw_saves = body.get("saves", {})
    deleted_slots = session.get("deleted_slots", [])
    if raw_saves:
        all_saves = load_jf(KB_SAVES, {})
        user_saves = all_saves.get(uname, {})
        for slot, data in raw_saves.items():
            if slot in deleted_slots:           # preskočiť vymazané sloty
                continue
            if slot not in user_saves:          # uložíme iba ak server slot chýba
                user_saves[slot] = data
                synced += 1
        all_saves[uname] = user_saves
        save_jf(KB_SAVES, all_saves)

    # Leaderboard — iba záznamy tohto hráča
    lb_entries = body.get("leaderboard", [])
    if lb_entries:
        all_lb = load_jf(KB_LB, [])
        existing_ts = {e.get("ts") for e in all_lb}
        for entry in lb_entries:
            if (entry.get("username", "").upper() == uname
                    and entry.get("ts") not in existing_ts):
                all_lb.append(entry)
        all_lb.sort(key=lambda x: x.get("score", 0), reverse=True)
        save_jf(KB_LB, all_lb[:50])

    return {"ok": True, "synced": synced}


@app.route("/export_data")
def export_data():
    if "username" not in session:
        return redirect("/")
    uname = _uname()
    all_saves  = load_jf(KB_SAVES, {})
    all_career = load_jf(KB_CAREER, {})
    all_lb     = load_jf(KB_LB, [])
    export = {
        "username": uname,
        "saves":    all_saves.get(uname, {}),
        "career":   all_career.get(uname, {}),
        "lb":       [e for e in all_lb if e.get("username","").upper() == uname],
    }
    resp = make_response(json.dumps(export, ensure_ascii=False, indent=2))
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = f'attachment; filename="kb_export_{uname.lower()}.json"'
    return resp


@app.route("/import_data", methods=["GET", "POST"])
def import_data():
    if "username" not in session:
        return redirect("/")
    uname = _uname()

    if request.method == "POST":
        file = request.files.get("datafile")
        if not file:
            return _import_page("⚠ Žiadny súbor nebol nahraný.", error=True)
        try:
            data = json.loads(file.read().decode("utf-8"))
        except Exception:
            return _import_page("⚠ Neplatný JSON súbor.", error=True)

        def _merge(path, key):
            d = load_jf(path, {})
            d[uname] = data[key]
            save_jf(path, d)

        if data.get("saves"):  _merge(KB_SAVES,  "saves")
        if data.get("career"): _merge(KB_CAREER, "career")

        # Leaderboard — pridaj záznamy (bez duplikátov podľa ts)
        if data.get("lb"):
            lb = load_jf(KB_LB, [])
            existing_ts = {e.get("ts") for e in lb}
            for entry in data["lb"]:
                if entry.get("ts") not in existing_ts:
                    lb.append(entry)
            lb.sort(key=lambda x: -x.get("score", 0))
            save_jf(KB_LB, lb[:50])

        return _import_page(f"✓ Dáta pre {uname} úspešne importované!", error=False)

    return _import_page("")


def _import_page(msg, error=None):
    color = "#ff3a3a" if error else "#39ff6a"
    msg_html = f'<div style="color:{color};margin-bottom:1rem;font-size:1.1rem">{msg}</div>' if msg else ""
    return f"""<!DOCTYPE html><html lang="sk"><head><meta charset="UTF-8">
<title>Import / Export — KB</title>
<style>
  body{{background:#000;color:#ffb000;font-family:'Courier New',monospace;display:flex;
       align-items:center;justify-content:center;min-height:100vh;margin:0;}}
  .box{{border:2px solid #ffb000;padding:2rem;max-width:500px;width:90%;}}
  h2{{margin:0 0 1.2rem;letter-spacing:.1em;}}
  .btn{{display:block;background:#0d0a00;border:1px solid #a07000;color:#ffb000;
        font-family:'Courier New',monospace;font-size:1rem;padding:.55rem 1rem;
        cursor:pointer;text-align:center;text-decoration:none;margin-bottom:.5rem;width:100%;box-sizing:border-box;}}
  .btn:hover{{background:#1a1200;border-color:#ffb000;}}
  .btn-g{{border-color:#39ff6a44;color:#39ff6a;}}
  .btn-g:hover{{background:#001800;border-color:#39ff6a;}}
  input[type=file]{{color:#ffb000;margin-bottom:.8rem;width:100%;}}
</style></head><body><div class="box">
  <h2>⬆⬇ IMPORT / EXPORT DÁT</h2>
  {msg_html}
  <p style="color:#a07000;font-size:.92rem;margin-bottom:1.2rem">
    <strong>Export</strong> — stiahni svoje dáta ako JSON súbor.<br>
    <strong>Import</strong> — nahraj JSON súbor (napr. zo svojho PC).
  </p>
  <a href="/export_data" class="btn btn-g">⬇ Exportovať moje dáta (stiahni JSON)</a>
  <hr style="border-color:#333;margin:1rem 0">
  <form method="POST" enctype="multipart/form-data">
    <div style="margin-bottom:.4rem">⬆ Nahrať JSON súbor:</div>
    <input type="file" name="datafile" accept=".json">
    <button type="submit" class="btn">⬆ Importovať</button>
  </form>
  <hr style="border-color:#333;margin:1rem 0">
  <a href="/lobby" class="btn">◀ Späť do lobby</a>
</div></body></html>"""


# ── Admin panel ────────────────────────────────────────────────────────────

ADMIN_CSS = """
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#0a0a0a;color:#ddd;font-family:'Courier New',monospace;padding:1rem;}
h1{color:#ffb000;margin-bottom:1rem;}
h2{color:#ff8800;margin:.8rem 0 .4rem;}
table{width:100%;border-collapse:collapse;font-size:.88rem;margin-bottom:1.5rem;}
th{background:#1a1200;color:#ffb000;padding:.35rem .6rem;text-align:left;border:1px solid #333;}
td{padding:.3rem .6rem;border:1px solid #222;}
tr:hover td{background:#111;}
.me td{background:#1a1a00;}
a.btn{display:inline-block;padding:.25rem .7rem;border:1px solid #555;color:#ffb000;
  text-decoration:none;border-radius:3px;font-size:.82rem;margin:.15rem;}
a.btn:hover{background:#1a1200;border-color:#ffb000;}
a.btn-r{border-color:#ff4444;color:#ff4444;}
a.btn-r:hover{background:#1a0000;border-color:#ff4444;}
a.btn-g{border-color:#39ff6a;color:#39ff6a;}
.warn{color:#ff4444;margin:.5rem 0;}
.ok{color:#39ff6a;margin:.5rem 0;}
form.inline{display:inline;}
input{background:#111;border:1px solid #444;color:#ffb000;padding:.2rem .4rem;
  border-radius:3px;font-family:inherit;font-size:.85rem;}
</style>
"""

def _owner_check():
    """Vráti True ak je owner session aktívna."""
    return session.get("owner") is True

def _is_admin_user():
    """Vráti True ak je prihlásený hráč s is_admin=True."""
    if "username" not in session:
        return False
    users = load_users()
    return users.get(session["username"], {}).get("is_admin") is True

@app.route("/owner", methods=["GET", "POST"])
def owner_login():
    if _owner_check():
        return redirect("/owner/panel")
    err = ""
    if request.method == "POST":
        code = request.form.get("code", "")
        if OWNER_CODE and code == OWNER_CODE:
            session["owner"] = True
            return redirect("/owner/panel")
        err = "Nesprávny owner kód."
    return f"""<!DOCTYPE html><html><head><title>Owner</title>{ADMIN_CSS}</head><body>
<h1>&#128081; OWNER PR&#205;STUP</h1>
<p style="color:#888;margin-bottom:1rem">Zadaj owner kód nastavený v env premennej <code>OWNER_CODE</code>.</p>
{"<p class='warn'>"+err+"</p>" if err else ""}
<form method="POST">
  <input type="password" name="code" placeholder="Owner kód" autofocus style="width:220px">
  <button type="submit" style="background:#1a1200;border:1px solid #ffb000;color:#ffb000;
    padding:.25rem .8rem;cursor:pointer;font-family:inherit;border-radius:3px;margin-left:.4rem">
    Vstúpiť
  </button>
</form>
</body></html>"""

@app.route("/owner/kv")
def owner_kv_status():
    if not _owner_check():
        return redirect("/owner")
    rows = ""
    if not _KV_URL:
        status_html = '<p style="color:#ff3a3a">UPSTASH_REDIS_REST_URL nie je nastavený.</p>'
    else:
        ping_ok = _kv_ping()
        status_html = (
            f'<p style="color:{"#39ff6a" if ping_ok else "#ff3a3a"}">'
            f'Upstash ping: {"OK ✓" if ping_ok else "ZLYHALO ✗"}</p>'
            f'<p style="color:#888;font-size:.85em">URL: {_KV_URL[:40]}...</p>'
        )
        for path, key in _KV_KEYS.items():
            data = _kv_get(key)
            if data is None:
                size_str = '<span style="color:#ff3a3a">prázdne / chyba</span>'
            else:
                size_str = f'<span style="color:#39ff6a">{len(json.dumps(data))} B</span>'
            rows += f"<tr><td>{key}</td><td>{size_str}</td></tr>"

    css = "<style>body{background:#000;color:#ccc;font-family:monospace;padding:20px}" \
          "h1{color:#ffb000}table{border-collapse:collapse;margin-top:10px}" \
          "td,th{border:1px solid #333;padding:6px 14px}a{color:#ffb000}</style>"
    return f"""<!DOCTYPE html><html><head><title>KV Status</title>{css}</head><body>
<h1>&#128190; Upstash KV Status</h1>
{status_html}
<table><tr><th>Kľúč</th><th>Veľkosť v Upstash</th></tr>{rows}</table>
<br><a href="/owner/panel">← Owner panel</a>
&nbsp;|&nbsp;
<form method="POST" action="/owner/kv_write_test" style="display:inline">
  <button style="background:#111;border:1px solid #ffb000;color:#ffb000;
    padding:4px 12px;cursor:pointer;font-family:monospace">
    ⬆ Zapíš aktuálne lokálne súbory do Upstash
  </button>
</form>
</body></html>"""


@app.route("/owner/kv_write_test", methods=["POST"])
def owner_kv_write_test():
    if not _owner_check():
        return redirect("/owner")
    if not _KV_URL:
        return "Upstash nie je nakonfigurovaný.", 400
    results = []
    for path, key in _KV_KEYS.items():
        data = load_jf(path, {})
        ok = _kv_set(key, data)
        results.append(f"{key}: {'OK ✓' if ok else 'ZLYHALO ✗'}")
    css = "<style>body{background:#000;color:#ccc;font-family:monospace;padding:20px}a{color:#ffb000}</style>"
    return f"""<!DOCTYPE html><html><head><title>KV Write</title>{css}</head><body>
<h1>Výsledky zápisu</h1>
<pre>{'<br>'.join(results)}</pre>
<br><a href="/owner/kv">← Späť na KV Status</a>
</body></html>"""


@app.route("/owner/logout")
def owner_logout():
    session.pop("owner", None)
    return redirect("/owner")

@app.route("/owner/diag")
def owner_diag():
    if not _owner_check():
        return redirect("/owner")
    import platform, sys
    users  = load_users()
    career = load_jf(KB_CAREER, {})
    saves  = load_jf(KB_SAVES,  {})
    lines  = [
        f"DATA_DIR: {DATA_DIR}",
        f"DATA_DIR exists: {DATA_DIR.exists()}",
        f"RENDER env: {os.environ.get('RENDER','not set')}",
        f"game_users.json: {'EXISTS' if DATA_FILE.exists() else 'MISSING'} | {len(users)} users",
        f"kb_career.json:  {'EXISTS' if KB_CAREER.exists() else 'MISSING'} | {len(career)} entries",
        f"kb_saves.json:   {'EXISTS' if KB_SAVES.exists() else 'MISSING'} | {len(saves)} users",
        f"Python: {sys.version}",
        f"Platform: {platform.platform()}",
        "",
        "=== USERS ===",
    ]
    for u, d in sorted(users.items()):
        lines.append(f"  {u}: pw={d.get('password','?')[:20]}  ban={d.get('banned_until')}")
    pre = "\n".join(lines)
    return f"<pre style='background:#000;color:#0f0;padding:20px;font-family:monospace'>{pre}</pre>"

@app.route("/owner/panel")
def owner_panel():
    if not _owner_check():
        return redirect("/owner")
    users   = load_users()
    career  = load_jf(KB_CAREER, {})
    saves   = load_jf(KB_SAVES, {})

    # Zlúč účty z users + career (aby sa zobrazili aj keď je users.json poškodený)
    all_names = set(u.lower() for u in users.keys())
    for ckey in career.keys():
        all_names.add(ckey.lower())

    now_ts = datetime.now().timestamp()
    rows = ""
    for uname_lower in sorted(all_names):
        u_orig = next((k for k in users if k.lower() == uname_lower), None)
        u  = users.get(u_orig, {}) if u_orig else {}
        c  = career.get(uname_lower.upper(), {})
        sv = saves.get(uname_lower.upper(), {})
        display = u_orig or uname_lower.upper()
        pw_str = u.get("password", "") or "<em style='color:#555'>—</em>"
        bu = u.get("banned_until")
        if bu == -1:
            ban_cell = "<span style='color:#ff4444'>&#128683; PERM</span>"
        elif bu and bu > now_ts:
            mins = int((bu - now_ts) / 60) + 1
            ban_cell = f"<span style='color:#ff9900'>&#9203; {mins} min</span>"
        else:
            ban_cell = "<span style='color:#444'>—</span>"
        cr_val  = c.get("career_cr", 0)
        rtitle  = c.get("rank_title", "")
        earned_rname = kb_rank(cr_val)[1]
        cur_tier = next((t for t, name, _ in RANKS if name == rtitle), kb_rank(cr_val)[0])
        rank_btns = "".join(
            f'<form method="POST" action="/owner/set_rank_tier" style="display:inline;margin:0 1px 2px 0">'
            f'<input type="hidden" name="uname" value="{display}">'
            f'<input type="hidden" name="tier" value="{t}">'
            f'<button type="submit" style="background:{"#001800" if t==cur_tier else "#000"};'
            f'border:1px solid {"#39ff6a" if t==cur_tier else "#2a3a2a"};'
            f'color:{"#39ff6a" if t==cur_tier else "#556655"};'
            f'padding:1px 5px;cursor:pointer;font-family:inherit;font-size:.75em;white-space:nowrap">'
            f'{name}</button></form>'
            for t, name, _ in RANKS
        )
        spr     = get_sp_ranks(u)
        sp_cell = (" ".join(f"<span style='color:#ffd700'>&#9733;{s}</span>" for s in spr)
                   if spr else "<span style='color:#444'>—</span>")
        is_adm  = u.get("is_admin") is True
        adm_cell = ("<span style='color:#00ccff'>&#9679; Admin</span>" if is_adm
                    else "<span style='color:#444'>—</span>")
        is_tst  = u.get("is_tester") is True
        tst_cell = ("<span style='color:#39ff6a'>&#946; Tester</span>" if is_tst
                    else "<span style='color:#444'>—</span>")
        # Správy tohto hráča
        notifs_all = u.get("notifications", [])
        notif_hist = "".join(
            f'<div style="font-size:.78em;color:{"#aaa" if n.get("read") else "#ffe08a"};'
            f'border-bottom:1px solid #1a1a1a;padding:1px 0">'
            f'<span style="color:#555">[{n.get("ts","")} {n.get("from","")}]</span> {n.get("text","")}'
            f'</div>'
            for n in notifs_all[-5:]  # posledných 5
        ) or f'<span style="color:#333;font-size:.78em">—</span>'
        msg_cell = (
            f'<details style="min-width:160px">'
            f'<summary style="cursor:pointer;color:#7788cc;font-size:.8em;list-style:none">'
            f'&#9993; {len(notifs_all)} správ</summary>'
            f'<div style="max-height:80px;overflow-y:auto;margin:3px 0">{notif_hist}</div>'
            f'<form method="POST" action="/owner/message" style="display:flex;gap:3px;margin-top:3px">'
            f'<input type="hidden" name="uname" value="{display}">'
            f'<input type="text" name="msg_text" placeholder="správa..." '
            f'style="flex:1;min-width:0;background:#000;border:1px solid #7788cc;color:#aabbff;'
            f'font-family:inherit;font-size:.8em;padding:2px 4px;outline:none">'
            f'<button type="submit" style="background:#000;border:1px solid #7788cc;'
            f'color:#aabbff;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.8em">&#9993;</button>'
            f'</form></details>'
        )
        rows += f"""<tr>
          <td><strong>{display}</strong></td>
          <td style="color:#ffee88">{pw_str}</td>
          <td style="color:#888;font-size:.85em">{u.get('created_at') or u.get('registered','–')}</td>
          <td style="color:#888;font-size:.85em">{u.get('last_web_login') or c.get('last_seen','–')}</td>
          <td style="color:#ffdd44">{rtitle if rtitle else earned_rname}{"&nbsp;<span style='color:#00ccff;font-size:.8em'>★</span>" if rtitle else ""}</td>
          <td>{cr_val:,} CR</td>
          <td>{sp_cell}</td>
          <td>{ban_cell}</td>
          <td>{adm_cell}</td>
          <td>{tst_cell}</td>
          <td>{len(sv)}</td>
          <td>{msg_cell}</td>
          <td style="white-space:nowrap">
            <form method="POST" action="/owner/set_rank" style="display:inline">
              <input type="hidden" name="uname" value="{display}">
              <input type="number" name="cr" value="{cr_val}" min="0"
                style="width:80px;background:#000;border:1px solid #3a2800;color:#fff8e0;
                font-family:inherit;font-size:.85em;padding:2px 4px">
              <button type="submit" style="background:#001a00;border:1px solid #39ff6a;
                color:#39ff6a;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.85em">
                =CR
              </button>
            </form>
            <form method="POST" action="/owner/add_cr" style="display:inline">
              <input type="hidden" name="uname" value="{display}">
              <input type="number" name="delta" value="0"
                style="width:65px;background:#000;border:1px solid #3a2800;color:#fff8e0;
                font-family:inherit;font-size:.85em;padding:2px 4px">
              <button type="submit" style="background:#001a00;border:1px solid #39ff6a;
                color:#39ff6a;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.85em">
                +/-CR
              </button>
            </form>
            <form method="POST" action="/owner/set_heat" style="display:inline">
              <input type="hidden" name="uname" value="{display}">
              <input type="number" name="heat" value="0" min="0" max="100"
                style="width:50px;background:#000;border:1px solid #ff440044;color:#ff9900;
                font-family:inherit;font-size:.85em;padding:2px 4px">
              <button type="submit" style="background:#0d0000;border:1px solid #ff4400;
                color:#ff9900;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.85em">
                =heat
              </button>
            </form>
            &nbsp;
            <form method="POST" action="/owner/ban" style="display:inline">
              <input type="hidden" name="uname" value="{display}">
              <select name="dur" style="background:#1a0000;border:1px solid #ff4444;
                color:#ff4444;font-family:inherit;font-size:.85em;padding:2px">
                <option value="10m">10 min</option>
                <option value="1h">1 hod</option>
                <option value="12h">12 hod</option>
                <option value="24h">24 hod</option>
                <option value="perm">Permanent</option>
              </select>
              <button type="submit" style="background:#1a0000;border:1px solid #ff4444;
                color:#ff4444;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.85em">
                Ban
              </button>
            </form>
            <a href="/owner/unban/{display}"
               style="color:#aaa;font-size:.8em;margin-left:3px">Unban</a>
            &nbsp;
            <form method="POST" action="/owner/reset_pw" style="display:inline">
              <input type="hidden" name="uname" value="{display}">
              <input type="text" name="new_pw" placeholder="nove heslo"
                style="width:90px;background:#000;border:1px solid #555;color:#fff8e0;
                font-family:inherit;font-size:.8em;padding:2px 4px">
              <button type="submit" style="background:#000;border:1px solid #888;
                color:#aaa;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.8em">
                PW
              </button>
            </form>
            &nbsp;
            &nbsp;
            <form method="POST" action="/owner/set_special_rank" style="display:inline">
              <input type="hidden" name="uname" value="{display}">
              <input type="text" name="title1" value="{spr[0] if len(spr)>0 else ''}" placeholder="Rank 1"
                style="width:70px;background:#1a1400;border:1px solid #ffd700;color:#ffd700;
                font-family:inherit;font-size:.8em;padding:2px 4px">
              <input type="text" name="title2" value="{spr[1] if len(spr)>1 else ''}" placeholder="Rank 2"
                style="width:70px;background:#1a1400;border:1px solid #ffd700;color:#ffd700;
                font-family:inherit;font-size:.8em;padding:2px 4px">
              <button type="submit" style="background:#1a1400;border:1px solid #ffd700;
                color:#ffd700;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.8em">
                &#9733;
              </button>
            </form>
            &nbsp;
            <a href="/owner/toggle_admin/{display}"
               style="color:{'#ff9900' if is_adm else '#00ccff'};font-size:.8em;margin-left:3px">
               {'Revoke Admin' if is_adm else 'Make Admin'}</a>
            &nbsp;
            <a href="/owner/toggle_tester/{display}"
               style="color:{'#ff9900' if is_tst else '#39ff6a'};font-size:.8em;margin-left:3px">
               {'Revoke Tester' if is_tst else 'Make Tester'}</a>
            &nbsp;
            <a href="/owner/delete/{display}" style="color:#ff4444;font-size:.8em"
               onclick="return confirm('Vymazat {display}?')">Del</a>
            &nbsp;
            <div style="display:inline-flex;flex-wrap:wrap;gap:1px;vertical-align:middle">{rank_btns}</div>
            &nbsp;
            <form method="POST" action="/owner/rename" style="display:inline">
              <input type="hidden" name="uname" value="{display}">
              <input type="text" name="new_name" placeholder="nové meno"
                style="width:80px;background:#000;border:1px solid #888;color:#fff8e0;
                font-family:inherit;font-size:.8em;padding:2px 4px">
              <button type="submit" style="background:#000;border:1px solid #888;
                color:#aaa;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.8em">
                &#8635;
              </button>
            </form>
            &nbsp;
            <form method="POST" action="/owner/message" style="display:inline">
              <input type="hidden" name="uname" value="{display}">
              <input type="text" name="msg_text" placeholder="správa..."
                style="width:110px;background:#000;border:1px solid #7788cc;color:#aabbff;
                font-family:inherit;font-size:.8em;padding:2px 4px">
              <button type="submit" style="background:#000;border:1px solid #7788cc;
                color:#aabbff;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.8em">
                &#9993;
              </button>
            </form>
          </td>
        </tr>"""

    total_cr  = sum(d.get("career_cr", 0) for d in career.values())
    sp_holders = ", ".join(
        f"<span style='color:#ffd700'>{k}: " + " | ".join(f"&#9733;{s}" for s in get_sp_ranks(v)) + "</span>"
        for k, v in users.items() if get_sp_ranks(v)
    ) or "—"
    return f"""<!DOCTYPE html><html><head><title>Owner Panel</title>{ADMIN_CSS}
<style>table{{font-size:.82em}}td,th{{padding:4px 6px;vertical-align:middle}}
input[type=number],input[type=text],select{{outline:none}}</style>
</head><body>
<h1>&#128081; OWNER PANEL &mdash; KOZMICK&#201; BANE v4.8</h1>
<p style="color:#888;font-size:.85rem">
  Ucty: <strong style="color:#ffb000">{len(all_names)}</strong> &nbsp;|&nbsp;
  Celkove CR: <strong style="color:#ffb000">{total_cr:,}</strong> &nbsp;|&nbsp;
  Spec. ranky: <strong style="color:#ffd700">{sum(1 for u in users.values() if get_sp_ranks(u))}</strong> &nbsp;|&nbsp;
  <a href="/owner/logout" class="btn btn-r">Logout</a>
  <a href="/owner/kv" class="btn" style="border-color:#39ff6a;color:#39ff6a">&#128190; KV</a>
  <a href="/owner/diag" class="btn">Diag</a>
  <a href="/lobby" class="btn">Lobby</a>
</p>
<p style="font-size:.85rem;margin-bottom:8px">
  &#9733; Specialne ranky: {sp_holders}
</p>
<h2>&#128101; VSETKY UCTY</h2>
<table>
  <tr>
    <th>Pouzivatel</th><th>Heslo</th><th>Reg.</th><th>Posl. login</th>
    <th>Rank</th><th>Kariera</th><th>Spec. rank</th><th>Ban</th><th>Admin</th><th>&#946; Tester</th><th>Sloty</th><th>&#9993; Správy</th><th>Akcie</th>
  </tr>
  {rows}
</table>

<h2>🌍 KRAJINY — PRIDELENIE ROLÍ</h2>
<p style="font-size:.82rem;color:#888">Každý hráč môže mať viacero rolí v rôznych krajinách.</p>
<form method="POST" action="/owner/assign_role" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
  <select name="cid" style="font-size:.85rem;min-width:160px">
    {''.join(f'<option value="{c["id"]}">{c["flag"]} {c["name"]}</option>' for c in COUNTRIES)}
  </select>
  <select name="rid" style="font-size:.85rem;min-width:180px">
    {''.join(f'<option value="{r["id"]}">{r["icon"]} {r["name_sk"]}</option>' for r in COUNTRY_ROLES)}
  </select>
  <input type="text" name="uname" placeholder="username" style="width:120px;font-size:.85rem">
  <button type="submit" name="action" value="add" class="btn" style="color:#39ff6a;border-color:#39ff6a">+ Prideliť</button>
  <button type="submit" name="action" value="remove" class="btn btn-r">− Odobrať</button>
</form>
<h2>☢ JADROVÝ PROGRAM — schválenie krajín</h2>
<p style="font-size:.82rem;color:#888">Stáli členovia (USA/RU/CN/UK/FR) majú schválenie automaticky pri štarte.</p>
<form method="POST" action="/owner/nuclear_approve" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
  <select name="cid" style="font-size:.85rem;min-width:160px">
    {''.join(f'<option value="{c["id"]}">{c["flag"]} {c["name"]}</option>' for c in COUNTRIES)}
  </select>
  <button type="submit" name="approved" value="1" class="btn" style="color:#39ff6a;border-color:#39ff6a">✅ Schváliť</button>
  <button type="submit" name="approved" value="0" class="btn btn-r">❌ Zrušiť schválenie</button>
</form>
<p><a href="/countries" style="color:#39ff6a;font-size:.9rem">🌍 Zobraziť stránku krajín</a>
  &nbsp;|&nbsp; <a href="/countries/pu_market" style="color:#ff9900;font-size:.9rem">🔬 Trh Pu</a>
  &nbsp;|&nbsp; <a href="/council" style="color:#ff88ff;font-size:.9rem">🏛 Rada</a></p>
</body></html>"""

@app.route("/owner/reset_pw", methods=["POST"])
def owner_reset_pw():
    if not _owner_check():
        return redirect("/owner")
    uname  = request.form.get("uname", "").strip()
    new_pw = request.form.get("new_pw", "").strip()
    users  = load_users()
    if uname not in users or not new_pw:
        return redirect("/owner/panel")
    users[uname]["password"] = new_pw
    save_users(users)
    return redirect("/owner/panel")

@app.route("/owner/set_rank", methods=["POST"])
def owner_set_rank():
    if not _owner_check():
        return redirect("/owner")
    uname = request.form.get("uname", "").strip()
    try:
        cr = max(0, int(request.form.get("cr", 0)))
    except ValueError:
        return redirect("/owner/panel")
    career = load_jf(KB_CAREER, {})
    key = uname.upper()
    e = career.get(key, {"sessions": 0, "wins": 0, "total_mined": 0,
                         "best_session": 0, "last_seen": "–"})
    e["career_cr"] = cr
    r, rname = kb_rank(cr)
    e["rank"] = r
    e["rank_name"] = rname
    career[key] = e
    save_jf(KB_CAREER, career)
    return redirect("/owner/panel")

@app.route("/owner/set_heat", methods=["POST"])
def owner_set_heat():
    if not _owner_check():
        return redirect("/owner")
    uname = request.form.get("uname", "").strip().upper()
    try:
        heat = max(0.0, min(100.0, float(request.form.get("heat", 0))))
    except ValueError:
        return redirect("/owner/panel")
    data = load_jf(KB_ENERGY, {})
    if uname in data:
        data[uname]["proliferation_heat"] = heat
        save_jf(KB_ENERGY, data)
    return redirect("/owner/panel")


@app.route("/owner/add_cr", methods=["POST"])
def owner_add_cr():
    if not _owner_check():
        return redirect("/owner")
    uname = request.form.get("uname", "").strip()
    try:
        delta = int(request.form.get("delta", 0))
    except ValueError:
        return redirect("/owner/panel")
    career = load_jf(KB_CAREER, {})
    key = uname.upper()
    e = career.get(key, {"sessions": 0, "wins": 0, "total_mined": 0,
                         "best_session": 0, "last_seen": "–"})
    e["career_cr"] = max(0, e.get("career_cr", 0) + delta)
    r, rname = kb_rank(e["career_cr"])
    e["rank"] = r
    e["rank_name"] = rname
    career[key] = e
    save_jf(KB_CAREER, career)
    return redirect("/owner/panel")

@app.route("/owner/set_special_rank", methods=["POST"])
def owner_set_special_rank():
    if not _owner_check():
        return redirect("/owner")
    uname = request.form.get("uname", "").strip()
    t1 = request.form.get("title1", "").strip()
    t2 = request.form.get("title2", "").strip()
    users = load_users()
    if uname not in users:
        return redirect("/owner/panel")
    new_ranks = [t for t in [t1, t2] if t][:2]
    users[uname]["special_ranks"] = new_ranks
    users[uname].pop("special_rank", None)  # odstráň starý formát
    save_users(users)
    return redirect("/owner/panel")

@app.route("/owner/ban", methods=["POST"])
def owner_ban():
    if not _owner_check():
        return redirect("/owner")
    uname = request.form.get("uname", "").strip()
    dur   = request.form.get("dur", "1h")
    users = load_users()
    if uname not in users:
        return redirect("/owner/panel")
    dur_map = {"10m": 600, "1h": 3600, "12h": 43200, "24h": 86400}
    if dur == "perm":
        users[uname]["banned_until"] = -1
    else:
        secs = dur_map.get(dur, 3600)
        users[uname]["banned_until"] = datetime.now().timestamp() + secs
    save_users(users)
    return redirect("/owner/panel")

@app.route("/owner/unban/<uname>")
def owner_unban(uname):
    if not _owner_check():
        return redirect("/owner")
    users = load_users()
    if uname in users:
        users[uname]["banned_until"] = None
        save_users(users)
    return redirect("/owner/panel")

@app.route("/owner/toggle_admin/<uname>")
def owner_toggle_admin(uname):
    if not _owner_check():
        return redirect("/owner")
    users = load_users()
    if uname in users:
        users[uname]["is_admin"] = not users[uname].get("is_admin", False)
        save_users(users)
    return redirect("/owner/panel")


@app.route("/owner/toggle_tester/<uname>")
def owner_toggle_tester(uname):
    if not _owner_check():
        return redirect("/owner")
    users = load_users()
    if uname in users:
        users[uname]["is_tester"] = not users[uname].get("is_tester", False)
        save_users(users)
    return redirect("/owner/panel")


@app.route("/owner/delete/<uname>")
def owner_delete_user(uname):
    if not _owner_check():
        return redirect("/owner")
    users = load_users()
    users.pop(uname, None)
    save_users(users)
    # Vymaž aj kariéru a uloženia
    career = load_jf(KB_CAREER, {})
    career.pop(uname.upper(), None)
    save_jf(KB_CAREER, career)
    saves = load_jf(KB_SAVES, {})
    saves.pop(uname.upper(), None)
    save_jf(KB_SAVES, saves)
    return redirect("/owner/panel")


@app.route("/owner/set_rank_tier", methods=["POST"])
def owner_set_rank_tier():
    if not _owner_check():
        return redirect("/owner")
    uname = request.form.get("uname", "").strip()
    try:
        tier = int(request.form.get("tier", 1))
    except ValueError:
        return redirect("/owner/panel")
    name_map = {t: name for t, name, _ in RANKS}
    rank_title = name_map.get(tier, "")
    career = load_jf(KB_CAREER, {})
    key = uname.upper()
    e = career.get(key, {"sessions": 0, "wins": 0, "total_mined": 0,
                         "best_session": 0, "last_seen": "–"})
    if rank_title:
        e["rank_title"] = rank_title
    else:
        e.pop("rank_title", None)
    career[key] = e
    save_jf(KB_CAREER, career)
    return redirect("/owner/panel")


@app.route("/owner/rename", methods=["POST"])
def owner_rename():
    if not _owner_check():
        return redirect("/owner")
    old_name = request.form.get("uname", "").strip()
    new_name = request.form.get("new_name", "").strip()
    if not old_name or not new_name or old_name == new_name:
        return redirect("/owner/panel")
    users = load_users()
    if old_name not in users or new_name in users:
        return redirect("/owner/panel")
    # Premenuj v users
    users[new_name] = users.pop(old_name)
    save_users(users)
    # Premenuj v career
    career = load_jf(KB_CAREER, {})
    old_key, new_key = old_name.upper(), new_name.upper()
    if old_key in career:
        career[new_key] = career.pop(old_key)
        save_jf(KB_CAREER, career)
    # Premenuj v saves
    saves = load_jf(KB_SAVES, {})
    if old_key in saves:
        saves[new_key] = saves.pop(old_key)
        save_jf(KB_SAVES, saves)
    # Premenuj v leaderboard
    lb = load_jf(KB_LB, [])
    for entry in lb:
        if entry.get("username", "").upper() == old_key:
            entry["username"] = new_name
    save_jf(KB_LB, lb)
    return redirect("/owner/panel")


@app.route("/owner/message", methods=["POST"])
def owner_message():
    if not _owner_check():
        return redirect("/owner")
    uname = request.form.get("uname", "").strip()
    text  = request.form.get("msg_text", "").strip()
    if uname and text:
        send_notification(uname, text, from_role="Owner")
    return redirect("/owner/panel")


# ── Admin Panel (pre hráčov s is_admin=True) ────────────────────────────────

@app.route("/adminpanel")
def adminpanel():
    if not _is_admin_user():
        return redirect("/lobby")
    users = load_users()
    career = load_jf(KB_CAREER, {})
    rows = ""
    for uname_orig in sorted(users.keys()):
        u = users[uname_orig]
        spr = get_sp_ranks(u)
        spr_html = ("  ".join(f'<span style="color:#ffd700">&#9733;{s}</span>' for s in spr)
                    or '<span style="color:#444">—</span>')
        c_a = career.get(uname_orig.upper(), {})
        cr_val_a = c_a.get('career_cr', 0)
        rtitle_a = c_a.get('rank_title', '')
        rank_name = rtitle_a if rtitle_a else c_a.get('rank_name', 'Baník')
        cur_tier_a = next((t for t, name, _ in RANKS if name == rtitle_a), kb_rank(cr_val_a)[0])
        rank_btns_a = "".join(
            f'<form method="POST" action="/adminpanel/set_rank_tier" style="display:inline;margin:0 1px 2px 0">'
            f'<input type="hidden" name="uname" value="{uname_orig}">'
            f'<input type="hidden" name="tier" value="{t}">'
            f'<button type="submit" style="background:{"#001800" if t==cur_tier_a else "#000"};'
            f'border:1px solid {"#39ff6a" if t==cur_tier_a else "#2a3a2a"};'
            f'color:{"#39ff6a" if t==cur_tier_a else "#556655"};'
            f'padding:1px 5px;cursor:pointer;font-family:inherit;font-size:.75em;white-space:nowrap">'
            f'{name}</button></form>'
            for t, name, _ in RANKS
        )
        spr0 = spr[0] if len(spr) > 0 else ''
        spr1 = spr[1] if len(spr) > 1 else ''
        notifs_a = u.get("notifications", [])
        notif_hist_a = "".join(
            f'<div style="font-size:.78em;color:{"#aaa" if n.get("read") else "#ffe08a"};'
            f'border-bottom:1px solid #1a1a1a;padding:1px 0">'
            f'<span style="color:#555">[{n.get("ts","")} {n.get("from","")}]</span> {n.get("text","")}'
            f'</div>'
            for n in notifs_a[-5:]
        ) or f'<span style="color:#333;font-size:.78em">—</span>'
        msg_cell_a = (
            f'<details style="min-width:160px">'
            f'<summary style="cursor:pointer;color:#7788cc;font-size:.8em;list-style:none">'
            f'&#9993; {len(notifs_a)} správ</summary>'
            f'<div style="max-height:80px;overflow-y:auto;margin:3px 0">{notif_hist_a}</div>'
            f'<form method="POST" action="/adminpanel/message" style="display:flex;gap:3px;margin-top:3px">'
            f'<input type="hidden" name="uname" value="{uname_orig}">'
            f'<input type="text" name="msg_text" placeholder="správa..." '
            f'style="flex:1;min-width:0;background:#000;border:1px solid #7788cc;color:#aabbff;'
            f'font-family:inherit;font-size:.8em;padding:2px 4px;outline:none">'
            f'<button type="submit" style="background:#000;border:1px solid #7788cc;'
            f'color:#aabbff;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.8em">&#9993;</button>'
            f'</form></details>'
        )
        rows += f"""<tr>
          <td><strong>{uname_orig}</strong></td>
          <td style="color:#ffdd44">{rank_name}</td>
          <td>{spr_html}</td>
          <td>{msg_cell_a}</td>
          <td style="white-space:nowrap">
            <form method="POST" action="/adminpanel/set_rank" style="display:inline">
              <input type="hidden" name="uname" value="{uname_orig}">
              <input type="text" name="title1" value="{spr0}" placeholder="Rank 1"
                style="width:80px;background:#1a1400;border:1px solid #ffd700;color:#ffd700;
                font-family:inherit;font-size:.85em;padding:2px 4px">
              <input type="text" name="title2" value="{spr1}" placeholder="Rank 2"
                style="width:80px;background:#1a1400;border:1px solid #ffd700;color:#ffd700;
                font-family:inherit;font-size:.85em;padding:2px 4px">
              <button type="submit" style="background:#1a1400;border:1px solid #ffd700;
                color:#ffd700;padding:2px 8px;cursor:pointer;font-family:inherit;font-size:.85em">
                &#9733; Uloz
              </button>
            </form>
            <div style="display:inline-flex;flex-wrap:wrap;gap:1px;vertical-align:middle">{rank_btns_a}</div>
          </td>
        </tr>"""
    return f"""<!DOCTYPE html><html><head><title>Admin Panel</title>{ADMIN_CSS}
<style>table{{font-size:.85em}}td,th{{padding:5px 8px;vertical-align:middle}}
input[type=text]{{outline:none}}</style>
</head><body>
<h1>&#9733; ADMIN PANEL &mdash; KOZMICK&#201; BANE v4.8</h1>
<p style="color:#888;font-size:.85rem">
  Prihlasen&#253; ako: <strong style="color:#ffb000">{session['username']}</strong> &nbsp;|&nbsp;
  <a href="/lobby" class="btn">Lobby</a>
</p>
<p style="color:#aaa;font-size:.85rem">Nastav&#237; maxim&#225;lne 2 &#353;peci&#225;lne ranky pre hr&#225;&#269;a.
  <span style="color:#39ff6a"> Napr.: {", ".join(ADMIN_RANKS_HINT)}</span> &nbsp;|&nbsp;
  <span style="color:#ff9900"> Vyhradené pre ownera: {", ".join(sorted(OWNER_ONLY_RANKS))}</span>
</p>
<h2>&#128101; HR&#193;&#268;I</h2>
<table>
  <tr><th>Hrac</th><th>Rang</th><th>Spec. ranky</th><th>&#9993; Správy</th><th>Nastav</th></tr>
  {rows}
</table>
</body></html>"""


@app.route("/adminpanel/set_rank", methods=["POST"])
def adminpanel_set_rank():
    if not _is_admin_user():
        return redirect("/lobby")
    uname = request.form.get("uname", "").strip()
    t1 = request.form.get("title1", "").strip()
    t2 = request.form.get("title2", "").strip()
    users = load_users()
    if uname not in users:
        return redirect("/adminpanel")
    # Admin nemôže nastaviť owner-only tituly
    forbidden = {r.lower() for r in OWNER_ONLY_RANKS}
    new_ranks = [t for t in [t1, t2] if t and t.lower() not in forbidden][:2]
    users[uname]["special_ranks"] = new_ranks
    users[uname].pop("special_rank", None)
    save_users(users)
    return redirect("/adminpanel")


@app.route("/adminpanel/set_rank_tier", methods=["POST"])
def adminpanel_set_rank_tier():
    if not _is_admin_user():
        return redirect("/lobby")
    uname = request.form.get("uname", "").strip()
    try:
        tier = int(request.form.get("tier", 1))
    except ValueError:
        return redirect("/adminpanel")
    name_map = {t: name for t, name, _ in RANKS}
    rank_title = name_map.get(tier, "")
    career = load_jf(KB_CAREER, {})
    key = uname.upper()
    e = career.get(key, {"sessions": 0, "wins": 0, "total_mined": 0,
                         "best_session": 0, "last_seen": "–"})
    if rank_title:
        e["rank_title"] = rank_title
    else:
        e.pop("rank_title", None)
    career[key] = e
    save_jf(KB_CAREER, career)
    return redirect("/adminpanel")


@app.route("/adminpanel/message", methods=["POST"])
def adminpanel_message():
    if not _is_admin_user():
        return redirect("/lobby")
    uname = request.form.get("uname", "").strip()
    text  = request.form.get("msg_text", "").strip()
    if uname and text:
        send_notification(uname, text, from_role="Admin")
    return redirect("/adminpanel")


# ── Energetická minihra ─────────────────────────────────────────────────────

def _energy_allowed():
    """True ak má user prístup k energetickej minihre (tester alebo feature je public)."""
    if "username" not in session:
        return False
    energy_public = next((f for f in BETA_FEATURES if f["id"] == "energy_minigame"), {}).get("public", False)
    if energy_public:
        return True
    users = load_users()
    u = users.get(session["username"], {})
    return u.get("is_tester") is True


@app.route("/energy")
def energy_page():
    if not _require_session():
        return redirect("/")
    if not _energy_allowed():
        return redirect("/lobby")

    uname = _uname()
    profile = _energy_tick(uname)

    career  = load_jf(KB_CAREER, {})
    cr      = career.get(uname, {}).get("career_cr", 0)

    # Počty elektrární podľa typu

    plant_counts = Counter(profile.get("plants", []))
    fuel  = profile.get("fuel", {"coal": 0, "uranium": 0})
    energy = profile.get("energy", 0.0)

    # Celková produkcia za hodinu
    total_rate = 0.0
    raw_mats_disp = profile.get("raw_materials", {})
    for pid, cnt in plant_counts.items():
        pt = PLANT_TYPES.get(pid)
        if not pt:
            continue
        if pt["fuel_type"] is None:
            total_rate += pt["energy_per_hr"] * cnt
        elif pt["fuel_type"] == "u238_breed":
            if raw_mats_disp.get("u238", 0) > 0:
                total_rate += pt["energy_per_hr"] * cnt
        else:
            has_fuel = fuel.get(pt["fuel_type"], 0) > 0
            if pt["fuel_type"] == "uranium":
                has_fuel = has_fuel or fuel.get("pu239", 0) > 0
            if has_fuel:
                total_rate += pt["energy_per_hr"] * cnt

    lang = session.get("lang", "sk")

    def Lp(sk, en):
        return en if lang == "en" else sk

    css = """
<style>
@import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#000;color:#39ff6a;font-family:'VT323',monospace;
  min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:16px 16px 40px;}
h1{color:#39ff6a;font-size:1.8em;letter-spacing:.1em;margin:10px 0 4px;text-align:center;
  text-shadow:0 0 18px #39ff6a88;}
.sub{color:#2a7a45;font-size:.9em;margin-bottom:18px;letter-spacing:.08em;}
.card{background:#010d01;border:1px solid #39ff6a44;width:100%;max-width:680px;
  padding:16px 20px 18px;margin-bottom:12px;}
.card-title{color:#39ff6a;font-size:1.05em;border-bottom:1px solid #0d2a0d;
  padding-bottom:5px;margin-bottom:12px;letter-spacing:.08em;}
.row{display:flex;justify-content:space-between;align-items:center;
  padding:5px 0;border-bottom:1px solid #0a1a0a;font-size:.95em;}
.row:last-child{border-bottom:none;}
.lbl{color:#2a7a45;}
.val{color:#cfffcf;}
.plant-row{display:flex;justify-content:space-between;align-items:center;
  padding:7px 0;border-bottom:1px solid #0a1a0a;}
.plant-row:last-child{border-bottom:none;}
.active{color:#39ff6a;}
.idle{color:#555;}
.btn-buy{background:#010d01;border:1px solid #39ff6a;color:#39ff6a;
  font-family:'VT323',monospace;font-size:.95em;padding:4px 14px;cursor:pointer;
  letter-spacing:.05em;}
.btn-buy:hover{background:#003a10;}
.btn-buy:disabled{border-color:#1a3a1a;color:#1a3a1a;cursor:default;}
.btn-back{display:inline-block;background:#000;border:1px solid #2a7a45;color:#2a7a45;
  font-family:'VT323',monospace;font-size:1em;padding:6px 16px;text-decoration:none;
  letter-spacing:.06em;margin-bottom:14px;}
.btn-back:hover{background:#0a1a0a;color:#39ff6a;}
.warn{color:#ff9900;font-size:.85em;}
.energy-bar-wrap{background:#0a1a0a;border:1px solid #1a4a1a;height:18px;
  width:100%;margin:6px 0 2px;position:relative;}
.energy-bar-fill{height:100%;background:#39ff6a;transition:width .3s;}
.energy-bar-label{position:absolute;top:0;left:50%;transform:translateX(-50%);
  font-size:.85em;line-height:18px;color:#000;mix-blend-mode:difference;}
</style>"""

    # ── Vyhorené palivo ──────────────────────────────────────────
    spent_f    = profile.get("raw_materials", {}).get("spent_fuel", 0.0)
    mox_stock  = fuel.get("mox", 0.0)
    has_nuclear = any(p in profile.get("plants", []) for p in ("nuclear", "rbmk"))
    spent_html = ""
    if has_nuclear or spent_f > 0 or mox_stock > 0:
        warn_sf = ""
        sf_col  = "#ff3a3a" if spent_f >= SPENT_FUEL_MAX else "#ff9900" if spent_f >= SPENT_FUEL_MAX * 0.7 else "#2a7a45"
        if spent_f >= SPENT_FUEL_MAX:
            warn_sf = f'<div style="color:#ff3a3a;font-size:.82em">🔒 {Lp("REGULATORY SHUTDOWN aktívny!","REGULATORY SHUTDOWN active!")}</div>'
        elif spent_f >= SPENT_FUEL_MAX * 0.7:
            warn_sf = f'<div style="color:#ff9900;font-size:.82em">⚠ {Lp("Kapacita spent fuel sa blíži limitu!","Spent fuel approaching capacity!")}</div>'
        int_spent = int(spent_f)
        purex_rods_out = int_spent // PUREX_RATIO
        pu_avail_mox = fuel.get("pu239", 0.0)
        u238_avail_mox = profile.get("raw_materials", {}).get("u238", 0.0)
        max_mox = min(int_spent, int(u238_avail_mox / MOX_U238_PER_ROD), int(pu_avail_mox / MOX_PU_PER_ROD))
        cr_dispose = int_spent * DISPOSAL_CR
        cr_purex   = purex_rods_out * PUREX_CR_PER_ROD

        def sf_form(action, qty_max, btn_label, btn_col, extra_note="", disabled=False):
            d = "disabled" if disabled or qty_max < 1 else ""
            return (
                f'<form method="POST" action="/energy/spent_fuel" '
                f'style="display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap">'
                f'<input type="hidden" name="action" value="{action}">'
                f'<input type="number" name="qty" value="{min(qty_max,5)}" min="1" max="{qty_max}" '
                f'style="width:60px;background:#000;border:1px solid #2a7a45;color:#cfffcf;'
                f'font-family:inherit;font-size:.9em;padding:2px 4px">'
                f'<button class="btn-buy" style="border-color:{btn_col};color:{btn_col}" {d}>'
                f'{btn_label}</button>'
                f'{"<span style=color:#2a7a45;font-size:.82em>" + extra_note + "</span>" if extra_note else ""}'
                f'</form>'
            )

        lbl_spent   = Lp("Vyhorene clanky", "Spent rods")
        lbl_fuel    = Lp("palivo", "fuel")
        lbl_ks      = Lp("ks", "rods")
        lbl_title   = Lp("VYHORENE PALIVO", "SPENT FUEL")
        note_disp   = "-" + str(DISPOSAL_CR) + " CR/" + lbl_ks
        note_purex  = "+" + str(purex_rods_out) + " Pu-239 | heat+" + str(purex_rods_out * PUREX_HEAT)
        note_mox    = "potreb. " + str(MOX_PU_PER_ROD) + "/ks Pu + " + str(MOX_U238_PER_ROD) + "t U-238"
        lbl_dispose = Lp("Likvidovat", "Dispose")
        lbl_purex   = "PUREX ->" + str(purex_rods_out) + " Pu"
        lbl_mox_btn = "MOX ->" + str(max_mox) + " rod"
        form_dispose = sf_form("dispose", int_spent, lbl_dispose, "#7a7a7a",
                                note_disp, cr >= cr_dispose) if int_spent > 0 else ""
        form_purex   = sf_form("purex", int_spent, lbl_purex, "#ff44aa",
                                note_purex, purex_rods_out >= 1 and cr >= cr_purex
                                ) if purex_rods_out >= 1 else ""
        form_mox     = sf_form("mox", max_mox, lbl_mox_btn, "#99ddff",
                                note_mox, max_mox >= 1) if max_mox >= 1 else ""
        spent_html = (
            f'<div class="card" style="border-color:{sf_col}44">'
            f'<div class="card-title" style="color:{sf_col}">&#9762; {lbl_title}</div>'
            f'<div class="row"><span class="lbl">{lbl_spent}</span>'
            f'<span style="color:{sf_col}">{spent_f:.1f} / {SPENT_FUEL_MAX}</span></div>'
            f'<div class="row"><span class="lbl">MOX {lbl_fuel}</span>'
            f'<span class="val">{mox_stock:.1f} {lbl_ks}</span></div>'
            f'{warn_sf}{form_dispose}{form_purex}{form_mox}'
            f'</div>'
        )

    # ── Proliferačná horúčava ─────────────────────────────────────
    p_heat = profile.get("proliferation_heat", 0.0)
    pu239_stock = fuel.get("pu239", 0.0)
    wg_pu_stock = fuel.get("wg_pu", 0.0)
    heat_col = "#ff3a3a" if p_heat >= 80 else "#ff9900" if p_heat >= 50 else "#2a7a45"
    heat_html = ""
    if pu239_stock > 0 or wg_pu_stock > 0 or p_heat > 0:
        heat_warn = ""
        if p_heat >= 80:
            heat_warn = (f'<div style="color:#ff3a3a;font-size:.82em">'
                         f'&#9888; {Lp("SANKCIE AKTÍVNE — predaj energie za 75% ceny","SANCTIONS ACTIVE — energy sells at 75% price")}'
                         f'</div>')
        elif p_heat >= 50:
            heat_warn = (f'<div style="color:#ff9900;font-size:.82em">'
                         f'&#9888; {Lp("Medzinárodný dohľad — predaj energie za 90% ceny","International monitoring — energy sells at 90% price")}'
                         f'</div>')
        wg_row = ""
        if wg_pu_stock > 0:
            wg_can_sell = int(wg_pu_stock)
            wg_sell_form = ""
            if wg_can_sell >= 1:
                wg_sell_form = (
                    f'<form method="POST" action="/energy/wg_sell" '
                    f'style="display:flex;gap:6px;align-items:center;margin-top:4px;flex-wrap:wrap">'
                    f'<input type="number" name="qty" value="1" min="1" max="{wg_can_sell}" '
                    f'style="width:55px;background:#000;border:1px solid #ff3a3a;color:#ffcfcf;'
                    f'font-family:inherit;font-size:.9em;padding:2px 4px">'
                    f'<button class="btn-buy" style="border-color:#ff3a3a;color:#ff3a3a">'
                    f'&#9760; {Lp("Predaj WG-Pu","Sell WG-Pu")} +{WG_PU_SELL_CR:,} CR/{Lp("ks","ea")}'
                    f'</button>'
                    f'<span style="color:#7a0000;font-size:.82em">heat+{WG_PU_HEAT_PER_UNIT:.0f}/{Lp("ks","ea")}</span>'
                    f'</form>'
                )
            wg_row = (
                f'<div class="row"><span class="lbl">☣ WG-Pu239</span>'
                f'<span style="color:#ff3a3a">{wg_pu_stock:.3f} {Lp("ks","units")}</span></div>'
                f'{wg_sell_form}'
            )
        heat_html = (
            f'<div class="card" style="border-color:{heat_col}44">'
            f'<div class="card-title" style="color:{heat_col}">&#9762; {Lp("JADROVÉ MATERIÁLY","NUCLEAR MATERIALS")}</div>'
            f'<div class="row"><span class="lbl">☣ Pu-239</span>'
            f'<span class="val">{pu239_stock:.2f} {Lp("ks","rods")}</span></div>'
            f'{wg_row}'
            f'<div class="row"><span class="lbl">{Lp("Proliferačná horúčava","Proliferation heat")}</span>'
            f'<span style="color:{heat_col}">{p_heat:.1f}% '
            f'{"🔴" if p_heat>=80 else "🟠" if p_heat>=50 else "🟢"}</span></div>'
            f'{heat_warn}'
            f'</div>'
        )

    # ── Energy bar
    bar_pct = min(100, round(energy / MAX_ENERGY * 100))
    bar_html = (
        f'<div class="energy-bar-wrap">'
        f'<div class="energy-bar-fill" style="width:{bar_pct}%"></div>'
        f'<div class="energy-bar-label">{energy:.1f} / {MAX_ENERGY}</div>'
        f'</div>'
        f'<div style="color:#2a7a45;font-size:.82em">'
        f'+{total_rate:.0f} {Lp("energie/hod","energy/hr")} &nbsp;|&nbsp; '
        f'{Lp("kariéra CR","career CR")}: <span style="color:#cfffcf">{cr:,}</span>'
        f'</div>'
    )

    # ── Plantas zoznam
    plants_html = ""
    if not plant_counts:
        plants_html = f'<div class="idle" style="padding:6px 0">{Lp("Žiadne elektrárne. Postav svoju prvú elektráreň nižšie.", "No power plants. Build your first one below.")}</div>'
    else:
        for pid, cnt in sorted(plant_counts.items()):
            pt = PLANT_TYPES[pid]
            name  = Lp(pt["name_sk"], pt["name_en"])
            if pt["fuel_type"] is None:
                status = f'<span class="active">▶ {Lp("AKTÍVNA","ACTIVE")}</span>'
                rate_str = f'+{pt["energy_per_hr"] * cnt} E/hod'
                fuel_str = ""
            else:
                # Špeciálne fuel typy (breeder používa U-238 z raw_materials)
                if pt["fuel_type"] == "u238_breed":
                    raw_mats = profile.get("raw_materials", {})
                    fstock   = raw_mats.get("u238", 0.0)
                    fuel_name = Lp("Ochudobnený U-238", "Depleted U-238")
                    unit      = "t"
                else:
                    fstock    = fuel.get(pt["fuel_type"], 0)
                    fs_entry  = next((f for f in FUEL_SHOP if f["id"] == pt["fuel_type"]), None)
                    fuel_name = Lp(fs_entry["name_sk"], fs_entry["name_en"]) if fs_entry else pt["fuel_type"]
                    unit      = Lp(fs_entry["unit_sk"], fs_entry["unit_en"]) if fs_entry else "ks"
                # Pu-239 má prednosť v jadrových elektrárňach
                if pt["fuel_type"] == "uranium":
                    pu_stk = fuel.get("pu239", 0.0)
                    if pu_stk > 0:
                        fstock = pu_stk
                        fuel_name = "Pu-239"
                        unit = "ks"
                if fstock > 0:
                    status   = f'<span class="active">▶ {Lp("AKTÍVNA","ACTIVE")}</span>'
                    rate_str = f'+{pt["energy_per_hr"] * cnt} E/hod'
                else:
                    status   = f'<span class="idle">⏸ {Lp("NEČINNÁ — bez paliva","IDLE — no fuel")}</span>'
                    rate_str = "+0 E/hod"
                fuel_str = f'<span style="color:#2a7a45;font-size:.85em"> — {fuel_name}: {fstock:.1f} {unit}</span>'
            refund = round(pt["build_cost"] * 0.20)
            plants_html += (
                f'<div class="plant-row">'
                f'<span>{pt["icon"]} {name} ×{cnt}{fuel_str}</span>'
                f'<span style="display:flex;align-items:center;gap:6px">'
                f'{status} &nbsp; <span class="val">{rate_str}</span>'
                f'<form method="POST" action="/energy/demolish" style="display:inline;margin:0">'
                f'<input type="hidden" name="plant_id" value="{pid}">'
                f'<button class="btn-buy" style="border-color:#ff3a3a44;color:#ff6a6a;font-size:.78em;padding:1px 6px" '
                f'title="{Lp("Zbúrať 1 kus","Demolish 1")} (+{refund:,} CR)">'
                f'🗑 {refund:,}</button>'
                f'</form>'
                f'</span>'
                f'</div>'
            )

    # ── Elektrárne na kúpu
    _PLANT_PREREQS = {"bn800": "breeder"}  # bn800 vyžaduje breeder
    buy_plants_html = ""
    for pid, pt in PLANT_TYPES.items():
        name = Lp(pt["name_sk"], pt["name_en"])
        desc = Lp(pt["desc_sk"], pt["desc_en"])
        cnt  = plant_counts.get(pid, 0)
        prereq = _PLANT_PREREQS.get(pid)
        missing_prereq = prereq and prereq not in profile.get("plants", [])
        can_afford = cr >= pt["build_cost"]
        at_max     = cnt >= pt["max_count"]
        disabled   = "disabled" if (not can_afford or at_max or missing_prereq) else ""
        note       = f'({Lp("max","max")} {pt["max_count"]})' if at_max else ""
        if missing_prereq:
            prereq_name = Lp(PLANT_TYPES[prereq]["name_sk"], PLANT_TYPES[prereq]["name_en"])
            warn = f'<span class="warn"> — {Lp("vyžaduje","requires")} {prereq_name}</span>'
        elif not can_afford and not at_max:
            warn = f'<span class="warn"> — {Lp("nedostatok CR","not enough CR")}</span>'
        else:
            warn = ""
        buy_plants_html += (
            f'<div class="plant-row">'
            f'<div><span style="color:#cfffcf">{pt["icon"]} {name}</span>'
            f' &nbsp;<span style="color:#2a7a45;font-size:.85em">{desc}</span>'
            f'{warn}</div>'
            f'<form method="POST" action="/energy/build" style="display:inline">'
            f'<input type="hidden" name="type" value="{pid}">'
            f'<button class="btn-buy" {disabled}>'
            f'{pt["build_cost"]:,} CR {note}'
            f'</button></form>'
            f'</div>'
        )

    # ── Palivo na kúpu
    buy_fuel_html = ""
    for fs in FUEL_SHOP:
        fname = Lp(fs["name_sk"], fs["name_en"])
        unit  = Lp(fs["unit_sk"], fs["unit_en"])
        stock = fuel.get(fs["id"], 0)
        can_afford = cr >= fs["pack_cost"]
        disabled   = "" if can_afford else "disabled"
        warn       = f'<span class="warn"> {Lp("nedostatok CR","not enough CR")}</span>' if not can_afford else ""
        buy_fuel_html += (
            f'<div class="plant-row">'
            f'<div><span style="color:#cfffcf">{fs["icon"]} {fname}</span>'
            f' &nbsp;<span style="color:#2a7a45;font-size:.85em">{Lp("Zostatok","Stock")}: {stock:.1f} {unit}</span>'
            f'{warn}</div>'
            f'<form method="POST" action="/energy/buy_fuel" style="display:inline">'
            f'<input type="hidden" name="fuel_id" value="{fs["id"]}">'
            f'<button class="btn-buy" {disabled}>'
            f'+{fs["pack_qty"]} {unit} — {fs["pack_cost"]:,} CR'
            f'</button></form>'
            f'</div>'
        )

    # ── Jadrová vetva — bane + obohacovanie ─────────────────────
    mine_counts  = Counter(profile.get("mines", []))
    raw_mats     = profile.get("raw_materials", {})
    uranium_raw  = raw_mats.get("uranium_raw", 0.0)
    max_rods     = int(uranium_raw) // ENRICH_RATIO

    # Aktívne bane
    mine_status_html = ""
    for mid, mt in MINE_TYPES.items():
        cnt  = mine_counts.get(mid, 0)
        name = Lp(mt["name_sk"], mt["name_en"])
        if cnt > 0:
            mine_status_html += (
                f'<div class="plant-row">'
                f'<span>{mt["icon"]} {name} ×{cnt}</span>'
                f'<span class="active">▶ +{mt["rate_per_hr"]*cnt:.0f} t/hod</span>'
                f'</div>'
            )

    # Surový urán + obohacovanie
    enrich_html = ""
    if uranium_raw > 0:
        can_enrich = max_rods > 0
        enrich_html = (
            f'<div class="plant-row">'
            f'<div>'
            f'<span style="color:#cfffcf">☢ {Lp("Surový urán","Raw uranium")}: '
            f'{uranium_raw:.1f} t</span>'
            f'<span style="color:#2a7a45;font-size:.85em"> '
            f'({Lp("max","max")} {max_rods} {Lp("palivových článkov","fuel rods")})</span>'
            f'</div>'
            f'{"" if not can_enrich else ""}'
            f'</div>'
        )
        if can_enrich:
            enrich_html += (
                f'<div class="plant-row">'
                f'<span style="color:#2a7a45;font-size:.85em">'
                f'{Lp("Obohacovanie","Enrichment")}: {ENRICH_RATIO} t → 1 {Lp("palivový článok","fuel rod")}</span>'
                f'<form method="POST" action="/energy/enrich" style="display:inline-flex;gap:4px;align-items:center">'
                f'<input type="number" name="qty_raw" value="{ENRICH_RATIO}" '
                f'min="{ENRICH_RATIO}" step="{ENRICH_RATIO}" max="{int(uranium_raw)}" '
                f'style="width:70px;background:#000;border:1px solid #2a7a45;color:#cfffcf;'
                f'font-family:inherit;font-size:.9em;padding:2px 4px">'
                f'<button class="btn-buy">⚗ {Lp("Obohatiť","Enrich")}</button>'
                f'</form>'
                f'</div>'
            )

    # Kúpa bane
    buy_mines_html = ""
    for mid, mt in MINE_TYPES.items():
        name  = Lp(mt["name_sk"], mt["name_en"])
        desc  = Lp(mt["desc_sk"], mt["desc_en"])
        cnt   = mine_counts.get(mid, 0)
        at_max     = cnt >= mt["max_count"]
        can_afford = cr >= mt["build_cost"]
        disabled   = "disabled" if (at_max or not can_afford) else ""
        note  = f'({Lp("max","max")} {mt["max_count"]})' if at_max else ""
        warn  = f'<span class="warn"> — {Lp("nedostatok CR","not enough CR")}</span>' if (not can_afford and not at_max) else ""
        buy_mines_html += (
            f'<div class="plant-row">'
            f'<div><span style="color:#cfffcf">{mt["icon"]} {name}</span>'
            f' &nbsp;<span style="color:#2a7a45;font-size:.85em">{desc}</span>'
            f'{warn}</div>'
            f'<form method="POST" action="/energy/build_mine" style="display:inline">'
            f'<input type="hidden" name="type" value="{mid}">'
            f'<button class="btn-buy" {disabled}>{mt["build_cost"]:,} CR {note}</button>'
            f'</form>'
            f'</div>'
        )

    mine_section = mine_status_html + enrich_html
    _mines_html = (
        f'<div class="card" style="border-color:#ffaa0044">'
        f'<div class="card-title" style="color:#ffaa00">'
        f'&#9762; {Lp("JADROVÁ VETVA — ŤAŽBA URÁNU","NUCLEAR — URANIUM MINING")}</div>'
        f'{mine_section if mine_section else f"""<div class="idle" style="padding:6px 0">{Lp("Žiadne bane. Postav svoju prvú baňu nižšie.", "No mines. Build your first one below.")}</div>"""}'
        f'<div style="border-top:1px solid #0d2a0d;margin-top:10px;padding-top:8px">'
        f'<div style="color:#2a7a45;font-size:.82em;margin-bottom:6px">'
        f'&#43; {Lp("POSTAVIŤ BAŇU","BUILD MINE")}</div>'
        f'{buy_mines_html}'
        f'</div>'
        f'</div>'
    ) if (mine_section or True) else ""

    # ── Safety upgrades + poškodené reaktory ────────────────────
    now = time.time()
    safety_lvl  = profile.get("safety_level", 0)
    damaged_now = [d for d in profile.get("damaged_plants", []) if d["expires_at"] > now]
    has_rbmk    = "rbmk" in profile.get("plants", [])
    has_breeder = "breeder" in profile.get("plants", [])
    has_bn800   = "bn800" in profile.get("plants", [])
    show_safety = has_rbmk or has_breeder or has_bn800 or safety_lvl > 0

    # ── Dispatch pressure ────────────────────────────────────────
    dispatch_html = ""
    if profile.get("dispatch_pending"):
        opts_html = ""
        for o in DISPATCH_OPTS:
            label = Lp(o["label_sk"], o["label_en"])
            opts_html += (
                f'<form method="POST" action="/energy/dispatch" style="margin:3px 0">'
                f'<input type="hidden" name="choice" value="{o["id"]}">'
                f'<button class="btn-buy" style="width:100%;text-align:left;'
                f'{"border-color:#ff9900;color:#ff9900" if o["id"]=="accept" else "border-color:#2a7a45;color:#2a7a45" if o["id"]=="refuse" else ""}">'
                f'{label}</button></form>'
            )
        dispatch_html = (
            f'<div class="card" style="border-color:#ff990088;background:#0d0900">'
            f'<div class="card-title" style="color:#ff9900">'
            f'📞 {Lp("DISPEČING — POŽIADAVKA NA VÝKON","DISPATCH — POWER DEMAND")}</div>'
            f'<div style="color:#cfffcf;font-size:.9em;margin-bottom:8px">'
            f'{Lp("Sieťový dispečer žiada dodatočný výkon. Rozhodni sa:","Grid operator demands extra output. Choose:")}'
            f'</div>'
            f'{opts_html}'
            f'</div>'
        )

    # ── Soviet event ─────────────────────────────────────────────
    soviet_html = ""
    if profile.get("soviet_event_pending"):
        s_opts_html = ""
        for o in SOVIET_OPTS:
            label = Lp(o["label_sk"], o["label_en"])
            s_color = "#ff3a3a" if o["id"] == "accept" else "#2a7a45" if o["id"] == "refuse" else "#ff9900"
            s_opts_html += (
                f'<form method="POST" action="/energy/soviet_event" style="margin:3px 0">'
                f'<input type="hidden" name="choice" value="{o["id"]}">'
                f'<button class="btn-buy" style="width:100%;text-align:left;'
                f'border-color:{s_color};color:{s_color}">'
                f'{label}</button></form>'
            )
        soviet_html = (
            f'<div class="card" style="border-color:#ff3a3a88;background:#0d0000">'
            f'<div class="card-title" style="color:#ff3a3a">'
            f'☢ {Lp("UTAJENÁ SOVIETSKÁ ELEKTRÁREŇ","SECRET SOVIET PLANT")}</div>'
            f'<div style="color:#ffcfcf;font-size:.9em;margin-bottom:8px">'
            f'{Lp("Neidentifikovaná jadrová elektráreň žiada koodinovaný výkon. Riziko je reálne.","Unidentified nuclear facility requests coordinated output. The risk is real.")}'
            f'</div>'
            f'{s_opts_html}'
            f'</div>'
        )

    # ── RBMK online refueling ─────────────────────────────────────
    rbmk_ol_html = ""
    if has_rbmk:
        ol_on = profile.get("rbmk_online_refuel", False)
        ol_col = "#ff3a3a" if ol_on else "#ff9900"
        ol_btn_txt = Lp("Vypnúť online refueling", "Disable online refueling") if ol_on else Lp("Zapnúť RBMK online refueling", "Enable RBMK online refueling")
        wg_rate = WG_PU_PER_HR_RBMK_OL
        rbmk_ol_html = (
            f'<div class="card" style="border-color:{ol_col}44;background:#0d0500">'
            f'<div class="card-title" style="color:{ol_col}">⚙☢ {Lp("RBMK — ONLINE PALIVOVÁ VÝMENA","RBMK — ONLINE FUEL REPLACEMENT")}</div>'
            f'<div style="color:#ffcfcf;font-size:.85em;margin-bottom:6px">'
            f'{Lp(f"RBMK nevypína pri výmene paliva — produkuje +{wg_rate}/hod WG-Pu a zvyšuje proliferačnú horúčavu +{ONLINE_REFUEL_HEAT_HR}/hod.",f"RBMK stays online during fuel swap — generates +{wg_rate}/hr WG-Pu and raises proliferation heat +{ONLINE_REFUEL_HEAT_HR}/hr.")}'
            f'</div>'
            f'<div class="row"><span class="lbl">{Lp("Stav","Status")}</span>'
            f'<span style="color:{ol_col}">{"🟢 " + Lp("AKTÍVNY","ACTIVE") if ol_on else "⚫ " + Lp("NEAKTÍVNY","INACTIVE")}</span></div>'
            f'<form method="POST" action="/energy/rbmk_online" style="margin-top:6px">'
            f'<button class="btn-buy" style="border-color:{ol_col};color:{ol_col};width:100%">'
            f'{"⚙ " + ol_btn_txt}</button></form>'
            f'</div>'
        )

    # ── Xenón + Kalkar ───────────────────────────────────────────
    xenon_html = ""
    xenon_val  = profile.get("xenon_level", 0.0)
    purge_on   = profile.get("xenon_purge", False)
    hm_val     = profile.get("hazard_mult_val", 1.0)
    hm_exp     = profile.get("hazard_mult_expires", 0.0)
    kalkar_ok  = (
        "rbmk" in profile.get("plants", []) and
        xenon_val == 0.0 and
        "rbmk" not in {d["plant_id"] for d in profile.get("damaged_plants", [])} and
        not profile.get("kalkar_converted", False)
    )
    if "rbmk" in profile.get("plants", []):
        xen_col  = "#ff3a3a" if xenon_val >= XENON_DANGER else "#ff9900" if xenon_val >= XENON_WARN else "#39ff6a"
        xen_warn = ""
        if xenon_val >= XENON_DANGER:
            xen_warn = f'<div style="color:#ff3a3a;font-size:.82em">⚠ {Lp("KRITICKÁ HLADINA — riziko havárie ×2.5!","CRITICAL — accident risk ×2.5!")}</div>'
        elif xenon_val >= XENON_REDUCE:
            xen_warn = f'<div style="color:#ff9900;font-size:.82em">⚠ {Lp("Výkon RBMK znížený na 55%","RBMK output reduced to 55%")}</div>'
        elif xenon_val >= XENON_WARN:
            xen_warn = f'<div style="color:#ff9900;font-size:.82em">{Lp("Xenón rastie. Zváž purge.","Xenon rising. Consider purge.")}</div>'
        # Kaskádové skóre
        cas_score = profile.get("cascade_score", 0.0)
        if cas_score >= CASCADE_THRESHOLD:
            cas_col = "#ff3a3a"
            cas_warn = f'<div style="color:#ff3a3a;font-size:.82em">🔴 {Lp("KASKÁDOVÁ HAVÁRIA HROZÍ!","CASCADE ACCIDENT IMMINENT!")}</div>'
        elif cas_score >= CASCADE_THRESHOLD * 0.6:
            cas_col = "#ff9900"
            cas_warn = f'<div style="color:#ff9900;font-size:.82em">⚠ {Lp("Kaskádové faktory sa hromadia!","Cascade factors accumulating!")}</div>'
        else:
            cas_col = "#2a7a45"
            cas_warn = ""
        xen_warn += (
            f'<div class="row"><span class="lbl">{Lp("Kaskádové skóre","Cascade score")}</span>'
            f'<span style="color:{cas_col}">{cas_score:.1f} / {CASCADE_THRESHOLD}</span></div>'
            f'{cas_warn}'
        )
        purge_btn_txt = Lp("Vypnúť PURGE","Stop PURGE") if purge_on else Lp("Spustiť XENÓN PURGE","Start XENON PURGE")
        purge_note    = Lp("(−40 E/hod, rýchle čistenie xenónu)","(−40 E/hr, fast xenon cleanup)")
        hm_html = ""
        if hm_val > 1.0 and hm_exp > now:
            secs_hm = max(0, int(hm_exp - now))
            hm_html = (f'<div style="color:#ff3a3a;font-size:.82em">'
                       f'⚡ {Lp("Riziko havárie","Accident risk")} ×{hm_val:.1f} '
                       f'({secs_hm//3600}h {(secs_hm%3600)//60:02d}m {Lp("zostatok","left")})'
                       f'</div>')
        kalkar_btn = ""
        if kalkar_ok:
            kalkar_btn = (
                f'<form method="POST" action="/energy/kalkar" style="margin-top:8px">'
                f'<button class="btn-buy" style="border-color:#ff88ff;color:#ff88ff;width:100%">'
                f'🎢 {Lp("KALKAR — Konvertuj na zábavný park (+50 000 CR)","KALKAR — Convert to theme park (+50,000 CR)")}'
                f'</button></form>'
            )
        xenon_html = (
            f'<div class="card" style="border-color:{xen_col}44">'
            f'<div class="card-title" style="color:{xen_col}">☢ {Lp("RBMK — XENÓN-135","RBMK — XENON-135")}</div>'
            f'<div class="row"><span class="lbl">Xe-135</span>'
            f'<span style="color:{xen_col}">{xenon_val:.1f} / 100</span></div>'
            f'<div style="background:#0a0a0a;height:8px;border:1px solid {xen_col}33;margin:4px 0">'
            f'<div style="height:100%;width:{xenon_val:.0f}%;background:{xen_col};transition:width .3s"></div>'
            f'</div>'
            f'{xen_warn}{hm_html}'
            f'<form method="POST" action="/energy/xenon_purge" style="margin-top:6px">'
            f'<button class="btn-buy" style="border-color:{"#39ff6a" if purge_on else "#ff9900"};'
            f'color:{"#39ff6a" if purge_on else "#ff9900"}">'
            f'{"⚙ "+purge_btn_txt}</button></form>'
            f'<div style="color:#2a7a45;font-size:.82em;margin-top:2px">{purge_note}</div>'
            f'{kalkar_btn}'
            f'</div>'
        )

    _hazard_html = ""
    if show_safety:
        # Poškodené reaktory
        dmg_rows = ""
        for d in damaged_now:
            pid   = d["plant_id"]
            pname = PLANT_TYPES.get(pid, {}).get("name_sk" if lang != "en" else "name_en", pid)
            secs  = max(0, int(d["expires_at"] - now))
            h, r  = divmod(secs, 3600)
            m_d, _ = divmod(r, 60)
            t_str = f"{h}h {m_d:02d}m"
            rcost = REPAIR_COSTS.get(pid, 10000)
            can_repair = cr >= rcost
            dmg_rows += (
                f'<div class="plant-row" style="color:#ff3a3a">'
                f'<span>💥 {pname} — {Lp("offline","offline")} {t_str} | '
                f'{d["damage_type"]}</span>'
                f'<form method="POST" action="/energy/repair" style="display:inline">'
                f'<input type="hidden" name="plant_id" value="{pid}">'
                f'<button class="btn-buy" style="border-color:#ff3a3a;color:#ff3a3a" '
                f'{"" if can_repair else "disabled"}>'
                f'{Lp("Opraviť","Repair")} — {rcost:,} CR</button>'
                f'</form></div>'
            )

        # Safety upgrade button
        next_lvl = safety_lvl + 1
        next_upg = next((u for u in SAFETY_UPGRADES if u["level"] == next_lvl), None)
        safety_btn = ""
        if next_upg:
            uname_txt = Lp(next_upg["name_sk"], next_upg["name_en"])
            udesc_txt = Lp(next_upg["desc_sk"], next_upg["desc_en"])
            can_buy   = cr >= next_upg["cost"]
            safety_btn = (
                f'<div class="plant-row">'
                f'<div><span style="color:#cfffcf">🛡 {uname_txt}</span>'
                f'<span style="color:#2a7a45;font-size:.85em"> — {udesc_txt}</span></div>'
                f'<form method="POST" action="/energy/buy_safety" style="display:inline">'
                f'<button class="btn-buy" {"" if can_buy else "disabled"}>'
                f'{next_upg["cost"]:,} CR</button></form>'
                f'</div>'
            )
        safety_stars = "★" * safety_lvl + "☆" * (3 - safety_lvl)
        prob_rbmk = f'{HAZARD_PROBS["rbmk"][min(safety_lvl,3)]*100:.1f}%' if has_rbmk else "—"
        prob_brd  = f'{HAZARD_PROBS["breeder"][min(safety_lvl,3)]*100:.1f}%' if has_breeder else "—"

        _hazard_html = (
            f'<div class="card" style="border-color:#ff440044">'
            f'<div class="card-title" style="color:#ff9900">'
            f'🛡 {Lp("BEZPEČNOSŤ REAKTOROV","REACTOR SAFETY")}</div>'
            f'<div class="row"><span class="lbl">{Lp("Bezpečnostný stupeň","Safety level")}</span>'
            f'<span style="color:#ff9900">{safety_stars} (L{safety_lvl})</span></div>'
            f'<div class="row"><span class="lbl">{Lp("Riziko havárie/tick","Accident risk/tick")}</span>'
            f'<span class="val">RBMK {prob_rbmk} | Breeder {prob_brd}</span></div>'
            f'{dmg_rows}'
            f'{safety_btn}'
            f'</div>'
        )

    # ── Eventy ──────────────────────────────────────────────────
    last_ev = profile.get("last_event")
    last_ev_html = ""
    if last_ev and (now - last_ev.get("ts", 0)) < 120:
        name_ev = Lp(last_ev["name_sk"], last_ev["name_en"])
        desc_ev = Lp(last_ev["desc_sk"], last_ev["desc_en"])
        col = "#39ff6a" if last_ev["type"] == "pos" else "#ff3a3a"
        last_ev_html = (
            f'<div style="background:{col}18;border:1px solid {col}55;'
            f'color:{col};padding:8px 14px;font-size:1em;max-width:680px;'
            f'width:100%;margin-bottom:10px;letter-spacing:.05em">'
            f'&#9889; {name_ev} &mdash; {desc_ev}'
            f'</div>'
        )

    active_evs = profile.get("active_events", [])
    active_ev_html = ""
    if active_evs:
        rows_ev = ""
        for ae in active_evs:
            name_ae = Lp(ae["name_sk"], ae["name_en"])
            secs_left = max(0, int(ae["expires_at"] - now))
            h, r = divmod(secs_left, 3600)
            m_left, s_left = divmod(r, 60)
            t_str = f"{h}h {m_left:02d}m" if h else f"{m_left:02d}:{s_left:02d}"
            col = "#39ff6a" if ae["effect"] in ("solar_boost","sell_bonus") else "#ff9900"
            rows_ev += (
                f'<div style="display:flex;justify-content:space-between;'
                f'padding:4px 0;border-bottom:1px solid #0a1a0a;font-size:.9em">'
                f'<span style="color:{col}">{name_ae}</span>'
                f'<span style="color:#2a7a45">⏱ {t_str}</span>'
                f'</div>'
            )
        active_ev_html = (
            f'<div class="card" style="border-color:#ff990044">'
            f'<div class="card-title" style="color:#ff9900">'
            f'&#9888; {Lp("AKTÍVNE EVENTY","ACTIVE EVENTS")}</div>'
            f'{rows_ev}</div>'
        )

    html = f"""<!DOCTYPE html><html lang='{lang}'><head>
<meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{Lp("Energetická minihra","Energy Minigame")} — KB</title>
{css}</head><body>
<a href="/lobby" class="btn-back">&#8592; {Lp("Späť do lobby","Back to lobby")}</a>
<h1>&#9889; {Lp("ENERGETICKÁ MINIHRA","ENERGY MINIGAME")}</h1>
<div class="sub">PILOT: {session['username'].upper()} &nbsp;|&nbsp; BETA v0.6 &nbsp;|&nbsp; &#946;</div>
{last_ev_html}
{active_ev_html}
{soviet_html}
{dispatch_html}
{rbmk_ol_html}
{xenon_html}
{_hazard_html}
{spent_html}
{heat_html}

<div class="card">
  <div class="card-title">&#9889; {Lp("ENERGIA — ZÁSOBNÍK","ENERGY STORAGE")}</div>
  {bar_html}
</div>

<div class="card">
  <div class="card-title">&#9108; {Lp("VAŠE ELEKTRÁRNE","YOUR POWER PLANTS")}</div>
  {plants_html}
</div>

<div class="card">
  <div class="card-title">&#43; {Lp("POSTAVIŤ ELEKTRÁREŇ","BUILD POWER PLANT")}</div>
  <div style="color:#2a7a45;font-size:.82em;margin-bottom:8px">
    {Lp("Cena sa odpočíta z tvojich kariérnych CR.","Cost is deducted from your career CR.")}
  </div>
  {buy_plants_html}
</div>

<div class="card">
  <div class="card-title">&#9981; {Lp("PALIVO — ZÁSOBY","FUEL SUPPLIES")}</div>
  <div style="color:#2a7a45;font-size:.82em;margin-bottom:8px">
    {Lp("Cena sa odpočíta z tvojich kariérnych CR.","Cost is deducted from your career CR.")}
  </div>
  {buy_fuel_html}
</div>

{_mines_html}

<a href="/energy/enrichment"
  style="display:block;width:100%;max-width:680px;margin-bottom:8px;
    background:#0d0900;border:1px solid #ffaa00;color:#ffaa00;
    font-family:'VT323',monospace;font-size:1.1em;padding:10px;
    text-align:center;text-decoration:none;letter-spacing:.06em">
  ⚗ {Lp("OBOHACOVACIA STANICA — U-235 vs U-238","ENRICHMENT STATION — U-235 vs U-238")}
</a>
<a href="/market"
  style="display:block;width:100%;max-width:680px;margin-bottom:8px;
    background:#010d01;border:1px solid #39ff6a;color:#39ff6a;
    font-family:'VT323',monospace;font-size:1.1em;padding:10px;
    text-align:center;text-decoration:none;letter-spacing:.06em">
  &#128202; {Lp("NPC TRH — predaj energiu, kúp komodity","NPC MARKET — sell energy, buy commodities")}
</a>
<a href="/auctions"
  style="display:block;width:100%;max-width:680px;margin-bottom:12px;
    background:#010d01;border:1px solid #ff9900;color:#ff9900;
    font-family:'VT323',monospace;font-size:1.1em;padding:10px;
    text-align:center;text-decoration:none;letter-spacing:.06em">
  &#127917; {Lp("AUKCIE — dražby komoditných lotov","AUCTIONS — commodity lot bidding")}
</a>
<a href="/my_country"
  style="display:block;width:100%;max-width:680px;margin-bottom:8px;
    background:#010808;border:1px solid #38d1ff;color:#38d1ff;
    font-family:'VT323',monospace;font-size:1.1em;padding:10px;
    text-align:center;text-decoration:none;letter-spacing:.06em">
  🌍 {Lp("MOJA KRAJINA — správa krajiny a armády","MY COUNTRY — manage country and army")}
</a>
<a href="/energy/invest"
  style="display:block;width:100%;max-width:680px;margin-bottom:12px;
    background:#0a0700;border:1px solid #ff9900;color:#ffcc44;
    font-family:'VT323',monospace;font-size:1.1em;padding:10px;
    text-align:center;text-decoration:none;letter-spacing:.06em">
  💰 {Lp("INVESTÍCIE — vklad CR/energie do iných hráčov","INVESTMENTS — invest CR/energy in other players")}
</a>

</body></html>"""

    return html


@app.route("/energy/build", methods=["POST"])
def energy_build():
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    plant_id = request.form.get("type", "").strip()
    if plant_id not in PLANT_TYPES:
        return redirect("/energy")

    uname  = _uname()
    pt     = PLANT_TYPES[plant_id]
    career = load_jf(KB_CAREER, {})
    entry  = career.get(uname, {})
    cr     = entry.get("career_cr", 0)

    profile = _energy_tick(uname)

    cnt = Counter(profile.get("plants", [])).get(plant_id, 0)

    _prereqs = {"bn800": "breeder"}
    prereq = _prereqs.get(plant_id)
    if prereq and prereq not in profile.get("plants", []):
        return redirect("/energy")

    if cr < pt["build_cost"] or cnt >= pt["max_count"]:
        return redirect("/energy")

    entry["career_cr"] = cr - pt["build_cost"]
    career[uname] = entry
    save_jf(KB_CAREER, career)

    data = load_jf(KB_ENERGY, {})
    data.setdefault(uname, profile)["plants"].append(plant_id)
    save_jf(KB_ENERGY, data)

    return redirect("/energy")


@app.route("/energy/demolish", methods=["POST"])
def energy_demolish():
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    plant_id = request.form.get("plant_id", "").strip()
    if plant_id not in PLANT_TYPES:
        return redirect("/energy")

    uname   = _uname()
    profile = _energy_tick(uname)
    plants  = profile.get("plants", [])

    if plant_id not in plants:
        return redirect("/energy")

    # Odober jednu inštanciu
    plants.remove(plant_id)

    # Ak odstraňujeme posledný breeder a bn800 existuje, zbúraj aj bn800
    _prereqs_reverse = {"breeder": "bn800"}
    dep_plant = _prereqs_reverse.get(plant_id)
    if dep_plant and plant_id not in plants and dep_plant in plants:
        plants.remove(dep_plant)

    # Refund 20 % stavebnej ceny
    pt     = PLANT_TYPES[plant_id]
    refund = round(pt["build_cost"] * 0.20)
    career = load_jf(KB_CAREER, {})
    entry  = career.get(uname, {})
    entry["career_cr"] = entry.get("career_cr", 0) + refund
    career[uname] = entry
    save_jf(KB_CAREER, career)

    profile["plants"] = plants
    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


@app.route("/energy/buy_fuel", methods=["POST"])
def energy_buy_fuel():
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    fuel_id = request.form.get("fuel_id", "").strip()
    fs = next((f for f in FUEL_SHOP if f["id"] == fuel_id), None)
    if not fs:
        return redirect("/energy")

    uname  = _uname()
    career = load_jf(KB_CAREER, {})
    entry  = career.get(uname, {})
    cr     = entry.get("career_cr", 0)

    if cr < fs["pack_cost"]:
        return redirect("/energy")

    entry["career_cr"] = cr - fs["pack_cost"]
    career[uname] = entry
    save_jf(KB_CAREER, career)

    _energy_tick(uname)
    data = load_jf(KB_ENERGY, {})
    profile = data.get(uname, {})
    fuel = profile.get("fuel", {"coal": 0, "uranium": 0})
    fuel[fuel_id] = round(fuel.get(fuel_id, 0) + fs["pack_qty"], 2)
    profile["fuel"] = fuel
    data[uname] = profile
    save_jf(KB_ENERGY, data)

    return redirect("/energy")


# ── Jadrová vetva — Fáza 1 ──────────────────────────────────────────────────

@app.route("/energy/build_mine", methods=["POST"])
def energy_build_mine():
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    mine_id = request.form.get("type", "").strip()
    if mine_id not in MINE_TYPES:
        return redirect("/energy")

    uname  = _uname()
    mt     = MINE_TYPES[mine_id]
    career = load_jf(KB_CAREER, {})
    entry  = career.get(uname, {})
    cr     = entry.get("career_cr", 0)
    profile = _energy_tick(uname)
    cnt = profile.get("mines", []).count(mine_id)

    if cr < mt["build_cost"] or cnt >= mt["max_count"]:
        return redirect("/energy")

    entry["career_cr"] = cr - mt["build_cost"]
    career[uname] = entry
    save_jf(KB_CAREER, career)

    data = load_jf(KB_ENERGY, {})
    data[uname].setdefault("mines", []).append(mine_id)
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


@app.route("/energy/enrich", methods=["POST"])
def energy_enrich():
    """Obohatenie surového uránu na palivové články."""
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    uname = _uname()
    try:
        qty_raw = max(ENRICH_RATIO, int(request.form.get("qty_raw", ENRICH_RATIO)))
        qty_raw = (qty_raw // ENRICH_RATIO) * ENRICH_RATIO  # zaokrúhli na násobok
    except ValueError:
        return redirect("/energy")

    profile = _energy_tick(uname)
    raw = profile.get("raw_materials", {})
    have_raw = raw.get("uranium_raw", 0.0)

    if have_raw < qty_raw:
        qty_raw = int(have_raw // ENRICH_RATIO) * ENRICH_RATIO
    if qty_raw < ENRICH_RATIO:
        return redirect("/energy")

    rods = qty_raw // ENRICH_RATIO
    raw["uranium_raw"] = round(have_raw - qty_raw, 2)
    profile["raw_materials"] = raw
    profile.setdefault("fuel", {})
    profile["fuel"]["uranium"] = round(profile["fuel"].get("uranium", 0) + rods, 2)

    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


# ── Jadrová vetva — Fáza 4: Safety upgrades + oprava ───────────────────────

@app.route("/energy/buy_safety", methods=["POST"])
def energy_buy_safety():
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    uname   = _uname()
    profile = _energy_tick(uname)
    current = profile.get("safety_level", 0)
    next_lvl = current + 1
    upg = next((u for u in SAFETY_UPGRADES if u["level"] == next_lvl), None)
    if not upg:
        return redirect("/energy")
    career = load_jf(KB_CAREER, {})
    entry  = career.get(uname, {})
    cr     = entry.get("career_cr", 0)
    if cr < upg["cost"]:
        return redirect("/energy")
    entry["career_cr"] = cr - upg["cost"]
    career[uname] = entry
    save_jf(KB_CAREER, career)
    profile["safety_level"] = next_lvl
    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


@app.route("/energy/repair", methods=["POST"])
def energy_repair():
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    plant_id = request.form.get("plant_id", "").strip()
    uname    = _uname()
    profile  = _energy_tick(uname)
    damaged  = profile.get("damaged_plants", [])
    dmg_item = next((d for d in damaged if d["plant_id"] == plant_id), None)
    if not dmg_item:
        return redirect("/energy")
    cost = REPAIR_COSTS.get(plant_id, 10000)
    career = load_jf(KB_CAREER, {})
    entry  = career.get(uname, {})
    cr     = entry.get("career_cr", 0)
    if cr < cost:
        return redirect("/energy")
    entry["career_cr"] = cr - cost
    career[uname] = entry
    save_jf(KB_CAREER, career)
    profile["damaged_plants"] = [d for d in damaged if d["plant_id"] != plant_id]
    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


# ── Jadrová vetva — Fáza 5: Xenón + Dispatch + Kalkar ───────────────────────

@app.route("/energy/xenon_purge", methods=["POST"])
def energy_xenon_purge():
    """Toggle xenón purge mód pre RBMK."""
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    uname   = _uname()
    profile = _energy_tick(uname)
    if "rbmk" not in profile.get("plants", []):
        return redirect("/energy")
    profile["xenon_purge"] = not profile.get("xenon_purge", False)
    data = load_jf(KB_ENERGY, {})
    data.setdefault(uname, profile)["xenon_purge"] = profile["xenon_purge"]
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


@app.route("/energy/dispatch", methods=["POST"])
def energy_dispatch():
    """Odpoveď na dispatch pressure ponuku."""
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    choice = request.form.get("choice", "refuse")
    opt    = next((o for o in DISPATCH_OPTS if o["id"] == choice), DISPATCH_OPTS[2])
    uname  = _uname()
    profile = _energy_tick(uname)
    if not profile.get("dispatch_pending"):
        return redirect("/energy")

    career = load_jf(KB_CAREER, {})
    entry  = career.get(uname, {})
    cr_change = opt["cr"]
    entry["career_cr"] = max(0, entry.get("career_cr", 0) + cr_change)
    career[uname] = entry
    save_jf(KB_CAREER, career)

    if opt["hazard_mult"] > 1.0:
        now_t = time.time()
        profile["hazard_mult_val"]     = opt["hazard_mult"]
        profile["hazard_mult_expires"] = now_t + opt["hours"] * 3600

    profile["dispatch_pending"] = None
    data = load_jf(KB_ENERGY, {})
    data.setdefault(uname, profile).update({
        "dispatch_pending":    None,
        "hazard_mult_val":     profile.get("hazard_mult_val", 1.0),
        "hazard_mult_expires": profile.get("hazard_mult_expires", 0.0),
    })
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


@app.route("/energy/kalkar", methods=["POST"])
def energy_kalkar():
    """Kalkar easter egg — konvertuj nevyužitý RBMK na zábavný park."""
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    uname   = _uname()
    profile = _energy_tick(uname)
    plants  = profile.get("plants", [])
    # Podmienka: má RBMK, xenón = 0 (nikdy neprebehol), nie je poškodený
    has_rbmk   = "rbmk" in plants
    xenon_zero = profile.get("xenon_level", 0.0) == 0.0
    not_damaged = "rbmk" not in {d["plant_id"] for d in profile.get("damaged_plants", [])}
    already_done = profile.get("kalkar_converted", False)
    if not (has_rbmk and xenon_zero and not_damaged and not already_done):
        return redirect("/energy")

    # Odober RBMK, daj bonus CR a set flag
    plants.remove("rbmk")
    profile["plants"]           = plants
    profile["kalkar_converted"] = True
    career = load_jf(KB_CAREER, {})
    entry  = career.get(uname, {})
    entry["career_cr"] = entry.get("career_cr", 0) + 50000
    career[uname] = entry
    save_jf(KB_CAREER, career)
    data = load_jf(KB_ENERGY, {})
    data.setdefault(uname, profile)["plants"]           = plants
    data[uname]["kalkar_converted"] = True
    save_jf(KB_ENERGY, data)
    return redirect("/energy?kalkar=1")


# ── Jadrová vetva — Fáza 6: Spent fuel management ──────────────────────────

@app.route("/energy/spent_fuel", methods=["POST"])
def energy_spent_fuel():
    """Správa vyhorených palivových článkov: disposal / PUREX / MOX."""
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    action   = request.form.get("action", "")
    uname    = _uname()
    profile  = _energy_tick(uname)
    raw      = profile.get("raw_materials", {})
    spent    = raw.get("spent_fuel", 0.0)
    career   = load_jf(KB_CAREER, {})
    entry    = career.get(uname, {})
    cr       = entry.get("career_cr", 0)

    try:
        qty = max(1, int(request.form.get("qty", 1)))
    except ValueError:
        return redirect("/energy")

    qty = min(qty, int(spent))
    if qty <= 0:
        return redirect("/energy")

    if action == "dispose":
        cost = qty * DISPOSAL_CR
        if cr < cost:
            return redirect("/energy")
        entry["career_cr"] = cr - cost
        raw["spent_fuel"]  = round(spent - qty, 3)
        # Malý heat nárast za disposal
        profile["proliferation_heat"] = min(100.0, round(
            profile.get("proliferation_heat", 0) + qty * 0.5, 2))

    elif action == "purex":
        rods_out = qty // PUREX_RATIO
        if rods_out < 1:
            return redirect("/energy")
        actual_qty = rods_out * PUREX_RATIO
        cost = rods_out * PUREX_CR_PER_ROD
        if cr < cost:
            return redirect("/energy")
        entry["career_cr"]   = cr - cost
        raw["spent_fuel"]    = round(spent - actual_qty, 3)
        profile.setdefault("fuel", {})
        profile["fuel"]["pu239"] = round(profile["fuel"].get("pu239", 0) + rods_out, 3)
        profile["proliferation_heat"] = min(100.0, round(
            profile.get("proliferation_heat", 0) + rods_out * PUREX_HEAT, 2))

    elif action == "mox":
        u238_avail = raw.get("u238", 0.0)
        pu_avail   = profile.get("fuel", {}).get("pu239", 0.0)
        max_by_spent = qty
        max_by_u238  = int(u238_avail / MOX_U238_PER_ROD)
        max_by_pu    = int(pu_avail   / MOX_PU_PER_ROD)
        mox_rods = min(max_by_spent, max_by_u238, max_by_pu)
        if mox_rods < 1:
            return redirect("/energy")
        raw["spent_fuel"]     = round(spent - mox_rods, 3)
        raw["u238"]           = round(u238_avail - mox_rods * MOX_U238_PER_ROD, 2)
        profile["fuel"]["pu239"] = round(pu_avail - mox_rods * MOX_PU_PER_ROD, 3)
        profile["fuel"]["mox"]   = round(profile["fuel"].get("mox", 0) + mox_rods, 3)
        qty = mox_rods
    else:
        return redirect("/energy")

    profile["raw_materials"] = raw
    career[uname] = entry
    save_jf(KB_CAREER, career)
    data = load_jf(KB_ENERGY, {})
    data.setdefault(uname, profile)
    data[uname] = profile
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


# ── Jadrová vetva — Fáza 7 routes ──────────────────────────────────────────

@app.route("/energy/rbmk_online", methods=["POST"])
def energy_rbmk_online():
    """Prepni RBMK online refueling mód (generuje WG-Pu)."""
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    uname   = _uname()
    profile = _energy_tick(uname)
    if "rbmk" not in profile.get("plants", []):
        return redirect("/energy")
    profile["rbmk_online_refuel"] = not profile.get("rbmk_online_refuel", False)
    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


@app.route("/energy/soviet_event", methods=["POST"])
def energy_soviet_event():
    """Odpoveď na sovietský event."""
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    choice  = request.form.get("choice", "refuse")
    uname   = _uname()
    profile = _energy_tick(uname)
    opt     = next((o for o in SOVIET_OPTS if o["id"] == choice), SOVIET_OPTS[2])

    profile["soviet_event_pending"] = None

    if opt["cr"] or opt["wg_pu"]:
        career = load_jf(KB_CAREER, {})
        entry  = career.get(uname, {})
        entry["career_cr"] = entry.get("career_cr", 0) + opt["cr"]
        career[uname] = entry
        save_jf(KB_CAREER, career)

    if opt["wg_pu"] > 0:
        profile.setdefault("fuel", {})["wg_pu"] = round(
            profile["fuel"].get("wg_pu", 0.0) + opt["wg_pu"], 3)
    if opt["heat"] > 0:
        profile["proliferation_heat"] = min(100.0, round(
            profile.get("proliferation_heat", 0) + opt["heat"], 2))
    if opt["hazard_mult"] > 1.0 and opt["hours"] > 0:
        exp = time.time() + opt["hours"] * 3600
        profile["hazard_mult_expires"] = exp
        profile["hazard_mult_val"]     = max(profile.get("hazard_mult_val", 1.0), opt["hazard_mult"])

    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


@app.route("/energy/wg_sell", methods=["POST"])
def energy_wg_sell():
    """Predaj WG-Pu na čiernom trhu."""
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    uname   = _uname()
    profile = _energy_tick(uname)
    try:
        qty = max(1, int(request.form.get("qty", 1)))
    except ValueError:
        return redirect("/energy")

    wg_avail = profile.get("fuel", {}).get("wg_pu", 0.0)
    qty = min(qty, int(wg_avail))
    if qty <= 0:
        return redirect("/energy")

    cr_gain = qty * WG_PU_SELL_CR
    heat_gain = qty * WG_PU_HEAT_PER_UNIT
    profile["fuel"]["wg_pu"] = round(wg_avail - qty, 3)
    profile["proliferation_heat"] = min(100.0, round(
        profile.get("proliferation_heat", 0) + heat_gain, 2))

    career = load_jf(KB_CAREER, {})
    entry  = career.get(uname, {})
    entry["career_cr"] = entry.get("career_cr", 0) + cr_gain
    career[uname] = entry
    save_jf(KB_CAREER, career)

    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)
    return redirect("/energy")


# ── Fáza 2 jadrovej vetvy — obohacovacia minihra ────────────────────────────

@app.route("/energy/enrichment")
def enrichment_page():
    if not _require_session() or not _energy_allowed():
        return redirect("/lobby")

    uname   = _uname()
    profile = _energy_tick(uname)
    career  = load_jf(KB_CAREER, {})
    cr      = career.get(uname, {}).get("career_cr", 0)
    fuel    = profile.get("fuel", {})
    raw     = profile.get("raw_materials", {})
    lang    = session.get("lang", "sk")
    msg     = request.args.get("msg", "")

    def Lp(sk, en): return en if lang == "en" else sk

    css = """
<style>
@import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#000;color:#39ff6a;font-family:'VT323',monospace;
  min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:16px 16px 40px;}
h1{color:#ffaa00;font-size:1.8em;letter-spacing:.1em;margin:10px 0 4px;
  text-align:center;text-shadow:0 0 18px #ffaa0088;}
h2{color:#ffaa00;font-size:1.05em;letter-spacing:.08em;margin:12px 0 6px;}
.sub{color:#7a6000;font-size:.9em;margin-bottom:18px;letter-spacing:.08em;}
.card{background:#0d0900;border:1px solid #ffaa0044;width:100%;max-width:720px;
  padding:16px 20px 18px;margin-bottom:12px;}
.card-title{color:#ffaa00;font-size:1.05em;border-bottom:1px solid #2a1a00;
  padding-bottom:5px;margin-bottom:12px;letter-spacing:.08em;}
.stock-row{display:flex;justify-content:space-between;padding:5px 0;
  border-bottom:1px solid #0d0900;font-size:.95em;}
.stock-row:last-child{border-bottom:none;}
.lbl{color:#7a6000;}
.val{color:#cfffcf;}
.grade-card{border:1px solid #ffaa0033;padding:12px 16px;margin-bottom:10px;
  background:#080500;}
.grade-title{color:#ffaa00;font-size:1.05em;margin-bottom:4px;}
.grade-desc{color:#7a6000;font-size:.85em;margin-bottom:8px;}
.cascade{font-family:'VT323',monospace;font-size:.78em;color:#4a3a00;
  white-space:pre;overflow-x:auto;margin:6px 0 10px;line-height:1.3;}
.cascade .active-stage{color:#ffaa00;}
.inp{background:#000;border:1px solid #7a6000;color:#cfffcf;
  font-family:'VT323',monospace;font-size:1em;padding:3px 8px;width:80px;}
.btn{background:#0d0900;border:1px solid #ffaa00;color:#ffaa00;
  font-family:'VT323',monospace;font-size:.95em;padding:4px 12px;cursor:pointer;}
.btn:hover{background:#1a0d00;}
.btn:disabled{border-color:#2a1a00;color:#2a1a00;cursor:default;}
.form-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.tag-mult{color:#39ff6a;font-size:.85em;}
.tag-cost{color:#ff9900;font-size:.85em;}
.tag-u238{color:#7a6000;font-size:.85em;}
.flash{color:#39ff6a;background:#001a00;border:1px solid #39ff6a33;
  padding:6px 14px;font-size:.95em;margin-bottom:10px;max-width:720px;width:100%;}
.flash.err{color:#ff3a3a;border-color:#ff3a3a33;background:#1a0000;}
.btn-back{display:inline-block;background:#000;border:1px solid #7a6000;
  color:#7a6000;font-family:'VT323',monospace;font-size:1em;
  padding:6px 16px;text-decoration:none;letter-spacing:.06em;margin-bottom:14px;}
.btn-back:hover{background:#0d0900;color:#ffaa00;}
</style>"""

    uranium_raw = raw.get("uranium_raw", 0.0)
    u238_stock  = raw.get("u238", 0.0)

    flash_cls  = "flash err" if msg.startswith("!") else "flash"
    flash_html = f'<div class="{flash_cls}">{msg.lstrip("!")}</div>' if msg else ""

    # Zásoby
    stocks_html = (
        f'<div class="stock-row"><span class="lbl">☢ {Lp("Surový urán","Raw uranium")}</span>'
        f'<span class="val">{uranium_raw:.1f} t</span></div>'
    )
    for g in ENRICHMENT_GRADES:
        stk = fuel.get(g["fuel_key"], 0.0)
        stocks_html += (
            f'<div class="stock-row">'
            f'<span class="lbl">{g["name_sk"] if lang!="en" else g["name_en"]}</span>'
            f'<span class="val">{stk:.1f} {Lp("ks","rods")}</span>'
            f'</div>'
        )
    stocks_html += (
        f'<div class="stock-row"><span class="lbl">⚫ {Lp("Ochudobnený U-238","Depleted U-238")}</span>'
        f'<span class="val">{u238_stock:.1f} t</span></div>'
    )

    def _cascade_art(stages, highlight=True):
        """Vygeneruj ASCII kaskádu centrifúg."""
        cols = min(stages, 12)
        rows = (stages + cols - 1) // cols
        lines = []
        idx = 0
        for r in range(rows):
            row_stages = min(cols, stages - r * cols)
            line = ""
            for c in range(row_stages):
                cls = "active-stage" if highlight else ""
                line += f'[C{idx+1:02d}]'
                idx += 1
            lines.append("  " + " → ".join(f"[C{r*cols+c+1:02d}]" for c in range(row_stages)))
        return "\n".join(lines)

    # Grade karty
    grades_html = ""
    for g in ENRICHMENT_GRADES:
        gid      = g["id"]
        desc     = Lp(g["desc_sk"], g["desc_en"])
        name     = Lp(g["name_sk"], g["name_en"])
        min_rods = 1
        max_rods = max(1, int(uranium_raw) // g["feed_per_rod"])
        can_enrich = uranium_raw >= g["feed_per_rod"]
        cost_1   = g["cr_cost"]
        disabled = "disabled" if (not can_enrich or (cr < cost_1 and cost_1 > 0)) else ""
        warn = ""
        feed_needed = g["feed_per_rod"]
        if not can_enrich:
            warn_txt = Lp(f"Potrebuješ aspoň {feed_needed} t surového uránu.",
                          f"Need at least {feed_needed} t raw uranium.")
            warn = f'<div style="color:#7a4400;font-size:.82em">{warn_txt}</div>'
        elif cost_1 > 0 and cr < cost_1:
            warn = f'<div style="color:#7a4400;font-size:.82em">{Lp("Nedostatok CR.","Not enough CR.")}</div>'

        cascade = _cascade_art(g["stages"])
        grades_html += f"""
<div class="grade-card">
  <div class="grade-title">⚗ {name} &nbsp;
    <span class="tag-mult">×{g["energy_mult"]:.1f} {Lp("energia","energy")}</span>
    {f'<span class="tag-cost"> | {cost_1:,} CR/{Lp("dávku","batch")}</span>' if cost_1 else ''}
    <span class="tag-u238"> | U-238: +{g["u238_per_rod"]} t/{Lp("čl","rod")}</span>
  </div>
  <div class="grade-desc">{desc} &nbsp; {Lp("Stupeň","Grade")}: {g["u235_pct"]}% U-235</div>
  <div class="cascade">{cascade}</div>
  {warn}
  <form method="POST" action="/energy/enrichment" class="form-row">
    <input type="hidden" name="grade" value="{gid}">
    <span class="lbl">{Lp("Počet článkov","Rods")}:</span>
    <input class="inp" type="number" name="rods" value="1" min="1" max="{max_rods}" step="1">
    <span class="lbl" style="font-size:.82em">
      ({Lp("potrebuješ","need")} {g["feed_per_rod"]} t/čl,
       {Lp("máš","have")} {uranium_raw:.1f} t → max {max_rods})
    </span>
    <button class="btn" {disabled}>⚗ {Lp("Obohatiť","Enrich")}</button>
  </form>
</div>"""

    html = f"""<!DOCTYPE html><html lang='{lang}'><head>
<meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{Lp("Obohacovacia stanica","Enrichment Station")} — KB</title>
{css}</head><body>
<a href="/energy" class="btn-back">&#8592; {Lp("Späť","Back")}</a>
<h1>⚗ {Lp("OBOHACOVACIA STANICA","ENRICHMENT STATION")}</h1>
<div class="sub">
  PILOT: {session['username'].upper()} &nbsp;|&nbsp;
  {Lp("Kariéra CR","Career CR")}:
  <span style="color:#cfffcf">{cr:,} CR</span>
  &nbsp;|&nbsp; {Lp("Jadrová vetva","Nuclear branch")} Fáza 2
</div>
{flash_html}
<div class="card">
  <div class="card-title">&#128204; {Lp("ZÁSOBY MATERIÁLU","MATERIAL STOCKS")}</div>
  {stocks_html}
</div>
<h2>⚗ {Lp("ZVOĽ STUPEŇ OBOHACOVANIA","SELECT ENRICHMENT GRADE")}</h2>
{grades_html}
</body></html>"""
    return html


@app.route("/energy/enrichment", methods=["POST"])
def enrichment_process():
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    grade_id = request.form.get("grade", "").strip()
    g = _GRADE_BY_ID.get(grade_id)
    if not g:
        return redirect("/energy/enrichment")

    try:
        rods = max(1, int(request.form.get("rods", 1)))
    except ValueError:
        return redirect("/energy/enrichment")

    uname   = _uname()
    profile = _energy_tick(uname)
    career  = load_jf(KB_CAREER, {})
    entry   = career.get(uname, {})
    cr      = entry.get("career_cr", 0)

    raw         = profile.get("raw_materials", {})
    uranium_raw = raw.get("uranium_raw", 0.0)
    needed_raw  = rods * g["feed_per_rod"]
    total_cr    = rods * g["cr_cost"]

    if uranium_raw < needed_raw:
        rods = int(uranium_raw // g["feed_per_rod"])
        needed_raw = rods * g["feed_per_rod"]
        total_cr   = rods * g["cr_cost"]
    if rods <= 0:
        return redirect(f"/energy/enrichment?msg=!{Lp('Nedostatok surového uránu.','Not enough raw uranium.')}")
    if cr < total_cr:
        return redirect(f"/energy/enrichment?msg=!{Lp('Nedostatok CR.','Not enough CR.')}")

    # Odpočítaj suroviny a CR
    raw["uranium_raw"] = round(uranium_raw - needed_raw, 2)
    raw["u238"] = round(raw.get("u238", 0.0) + rods * g["u238_per_rod"], 2)
    profile["raw_materials"] = raw
    profile.setdefault("fuel", {})[g["fuel_key"]] = round(
        profile["fuel"].get(g["fuel_key"], 0.0) + rods, 2)

    if total_cr > 0:
        entry["career_cr"] = cr - total_cr
        career[uname] = entry
        save_jf(KB_CAREER, career)

    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)

    lang = session.get("lang", "sk")
    name = g["name_sk"] if lang != "en" else g["name_en"]
    u238_gained = round(rods * g["u238_per_rod"], 1)
    msg = (f'+{rods} {name} | '
           f'+{u238_gained} t U-238 | '
           f'-{total_cr:,} CR' if total_cr else f'+{rods} {name} | +{u238_gained} t U-238')
    return redirect(f"/energy/enrichment?msg={msg}")


# ── Fáza 3 — NPC trh ────────────────────────────────────────────────────────

@app.route("/market")
def market_page():
    if not _require_session() or not _energy_allowed():
        return redirect("/lobby")

    uname = _uname()
    profile = _energy_tick(uname)
    career = load_jf(KB_CAREER, {})
    cr = career.get(uname, {}).get("career_cr", 0)
    lang = session.get("lang", "sk")

    def Lp(sk, en):
        return en if lang == "en" else sk

    msg = request.args.get("msg", "")
    prices = _get_market_prices()

    css = """
<style>
@import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#000;color:#39ff6a;font-family:'VT323',monospace;
  min-height:100vh;display:flex;flex-direction:column;align-items:center;
  padding:16px 16px 40px;}
h1{color:#39ff6a;font-size:1.8em;letter-spacing:.1em;margin:10px 0 4px;
  text-align:center;text-shadow:0 0 18px #39ff6a88;}
.sub{color:#2a7a45;font-size:.9em;margin-bottom:18px;letter-spacing:.08em;}
.card{background:#010d01;border:1px solid #39ff6a44;width:100%;
  max-width:700px;padding:16px 20px 18px;margin-bottom:12px;}
.card-title{color:#39ff6a;font-size:1.05em;border-bottom:1px solid #0d2a0d;
  padding-bottom:5px;margin-bottom:12px;letter-spacing:.08em;}
.row{display:flex;justify-content:space-between;align-items:center;
  padding:6px 0;border-bottom:1px solid #0a1a0a;gap:8px;flex-wrap:wrap;}
.row:last-child{border-bottom:none;}
.lbl{color:#2a7a45;font-size:.9em;flex:1;min-width:160px;}
.note{color:#2a7a45;font-size:.8em;margin-top:1px;}
.stock{color:#cfffcf;white-space:nowrap;}
.price-buy{color:#ff9900;}
.price-sell{color:#39ff6a;}
.trend-up{color:#ff4444;}
.trend-dn{color:#39ff6a;}
.trend-eq{color:#2a7a45;}
.btn-trade{background:#010d01;border:1px solid #39ff6a;color:#39ff6a;
  font-family:'VT323',monospace;font-size:.95em;padding:3px 10px;
  cursor:pointer;white-space:nowrap;}
.btn-trade:hover{background:#003a10;}
.btn-trade.sell{border-color:#ff9900;color:#ff9900;}
.btn-trade.sell:hover{background:#1a0d00;}
.btn-trade:disabled{border-color:#1a3a1a;color:#1a3a1a;cursor:default;}
.qty{background:#000;border:1px solid #1a3a1a;color:#cfffcf;
  font-family:'VT323',monospace;font-size:.95em;padding:2px 6px;
  width:60px;text-align:right;}
.btn-back{display:inline-block;background:#000;border:1px solid #2a7a45;
  color:#2a7a45;font-family:'VT323',monospace;font-size:1em;
  padding:6px 16px;text-decoration:none;letter-spacing:.06em;
  margin-bottom:14px;}
.btn-back:hover{background:#0a1a0a;color:#39ff6a;}
.flash{color:#39ff6a;background:#001a00;border:1px solid #39ff6a33;
  padding:6px 14px;font-size:.95em;margin-bottom:10px;max-width:700px;
  width:100%;}
.warn{color:#ff3a3a;}
</style>"""

    flash_html = f'<div class="flash">{msg}</div>' if msg else ""

    def _trend(cur, base):
        if cur is None or base is None:
            return '<span class="trend-eq">→</span>'
        diff = (cur - base) / max(1, abs(base))
        if diff > 0.02:
            return '<span class="trend-up">↑</span>'
        if diff < -0.02:
            return '<span class="trend-dn">↓</span>'
        return '<span class="trend-eq">→</span>'

    rows_html = ""
    for item in NPC_MARKET:
        name = Lp(item["name_sk"], item["name_en"])
        note = Lp(item["note_sk"], item["note_en"])
        unit = Lp(item["unit_sk"], item["unit_en"])
        stock = _get_commodity_stock(profile, item["source"])
        step = item["step"]
        min_q = item["min_qty"]
        p = prices.get(item["id"], {})
        dyn_b = p.get("b")
        dyn_s = p.get("s")

        sell_html = ""
        buy_html = ""

        if dyn_b is not None:
            can_sell = stock >= min_q
            tr = _trend(dyn_b, item["npc_buys"])
            sell_html = (
                f'<form method="POST" action="/market/sell"'
                f' style="display:inline-flex;gap:4px;align-items:center">'
                f'<input type="hidden" name="item_id" value="{item["id"]}">'
                f'<input class="qty" type="number" name="qty"'
                f' value="{min_q}" min="{min_q}" step="{step}">'
                f'<button class="btn-trade sell"'
                f' {"" if can_sell else "disabled"}>'
                f'{Lp("Predaj","Sell")} {tr} {dyn_b:.1f} CR/{unit}'
                f'</button></form>'
            )

        if dyn_s is not None:
            can_buy = cr >= dyn_s * min_q
            tr = _trend(dyn_s, item["npc_sells"])
            buy_html = (
                f'<form method="POST" action="/market/buy"'
                f' style="display:inline-flex;gap:4px;align-items:center">'
                f'<input type="hidden" name="item_id" value="{item["id"]}">'
                f'<input class="qty" type="number" name="qty"'
                f' value="{min_q}" min="{min_q}" step="{step}">'
                f'<button class="btn-trade"'
                f' {"" if can_buy else "disabled"}>'
                f'{Lp("Kúp","Buy")} {tr} {dyn_s:.1f} CR/{unit}'
                f'</button></form>'
            )

        rows_html += (
            f'<div class="row">'
            f'<div class="lbl">{item["icon"]} {name}'
            f'<div class="note">{note}</div></div>'
            f'<span class="stock">'
            f'{Lp("Zásoba","Stock")}: {stock:.1f} {unit}</span>'
            f'{sell_html}{buy_html}'
            f'</div>'
        )

    html = f"""<!DOCTYPE html><html lang='{lang}'><head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{Lp("NPC Trh","NPC Market")} — KB</title>
{css}</head><body>
<a href="/energy" class="btn-back">&#8592; {Lp("Späť","Back")}</a>
<a href="/auctions" class="btn-back" style="margin-left:8px">&#127917; {Lp("Aukcie","Auctions")}</a>
<h1>&#128202; {Lp("NPC TRH","NPC MARKET")}</h1>
<div class="sub">
  PILOT: {session['username'].upper()} &nbsp;|&nbsp;
  {Lp("Kariéra CR","Career CR")}:
  <span style="color:#cfffcf">{cr:,} CR</span>
  &nbsp;|&nbsp; BETA v0.4 &nbsp;|&nbsp; &#946;
</div>
{flash_html}
<div class="card">
  <div class="card-title">
    &#128202; {Lp("KOMODITY — DYNAMICKÉ CENY","COMMODITIES — DYNAMIC PRICES")}
  </div>
  <div style="color:#2a7a45;font-size:.82em;margin-bottom:10px">
    {Lp(
      "Ceny reagujú na obchodovanie. Reverzia k základu prebieha časom.",
      "Prices react to trading activity. Revert to baseline over time."
    )}
    &nbsp;|&nbsp;
    <span class="trend-up">↑</span> {Lp("nad základom","above base")}
    &nbsp;
    <span class="trend-dn">↓</span> {Lp("pod základom","below base")}
    &nbsp;
    <span class="trend-eq">→</span> {Lp("pri základe","at base")}
    &nbsp;|&nbsp;
    <span class="price-sell">&#9650; {Lp("NPC kúpi od teba","NPC buys from you")}</span>
    &nbsp;
    <span class="price-buy">&#9660; {Lp("NPC predá tebe","NPC sells to you")}</span>
  </div>
  {rows_html}
</div>
</body></html>"""

    return html


@app.route("/market/sell", methods=["POST"])
def market_sell():
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    item_id = request.form.get("item_id", "").strip()
    item = next((i for i in NPC_MARKET if i["id"] == item_id), None)
    if not item or item["npc_buys"] is None:
        return redirect("/market")

    try:
        qty = max(item["min_qty"], int(request.form.get("qty", item["min_qty"])))
    except ValueError:
        return redirect("/market")

    uname = _uname()
    profile = _energy_tick(uname)
    stock = _get_commodity_stock(profile, item["source"])

    if stock < qty:
        qty = int(stock)
    if qty <= 0:
        return redirect("/market?msg=Nedostatok+zasoby")

    prices = _get_market_prices()
    price_b = (prices.get(item_id) or {}).get("b") or item["npc_buys"]
    # sell_bonus event — energia za 2× cenu
    sell_mult = 1.0
    now_t = time.time()
    for ae in profile.get("active_events", []):
        if ae["effect"] == "sell_bonus" and ae["expires_at"] > now_t:
            sell_mult = max(sell_mult, ae["value"])
    # Proliferačná horúčava zníži cenu pre energiu
    if item_id == "energy":
        p_heat = profile.get("proliferation_heat", 0.0)
        if p_heat >= 80:
            sell_mult *= 0.75
        elif p_heat >= 50:
            sell_mult *= 0.90
    earnings = round(qty * price_b * sell_mult)
    _set_commodity_stock(profile, item["source"], stock - qty)

    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)

    career = load_jf(KB_CAREER, {})
    entry = career.get(uname, {})
    entry["career_cr"] = entry.get("career_cr", 0) + earnings
    career[uname] = entry
    save_jf(KB_CAREER, career)

    _apply_price_impact(item_id, qty, "sell")

    # Predaj Pu-239 zvyšuje proliferačnú horúčavu
    if item_id == "pu239":
        edata = load_jf(KB_ENERGY, {})
        ep = edata.get(uname, {})
        ep["proliferation_heat"] = min(100.0, round(
            ep.get("proliferation_heat", 0.0) + qty * 12, 2))
        edata[uname] = ep
        save_jf(KB_ENERGY, edata)

    lang = session.get("lang", "sk")
    unit = item["unit_sk"] if lang != "en" else item["unit_en"]
    bonus_tag = f" ×{sell_mult:.0f} EVENT BONUS" if sell_mult > 1 else ""
    msg = f"+{earnings:,} CR ({qty} {unit} @ {price_b:.1f} CR{bonus_tag})"
    return redirect(f"/market?msg={msg}")


@app.route("/market/buy", methods=["POST"])
def market_buy():
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    item_id = request.form.get("item_id", "").strip()
    item = next((i for i in NPC_MARKET if i["id"] == item_id), None)
    if not item or item["npc_sells"] is None:
        return redirect("/market")

    try:
        qty = max(item["min_qty"], int(request.form.get("qty", item["min_qty"])))
    except ValueError:
        return redirect("/market")

    uname = _uname()
    prices = _get_market_prices()
    price_s = (prices.get(item_id) or {}).get("s") or item["npc_sells"]
    cost = round(qty * price_s)

    career = load_jf(KB_CAREER, {})
    entry = career.get(uname, {})
    cr = entry.get("career_cr", 0)
    if cr < cost:
        return redirect("/market?msg=Nedostatok+CR")

    entry["career_cr"] = cr - cost
    career[uname] = entry
    save_jf(KB_CAREER, career)

    profile = _energy_tick(uname)
    stock = _get_commodity_stock(profile, item["source"])
    _set_commodity_stock(profile, item["source"], stock + qty)

    data = load_jf(KB_ENERGY, {})
    data[uname] = profile
    save_jf(KB_ENERGY, data)

    _apply_price_impact(item_id, qty, "buy")

    lang = session.get("lang", "sk")
    unit = item["unit_sk"] if lang != "en" else item["unit_en"]
    msg = f"-{cost:,} CR ({qty} {unit} @ {price_s:.1f} CR)"
    return redirect(f"/market?msg={msg}")


# ── Fáza 5a — Aukcie ────────────────────────────────────────────────────────

@app.route("/auctions")
def auctions_page():
    if not _require_session() or not _energy_allowed():
        return redirect("/lobby")

    uname = _uname()
    auc = _auction_tick()
    lots = auc.get("lots", [])
    my_pending = auc.get("pending", {}).get(uname, [])
    career = load_jf(KB_CAREER, {})
    cr = career.get(uname, {}).get("career_cr", 0)
    lang = session.get("lang", "sk")
    now = time.time()

    def Lp(sk, en):
        return en if lang == "en" else sk

    msg = request.args.get("msg", "")

    css = """
<style>
@import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#000;color:#39ff6a;font-family:'VT323',monospace;
  min-height:100vh;display:flex;flex-direction:column;align-items:center;
  padding:16px 16px 40px;}
h1{color:#39ff6a;font-size:1.8em;letter-spacing:.1em;margin:10px 0 4px;
  text-align:center;text-shadow:0 0 18px #39ff6a88;}
h2{color:#39ff6a;font-size:1.1em;letter-spacing:.08em;margin:14px 0 6px;}
.sub{color:#2a7a45;font-size:.9em;margin-bottom:18px;letter-spacing:.08em;}
.card{background:#010d01;border:1px solid #39ff6a44;width:100%;
  max-width:700px;padding:16px 20px 18px;margin-bottom:12px;}
.lot-header{display:flex;justify-content:space-between;align-items:baseline;
  border-bottom:1px solid #0d2a0d;padding-bottom:6px;margin-bottom:10px;}
.lot-title{color:#39ff6a;font-size:1.1em;letter-spacing:.07em;}
.timer{color:#ff9900;font-size:.95em;font-family:'VT323',monospace;}
.timer.urgent{color:#ff3a3a;animation:blink .8s step-end infinite;}
@keyframes blink{50%{opacity:0;}}
.lot-detail{color:#cfffcf;font-size:.95em;margin-bottom:8px;}
.lot-detail span{color:#2a7a45;}
.bid-row{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap;}
.bid-current{color:#39ff6a;font-size:1.05em;}
.bidder-tag{color:#2a7a45;font-size:.82em;}
.inp{background:#000;border:1px solid #2a7a45;color:#cfffcf;
  font-family:'VT323',monospace;font-size:1em;padding:3px 8px;width:100px;}
.btn{background:#010d01;border:1px solid #39ff6a;color:#39ff6a;
  font-family:'VT323',monospace;font-size:.95em;padding:4px 12px;cursor:pointer;}
.btn:hover{background:#003a10;}
.btn.warn{border-color:#ff3a3a;color:#ff3a3a;}
.btn.warn:hover{background:#1a0000;}
.btn.gold{border-color:#ff9900;color:#ff9900;}
.btn.gold:hover{background:#1a0d00;}
.btn:disabled{border-color:#1a3a1a;color:#1a3a1a;cursor:default;}
.pending-item{padding:6px 0;border-bottom:1px solid #0a1a0a;color:#cfffcf;}
.pending-item:last-child{border-bottom:none;}
.flash{color:#39ff6a;background:#001a00;border:1px solid #39ff6a33;
  padding:6px 14px;font-size:.95em;margin-bottom:10px;max-width:700px;width:100%;}
.flash.err{color:#ff3a3a;border-color:#ff3a3a33;background:#1a0000;}
.btn-back{display:inline-block;background:#000;border:1px solid #2a7a45;
  color:#2a7a45;font-family:'VT323',monospace;font-size:1em;
  padding:6px 16px;text-decoration:none;letter-spacing:.06em;margin-bottom:14px;}
.btn-back:hover{background:#0a1a0a;color:#39ff6a;}
.no-lots{color:#2a7a45;font-size:.9em;padding:10px 0;}
</style>"""

    flash_cls = "flash err" if msg.startswith("!") else "flash"
    flash_html = f'<div class="{flash_cls}">{msg.lstrip("!")}</div>' if msg else ""

    def _fmt_timer(ends_at):
        secs = max(0, int(ends_at - now))
        mins, s = divmod(secs, 60)
        hrs, m = divmod(mins, 60)
        urgent = secs < 120
        label = f"{hrs}h {m:02d}m {s:02d}s" if hrs else f"{m:02d}:{s:02d}"
        cls = "timer urgent" if urgent else "timer"
        return f'<span class="{cls}" data-ends="{int(ends_at)}">{label}</span>'

    lots_html = ""
    if not lots:
        lots_html = f'<p class="no-lots">{Lp("Žiadne aktívne loty.","No active lots.")}</p>'
    for lot in lots:
        unit = Lp(lot["unit_sk"], lot["unit_en"])
        name = Lp(lot["name_sk"], lot["name_en"])
        bidder_tag = ""
        if lot["bidder"]:
            bidder_label = Lp("Najvyššia ponuka od","Top bid by")
            you = Lp("(ty)","(you)") if lot["bidder"] == uname else ""
            bidder_tag = f'<span class="bidder-tag"> — {bidder_label}: {lot["bidder"]} {you}</span>'
        min_next = lot["current_bid"] + 1
        can_bid = cr >= min_next and lot["bidder"] != uname
        lots_html += f"""
<div class="card">
  <div class="lot-header">
    <span class="lot-title">{lot['icon']} {name}</span>
    {_fmt_timer(lot['ends_at'])}
  </div>
  <div class="lot-detail">
    <span>{Lp('Množstvo','Qty')}:</span> {lot['qty']} {unit} &nbsp;|&nbsp;
    <span>{Lp('Štartovacia cena','Start bid')}:</span> {lot['start_bid']:,} CR
  </div>
  <div class="bid-row">
    <span class="bid-current">{Lp('Aktuálna ponuka','Current bid')}: {lot['current_bid']:,} CR</span>
    {bidder_tag}
  </div>
  <form method="POST" action="/auctions/bid" class="bid-row">
    <input type="hidden" name="lot_id" value="{lot['id']}">
    <input class="inp" type="number" name="amount"
      value="{min_next}" min="{min_next}" step="1">
    <button class="btn" {"" if can_bid else "disabled"}>
      &#128200; {Lp('Ponúknuť','Place bid')}
    </button>
  </form>
</div>"""

    pending_html = ""
    if my_pending:
        items_html = ""
        for p in my_pending:
            unit = Lp(p["unit_sk"], p["unit_en"])
            name = Lp(p["name_sk"], p["name_en"])
            items_html += (
                f'<div class="pending-item">'
                f'{p["icon"]} {name}: {p["qty"]} {unit} &nbsp;—&nbsp;'
                f'{Lp("zaplatíš","you pay")}: <strong>{p["paid"]:,} CR</strong>'
                f'</div>'
            )
        collect_label = Lp("VYBRAŤ VÝHRY", "COLLECT WINNINGS")
        pending_html = f"""
<div class="card" style="border-color:#ff9900aa">
  <h2 style="color:#ff9900">&#127881; {Lp('VÝHRY NA VYZDVIHNUTIE','WINNINGS TO COLLECT')}</h2>
  {items_html}
  <form method="POST" action="/auctions/collect" style="margin-top:10px">
    <button class="btn gold">&#128179; {collect_label}</button>
  </form>
</div>"""

    # JS countdown
    js = """
<script>
(function(){
  function tick(){
    document.querySelectorAll('[data-ends]').forEach(function(el){
      var secs=Math.max(0,parseInt(el.dataset.ends)-Math.floor(Date.now()/1000));
      var h=Math.floor(secs/3600),m=Math.floor((secs%3600)/60),s=secs%60;
      el.textContent=h?(h+'h '+('0'+m).slice(-2)+'m '+('0'+s).slice(-2)+'s')
                      :('0'+m).slice(-2)+':'+('0'+s).slice(-2);
      el.className=secs<120?'timer urgent':'timer';
      if(secs===0)setTimeout(function(){location.reload();},1500);
    });
  }
  tick();setInterval(tick,1000);
})();
</script>"""

    html = f"""<!DOCTYPE html><html lang='{lang}'><head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{Lp('Aukcie','Auctions')} — KB</title>
{css}</head><body>
<a href="/market" class="btn-back">&#8592; {Lp('Späť na trh','Back to market')}</a>
<h1>&#127917; {Lp('KOMODITNÉ AUKCIE','COMMODITY AUCTIONS')}</h1>
<div class="sub">
  PILOT: {session['username'].upper()} &nbsp;|&nbsp;
  {Lp('Kariéra CR','Career CR')}:
  <span style="color:#cfffcf">{cr:,} CR</span>
  &nbsp;|&nbsp; BETA v0.5a &nbsp;|&nbsp; &#946;
</div>
{flash_html}
{pending_html}
<a href="/auctions/company"
  style="display:block;width:100%;max-width:700px;margin-bottom:8px;
    background:#010d01;border:1px solid #ff44aa;color:#ff44aa;
    font-family:'VT323',monospace;font-size:1.05em;padding:9px;
    text-align:center;text-decoration:none;letter-spacing:.06em">
  🏢 {Lp("FIREMNÉ AUKCIE — predaj alebo kúp celú firmu","COMPANY AUCTIONS — buy or sell an entire company")}
</a>
<a href="/auctions/bankrupt"
  style="display:block;width:100%;max-width:700px;margin-bottom:14px;
    background:#0d0000;border:1px solid #ff3a3a;color:#ff3a3a;
    font-family:'VT323',monospace;font-size:1.05em;padding:9px;
    text-align:center;text-decoration:none;letter-spacing:.06em">
  💥 {Lp("BANKROTOVÉ AUKCIE — mŕtve firmy so zľavou","BANKRUPTCY AUCTIONS — distressed assets at discount")}
</a>
<h2>&#128203; {Lp('AKTÍVNE LOTY','ACTIVE LOTS')} ({len(lots)})</h2>
{lots_html}
{js}
</body></html>"""
    return html


@app.route("/auctions/bid", methods=["POST"])
def auctions_bid():
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    lot_id = request.form.get("lot_id", "").strip()
    try:
        amount = int(request.form.get("amount", 0))
    except ValueError:
        return redirect("/auctions")

    uname = _uname()
    auc = _auction_tick()
    lots = auc.get("lots", [])
    now = time.time()

    lot = next((l for l in lots if l["id"] == lot_id), None)
    if not lot or now >= lot["ends_at"]:
        return redirect("/auctions?msg=!Lot+neexistuje+alebo+expiroval.")

    if amount <= lot["current_bid"]:
        return redirect(f"/auctions?msg=!Ponuka+musí+byť+vyššia+ako+{lot['current_bid']:,}+CR.")

    career = load_jf(KB_CAREER, {})
    cr = career.get(uname, {}).get("career_cr", 0)
    if cr < amount:
        return redirect(f"/auctions?msg=!Nedostatok+CR+(máš+{cr:,},+ponúkaš+{amount:,}).")

    if lot["bidder"] == uname:
        return redirect("/auctions?msg=!Už+máš+najvyššiu+ponuku.")

    lot["current_bid"] = amount
    lot["bidder"] = uname
    save_jf(KB_AUCTIONS, auc)

    lang = session.get("lang", "sk")
    name = lot["name_sk"] if lang != "en" else lot["name_en"]
    msg = f"Ponuka {amount:,} CR za {name} prijatá."
    return redirect(f"/auctions?msg={msg}")


@app.route("/auctions/collect", methods=["POST"])
def auctions_collect():
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    uname = _uname()
    auc = _auction_tick()
    pending = auc.get("pending", {})
    my_lots = pending.get(uname, [])
    if not my_lots:
        return redirect("/auctions?msg=Nič+na+vyzdvihnutie.")

    career = load_jf(KB_CAREER, {})
    entry = career.get(uname, {})
    cr = entry.get("career_cr", 0)

    energy_data = load_jf(KB_ENERGY, {})
    profile = energy_data.get(uname, {})

    collected = []
    skipped = []
    lang = session.get("lang", "sk")

    for p in my_lots:
        cost = p["paid"]
        if cr < cost:
            name = p["name_sk"] if lang != "en" else p["name_en"]
            skipped.append(name)
            continue
        cr -= cost
        stock = _get_commodity_stock(profile, p["source"])
        _set_commodity_stock(profile, p["source"], stock + p["qty"])
        unit = p["unit_sk"] if lang != "en" else p["unit_en"]
        name = p["name_sk"] if lang != "en" else p["name_en"]
        collected.append(f'{p["qty"]} {unit} {name}')

    entry["career_cr"] = cr
    career[uname] = entry
    save_jf(KB_CAREER, career)

    energy_data[uname] = profile
    save_jf(KB_ENERGY, energy_data)

    pending[uname] = []  # vymazať vyzdvihnuté
    auc["pending"] = pending
    save_jf(KB_AUCTIONS, auc)

    parts = []
    if collected:
        parts.append("Vyzdvihnuté: " + ", ".join(collected))
    if skipped:
        parts.append("!Nedostatok CR pre: " + ", ".join(skipped))
    msg = " | ".join(parts) if parts else "Hotovo."
    return redirect(f"/auctions?msg={msg}")


# ── Fáza 5b — Firemné aukcie ────────────────────────────────────────────────

def _company_tick():
    """Expirácia firemných lotov → pending výhry, vráti aktívne loty."""
    now = time.time()
    data = load_jf(KB_AUCTIONS, {"lots": [], "pending": {}, "company_lots": [], "company_pending": {}})
    clots = data.get("company_lots", [])
    cpending = data.get("company_pending", {})
    changed = False
    active = []
    for lot in clots:
        if now < lot["ends_at"]:
            active.append(lot)
            continue
        bidder = lot.get("bidder")
        if bidder:
            cpending.setdefault(bidder, []).append({
                "lot_id":    lot["id"],
                "seller":    lot["seller"],
                "snapshot":  lot["snapshot"],
                "paid":      lot["current_bid"],
            })
        changed = True
    data["company_lots"] = active
    data["company_pending"] = cpending
    if changed:
        save_jf(KB_AUCTIONS, data)
    return data


def _company_snapshot(profile):
    """Vezme snímku energie/zásoby hráča pre predaj firmy."""
    return {
        "plants":        list(profile.get("plants", [])),
        "mines":         list(profile.get("mines", [])),
        "energy":        round(profile.get("energy", 0), 1),
        "fuel":          dict(profile.get("fuel", {})),
        "commodities":   dict(profile.get("commodities", {})),
        "raw_materials": dict(profile.get("raw_materials", {})),
    }


def _snapshot_summary(snap, lang="sk"):
    """Textový súhrn firmy pre zobrazenie v aukcii."""
    plants = snap.get("plants", [])
    if not plants:
        return "Žiadne elektrárne" if lang != "en" else "No plants"
    counts = {}
    for p in plants:
        counts[p] = counts.get(p, 0) + 1
    icons = {"solar": "☀", "coal": "🏭", "nuclear": "⚛"}
    parts = [f"{icons.get(k,'?')}×{v}" for k, v in counts.items()]
    fuel = snap.get("fuel", {})
    comm = snap.get("commodities", {})
    extras = []
    if snap.get("energy", 0) > 0:
        extras.append(f"⚡{snap['energy']:.0f}")
    if fuel.get("coal", 0) > 0:
        extras.append(f"⛏{fuel['coal']:.0f}t")
    if fuel.get("uranium", 0) > 0:
        extras.append(f"☢{fuel['uranium']:.0f}ks")
    if comm.get("oil", 0) > 0:
        extras.append(f"🛢{comm['oil']:.0f}")
    if comm.get("gold", 0) > 0:
        extras.append(f"🥇{comm['gold']:.0f}oz")
    return "  ".join(parts + extras)


@app.route("/auctions/company")
def company_auctions_page():
    if not _require_session() or not _energy_allowed():
        return redirect("/lobby")

    uname = _uname()
    auc = _company_tick()
    clots = auc.get("company_lots", [])
    my_cpending = auc.get("company_pending", {}).get(uname, [])
    career = load_jf(KB_CAREER, {})
    cr = career.get(uname, {}).get("career_cr", 0)
    energy_data = load_jf(KB_ENERGY, {})
    profile = _energy_tick(uname)
    lang = session.get("lang", "sk")
    now = time.time()

    def Lp(sk, en):
        return en if lang == "en" else sk

    msg = request.args.get("msg", "")

    css = """
<style>
@import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#000;color:#39ff6a;font-family:'VT323',monospace;
  min-height:100vh;display:flex;flex-direction:column;align-items:center;
  padding:16px 16px 40px;}
h1{color:#39ff6a;font-size:1.8em;letter-spacing:.1em;margin:10px 0 4px;
  text-align:center;text-shadow:0 0 18px #39ff6a88;}
h2{color:#39ff6a;font-size:1.1em;letter-spacing:.08em;margin:14px 0 6px;}
.sub{color:#2a7a45;font-size:.9em;margin-bottom:18px;letter-spacing:.08em;}
.card{background:#010d01;border:1px solid #39ff6a44;width:100%;
  max-width:700px;padding:16px 20px 18px;margin-bottom:12px;}
.lot-header{display:flex;justify-content:space-between;align-items:baseline;
  border-bottom:1px solid #0d2a0d;padding-bottom:6px;margin-bottom:10px;}
.lot-title{color:#39ff6a;font-size:1.05em;letter-spacing:.07em;}
.timer{color:#ff9900;font-size:.95em;}
.timer.urgent{color:#ff3a3a;animation:blink .8s step-end infinite;}
@keyframes blink{50%{opacity:0;}}
.lot-detail{color:#2a7a45;font-size:.88em;margin-bottom:6px;}
.snap{color:#cfffcf;font-size:1em;margin-bottom:8px;letter-spacing:.05em;}
.bid-row{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap;}
.bid-current{color:#39ff6a;font-size:1.05em;}
.bidder-tag{color:#2a7a45;font-size:.82em;}
.inp{background:#000;border:1px solid #2a7a45;color:#cfffcf;
  font-family:'VT323',monospace;font-size:1em;padding:3px 8px;width:110px;}
.sel{background:#000;border:1px solid #2a7a45;color:#cfffcf;
  font-family:'VT323',monospace;font-size:1em;padding:3px 6px;}
.btn{background:#010d01;border:1px solid #39ff6a;color:#39ff6a;
  font-family:'VT323',monospace;font-size:.95em;padding:4px 12px;cursor:pointer;}
.btn:hover{background:#003a10;}
.btn.gold{border-color:#ff9900;color:#ff9900;}
.btn.gold:hover{background:#1a0d00;}
.btn.sell{border-color:#ff44aa;color:#ff44aa;}
.btn.sell:hover{background:#1a0010;}
.btn:disabled{border-color:#1a3a1a;color:#1a3a1a;cursor:default;}
.pending-item{padding:8px 0;border-bottom:1px solid #0a1a0a;color:#cfffcf;}
.pending-item:last-child{border-bottom:none;}
.flash{color:#39ff6a;background:#001a00;border:1px solid #39ff6a33;
  padding:6px 14px;font-size:.95em;margin-bottom:10px;max-width:700px;width:100%;}
.flash.err{color:#ff3a3a;border-color:#ff3a3a33;background:#1a0000;}
.btn-back{display:inline-block;background:#000;border:1px solid #2a7a45;
  color:#2a7a45;font-family:'VT323',monospace;font-size:1em;
  padding:6px 16px;text-decoration:none;letter-spacing:.06em;margin-bottom:14px;}
.btn-back:hover{background:#0a1a0a;color:#39ff6a;}
.no-lots{color:#2a7a45;font-size:.9em;padding:10px 0;}
.form-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px;}
.lbl{color:#2a7a45;font-size:.88em;}
</style>"""

    flash_cls = "flash err" if msg.startswith("!") else "flash"
    flash_html = f'<div class="{flash_cls}">{msg.lstrip("!")}</div>' if msg else ""

    def _fmt_timer(ends_at):
        secs = max(0, int(ends_at - now))
        h, r = divmod(secs, 3600)
        m, s = divmod(r, 60)
        label = f"{h}h {m:02d}m" if h else f"{m:02d}:{s:02d}"
        cls = "timer urgent" if secs < 300 else "timer"
        return f'<span class="{cls}" data-ends="{int(ends_at)}">{label}</span>'

    # Aktívne firemné loty
    my_active_lot = next((l for l in clots if l["seller"] == uname), None)

    lots_html = ""
    if not clots:
        lots_html = f'<p class="no-lots">{Lp("Žiadne aktívne firemné aukcie.","No active company auctions.")}</p>'
    for lot in clots:
        snap_txt = _snapshot_summary(lot["snapshot"], lang)
        bidder_tag = ""
        if lot["bidder"]:
            you = Lp("(ty)", "(you)") if lot["bidder"] == uname else ""
            bidder_tag = f'<span class="bidder-tag"> — {Lp("Top bid","Top bid")}: {lot["bidder"]} {you}</span>'
        min_next = lot["current_bid"] + 1
        is_seller = lot["seller"] == uname
        can_bid = cr >= min_next and not is_seller and lot["bidder"] != uname
        seller_tag = f' <span style="color:#ff44aa">({Lp("tvoje","yours")})</span>' if is_seller else ""
        lots_html += f"""
<div class="card">
  <div class="lot-header">
    <span class="lot-title">🏢 {lot['seller']}{seller_tag}</span>
    {_fmt_timer(lot['ends_at'])}
  </div>
  <div class="snap">{snap_txt}</div>
  <div class="bid-row">
    <span class="bid-current">{Lp('Ponuka','Bid')}: {lot['current_bid']:,} CR</span>
    {bidder_tag}
  </div>
  {"" if is_seller else f'''
  <form method="POST" action="/auctions/company/bid" class="bid-row">
    <input type="hidden" name="lot_id" value="{lot['id']}">
    <input class="inp" type="number" name="amount" value="{min_next}" min="{min_next}" step="1">
    <button class="btn" {"" if can_bid else "disabled"}>
      &#128200; {Lp("Ponúknuť","Place bid")}
    </button>
  </form>'''}
</div>"""

    # Formulár na pridanie vlastnej firmy
    my_plants = profile.get("plants", [])
    list_html = ""
    if my_plants and not my_active_lot:
        snap = _company_snapshot(profile)
        snap_txt = _snapshot_summary(snap, lang)
        list_html = f"""
<div class="card" style="border-color:#ff44aa44">
  <h2 style="color:#ff44aa">🏢 {Lp("PREDAJ SVOJU FIRMU","SELL YOUR COMPANY")}</h2>
  <div class="lbl" style="margin-bottom:8px">
    {Lp("Čo bude zahrnuté","What will be listed")}:
    <span class="snap">{snap_txt}</span>
  </div>
  <form method="POST" action="/auctions/company/list">
    <div class="form-row">
      <span class="lbl">{Lp("Štartovacia cena","Starting bid")} (CR):</span>
      <input class="inp" type="number" name="start_bid" value="10000" min="1000" step="1000">
      <span class="lbl">{Lp("Trvanie","Duration")}:</span>
      <select class="sel" name="duration_h">
        <option value="4">4h</option>
        <option value="8" selected>8h</option>
        <option value="12">12h</option>
        <option value="24">24h</option>
      </select>
      <button class="btn sell">📢 {Lp("Dať do aukcie","List for auction")}</button>
    </div>
  </form>
</div>"""
    elif my_active_lot:
        list_html = f"""
<div class="card" style="border-color:#ff44aa44">
  <div style="color:#ff44aa">{Lp("Tvoja firma je už v aukcii.","Your company is already listed.")}</div>
</div>"""
    elif not my_plants:
        list_html = f"""
<div class="card" style="border-color:#1a3a1a">
  <div class="no-lots">{Lp("Nemáš žiadne elektrárne — nie je čo predať.","No plants to sell.")}</div>
</div>"""

    # Čakajúce výhry
    pending_html = ""
    if my_cpending:
        items_html = ""
        for p in my_cpending:
            snap_txt = _snapshot_summary(p["snapshot"], lang)
            items_html += (
                f'<div class="pending-item">'
                f'🏢 {Lp("Firma od","Company from")} <strong>{p["seller"]}</strong>: '
                f'{snap_txt}<br>'
                f'{Lp("Zaplatíš","You pay")}: <strong>{p["paid"]:,} CR</strong>'
                f'</div>'
            )
        pending_html = f"""
<div class="card" style="border-color:#ff9900aa">
  <h2 style="color:#ff9900">&#127881; {Lp('FIRMA NA PREVZATIE','COMPANY TO COLLECT')}</h2>
  {items_html}
  <form method="POST" action="/auctions/company/collect" style="margin-top:10px">
    <button class="btn gold">&#128179; {Lp("PREVZIAŤ FIRMU","COLLECT COMPANY")}</button>
  </form>
</div>"""

    js = """
<script>
(function(){
  function tick(){
    document.querySelectorAll('[data-ends]').forEach(function(el){
      var secs=Math.max(0,parseInt(el.dataset.ends)-Math.floor(Date.now()/1000));
      var h=Math.floor(secs/3600),m=Math.floor((secs%3600)/60),s=secs%60;
      el.textContent=h?(h+'h '+('0'+m).slice(-2)+'m'):(('0'+m).slice(-2)+':'+('0'+s).slice(-2));
      el.className=secs<300?'timer urgent':'timer';
      if(secs===0)setTimeout(function(){location.reload();},1500);
    });
  }
  tick();setInterval(tick,1000);
})();
</script>"""

    html = f"""<!DOCTYPE html><html lang='{lang}'><head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{Lp('Firemné aukcie','Company Auctions')} — KB</title>
{css}</head><body>
<a href="/auctions" class="btn-back">&#8592; {Lp('Späť na aukcie','Back to auctions')}</a>
<h1>🏢 {Lp('FIREMNÉ AUKCIE','COMPANY AUCTIONS')}</h1>
<div class="sub">
  PILOT: {session['username'].upper()} &nbsp;|&nbsp;
  {Lp('Kariéra CR','Career CR')}:
  <span style="color:#cfffcf">{cr:,} CR</span>
  &nbsp;|&nbsp; BETA v0.5b &nbsp;|&nbsp; &#946;
</div>
{flash_html}
{pending_html}
{list_html}
<h2>&#128203; {Lp('AKTÍVNE FIREMNÉ AUKCIE','ACTIVE COMPANY AUCTIONS')} ({len(clots)})</h2>
{lots_html}
{js}
</body></html>"""
    return html


@app.route("/auctions/company/list", methods=["POST"])
def company_list():
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    uname = _uname()
    profile = _energy_tick(uname)
    plants = profile.get("plants", [])
    if not plants:
        return redirect("/auctions/company?msg=!Nemáš žiadne elektrárne.")

    auc = _company_tick()
    clots = auc.get("company_lots", [])
    if any(l["seller"] == uname for l in clots):
        return redirect("/auctions/company?msg=!Už máš aktívnu firemnú aukciu.")

    try:
        start_bid = max(1000, int(request.form.get("start_bid", 10000)))
        duration_h = max(4, min(24, int(request.form.get("duration_h", 8))))
    except ValueError:
        return redirect("/auctions/company")

    snap = _company_snapshot(profile)
    now = time.time()
    lot_id = f"company_{uname}_{int(now*1000) % 10**9}"
    clots.append({
        "id":          lot_id,
        "seller":      uname,
        "snapshot":    snap,
        "start_bid":   start_bid,
        "current_bid": start_bid,
        "bidder":      None,
        "ends_at":     now + duration_h * 3600,
    })
    auc["company_lots"] = clots
    save_jf(KB_AUCTIONS, auc)

    lang = session.get("lang", "sk")
    msg = f"Firma daná do aukcie na {duration_h}h." if lang != "en" else f"Company listed for {duration_h}h."
    return redirect(f"/auctions/company?msg={msg}")


@app.route("/auctions/company/bid", methods=["POST"])
def company_bid():
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    lot_id = request.form.get("lot_id", "").strip()
    try:
        amount = int(request.form.get("amount", 0))
    except ValueError:
        return redirect("/auctions/company")

    uname = _uname()
    auc = _company_tick()
    clots = auc.get("company_lots", [])
    now = time.time()

    lot = next((l for l in clots if l["id"] == lot_id), None)
    if not lot or now >= lot["ends_at"]:
        return redirect("/auctions/company?msg=!Lot neexistuje alebo expiroval.")
    if lot["seller"] == uname:
        return redirect("/auctions/company?msg=!Nemôžeš bidonvať na vlastnú firmu.")
    if amount <= lot["current_bid"]:
        return redirect(f"/auctions/company?msg=!Ponuka musí byť vyššia ako {lot['current_bid']:,} CR.")
    if lot["bidder"] == uname:
        return redirect("/auctions/company?msg=!Už máš najvyššiu ponuku.")

    career = load_jf(KB_CAREER, {})
    cr = career.get(uname, {}).get("career_cr", 0)
    if cr < amount:
        return redirect(f"/auctions/company?msg=!Nedostatok CR ({cr:,} máš, {amount:,} ponúkaš).")

    lot["current_bid"] = amount
    lot["bidder"] = uname
    save_jf(KB_AUCTIONS, auc)

    lang = session.get("lang", "sk")
    msg = f"Ponuka {amount:,} CR prijatá." if lang != "en" else f"Bid {amount:,} CR accepted."
    return redirect(f"/auctions/company?msg={msg}")


@app.route("/auctions/company/collect", methods=["POST"])
def company_collect():
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    uname = _uname()
    auc = _company_tick()
    cpending = auc.get("company_pending", {})
    my_lots = cpending.get(uname, [])
    if not my_lots:
        return redirect("/auctions/company?msg=Nič na prevzatie.")

    career = load_jf(KB_CAREER, {})
    energy_data = load_jf(KB_ENERGY, {})
    winner_profile = _energy_tick(uname)
    lang = session.get("lang", "sk")
    collected = []
    skipped = []

    for p in my_lots:
        cost = p["paid"]
        buyer_entry = career.get(uname, {})
        buyer_cr = buyer_entry.get("career_cr", 0)
        if buyer_cr < cost:
            skipped.append(p["seller"])
            continue

        # Odpočítaj CR kupujúcemu
        buyer_entry["career_cr"] = buyer_cr - cost
        career[uname] = buyer_entry

        # Preveď CR predajcovi
        seller = p["seller"]
        seller_entry = career.get(seller, {"career_cr": 0})
        seller_entry["career_cr"] = seller_entry.get("career_cr", 0) + cost
        career[seller] = seller_entry

        # Preveď assets kupujúcemu
        snap = p["snapshot"]
        winner_profile.setdefault("plants", []).extend(snap.get("plants", []))
        winner_profile["energy"] = min(
            MAX_ENERGY,
            winner_profile.get("energy", 0) + snap.get("energy", 0)
        )
        for k, v in snap.get("fuel", {}).items():
            winner_profile.setdefault("fuel", {})[k] = round(
                winner_profile["fuel"].get(k, 0) + v, 2)
        for k, v in snap.get("commodities", {}).items():
            winner_profile.setdefault("commodities", {})[k] = round(
                winner_profile["commodities"].get(k, 0) + v, 2)

        # Odober assets predajcovi (cap na 0)
        seller_profile = energy_data.get(seller, {})
        sp = seller_profile.get("plants", [])
        for plant in snap.get("plants", []):
            if plant in sp:
                sp.remove(plant)
        seller_profile["plants"] = sp
        seller_profile["energy"] = max(0, round(
            seller_profile.get("energy", 0) - snap.get("energy", 0), 1))
        for k, v in snap.get("fuel", {}).items():
            seller_profile.setdefault("fuel", {})[k] = max(0, round(
                seller_profile["fuel"].get(k, 0) - v, 2))
        for k, v in snap.get("commodities", {}).items():
            seller_profile.setdefault("commodities", {})[k] = max(0, round(
                seller_profile["commodities"].get(k, 0) - v, 2))
        energy_data[seller] = seller_profile

        collected.append(p["seller"])

    save_jf(KB_CAREER, career)
    energy_data[uname] = winner_profile
    save_jf(KB_ENERGY, energy_data)

    cpending[uname] = []
    auc["company_pending"] = cpending
    save_jf(KB_AUCTIONS, auc)

    parts = []
    if collected:
        parts.append(f"Prevzaté firmy od: {', '.join(collected)}")
    if skipped:
        parts.append(f"!Nedostatok CR pre firmy od: {', '.join(skipped)}")
    msg = " | ".join(parts) if parts else "Hotovo."
    return redirect(f"/auctions/company?msg={msg}")


# ── Fáza 5c — Bankrotové aukcie ─────────────────────────────────────────────

@app.route("/auctions/bankrupt")
def bankrupt_auctions_page():
    if not _require_session() or not _energy_allowed():
        return redirect("/lobby")

    uname = _uname()
    auc = _bankrupt_tick()
    blots = auc.get("bankrupt_lots", [])
    my_bpending = auc.get("bankrupt_pending", {}).get(uname, [])
    career = load_jf(KB_CAREER, {})
    cr = career.get(uname, {}).get("career_cr", 0)
    lang = session.get("lang", "sk")
    now = time.time()

    def Lp(sk, en):
        return en if lang == "en" else sk

    msg = request.args.get("msg", "")

    css = """
<style>
@import url('https://fonts.googleapis.com/css2?family=VT323&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{background:#000;color:#39ff6a;font-family:'VT323',monospace;
  min-height:100vh;display:flex;flex-direction:column;align-items:center;
  padding:16px 16px 40px;}
h1{color:#ff3a3a;font-size:1.8em;letter-spacing:.1em;margin:10px 0 4px;
  text-align:center;text-shadow:0 0 18px #ff3a3a88;}
h2{color:#39ff6a;font-size:1.1em;letter-spacing:.08em;margin:14px 0 6px;}
.sub{color:#2a7a45;font-size:.9em;margin-bottom:18px;letter-spacing:.08em;}
.card{background:#0d0000;border:1px solid #ff3a3a44;width:100%;
  max-width:700px;padding:16px 20px 18px;margin-bottom:12px;}
.lot-header{display:flex;justify-content:space-between;align-items:baseline;
  border-bottom:1px solid #2a0000;padding-bottom:6px;margin-bottom:10px;}
.lot-title{color:#ff3a3a;font-size:1.05em;letter-spacing:.07em;}
.timer{color:#ff9900;font-size:.95em;}
.timer.urgent{color:#ff3a3a;animation:blink .8s step-end infinite;}
@keyframes blink{50%{opacity:0;}}
.snap{color:#cfffcf;font-size:1em;margin-bottom:8px;letter-spacing:.05em;}
.discount{color:#ff9900;font-size:.85em;margin-bottom:6px;}
.bid-row{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap;}
.bid-current{color:#ff3a3a;font-size:1.05em;}
.bidder-tag{color:#7a2a2a;font-size:.82em;}
.inp{background:#000;border:1px solid #7a2a2a;color:#cfffcf;
  font-family:'VT323',monospace;font-size:1em;padding:3px 8px;width:110px;}
.btn{background:#0d0000;border:1px solid #ff3a3a;color:#ff3a3a;
  font-family:'VT323',monospace;font-size:.95em;padding:4px 12px;cursor:pointer;}
.btn:hover{background:#1a0000;}
.btn.gold{border-color:#ff9900;color:#ff9900;}
.btn.gold:hover{background:#1a0d00;}
.btn:disabled{border-color:#3a1a1a;color:#3a1a1a;cursor:default;}
.pending-item{padding:8px 0;border-bottom:1px solid #1a0000;color:#cfffcf;}
.pending-item:last-child{border-bottom:none;}
.flash{color:#39ff6a;background:#001a00;border:1px solid #39ff6a33;
  padding:6px 14px;font-size:.95em;margin-bottom:10px;max-width:700px;width:100%;}
.flash.err{color:#ff3a3a;border-color:#ff3a3a33;background:#1a0000;}
.btn-back{display:inline-block;background:#000;border:1px solid #2a7a45;
  color:#2a7a45;font-family:'VT323',monospace;font-size:1em;
  padding:6px 16px;text-decoration:none;letter-spacing:.06em;margin-bottom:14px;}
.btn-back:hover{background:#0a1a0a;color:#39ff6a;}
.no-lots{color:#7a2a2a;font-size:.9em;padding:10px 0;}
</style>"""

    flash_cls = "flash err" if msg.startswith("!") else "flash"
    flash_html = f'<div class="{flash_cls}">{msg.lstrip("!")}</div>' if msg else ""

    def _fmt_timer(ends_at):
        secs = max(0, int(ends_at - now))
        h, r = divmod(secs, 3600)
        m, s = divmod(r, 60)
        label = f"{h}h {m:02d}m" if h else f"{m:02d}:{s:02d}"
        cls = "timer urgent" if secs < 300 else "timer"
        return f'<span class="{cls}" data-ends="{int(ends_at)}">{label}</span>'

    lots_html = ""
    if not blots:
        lots_html = f'<p class="no-lots">{Lp("Žiadne aktívne bankrotové aukcie.","No active bankruptcy auctions.")}</p>'
    for lot in blots:
        snap_txt = _snapshot_summary(lot["snapshot"], lang)
        est = lot.get("est_value", lot["start_bid"])
        pct = round(lot["start_bid"] / max(1, est) * 100)
        bidder_tag = ""
        if lot["bidder"]:
            you = Lp("(ty)", "(you)") if lot["bidder"] == uname else ""
            bidder_tag = f'<span class="bidder-tag"> — {lot["bidder"]} {you}</span>'
        min_next = lot["current_bid"] + 1
        can_bid = cr >= min_next and lot["seller"] != uname and lot["bidder"] != uname
        lots_html += f"""
<div class="card">
  <div class="lot-header">
    <span class="lot-title">💥 BANKROT: {lot['seller']}</span>
    {_fmt_timer(lot['ends_at'])}
  </div>
  <div class="snap">{snap_txt}</div>
  <div class="discount">
    {Lp("Odh. hodnota","Est. value")}: {est:,} CR &nbsp;·&nbsp;
    {Lp("Štart","Start")}: {lot['start_bid']:,} CR ({pct}% {Lp("hodnoty","of value")})
  </div>
  <div class="bid-row">
    <span class="bid-current">{Lp("Ponuka","Bid")}: {lot["current_bid"]:,} CR</span>
    {bidder_tag}
  </div>
  <form method="POST" action="/auctions/bankrupt/bid" class="bid-row">
    <input type="hidden" name="lot_id" value="{lot['id']}">
    <input class="inp" type="number" name="amount" value="{min_next}" min="{min_next}" step="1">
    <button class="btn" {"" if can_bid else "disabled"}>
      &#128200; {Lp("Ponúknuť","Place bid")}
    </button>
  </form>
</div>"""

    pending_html = ""
    if my_bpending:
        items_html = ""
        for p in my_bpending:
            snap_txt = _snapshot_summary(p["snapshot"], lang)
            seller_share = round(p["paid"] * BANKRUPT_SHARE)
            items_html += (
                f'<div class="pending-item">'
                f'💥 {Lp("Bankrot od","Bankrupt from")} <strong>{p["seller"]}</strong>: '
                f'{snap_txt}<br>'
                f'{Lp("Zaplatíš","You pay")}: <strong>{p["paid"]:,} CR</strong> '
                f'<span style="color:#7a2a2a">({Lp("predávajúci dostane","seller gets")} {seller_share:,} CR)</span>'
                f'</div>'
            )
        pending_html = f"""
<div class="card" style="border-color:#ff9900aa;background:#0d0800">
  <h2 style="color:#ff9900">&#127881; {Lp('BANKROTOVÁ FIRMA NA PREVZATIE','BANKRUPT COMPANY TO COLLECT')}</h2>
  {items_html}
  <form method="POST" action="/auctions/bankrupt/collect" style="margin-top:10px">
    <button class="btn gold">&#128179; {Lp("PREVZIAŤ","COLLECT")}</button>
  </form>
</div>"""

    js = """
<script>
(function(){
  function tick(){
    document.querySelectorAll('[data-ends]').forEach(function(el){
      var secs=Math.max(0,parseInt(el.dataset.ends)-Math.floor(Date.now()/1000));
      var h=Math.floor(secs/3600),m=Math.floor((secs%3600)/60),s=secs%60;
      el.textContent=h?(h+'h '+('0'+m).slice(-2)+'m'):(('0'+m).slice(-2)+':'+('0'+s).slice(-2));
      el.className=secs<300?'timer urgent':'timer';
      if(secs===0)setTimeout(function(){location.reload();},1500);
    });
  }
  tick();setInterval(tick,1000);
})();
</script>"""

    html = f"""<!DOCTYPE html><html lang='{lang}'><head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{Lp('Bankrotové aukcie','Bankruptcy Auctions')} — KB</title>
{css}</head><body>
<a href="/auctions" class="btn-back">&#8592; {Lp('Späť na aukcie','Back to auctions')}</a>
<h1>💥 {Lp('BANKROTOVÉ AUKCIE','BANKRUPTCY AUCTIONS')}</h1>
<div class="sub">
  PILOT: {session['username'].upper()} &nbsp;|&nbsp;
  {Lp('Kariéra CR','Career CR')}:
  <span style="color:#cfffcf">{cr:,} CR</span>
  &nbsp;|&nbsp; BETA v0.5c &nbsp;|&nbsp; &#946;
</div>
<div style="color:#7a2a2a;font-size:.85em;max-width:700px;width:100%;margin-bottom:10px">
  {Lp(
    "Firmy ktoré zbankrotovali (0 paliva + 0 energie po dobu 8h) sa automaticky draží so zľavou 60%.",
    "Companies that went bankrupt (0 fuel + 0 energy for 8h) are auto-auctioned at 60% off."
  )}
  &nbsp;|&nbsp;
  {Lp(
    f"Predávajúci dostane {round(BANKRUPT_SHARE*100)}% z výťažku ako likvidačnú platbu.",
    f"Seller receives {round(BANKRUPT_SHARE*100)}% of winning bid as liquidation payment."
  )}
</div>
{flash_html}
{pending_html}
<h2>&#9888; {Lp('AKTÍVNE BANKROTY','ACTIVE BANKRUPTCIES')} ({len(blots)})</h2>
{lots_html}
{js}
</body></html>"""
    return html


@app.route("/auctions/bankrupt/bid", methods=["POST"])
def bankrupt_bid():
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    lot_id = request.form.get("lot_id", "").strip()
    try:
        amount = int(request.form.get("amount", 0))
    except ValueError:
        return redirect("/auctions/bankrupt")

    uname = _uname()
    auc = _bankrupt_tick()
    blots = auc.get("bankrupt_lots", [])
    now = time.time()

    lot = next((l for l in blots if l["id"] == lot_id), None)
    if not lot or now >= lot["ends_at"]:
        return redirect("/auctions/bankrupt?msg=!Lot neexistuje alebo expiroval.")
    if lot["seller"] == uname:
        return redirect("/auctions/bankrupt?msg=!Nemôžeš bidonvať na vlastný bankrot.")
    if amount <= lot["current_bid"]:
        return redirect(f"/auctions/bankrupt?msg=!Ponuka musí byť vyššia ako {lot['current_bid']:,} CR.")
    if lot["bidder"] == uname:
        return redirect("/auctions/bankrupt?msg=!Už máš najvyššiu ponuku.")

    career = load_jf(KB_CAREER, {})
    cr = career.get(uname, {}).get("career_cr", 0)
    if cr < amount:
        return redirect(f"/auctions/bankrupt?msg=!Nedostatok CR ({cr:,} máš, {amount:,} ponúkaš).")

    lot["current_bid"] = amount
    lot["bidder"] = uname
    save_jf(KB_AUCTIONS, auc)

    msg = f"Ponuka {amount:,} CR prijatá."
    return redirect(f"/auctions/bankrupt?msg={msg}")


@app.route("/auctions/bankrupt/collect", methods=["POST"])
def bankrupt_collect():
    if not _require_session() or not _energy_allowed():
        return redirect("/")

    uname = _uname()
    auc = _bankrupt_tick()
    bpending = auc.get("bankrupt_pending", {})
    my_lots = bpending.get(uname, [])
    if not my_lots:
        return redirect("/auctions/bankrupt?msg=Nič na prevzatie.")

    career = load_jf(KB_CAREER, {})
    energy_data = load_jf(KB_ENERGY, {})
    winner_profile = _energy_tick(uname)
    lang = session.get("lang", "sk")
    collected = []
    skipped = []

    for p in my_lots:
        cost = p["paid"]
        buyer_entry = career.get(uname, {})
        buyer_cr = buyer_entry.get("career_cr", 0)
        if buyer_cr < cost:
            skipped.append(p["seller"])
            continue

        buyer_entry["career_cr"] = buyer_cr - cost
        career[uname] = buyer_entry

        seller_share = round(cost * BANKRUPT_SHARE)
        seller = p["seller"]
        seller_entry = career.get(seller, {"career_cr": 0})
        seller_entry["career_cr"] = seller_entry.get("career_cr", 0) + seller_share
        career[seller] = seller_entry

        snap = p["snapshot"]
        winner_profile.setdefault("plants", []).extend(snap.get("plants", []))
        winner_profile["energy"] = min(
            MAX_ENERGY, winner_profile.get("energy", 0) + snap.get("energy", 0))
        for k, v in snap.get("fuel", {}).items():
            winner_profile.setdefault("fuel", {})[k] = round(
                winner_profile["fuel"].get(k, 0) + v, 2)
        for k, v in snap.get("commodities", {}).items():
            winner_profile.setdefault("commodities", {})[k] = round(
                winner_profile["commodities"].get(k, 0) + v, 2)

        collected.append(f'{p["seller"]} (+{seller_share:,} CR {Lp("predajcovi","to seller")})')

    save_jf(KB_CAREER, career)
    energy_data[uname] = winner_profile
    save_jf(KB_ENERGY, energy_data)

    bpending[uname] = []
    auc["bankrupt_pending"] = bpending
    save_jf(KB_AUCTIONS, auc)

    parts = []
    if collected:
        parts.append("Prevzaté: " + ", ".join(collected))
    if skipped:
        parts.append("!Nedostatok CR pre: " + ", ".join(skipped))
    msg = " | ".join(parts) if parts else "Hotovo."
    return redirect(f"/auctions/bankrupt?msg={msg}")


@app.route("/api/message_admin", methods=["POST"])
def api_message_admin():
    if not _require_session():
        return redirect("/")
    sender    = session["username"]
    recipient = request.form.get("to", "").strip()
    text      = request.form.get("msg", "").strip()
    if not recipient or not text:
        return redirect("/lobby")
    users = load_users()
    # Príjemca musí byť admin a nesmie byť odosielateľ
    if recipient in users and users[recipient].get("is_admin") and recipient != sender:
        send_notification(recipient,
                          f'{L("Správa od","Message from")} {sender}: {text}',
                          from_role=sender)
    return redirect("/lobby")


# ── Medzigalaktická rada — krajiny, roly ───────────────────────────────────

_COUNTRIES_CSS = """
<style>
body{background:#000;color:#cfffcf;font-family:'VT323',monospace;font-size:1.05rem;margin:0;padding:12px}
a{color:#39ff6a;text-decoration:none}.btn-back{display:inline-block;margin-bottom:10px;
  color:#39ff6a;border:1px solid #39ff6a44;padding:3px 10px;font-family:inherit;font-size:1rem}
.card{border:1px solid #1a3a1a;background:#020d02;max-width:900px;margin:0 auto 10px;padding:10px 14px}
.card-title{color:#39ff6a;letter-spacing:.1em;font-size:1.1rem;margin-bottom:8px}
.row{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid #0a1a0a;font-size:.95rem}
.lbl{color:#2a7a45}.val{color:#cfffcf}
.ctable{width:100%;border-collapse:collapse;max-width:900px;margin:0 auto}
.ctable th{color:#39ff6a;border-bottom:1px solid #1a3a1a;padding:4px 8px;text-align:left;font-size:.9rem;letter-spacing:.08em}
.ctable td{padding:4px 8px;border-bottom:1px solid #0a1a0a;font-size:.95rem;vertical-align:top}
.ctable tr:hover td{background:#0a1a0a}
.region{color:#39ff6a55;font-size:.8rem}
.role-tag{display:inline-block;background:#0a1a0a;border:1px solid #1a3a1a;
  color:#cfffcf;padding:1px 6px;font-size:.82rem;margin:1px 2px 1px 0}
.role-tag.high{border-color:#ff990044;color:#ff9900}
.role-tag.council{border-color:#ff88ff44;color:#ff88ff}
.war-badge{color:#ff3a3a;font-size:.85rem}
.perm{color:#ff88ff}
h1{color:#39ff6a;letter-spacing:.15em;font-size:1.4rem;margin:6px 0 4px}
.sub{color:#2a7a45;font-size:.85rem;margin-bottom:10px}
input,select{background:#000;border:1px solid #2a7a45;color:#cfffcf;font-family:inherit;
  font-size:.9rem;padding:2px 5px}
button.b{background:#010d01;border:1px solid #39ff6a;color:#39ff6a;
  font-family:inherit;font-size:.9rem;padding:2px 8px;cursor:pointer}
button.b:hover{background:#0a1a0a}
button.b.red{border-color:#ff3a3a;color:#ff3a3a;background:#0d0000}
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=VT323&display=swap" rel="stylesheet">
"""

@app.route("/my_country")
def my_country():
    """Osobný panel hráča — všetky krajiny kde má rolu, rýchle akcie."""
    if not _require_session() or not _countries_allowed():
        return redirect("/lobby")
    uname  = session["username"]
    cdata  = _get_country_data()
    pairs  = _player_countries(uname, cdata)  # [(cid, rid)]
    now    = time.time()

    if not pairs:
        return (
            f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
            f'<title>Moja krajina — KB</title>{_COUNTRIES_CSS}</head><body>'
            f'<a href="/lobby" class="btn-back">← Lobby</a>'
            f'<h1>🌍 MOJA KRAJINA</h1>'
            f'<div class="card"><div style="color:#ff9900">Nemáš pridelenú žiadnu rolu.</div>'
            f'<div style="color:#2a7a45;font-size:.85rem;margin-top:6px">'
            f'Požiadaj owera o pridelenie roly v krajine.</div></div>'
            f'<p><a href="/countries" style="color:#38d1ff">🌍 Zoznam krajín</a></p>'
            f'</body></html>'
        )

    cards = ""
    for cid, rid in pairs:
        c   = COUNTRY_BY_ID.get(cid, {})
        cd  = cdata.get(cid, {})
        w   = cd.get("weapons", {})
        role = ROLE_BY_ID.get(rid, {})
        at_war = cd.get("at_war", [])
        # Naštvanosť voči tejto krajine
        my_anger = {oc: ocd.get("anger", {}).get(cid, 0)
                    for oc, ocd in cdata.items()
                    if ocd.get("anger", {}).get(cid, 0) > 0}
        max_anger = max(my_anger.values(), default=0)
        anger_col = "#ff3a3a" if max_anger >= ANGER_WAR_AUTH_THRESHOLD else "#ff9900" if max_anger >= ANGER_SANCTIONS_THRESHOLD else "#2a7a45"

        # Nuclear status
        nuc_ok  = w.get("nuclear_approved", False)
        nuc_sym = "✅☢" if nuc_ok else "❌☢"

        # Energy z minihry
        eu       = uname.upper()
        edata_c  = load_jf(KB_ENERGY, {})
        ep       = edata_c.get(eu, {})
        fuel     = ep.get("fuel", {})
        pu239    = fuel.get("pu239", 0.0)
        wg_pu    = fuel.get("wg_pu", 0.0)
        heat     = ep.get("proliferation_heat", 0.0)

        war_badge = (f'<div style="color:#ff3a3a;font-size:.9rem;margin:4px 0">⚔ Vo vojne s: '
                    + ", ".join(COUNTRY_BY_ID.get(e, {}).get("name", e) for e in at_war)
                    + '</div>') if at_war else ""

        sanctions = cd.get("sanctions", [])
        sanc_badge = (f'<div style="color:#ff9900;font-size:.85rem">🚫 Sankcie od: {", ".join(sanctions)}</div>'
                     ) if sanctions else ""

        # Rýchle akcie
        quick = (
            f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">'
            f'<a href="/countries/{cid}" style="background:#010d01;border:1px solid #39ff6a44;color:#39ff6a;padding:3px 10px;font-family:inherit;font-size:.9rem;text-decoration:none">📋 Detail</a>'
            f'<a href="/countries/{cid}/weapons" style="background:#0d0000;border:1px solid #ff9900;color:#ff9900;padding:3px 10px;font-family:inherit;font-size:.9rem;text-decoration:none">⚔ Arzenál</a>'
            f'<a href="/countries/{cid}/war" style="background:#1a0000;border:1px solid #ff3a3a;color:#ff3a3a;padding:3px 10px;font-family:inherit;font-size:.9rem;text-decoration:none">💣 Vojna</a>'
            f'<a href="/council" style="background:#050010;border:1px solid #ff88ff44;color:#ff88ff;padding:3px 10px;font-family:inherit;font-size:.9rem;text-decoration:none">🏛 Rada</a>'
            f'<a href="/countries/pu_market" style="background:#0d0700;border:1px solid #ff990044;color:#ff9900;padding:3px 10px;font-family:inherit;font-size:.9rem;text-decoration:none">🔬 Pu trh</a>'
            f'<a href="/countries/transfer" style="background:#050505;border:1px solid #38d1ff44;color:#38d1ff;padding:3px 10px;font-family:inherit;font-size:.9rem;text-decoration:none">↔ Presun</a>'
            f'</div>'
        )

        cards += (
            f'<div class="card" style="border-color:#38d1ff44">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;margin-bottom:6px">'
            f'<span style="font-size:1.2rem">{c.get("flag","")} <strong style="color:#cfffcf">{c.get("name",cid)}</strong></span>'
            f'<span class="role-tag high">{role.get("icon","")} {role.get("name_sk","")}</span>'
            f'</div>'
            f'{war_badge}{sanc_badge}'
            f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px 12px;font-size:.9rem;margin-top:6px">'
            f'<div><span class="lbl">🪖 Konv.</span> <span class="val">{w.get("conventional",0):,}</span></div>'
            f'<div><span class="lbl">🚀 Rakety</span> <span class="val">{w.get("missiles",0)}</span></div>'
            f'<div><span class="lbl">☢</span> <span class="val">{w.get("warheads",0)} {nuc_sym}</span></div>'
            f'<div><span class="lbl">💻 Kyber</span> <span class="val">{w.get("cyber",0)}</span></div>'
            f'<div><span class="lbl" style="color:{anger_col}">😡 Naštv.</span> <span style="color:{anger_col}">{max_anger}</span></div>'
            f'</div>'
            f'<div style="border-top:1px solid #1a3a1a;margin-top:8px;padding-top:6px;font-size:.85rem;display:flex;gap:12px;flex-wrap:wrap">'
            f'<span><span class="lbl">⚡ Energia</span> {ep.get("energy",0):.0f}</span>'
            f'<span><span class="lbl">Pu-239</span> <span style="color:#ff9900">{pu239:.2f}</span></span>'
            f'<span><span class="lbl">WG-Pu</span> <span style="color:#ff3a3a">{wg_pu:.3f}</span></span>'
            f'<span><span class="lbl">Heat</span> <span style="color:{"#ff3a3a" if heat>=60 else "#ff9900" if heat>=30 else "#2a7a45"}">{heat:.1f}%</span></span>'
            f'</div>'
            f'{quick}'
            f'</div>'
        )

    return (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<title>Moja krajina — KB</title>{_COUNTRIES_CSS}</head><body>'
        f'<a href="/lobby" class="btn-back">← Lobby</a>'
        f'<h1>🌍 MOJA KRAJINA</h1>'
        f'<div class="sub">PILOT: {uname.upper()} &nbsp;|&nbsp; '
        f'Krajiny: {len(pairs)} &nbsp;|&nbsp; '
        f'<a href="/countries" style="color:#38d1ff">Všetky krajiny</a></div>'
        f'{cards}'
        f'</body></html>'
    )


@app.route("/countries")
def countries_page():
    if not _require_session() or not _countries_allowed():
        return redirect("/lobby")
    cdata = _get_country_data()
    uname = session["username"]
    lang  = session.get("lang", "sk")

    # Moje roly
    my_roles = []
    for cid, cd in cdata.items():
        for rid, ru in cd.get("roles", {}).items():
            if isinstance(ru, list):
                if uname.lower() in [x.lower() for x in ru]:
                    my_roles.append((cid, rid))
            elif ru.lower() == uname.lower():
                my_roles.append((cid, rid))

    # Zoskup krajiny podľa regiónu
    by_region = {}
    for c in COUNTRIES:
        by_region.setdefault(c["region"], []).append(c)

    rows = ""
    for region, clist in by_region.items():
        rows += f'<tr><td colspan="4" style="color:#2a7a45;font-size:.8rem;padding:6px 8px 2px;letter-spacing:.1em">{region.upper()}</td></tr>'
        for c in clist:
            cd    = cdata.get(c["id"], {})
            roles = cd.get("roles", {})
            perm  = "★" if c["id"] in COUNCIL_PERMANENT else ""
            # Zobraziť obsadené roly
            role_tags = ""
            for rid, ru in roles.items():
                r = ROLE_BY_ID.get(rid)
                if not r:
                    continue
                users = ru if isinstance(ru, list) else [ru]
                for u in users:
                    if not u:
                        continue
                    cls = "high" if r["power"] >= 8 else ""
                    role_tags += f'<span class="role-tag {cls}">{r["icon"]} {r["name_sk"] if lang!="en" else r["name_en"]}: {u}</span>'
            war = "⚔ " + ", ".join(cd.get("at_war", [])) if cd.get("at_war") else ""
            rows += (
                f'<tr onclick="location.href=\'/countries/{c["id"]}\'" style="cursor:pointer">'
                f'<td>{c["flag"]} <a href="/countries/{c["id"]}" style="color:#cfffcf">{c["name"]}</a>'
                f' <span class="perm">{perm}</span></td>'
                f'<td><span class="region">{c["region"]}</span></td>'
                f'<td style="max-width:320px">{role_tags or "<span style=color:#2a7a45;font-size:.82rem>—</span>"}</td>'
                f'<td><span class="war-badge">{war}</span></td>'
                f'</tr>'
            )

    my_roles_html = ""
    if my_roles:
        tags = ""
        for cid, rid in my_roles:
            c = COUNTRY_BY_ID.get(cid, {})
            r = ROLE_BY_ID.get(rid, {})
            tags += f'<span class="role-tag high">{c.get("flag","")} {c.get("name",cid)} — {r.get("icon","")} {r.get("name_sk" if lang!="en" else "name_en", rid)}</span> '
        my_roles_html = f'<div class="card" style="border-color:#ff990044;margin-bottom:10px"><div class="card-title" style="color:#ff9900">👤 Moje roly</div>{tags}</div>'

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Krajiny — KB</title>{_COUNTRIES_CSS}</head><body>
<a href="/lobby" class="btn-back">← Lobby</a>
<h1>🌍 MEDZIGALAKTICKÁ RADA</h1>
<div class="sub">PILOT: {uname.upper()} &nbsp;|&nbsp; Krajiny: {len(COUNTRIES)} &nbsp;|&nbsp; ★ = stály člen RB
  &nbsp;|&nbsp; <a href="/council" style="color:#ff88ff">🏛 Rada bezpečnosti</a>
  &nbsp;|&nbsp; <a href="/countries/transfer" style="color:#ff9900">↔ Presun materiálov</a>
</div>
{my_roles_html}
<div style="max-width:900px;margin:0 auto">
<table class="ctable">
<thead><tr><th>Krajina</th><th>Región</th><th>Obsadené roly</th><th>Stav</th></tr></thead>
<tbody>{rows}</tbody>
</table></div>
</body></html>"""


@app.route("/countries/<cid>")
def country_detail(cid):
    if not _require_session() or not _countries_allowed():
        return redirect("/lobby")
    c = COUNTRY_BY_ID.get(cid)
    if not c:
        return redirect("/countries")
    cdata = _get_country_data()
    cd    = cdata.get(cid, {"roles": {}, "at_war": [], "sanctions": []})
    uname = session["username"]
    lang  = session.get("lang", "sk")
    perm  = cid in COUNCIL_PERMANENT

    rows = ""
    for role in COUNTRY_ROLES:
        rid   = role["id"]
        users = cd["roles"].get(rid, [])
        if isinstance(users, str):
            users = [users] if users else []
        ulist = ", ".join(users) if users else "—"
        cls   = "high" if role["power"] >= 8 else ""
        rows += (
            f'<tr><td>{role["icon"]} <span class="role-tag {cls}">'
            f'{role["name_sk"] if lang!="en" else role["name_en"]}</span></td>'
            f'<td style="color:#ff9900">{role["power"]}/10</td>'
            f'<td>{ulist}</td></tr>'
        )

    at_war = cd.get("at_war", [])
    sanctions = cd.get("sanctions", [])
    status_html = ""
    if at_war:
        status_html += f'<div style="color:#ff3a3a;margin:4px 0">⚔ Vo vojne s: {", ".join(at_war)}</div>'
    if sanctions:
        status_html += f'<div style="color:#ff9900;margin:4px 0">🚫 Sankcie od: {", ".join(sanctions)}</div>'
    if perm:
        status_html += f'<div style="color:#ff88ff;margin:4px 0">★ Stály člen Rady bezpečnosti — má právo VETA</div>'

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>{c["name"]} — KB</title>{_COUNTRIES_CSS}</head><body>
<a href="/countries" class="btn-back">← Späť</a>
<h1>{c["flag"]} {c["name"]}</h1>
<div class="sub">{c["region"]} {"&nbsp;|&nbsp; ★ Stály člen RB" if perm else ""}</div>
{status_html}
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
  <a href="/countries/{cid}/weapons" style="display:inline-block;background:#0d0000;border:1px solid #ff3a3a44;color:#ff9900;padding:4px 12px;font-family:inherit;font-size:.95rem">⚔ Arzenál</a>
  <a href="/countries/{cid}/war" style="display:inline-block;background:#1a0000;border:1px solid #ff3a3a;color:#ff3a3a;padding:4px 12px;font-family:inherit;font-size:.95rem">⚔ Vojenský stav</a>
  <a href="/council" style="display:inline-block;background:#050010;border:1px solid #ff88ff44;color:#ff88ff;padding:4px 12px;font-family:inherit;font-size:.95rem">🏛 Rada</a>
</div>
<div class="card">
<div class="card-title">🏛 Obsadenie rolí</div>
<table class="ctable"><thead>
<tr><th>Rola</th><th>Sila</th><th>Hráč(i)</th></tr>
</thead><tbody>{rows}</tbody></table>
</div>
</body></html>"""


# ── Vojny ────────────────────────────────────────────────────────────────────

@app.route("/countries/<cid>/war", methods=["GET", "POST"])
def country_war(cid):
    """Vyhlásenie vojny + prehľad vojnového stavu krajiny."""
    if not _require_session() or not _countries_allowed():
        return redirect("/lobby")
    c = COUNTRY_BY_ID.get(cid)
    if not c:
        return redirect("/countries")
    uname = session["username"]
    cdata = _get_country_data()
    cd    = cdata.get(cid, {})

    # Overí či má hráč vojenskú rolu v tejto krajine
    roles  = cd.get("roles", {})
    has_war_role = any(
        uname in (v if isinstance(v, list) else ([v] if v else []))
        for rid, v in roles.items() if rid in WAR_ROLES
    )
    is_owner = session.get("owner") is True

    msg = ""
    if request.method == "POST" and not (has_war_role or is_owner):
        return redirect(f"/countries/{cid}/war")
    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "declare":
            target = request.form.get("target", "")
            if target in COUNTRY_BY_ID and target != cid and target != COUNCIL_HQ_COUNTRY:
                at_war = cd.get("at_war", [])
                if target not in at_war:
                    # Skontroluj či má Rada autorizáciu
                    cncl = _get_council_data()
                    authorized = any(
                        r["type"] == "war_auth" and r["target_country"] == target
                        and r["status"] == "passed"
                        for r in cncl["resolutions"]
                    )
                    at_war.append(target)
                    cd["at_war"] = at_war
                    # Aj cieľová krajina je vo vojne s nami
                    tcd = cdata.get(target, {})
                    tw  = tcd.setdefault("at_war", [])
                    if cid not in tw:
                        tw.append(cid)
                    tcd["at_war"] = tw
                    cdata[target] = tcd
                    if not authorized:
                        # Automatická sankčná rezolúcia
                        now = time.time()
                        cncl.setdefault("resolutions", []).append({
                            "id": f"res_auto_{int(now)}",
                            "type": "sanctions",
                            "target_country": cid,
                            "proposed_by": "ISGC_AUTO",
                            "proposed_by_country": "switzerland",
                            "proposed_at": now,
                            "votes_for": [], "votes_against": [],
                            "vetoed_by": None, "status": "open",
                            "expires_at": now + RES_VOTE_HOURS * 3600,
                        })
                        save_jf(KB_COUNCIL, cncl)
                        msg = f"⚔ Vojna vyhlásená proti {COUNTRY_BY_ID[target]['name']}! ⚠ Bez autorizácie Rady — návrh sankcií bol podaný."
                    else:
                        msg = f"⚔ Vojna vyhlásená proti {COUNTRY_BY_ID[target]['name']} (autorizovaná Radou)."

        elif action == "ceasefire":
            target = request.form.get("target", "")
            if target in cd.get("at_war", []):
                cd["at_war"] = [x for x in cd["at_war"] if x != target]
                tcd = cdata.get(target, {})
                tcd["at_war"] = [x for x in tcd.get("at_war", []) if x != cid]
                cdata[target] = tcd
                msg = f"🕊 Prímerie s {COUNTRY_BY_ID.get(target,{}).get('name',target)}."

        elif action == "strike":
            target = request.form.get("target", "")
            if target in cd.get("at_war", []) and target in cdata:
                w_src = cd.setdefault("weapons", {})
                w_dst = cdata[target].setdefault("weapons", {})
                used  = {}
                errors = []
                # Načítaj množstvo každej zbrane
                for wtype in WEAPON_STATS:
                    try:
                        qty = max(0, int(request.form.get(f"use_{wtype}", 0)))
                    except ValueError:
                        qty = 0
                    if qty > 0:
                        if wtype == "warheads" and not w_src.get("nuclear_approved"):
                            errors.append("☢ Nemáš schválenie Rady na jadrové zbrane!")
                            qty = 0
                        elif w_src.get(wtype, 0) < qty:
                            errors.append(f"Nedostatok {wtype}: {w_src.get(wtype,0)} < {qty}")
                            qty = 0
                    if qty > 0:
                        used[wtype] = qty

                if errors:
                    msg = "❌ " + " | ".join(errors)
                elif not used:
                    msg = "❌ Vyber aspoň jednu zbraň."
                else:
                    tname = COUNTRY_BY_ID.get(target, {}).get("name", target)
                    dmg_report = []
                    loss_report = []
                    total_anger_neutral = 0
                    total_anger_perm    = 0

                    for wtype, qty in used.items():
                        stats = WEAPON_STATS[wtype]
                        # Straty útočníka
                        loss = round(qty * stats["loss_rate"])
                        w_src[wtype] = max(0, w_src.get(wtype, 0) - loss)
                        if loss > 0:
                            loss_report.append(f"-{loss} {stats['icon']}")

                        # Škody nepriateľovi
                        for dmg_key in ("conventional", "missiles", "cyber", "warheads"):
                            dmg_per = stats.get(f"dmg_{dmg_key}", 0)
                            if dmg_per <= 0:
                                continue
                            total_dmg = round(qty * dmg_per)
                            actual    = min(total_dmg, w_dst.get(dmg_key, 0))
                            w_dst[dmg_key] = max(0, w_dst.get(dmg_key, 0) - total_dmg)
                            if actual > 0:
                                dmg_icon = WEAPON_STATS.get(dmg_key, {}).get("icon", dmg_key)
                                dmg_report.append(f"{dmg_icon}-{actual}")

                        # Naštvanosť
                        total_anger_neutral += qty * stats["anger_neutral"]
                        total_anger_perm    += qty * stats["anger_perm"]

                        # Jadrový úder → proliferačné heat
                        if wtype == "warheads":
                            eu = uname.upper()
                            edata = load_jf(KB_ENERGY, {})
                            if eu in edata:
                                edata[eu]["proliferation_heat"] = min(100.0, round(
                                    edata[eu].get("proliferation_heat", 0) + 40 * qty, 2))
                                save_jf(KB_ENERGY, edata)

                    cd["weapons"] = w_src
                    cdata[target]["weapons"] = w_dst

                    # Naštvanosť — uloží sa do každej krajiny okrem útočníka
                    for other_cid, other_cd in cdata.items():
                        if other_cid == cid:
                            continue
                        is_perm = other_cid in COUNCIL_PERMANENT
                        anger_add = total_anger_perm if is_perm else total_anger_neutral
                        if anger_add <= 0:
                            continue
                        anger = other_cd.setdefault("anger", {})
                        anger[cid] = min(200, anger.get(cid, 0) + anger_add)
                        other_cd["anger"] = anger
                        # Automatické rezolúcie pri prekročení prahu
                        cur_anger = anger[cid]
                        cncl = _get_council_data()
                        now_t = time.time()
                        existing_types = {r["type"] for r in cncl["resolutions"]
                                         if r["status"] == "open" and r["target_country"] == cid}
                        if cur_anger >= ANGER_WAR_AUTH_THRESHOLD and "war_auth" not in existing_types and is_perm:
                            cncl["resolutions"].append({
                                "id": f"res_auto_war_{int(now_t)}",
                                "type": "war_auth", "target_country": cid,
                                "proposed_by": "ISGC_AUTO",
                                "proposed_by_country": other_cid,
                                "proposed_at": now_t,
                                "votes_for": [], "votes_against": [],
                                "vetoed_by": None, "status": "open",
                                "expires_at": now_t + RES_VOTE_HOURS * 3600,
                            })
                            save_jf(KB_COUNCIL, cncl)
                        elif cur_anger >= ANGER_SANCTIONS_THRESHOLD and "sanctions" not in existing_types and is_perm:
                            cncl["resolutions"].append({
                                "id": f"res_auto_sanc_{int(now_t)}",
                                "type": "sanctions", "target_country": cid,
                                "proposed_by": "ISGC_AUTO",
                                "proposed_by_country": other_cid,
                                "proposed_at": now_t,
                                "votes_for": [], "votes_against": [],
                                "vetoed_by": None, "status": "open",
                                "expires_at": now_t + RES_VOTE_HOURS * 3600,
                            })
                            save_jf(KB_COUNCIL, cncl)

                    dmg_str  = ", ".join(dmg_report)  if dmg_report  else "žiadna"
                    loss_str = ", ".join(loss_report) if loss_report else "žiadne"
                    msg = (f"⚔ Útok na {tname} — škody: {dmg_str} | "
                           f"vlastné straty: {loss_str} | "
                           f"naštvanosť sveta +{total_anger_neutral:.0f}")

        cdata[cid] = cd
        save_jf(KB_COUNTRIES, cdata)

    # Zostavenie HTML
    at_war   = cd.get("at_war", [])
    w        = cd.get("weapons", {})
    msg_html = f'<div style="color:{"#39ff6a" if msg.startswith(("🕊","✅")) else "#ff3a3a" if msg.startswith("❌") else "#ff9900"};margin-bottom:8px">{msg}</div>' if msg else ""

    enemy_opts = "".join(
        f'<option value="{eid}">{COUNTRY_BY_ID[eid]["flag"]} {COUNTRY_BY_ID[eid]["name"]}</option>'
        for eid in at_war if eid in COUNTRY_BY_ID
    )
    declare_opts = "".join(
        f'<option value="{cc["id"]}">{cc["flag"]} {cc["name"]}</option>'
        for cc in COUNTRIES if cc["id"] != cid and cc["id"] != COUNCIL_HQ_COUNTRY and cc["id"] not in at_war
    )
    war_html = ""
    if at_war:
        war_html = '<div style="color:#ff3a3a;margin-bottom:8px">⚔ Vo vojne s: ' + \
            ", ".join(f'{COUNTRY_BY_ID.get(e,{}).get("flag","")} {COUNTRY_BY_ID.get(e,{}).get("name",e)}' for e in at_war) + '</div>'

    # Naštvanosť iných krajín voči nám
    anger_rows = ""
    my_anger = {other_cid: cd_o.get("anger", {}).get(cid, 0)
                for other_cid, cd_o in cdata.items()
                if cd_o.get("anger", {}).get(cid, 0) > 0}
    if my_anger:
        for other_cid, pts in sorted(my_anger.items(), key=lambda x: -x[1]):
            oc = COUNTRY_BY_ID.get(other_cid, {})
            col = "#ff3a3a" if pts >= ANGER_WAR_AUTH_THRESHOLD else "#ff9900" if pts >= ANGER_SANCTIONS_THRESHOLD else "#888"
            anger_rows += (f'<div class="row"><span class="lbl">{oc.get("flag","")} {oc.get("name",other_cid)}</span>'
                          f'<span style="color:{col}">{pts} {"⚔ vojenská akcia" if pts >= ANGER_WAR_AUTH_THRESHOLD else "🚫 sankcie" if pts >= ANGER_SANCTIONS_THRESHOLD else ""}</span></div>')

    # Útokový formulár — výber množstva zbraní
    strike_form = ""
    if at_war:
        weapon_inputs = ""
        for wtype, stats in WEAPON_STATS.items():
            stock = w.get(wtype, 0)
            if stock <= 0:
                continue
            nuc_warn = " ⚠ +40 heat/ks, potrebné schválenie" if wtype == "warheads" else ""
            anger_note = f"naštvanosť +{stats['anger_neutral']}/ks neutrálni, +{stats['anger_perm']}/ks RB"
            weapon_inputs += (
                f'<div class="row" style="padding:4px 0">'
                f'<span class="lbl">{stats["icon"]} {stats["name_sk"]}'
                f' <small style="color:#555">[{stock:,}]</small></span>'
                f'<span style="display:flex;align-items:center;gap:6px">'
                f'<input type="number" name="use_{wtype}" value="0" min="0" max="{stock}" style="width:70px">'
                f'<span style="color:#555;font-size:.8rem">{anger_note}{nuc_warn}</span>'
                f'</span></div>'
            )
        if weapon_inputs:
            strike_form = (
                f'<div style="margin-top:10px;border-top:1px solid #1a3a1a;padding-top:8px">'
                f'<div style="color:#ff9900;margin-bottom:6px">🎯 Útok — vyber zbrane:</div>'
                f'<select name="target" style="margin-bottom:6px">{enemy_opts}</select>'
                f'{weapon_inputs}'
                f'<button name="action" value="strike" class="b red" style="margin-top:6px">🎯 Zaútočiť</button>'
                f'</div>'
            )

    edit_html = ""
    if has_war_role or is_owner:
        ceasefire_form = ""
        if at_war:
            ceasefire_form = (f'<form method="POST" style="display:flex;gap:8px;flex-wrap:wrap;'
                             f'align-items:center;margin-bottom:8px">'
                             f'<select name="target">{enemy_opts}</select>'
                             f'<button name="action" value="ceasefire" class="b" '
                             f'style="border-color:#38d1ff;color:#38d1ff">🕊 Prímerie</button></form>')
        edit_html = (
            f'<div class="card" style="border-color:#ff3a3a44">'
            f'<div class="card-title" style="color:#ff3a3a">⚔ Vojenské operácie</div>'
            f'<form method="POST" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">'
            f'<select name="target">{declare_opts}</select>'
            f'<button name="action" value="declare" class="b red">⚔ Vyhlásiť vojnu</button></form>'
            f'{ceasefire_form}'
            f'{"<form method=POST>" + strike_form + "</form>" if at_war and strike_form else ""}'
            f'{"<div style=color:#888;font-size:.85rem>Žiadne zbrane v arzenáli.</div>" if at_war and not strike_form else ""}'
            f'</div>'
        )

    anger_html = ""
    if anger_rows:
        anger_html = (f'<div class="card" style="border-color:#ff440044">'
                     f'<div class="card-title" style="color:#ff9900">😡 Naštvanosť iných krajín voči nám</div>'
                     f'{anger_rows}</div>')

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>{c['name']} — Vojenský stav</title>{_COUNTRIES_CSS}</head><body>
<a href="/countries/{cid}" class="btn-back">← {c['name']}</a>
<h1>{c['flag']} {c['name']} — Vojenský stav</h1>
{msg_html}{war_html}{anger_html}
<div class="card">
  <div class="card-title">⚔ Arzenál</div>
  <div class="row"><span class="lbl">🪖 Konvenčné sily</span><span class="val">{w.get('conventional',0):,} tis.</span></div>
  <div class="row"><span class="lbl">🚀 Rakety</span><span class="val">{w.get('missiles',0)}</span></div>
  <div class="row"><span class="lbl">☢ Hlavice</span><span class="val">{w.get('warheads',0)} {"✅" if w.get("nuclear_approved") else "❌"}</span></div>
  <div class="row"><span class="lbl">💻 Kyber</span><span class="val">{w.get('cyber',0)}</span></div>
</div>
{edit_html}
</body></html>"""


@app.route("/countries/transfer", methods=["GET", "POST"])
def countries_transfer():
    """Presun materiálov (zbrane) medzi krajinami ktoré hráč vlastní."""
    if not _require_session() or not _countries_allowed():
        return redirect("/lobby")
    uname = session["username"]
    cdata = _get_country_data()
    my_pairs = _player_countries(uname, cdata)  # [(cid, rid), ...]
    my_cids  = list(dict.fromkeys(cid for cid, _ in my_pairs))  # unikátne krajiny

    msg = ""
    if request.method == "POST":
        src  = request.form.get("src", "")
        dst  = request.form.get("dst", "")
        mat  = request.form.get("material", "")
        try:
            qty = int(request.form.get("qty", 0))
        except ValueError:
            qty = 0

        valid_mats = {"warheads", "missiles", "conventional", "cyber"}
        if src in my_cids and dst in my_cids and src != dst and mat in valid_mats and qty > 0:
            src_w = cdata[src]["weapons"]
            dst_w = cdata[dst]["weapons"]

            # Jadrové hlavice — cieľová krajina musí mať schválenie
            if mat == "warheads" and not dst_w.get("nuclear_approved"):
                msg = "❌ Cieľová krajina nemá schválenie Rady na jadrové zbrane!"
            elif src_w.get(mat, 0) < qty:
                msg = f"❌ Nedostatok: {src_w.get(mat, 0)} < {qty}"
            else:
                src_w[mat] = src_w.get(mat, 0) - qty
                dst_w[mat] = dst_w.get(mat, 0) + qty
                cdata[src]["weapons"] = src_w
                cdata[dst]["weapons"] = dst_w
                save_jf(KB_COUNTRIES, cdata)
                src_name = COUNTRY_BY_ID.get(src, {}).get("name", src)
                dst_name = COUNTRY_BY_ID.get(dst, {}).get("name", dst)
                mat_name = WEAPON_TYPES.get(mat, {}).get("name_sk", mat)
                msg = f"✅ Presun: {qty}× {mat_name} | {src_name} → {dst_name}"

    # Zostavenie formulára
    if len(my_cids) < 2:
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Presun materiálov</title>{_COUNTRIES_CSS}</head><body>
<a href="/countries" class="btn-back">← Krajiny</a>
<h1>↔ Presun materiálov</h1>
<div class="card"><div style="color:#ff9900">
Musíš mať rolu aspoň v 2 krajinách aby si mohol presúvať materiály.</div></div>
</body></html>"""

    c_opts = lambda exclude="": "".join(
        f'<option value="{cid}">{COUNTRY_BY_ID.get(cid,{}).get("flag","")} {COUNTRY_BY_ID.get(cid,{}).get("name",cid)}</option>'
        for cid in my_cids if cid != exclude
    )

    # Prehľad arzenálov mojich krajín
    arsenal_rows = ""
    for cid in my_cids:
        c  = COUNTRY_BY_ID.get(cid, {})
        w  = cdata[cid].get("weapons", {})
        nuc_ok = "✅" if w.get("nuclear_approved") else "❌"
        arsenal_rows += (
            f'<div style="border:1px solid #1a3a1a;padding:6px 10px;margin-bottom:6px;background:#020d02">'
            f'<span style="color:#cfffcf">{c.get("flag","")} {c.get("name",cid)}</span>'
            f' &nbsp; ☢{nuc_ok} hlavice:{w.get("warheads",0)} '
            f'🚀{w.get("missiles",0)} 🪖{w.get("conventional",0):,}tis. 💻{w.get("cyber",0)}</div>'
        )

    msg_html = f'<div style="color:{"#39ff6a" if msg.startswith("✅") else "#ff3a3a"};margin-bottom:8px">{msg}</div>' if msg else ""

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Presun materiálov — KB</title>{_COUNTRIES_CSS}</head><body>
<a href="/countries" class="btn-back">← Krajiny</a>
<h1>↔ Presun materiálov medzi krajinami</h1>
<div class="sub">Môžeš presúvať iba medzi krajinami kde máš rolu.</div>
{msg_html}
<div class="card">
  <div class="card-title">🏦 Tvoj arzenál</div>
  {arsenal_rows}
</div>
<div class="card" style="border-color:#ff990044">
  <div class="card-title" style="color:#ff9900">↔ Nový presun</div>
  <form method="POST" style="display:grid;gap:8px;max-width:500px">
    <div class="row"><span class="lbl">Zo:</span>
      <select name="src">{c_opts()}</select></div>
    <div class="row"><span class="lbl">Do:</span>
      <select name="dst">{c_opts()}</select></div>
    <div class="row"><span class="lbl">Materiál:</span>
      <select name="material">
        <option value="warheads">☢ Jadrové hlavice</option>
        <option value="missiles">🚀 Balistické rakety</option>
        <option value="conventional">🪖 Konvenčné sily (tis.)</option>
        <option value="cyber">💻 Kybernetické zbrane</option>
      </select></div>
    <div class="row"><span class="lbl">Počet:</span>
      <input type="number" name="qty" value="1" min="1" style="width:80px"></div>
    <button type="submit" class="b" style="margin-top:4px">↔ Presunúť</button>
  </form>
  <div style="color:#2a7a45;font-size:.82em;margin-top:8px">
    ⚠ Jadrové hlavice možno presunúť len do krajiny so schválením Rady bezpečnosti.
  </div>
</div>
</body></html>"""


@app.route("/council")
def council_page():
    if not _require_session() or not _countries_allowed():
        return redirect("/lobby")
    uname  = session["username"]
    cdata  = _get_country_data()
    cncl   = _get_council_data()
    members = _council_members(cdata)
    me     = members.get(uname, {})
    now    = time.time()

    # ── Vypočítaj výsledky otvorených rezolúcií
    changed = False
    for res in cncl["resolutions"]:
        if res["status"] != "open":
            continue
        if res["expires_at"] < now:
            # Uzavri hlasovanie
            for_v = res["votes_for"]
            against_v = res["votes_against"]
            vetoed = res.get("vetoed_by")
            if vetoed:
                res["status"] = "vetoed"
            elif len(for_v) >= RES_QUOTA and len(against_v) == 0:
                res["status"] = "passed"
                cdata = _resolve_resolution(res, cdata)
                save_jf(KB_COUNTRIES, cdata)
            else:
                res["status"] = "rejected"
            changed = True
    if changed:
        save_jf(KB_COUNCIL, cncl)

    # ── Upozornenia z energetickej minihry (high proliferation heat)
    energy_data = load_jf(KB_ENERGY, {})
    heat_alerts = []
    for eu, ep in energy_data.items():
        heat = ep.get("proliferation_heat", 0)
        wg   = ep.get("fuel", {}).get("wg_pu", 0)
        if heat >= NUCLEAR_HEAT_THRESHOLD or wg > 0:
            # Nájdi krajinu hráča
            player_country = None
            for cid, cd in cdata.items():
                for users in cd.get("roles", {}).values():
                    ul = users if isinstance(users, list) else ([users] if users else [])
                    if eu.lower() in [u.lower() for u in ul]:
                        player_country = cid
                        break
                if player_country:
                    break
            heat_alerts.append({
                "player": eu, "heat": round(heat, 1),
                "wg_pu": round(wg, 3), "country": player_country
            })

    # ── HTML zostavenie
    open_res   = [r for r in cncl["resolutions"] if r["status"] == "open"]
    closed_res = [r for r in cncl["resolutions"] if r["status"] != "open"][-10:]

    def _res_html(res):
        rt   = RES_TYPES.get(res["type"], {})
        tc   = COUNTRY_BY_ID.get(res["target_country"], {})
        exp  = max(0, int(res["expires_at"] - now))
        th, tr2 = divmod(exp, 3600); tm, _ = divmod(tr2, 60)
        time_str = f"{th}h {tm:02d}m" if res["status"] == "open" else ""
        for_v = res["votes_for"]; against_v = res["votes_against"]
        vetoed = res.get("vetoed_by", "")
        col = {"open":"#ff9900","passed":"#39ff6a","rejected":"#ff3a3a","vetoed":"#ff88ff"}.get(res["status"],"#888")
        status_lbl = {"open":"🗳 OTVORENÉ","passed":"✅ SCHVÁLENÉ","rejected":"❌ ZAMIETNUTÉ","vetoed":"🚫 VETO"}.get(res["status"],res["status"])
        can_vote = res["status"] == "open" and me and uname not in for_v and uname not in against_v
        can_veto = res["status"] == "open" and me.get("is_permanent") and not vetoed
        vote_html = ""
        if can_vote or can_veto:
            vote_html = (
                f'<form method="POST" action="/council/vote" style="display:inline">'
                f'<input type="hidden" name="res_id" value="{res["id"]}">'
                f'<button name="vote" value="for" class="b">✅ ZA</button> '
                f'<button name="vote" value="against" class="b red">❌ PROTI</button>'
                f'{" <button name=vote value=veto class=b style=border-color:#ff88ff;color:#ff88ff>🚫 VETO</button>" if can_veto else ""}'
                f'</form>')
        return (
            f'<div style="border:1px solid {col}44;background:#050505;padding:8px 12px;margin-bottom:6px">'
            f'<div style="display:flex;justify-content:space-between;flex-wrap:wrap">'
            f'<span style="color:{col}">{rt.get("icon","")} {rt.get("name_sk","")}</span>'
            f'<span style="color:#888;font-size:.85rem">{status_lbl} {time_str}</span></div>'
            f'<div style="color:#cfffcf;margin:3px 0">{tc.get("flag","")} {tc.get("name",res["target_country"])}</div>'
            f'<div style="font-size:.85rem;color:#2a7a45">{rt.get("desc_sk","")}</div>'
            f'<div style="font-size:.82rem;margin-top:4px">'
            f'ZA: <span style="color:#39ff6a">{", ".join(for_v) or "—"}</span> &nbsp; '
            f'PROTI: <span style="color:#ff3a3a">{", ".join(against_v) or "—"}</span>'
            f'{" &nbsp; VETO: <span style=color:#ff88ff>" + vetoed + "</span>" if vetoed else ""}'
            f'</div>{vote_html}</div>'
        )

    open_html   = "".join(_res_html(r) for r in open_res) or '<div style="color:#2a7a45">Žiadne otvorené rezolúcie.</div>'
    closed_html = "".join(_res_html(r) for r in reversed(closed_res)) or '<div style="color:#2a7a45">—</div>'

    # Geneva mediátori
    geneva_members = {u: m for u, m in members.items() if m.get("is_geneva")}
    geneva_html = ""
    if geneva_members:
        gtags = " ".join(
            f'<span class="role-tag council">🕊 {u}</span>'
            for u in geneva_members
        )
        geneva_html = (
            f'<div class="card" style="border-color:#38d1ff44;background:#00080d">'
            f'<div class="card-title" style="color:#38d1ff">🇨🇭 SÍDLO RADY — ŽENEVA (neutrálne)</div>'
            f'<div style="color:#2a7a45;font-size:.85rem;margin-bottom:6px">'
            f'Neutrálni mediátori môžu navrhovať akékoľvek rezolúcie a hlasovať bez stranníctva. '
            f'Švajčiarsko je chránené — nikdy sa nemôže stať cieľom vojenskej akcie.'
            f'</div>{gtags}</div>'
        )

    # Formulár na novú rezolúciu (len pre členov Rady)
    new_res_html = ""
    if me:
        c_opts = "".join(f'<option value="{c["id"]}">{c["flag"]} {c["name"]}</option>' for c in COUNTRIES)
        t_opts = "".join(f'<option value="{tid}">{v["icon"]} {v["name_sk"]}</option>' for tid,v in RES_TYPES.items())
        role_label = ROLE_BY_ID.get(me.get("role",""), {}).get("name_sk", me.get("role",""))
        badges = ""
        if me.get("is_permanent"):
            badges += '&nbsp;<span style="color:#ff88ff">★ Stály člen — VETO</span>'
        if me.get("is_geneva"):
            badges += '&nbsp;<span style="color:#38d1ff">🕊 Neutrálny mediátor</span>'
        new_res_html = f"""
        <div class="card" style="border-color:#ff990044">
        <div class="card-title" style="color:#ff9900">📋 Podať novú rezolúciu</div>
        <div style="color:#2a7a45;font-size:.85rem;margin-bottom:6px">
          {COUNTRY_BY_ID.get(me.get("country",""),{}).get("flag","")}
          {COUNTRY_BY_ID.get(me.get("country",""),{}).get("name","").upper()} — {role_label}
          {badges}
        </div>
        <form method="POST" action="/council/propose" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <select name="target_country" style="min-width:160px">{c_opts}</select>
          <select name="res_type" style="min-width:200px">{t_opts}</select>
          <button type="submit" class="b">📋 Podať rezolúciu</button>
        </form></div>"""

    # Upozornenia z energetickej minihry
    alert_html = ""
    if heat_alerts:
        rows_a = ""
        for a in sorted(heat_alerts, key=lambda x: -x["heat"]):
            c_name = COUNTRY_BY_ID.get(a["country"], {}).get("name", "—") if a["country"] else "—"
            rows_a += (f'<div style="display:flex;gap:12px;padding:3px 0;border-bottom:1px solid #1a0000;font-size:.9rem">'
                      f'<span style="color:#ff9900;min-width:120px">{a["player"]}</span>'
                      f'<span style="color:#ff3a3a">heat: {a["heat"]}%</span>'
                      f'<span style="color:#ff88ff">WG-Pu: {a["wg_pu"]}</span>'
                      f'<span style="color:#888">{c_name}</span></div>')
        alert_html = (f'<div class="card" style="border-color:#ff3a3a44">'
                     f'<div class="card-title" style="color:#ff3a3a">⚠ JADROVÉ UPOZORNENIA — energetická minihra</div>'
                     f'{rows_a}</div>')

    # Zbraňový prehľad stálych členov
    perm_html = ""
    for cid in COUNCIL_PERMANENT:
        c  = COUNTRY_BY_ID.get(cid, {})
        cd = cdata.get(cid, {})
        w  = cd.get("weapons", {})
        nuc_ok = "✅" if w.get("nuclear_approved") else "❌"
        perm_html += (f'<span style="margin-right:16px">{c.get("flag","")} {c.get("name",cid)}: '
                     f'☢{nuc_ok} {w.get("warheads",0)} hlavíc 🚀{w.get("missiles",0)}</span>')

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Rada bezpečnosti — KB</title>{_COUNTRIES_CSS}</head><body>
<a href="/countries" class="btn-back">← Krajiny</a>
<h1>🏛 MEDZIGALAKTICKÁ RADA BEZPEČNOSTI</h1>
<div class="sub" style="margin-bottom:6px">Stáli členovia: {perm_html}</div>
{geneva_html}
{alert_html}
{new_res_html}
<div class="card">
  <div class="card-title">🗳 OTVORENÉ REZOLÚCIE</div>
  {open_html}
</div>
<div class="card" style="border-color:#2a7a4544">
  <div class="card-title" style="color:#2a7a45">📁 UZAVRETÉ REZOLÚCIE (posledných 10)</div>
  {closed_html}
</div>
<p><a href="/countries/transfer" style="color:#ff9900">↔ Presun materiálov medzi krajinami →</a></p>
</body></html>"""


@app.route("/council/propose", methods=["POST"])
def council_propose():
    if not _require_session() or not _countries_allowed():
        return redirect("/lobby")
    uname   = session["username"]
    cdata   = _get_country_data()
    members = _council_members(cdata)
    me      = members.get(uname)
    if not me:
        return redirect("/council")

    target  = request.form.get("target_country", "").strip()
    rtype   = request.form.get("res_type", "").strip()
    if target not in COUNTRY_BY_ID or rtype not in RES_TYPES:
        return redirect("/council")
    # Švajčiarsko je neutrálne — nemôže byť cieľom vojenskej akcie ani nukleárneho zákazu
    if target == COUNCIL_HQ_COUNTRY and rtype in ("war_auth", "nuclear_ban", "sanctions", "embargo"):
        return redirect("/council")

    cncl = _get_council_data()
    now  = time.time()
    res_id = f"res_{int(now)}_{uname[:4]}"
    cncl["resolutions"].append({
        "id":              res_id,
        "type":            rtype,
        "target_country":  target,
        "proposed_by":     uname,
        "proposed_by_country": me["country"],
        "proposed_at":     now,
        "votes_for":       [uname],
        "votes_against":   [],
        "vetoed_by":       None,
        "status":          "open",
        "expires_at":      now + RES_VOTE_HOURS * 3600,
    })
    save_jf(KB_COUNCIL, cncl)
    return redirect("/council")


@app.route("/council/vote", methods=["POST"])
def council_vote():
    if not _require_session() or not _countries_allowed():
        return redirect("/lobby")
    uname   = session["username"]
    cdata   = _get_country_data()
    members = _council_members(cdata)
    me      = members.get(uname)
    if not me:
        return redirect("/council")

    res_id = request.form.get("res_id", "")
    vote   = request.form.get("vote", "")
    cncl   = _get_council_data()

    for res in cncl["resolutions"]:
        if res["id"] != res_id or res["status"] != "open":
            continue
        if uname in res["votes_for"] or uname in res["votes_against"]:
            break
        if vote == "veto" and me.get("is_permanent"):
            res["vetoed_by"] = uname
            res["status"]    = "vetoed"
        elif vote == "for":
            res["votes_for"].append(uname)
        elif vote == "against":
            res["votes_against"].append(uname)
        break

    save_jf(KB_COUNCIL, cncl)
    return redirect("/council")


@app.route("/countries/<cid>/weapons", methods=["GET", "POST"])
def country_weapons(cid):
    """Správa zbraní krajiny — len owner alebo hráč s rolou v danej krajine."""
    if not _require_session() or not _countries_allowed():
        return redirect("/lobby")
    c = COUNTRY_BY_ID.get(cid)
    if not c:
        return redirect("/countries")
    uname  = session["username"]
    cdata  = _get_country_data()
    cd     = cdata.get(cid, {})
    w      = cd.setdefault("weapons", {"nuclear_approved": False, "warheads": 0,
                                        "missiles": 0, "conventional": 0, "cyber": 0})

    # Kto môže editovať: owner alebo hráč s rolou v krajine
    is_owner = session.get("owner") is True
    roles    = cd.get("roles", {})
    has_role = any(
        uname in (v if isinstance(v, list) else ([v] if v else []))
        for v in roles.values()
    )
    can_edit = is_owner or has_role
    if request.method == "POST" and not can_edit:
        return redirect(f"/countries/{cid}/weapons")

    msg = ""
    if request.method == "POST":
        action = request.form.get("action", "update")

        if action == "build_warhead":
            pu_type = request.form.get("pu_type", "wg_pu")
            try:
                qty = max(1, int(request.form.get("qty", 1)))
            except ValueError:
                qty = 1
            if not w.get("nuclear_approved"):
                msg = "NESCHVALENE"
            else:
                eu    = uname.upper()
                edata = load_jf(KB_ENERGY, {})
                ep    = edata.get(eu, {})
                fuel  = ep.get("fuel", {})
                cost_pu = qty * 1.0 if pu_type == "wg_pu" else qty * 5.0
                avail   = fuel.get(pu_type, 0.0)
                pu_label = "WG-Pu" if pu_type == "wg_pu" else "Pu-239"
                if avail < cost_pu:
                    msg = f"NEDOSTATOK:{pu_label}:{avail:.2f}"
                else:
                    fuel[pu_type] = round(avail - cost_pu, 3)
                    ep["fuel"] = fuel
                    heat_add = qty * (30 if pu_type == "wg_pu" else 10)
                    ep["proliferation_heat"] = min(100.0, round(
                        ep.get("proliferation_heat", 0) + heat_add, 2))
                    edata[eu] = ep
                    save_jf(KB_ENERGY, edata)
                    w["warheads"] = w.get("warheads", 0) + qty
                    msg = f"OK:{qty}:{pu_label}:{heat_add}"
        elif action == "buy_weapons":
            career = load_jf(KB_CAREER, {})
            entry  = career.get(uname, {})
            my_cr  = entry.get("career_cr", 0)
            total_cost = 0
            buys = {}
            for wtype, unit_cost in WEAPON_BUILD_COSTS.items():
                try:
                    qty = max(0, int(request.form.get(f"buy_{wtype}", 0)))
                except ValueError:
                    qty = 0
                if qty > 0:
                    buys[wtype] = qty
                    total_cost += qty * unit_cost
            if total_cost == 0:
                msg = "OK:nochange"
            elif my_cr < total_cost:
                msg = f"NEDOSTATOK_CR:{my_cr}:{total_cost}"
            else:
                entry["career_cr"] = my_cr - total_cost
                career[uname] = entry
                save_jf(KB_CAREER, career)
                for wtype, qty in buys.items():
                    w[wtype] = w.get(wtype, 0) + qty
                msg = f"OK_WEAPONS:{total_cost}"

        if w.get("warheads", 0) > 0 and not w.get("nuclear_approved"):
            cncl = _get_council_data()
            cncl.setdefault("alerts", []).append({
                "ts": time.time(),
                "msg": f"ALERT:{c['name']}:{w['warheads']}",
                "country": cid,
            })
            save_jf(KB_COUNCIL, cncl)
        cd["weapons"] = w
        cdata[cid]    = cd
        save_jf(KB_COUNTRIES, cdata)
        return redirect(f"/countries/{cid}/weapons?msg={msg}")

    # Čítaj správu z query stringu
    raw_msg = request.args.get("msg", "")
    msg_html = ""
    if raw_msg.startswith("OK:"):
        parts = raw_msg.split(":")
        msg_html = f'<div style="color:#39ff6a;margin-bottom:8px">✅ Vyrobených {parts[1]} hlavíc (−{parts[1]} {parts[2]}, heat +{parts[3]})</div>'
    elif raw_msg.startswith("OK_WEAPONS:"):
        msg_html = f'<div style="color:#39ff6a;margin-bottom:8px">✅ Zbrane nakúpené za {int(raw_msg.split(":")[1]):,} CR.</div>'
    elif raw_msg == "NESCHVALENE":
        msg_html = '<div style="color:#ff3a3a;margin-bottom:8px">❌ Krajina nemá schválenie Rady na jadrové zbrane!</div>'
    elif raw_msg.startswith("NEDOSTATOK:"):
        parts = raw_msg.split(":")
        msg_html = f'<div style="color:#ff3a3a;margin-bottom:8px">❌ Nedostatok {parts[1]}: {parts[2]}</div>'
    elif raw_msg.startswith("NEDOSTATOK_CR:"):
        parts = raw_msg.split(":")
        msg_html = f'<div style="color:#ff3a3a;margin-bottom:8px">❌ Nedostatok CR: máš {int(parts[1]):,}, potrebuješ {int(parts[2]):,}</div>'

    nuc_ok  = w.get("nuclear_approved", False)
    nuc_col = "#39ff6a" if nuc_ok else "#ff3a3a"
    nuc_lbl = "✅ SCHVÁLENÝ Radou bezpečnosti" if nuc_ok else "❌ NESCHVÁLENÝ — porušenie medzinárodného práva"

    # Pu zásoby hráča z minihry
    eu    = uname.upper()
    edata = load_jf(KB_ENERGY, {})
    ep    = edata.get(eu, {})
    fuel  = ep.get("fuel", {})
    pu239_avail = fuel.get("pu239", 0.0)
    wg_avail    = fuel.get("wg_pu", 0.0)

    build_html = ""
    if can_edit and nuc_ok:
        build_html = (
            f'<div class="card" style="border-color:#ff440044;margin-top:6px">'
            f'<div class="card-title" style="color:#ff9900">☢ Výroba jadrových hlavíc</div>'
            f'<div class="row"><span class="lbl">WG-Pu (zásoby)</span>'
            f'<span style="color:#ff3a3a">{wg_avail:.3f} ks'
            f' <small style="color:#555">(1 WG-Pu = 1 hlavica, +30 heat)</small></span></div>'
            f'<div class="row"><span class="lbl">Pu-239 (zásoby)</span>'
            f'<span style="color:#ff9900">{pu239_avail:.2f} ks'
            f' <small style="color:#555">(5 Pu-239 = 1 hlavica, +10 heat)</small></span></div>'
            f'<form method="POST" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px">'
            f'<input type="hidden" name="action" value="build_warhead">'
            f'<select name="pu_type" style="min-width:120px">'
            f'<option value="wg_pu">☢ WG-Pu (1:1)</option>'
            f'<option value="pu239">Pu-239 (5:1)</option></select>'
            f'<input type="number" name="qty" value="1" min="1" style="width:60px">'
            f'<button type="submit" class="b" style="border-color:#ff3a3a;color:#ff3a3a">☢ Vyrobiť</button>'
            f'</form>'
            f'<div style="color:#2a7a45;font-size:.82rem;margin-top:4px">'
            f'Plutónium: Energetická minihra alebo'
            f' <a href="/countries/pu_market" style="color:#ff9900">Trh Pu →</a>'
            f'</div></div>'
        )
    elif can_edit:
        build_html = (
            f'<div style="color:#ff9900;font-size:.9rem;margin:8px 0">'
            f'☢ Výroba hlavíc blokovaná — krajina nemá schválenie Rady. '
            f'<a href="/council" style="color:#ff88ff">Požiadaj Radu →</a></div>'
        )

    edit_html = ""
    # Načítaj CR hráča pre zobrazenie cien
    _career_tmp = load_jf(KB_CAREER, {})
    my_cr_disp  = _career_tmp.get(uname, {}).get("career_cr", 0)

    if can_edit:
        _rows_buy = ""
        _icons = {"conventional": "🪖", "missiles": "🚀", "cyber": "💻"}
        _names = {"conventional": "Konvenčné sily (tis.)", "missiles": "Balistické rakety", "cyber": "Kybernetické zbrane"}
        for wtype, unit_cost in WEAPON_BUILD_COSTS.items():
            stock = w.get(wtype, 0)
            _rows_buy += (
                f'<div class="row">'
                f'<span class="lbl">{_icons[wtype]} {_names[wtype]}</span>'
                f'<span style="display:flex;align-items:center;gap:6px">'
                f'<span class="val" style="min-width:50px">{stock:,}</span>'
                f'<input type="number" name="buy_{wtype}" value="0" min="0" style="width:60px" placeholder="+qty">'
                f'<span style="color:#2a7a45;font-size:.82rem">{unit_cost:,} CR/ks</span>'
                f'</span></div>'
            )
        edit_html = (
            f'<div class="card" style="border-color:#ff990044;margin-top:6px">'
            f'<div class="card-title" style="color:#ff9900">🏭 Nakúpiť zbrane <small style="color:#555">(tvoje CR: {my_cr_disp:,})</small></div>'
            f'<form method="POST">'
            f'<input type="hidden" name="action" value="buy_weapons">'
            f'{_rows_buy}'
            f'<button type="submit" class="b" style="margin-top:8px;border-color:#ff9900;color:#ff9900">🏭 Nakúpiť</button>'
            f'</form></div>'
        )

    return (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<title>{c["name"]} — Zbrane</title>{_COUNTRIES_CSS}</head><body>'
        f'<a href="/countries/{cid}" class="btn-back">← {c["name"]}</a>'
        f'<h1>{c["flag"]} {c["name"]} — Zbraňový arzenál</h1>'
        f'{msg_html}'
        f'<div class="card">'
        f'<div class="card-title">☢ Jadrový status</div>'
        f'<div style="color:{nuc_col};margin-bottom:6px">{nuc_lbl}</div>'
        f'<div class="row"><span class="lbl">☢ Hlavice</span><span class="val">{w.get("warheads",0)}</span></div>'
        f'<div class="row"><span class="lbl">🚀 Rakety</span><span class="val">{w.get("missiles",0)}</span></div>'
        f'<div class="row"><span class="lbl">🪖 Konv. sily</span><span class="val">{w.get("conventional",0):,} tis.</span></div>'
        f'<div class="row"><span class="lbl">💻 Kyber</span><span class="val">{w.get("cyber",0)}</span></div>'
        f'</div>'
        f'{edit_html}'
        f'{build_html}'
        f'<p><a href="/countries/pu_market" style="color:#ff9900">🔬 Trh Pu →</a>'
        f' &nbsp;|&nbsp; <a href="/council" style="color:#ff88ff">🏛 Rada →</a>'
        f' &nbsp;|&nbsp; <a href="/countries/{cid}" style="color:#39ff6a">← Späť</a></p>'
        f'</body></html>'
    )


@app.route("/owner/nuclear_approve", methods=["POST"])
def owner_nuclear_approve():
    if not _owner_check():
        return redirect("/owner")
    cid      = request.form.get("cid", "").strip()
    approved = request.form.get("approved", "0") == "1"
    if cid not in COUNTRY_BY_ID:
        return redirect("/owner/panel")
    cdata = _get_country_data()
    cdata[cid].setdefault("weapons", {})["nuclear_approved"] = approved
    save_jf(KB_COUNTRIES, cdata)
    return redirect("/owner/panel")


@app.route("/owner/assign_role", methods=["POST"])
def owner_assign_role():
    """Owner prideľuje / odoberá rolu hráčovi v krajine."""
    if not _owner_check():
        return redirect("/owner")
    cid    = request.form.get("cid", "").strip()
    rid    = request.form.get("rid", "").strip()
    uname  = request.form.get("uname", "").strip()
    action = request.form.get("action", "add")  # add / remove

    if cid not in COUNTRY_BY_ID or rid not in ROLE_BY_ID:
        return redirect("/owner/panel")

    cdata = _get_country_data()
    cd    = cdata.setdefault(cid, {"roles": {}, "at_war": [], "sanctions": []})
    users = cd["roles"].get(rid, [])
    if isinstance(users, str):
        users = [users] if users else []

    if action == "add" and uname and uname not in users:
        users.append(uname)
    elif action == "remove" and uname in users:
        users.remove(uname)

    cd["roles"][rid] = users
    cdata[cid] = cd
    save_jf(KB_COUNTRIES, cdata)
    return redirect("/owner/panel")


# ── Trh plutónia ─────────────────────────────────────────────────────────────

@app.route("/countries/pu_market", methods=["GET", "POST"])
def pu_market():
    """Trh Pu — hráči predávajú Pu-239 / WG-Pu iným krajinám."""
    if not _require_session() or not _countries_allowed():
        return redirect("/lobby")
    uname = _uname()
    msg   = ""

    cncl = _get_council_data()
    cncl.setdefault("pu_listings", [])
    now = time.time()
    cncl["pu_listings"] = [l for l in cncl["pu_listings"] if l.get("expires_at", 0) > now]

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "list":
            pu_type = request.form.get("pu_type", "pu239")
            try:
                qty      = float(request.form.get("qty", 0))
                price_cr = int(request.form.get("price_cr", 0))
            except ValueError:
                qty = price_cr = 0
            edata = load_jf(KB_ENERGY, {})
            ep    = edata.get(uname, {})
            avail = ep.get("fuel", {}).get(pu_type, 0.0)
            if qty <= 0 or price_cr <= 0:
                msg = "CHYBA_INVALID"
            elif avail < qty:
                msg = f"CHYBA_QTY:{avail:.3f}"
            else:
                ep["fuel"][pu_type] = round(avail - qty, 3)
                edata[uname] = ep
                save_jf(KB_ENERGY, edata)
                cncl["pu_listings"].append({
                    "id": f"pu_{int(now)}_{uname[:4]}",
                    "seller": uname, "pu_type": pu_type,
                    "qty": qty, "price_cr": price_cr,
                    "expires_at": now + 48 * 3600,
                })
                save_jf(KB_COUNCIL, cncl)
                msg = f"OK_LIST:{qty}:{pu_type}:{price_cr}"

        elif action == "buy":
            lid     = request.form.get("lid", "")
            listing = next((l for l in cncl["pu_listings"] if l["id"] == lid), None)
            if not listing or listing["seller"] == uname:
                msg = "CHYBA_BUY"
            else:
                career = load_jf(KB_CAREER, {})
                my_cr  = career.get(uname, {}).get("career_cr", 0)
                if my_cr < listing["price_cr"]:
                    msg = f"CHYBA_CR:{my_cr}"
                else:
                    career[uname]["career_cr"] = my_cr - listing["price_cr"]
                    career.setdefault(listing["seller"], {})["career_cr"] = \
                        career[listing["seller"]].get("career_cr", 0) + listing["price_cr"]
                    save_jf(KB_CAREER, career)
                    edata = load_jf(KB_ENERGY, {})
                    edata.setdefault(uname, {}).setdefault("fuel", {})[listing["pu_type"]] = round(
                        edata[uname]["fuel"].get(listing["pu_type"], 0.0) + listing["qty"], 3)
                    save_jf(KB_ENERGY, edata)
                    cncl["pu_listings"] = [l for l in cncl["pu_listings"] if l["id"] != lid]
                    save_jf(KB_COUNCIL, cncl)
                    msg = f"OK_BUY:{listing['qty']}:{listing['pu_type']}:{listing['seller']}:{listing['price_cr']}"

        elif action == "cancel":
            lid     = request.form.get("lid", "")
            listing = next((l for l in cncl["pu_listings"] if l["id"] == lid and l["seller"] == uname), None)
            if listing:
                edata = load_jf(KB_ENERGY, {})
                edata.setdefault(uname, {}).setdefault("fuel", {})[listing["pu_type"]] = round(
                    edata[uname]["fuel"].get(listing["pu_type"], 0.0) + listing["qty"], 3)
                save_jf(KB_ENERGY, edata)
                cncl["pu_listings"] = [l for l in cncl["pu_listings"] if l["id"] != lid]
                save_jf(KB_COUNCIL, cncl)
                msg = "OK_CANCEL"
        return redirect(f"/countries/pu_market?msg={msg}")

    # HTML
    raw_msg = request.args.get("msg", "")
    if raw_msg.startswith("OK_LIST:"):
        p = raw_msg.split(":")
        lbl = "WG-Pu" if p[2] == "wg_pu" else "Pu-239"
        msg_html = f'<div style="color:#39ff6a;margin-bottom:8px">✅ Ponuka {p[1]} {lbl} za {p[3]} CR pridaná.</div>'
    elif raw_msg.startswith("OK_BUY:"):
        p = raw_msg.split(":")
        lbl = "WG-Pu" if p[2] == "wg_pu" else "Pu-239"
        msg_html = f'<div style="color:#39ff6a;margin-bottom:8px">✅ Kúpené {p[1]} {lbl} od {p[3]} za {p[4]} CR.</div>'
    elif raw_msg == "OK_CANCEL":
        msg_html = '<div style="color:#39ff6a;margin-bottom:8px">✅ Ponuka zrušená.</div>'
    elif raw_msg.startswith("CHYBA"):
        msg_html = f'<div style="color:#ff3a3a;margin-bottom:8px">❌ Chyba: {raw_msg}</div>'
    else:
        msg_html = ""

    edata    = load_jf(KB_ENERGY, {})
    ep       = edata.get(uname, {})
    my_pu239 = ep.get("fuel", {}).get("pu239", 0.0)
    my_wg    = ep.get("fuel", {}).get("wg_pu", 0.0)
    listings = cncl.get("pu_listings", [])

    rows = ""
    for l in sorted(listings, key=lambda x: x["price_cr"]):
        lbl     = "☢ WG-Pu" if l["pu_type"] == "wg_pu" else "Pu-239"
        col     = "#ff3a3a" if l["pu_type"] == "wg_pu" else "#ff9900"
        is_mine = l["seller"] == uname
        left_h  = max(0, int((l["expires_at"] - now) / 3600))
        btns = ""
        if not is_mine:
            btns += (f'<form method="POST" style="display:inline">'
                    f'<input type="hidden" name="action" value="buy">'
                    f'<input type="hidden" name="lid" value="{l["id"]}">'
                    f'<button class="b" style="font-size:.85rem">Kúpiť</button></form>')
        if is_mine:
            btns += (f'<form method="POST" style="display:inline;margin-left:4px">'
                    f'<input type="hidden" name="action" value="cancel">'
                    f'<input type="hidden" name="lid" value="{l["id"]}">'
                    f'<button class="b red" style="font-size:.85rem">Zrušiť</button></form>')
        rows += (f'<tr><td style="color:{col}">{lbl}</td><td>{l["qty"]:.3f}</td>'
                f'<td style="color:#ff9900">{l["price_cr"]:,} CR</td>'
                f'<td style="color:#888">{l["seller"]}</td>'
                f'<td style="color:#555">{left_h}h</td><td>{btns}</td></tr>')
    if not rows:
        rows = '<tr><td colspan="6" style="color:#2a7a45">Žiadne ponuky.</td></tr>'

    return (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<title>Trh Pu — KB</title>{_COUNTRIES_CSS}</head><body>'
        f'<a href="/countries" class="btn-back">← Krajiny</a>'
        f'<h1>🔬 TRH PLUTÓNIA</h1>'
        f'<div class="sub">Tvoje: Pu-239: <span style="color:#ff9900">{my_pu239:.2f}</span>'
        f' &nbsp; WG-Pu: <span style="color:#ff3a3a">{my_wg:.3f}</span></div>'
        f'{msg_html}'
        f'<div class="card" style="border-color:#ff990044">'
        f'<div class="card-title" style="color:#ff9900">📤 Ponúknuť Pu na predaj</div>'
        f'<form method="POST" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        f'<input type="hidden" name="action" value="list">'
        f'<select name="pu_type"><option value="pu239">Pu-239</option>'
        f'<option value="wg_pu">☢ WG-Pu</option></select>'
        f'<input type="number" name="qty" value="1" min="0.001" step="0.001" style="width:80px">'
        f'<input type="number" name="price_cr" value="10000" min="1" style="width:100px" placeholder="CR">'
        f'<button type="submit" class="b">📤 Ponúknuť</button></form></div>'
        f'<div class="card"><div class="card-title">🛒 Aktívne ponuky</div>'
        f'<table class="ctable"><thead><tr>'
        f'<th>Typ</th><th>Mn.</th><th>Cena</th><th>Predajca</th><th>Zostatok</th><th></th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>'
        f'</body></html>'
    )


# ── Investície ───────────────────────────────────────────────────────────────

@app.route("/energy/invest", methods=["GET", "POST"])
def energy_invest():
    """Investor vloží CR alebo palivo do profilu iného hráča."""
    if not _require_session() or not _energy_allowed():
        return redirect("/lobby")
    uname  = _uname()
    career = load_jf(KB_CAREER, {})
    my_cr  = career.get(uname, {}).get("career_cr", 0)
    invs   = load_jf(KB_INVESTMENTS, {})
    now    = time.time()
    lang   = session.get("lang", "sk")

    def Lp(sk, en): return en if lang == "en" else sk

    msg = ""
    if request.method == "POST":
        target  = request.form.get("target", "").strip().upper()
        inv_type = request.form.get("inv_type", "cr")   # cr / fuel
        try:
            amount = int(request.form.get("amount", 0))
        except ValueError:
            amount = 0

        edata = load_jf(KB_ENERGY, {})
        # Overenia
        if target == uname:
            msg = "❌ Nemôžeš investovať sám do seba."
        elif target not in edata:
            msg = "❌ Hráč nemá energetický profil."
        elif amount < INV_MIN_CR and inv_type == "cr":
            msg = f"❌ Minimum {INV_MIN_CR} CR."
        elif amount < INV_MIN_FUEL and inv_type == "fuel":
            msg = f"❌ Minimum {INV_MIN_FUEL} paliva."
        else:
            my_invs = [i for i in invs.get(uname, []) if i["expires_at"] > now]
            if len(my_invs) >= INV_MAX_ACTIVE:
                msg = f"❌ Max {INV_MAX_ACTIVE} aktívnych investícií."
            elif inv_type == "cr" and my_cr < amount:
                msg = "❌ Nedostatok CR."
            elif inv_type == "fuel":
                my_profile = _energy_tick(uname)
                avail_fuel = my_profile.get("energy", 0.0)
                if avail_fuel < amount:
                    msg = f"❌ Nedostatok energie: {avail_fuel:.0f} < {amount}"
                else:
                    # Odober energiu
                    my_profile["energy"] = round(avail_fuel - amount, 1)
                    edata[uname] = my_profile
                    save_jf(KB_ENERGY, edata)
                    # Zapíš investíciu
                    inv_rec = {
                        "from": uname, "to": target,
                        "type": "fuel", "amount": amount,
                        "return_amount": round(amount * INV_RETURN_RATE),
                        "created_at": now,
                        "expires_at": now + INV_DURATION_H * 3600,
                        "paid": False,
                    }
                    invs.setdefault(uname, []).append(inv_rec)
                    save_jf(KB_INVESTMENTS, invs)
                    msg = f"✅ Investícia {amount} energie do {target} — návrat: {inv_rec['return_amount']} o {INV_DURATION_H}h"
            if not msg:  # CR investícia
                career[uname]["career_cr"] = my_cr - amount
                save_jf(KB_CAREER, career)
                inv_rec = {
                    "from": uname, "to": target,
                    "type": "cr", "amount": amount,
                    "return_amount": round(amount * INV_RETURN_RATE),
                    "created_at": now,
                    "expires_at": now + INV_DURATION_H * 3600,
                    "paid": False,
                }
                invs.setdefault(uname, []).append(inv_rec)
                save_jf(KB_INVESTMENTS, invs)
                msg = f"✅ Investícia {amount:,} CR do {target} — návrat: {inv_rec['return_amount']:,} CR o {INV_DURATION_H}h"

    # Načítaj aktívne investície
    my_invs   = [i for i in invs.get(uname, []) if i["expires_at"] > now and not i["paid"]]
    recv_invs = [i for i in invs.get(uname, []) if i.get("received_from")]   # prijatých nemáme zatiaľ
    # Aj investície KDE som cieľom (hľadaj vo všetkých)
    incoming  = [i for uid, ul in invs.items() if uid != uname
                 for i in ul if i["to"] == uname and i["expires_at"] > now and not i["paid"]]

    def _inv_row(i, show_collect=False):
        left = max(0, int(i["expires_at"] - now))
        h, r2 = divmod(left, 3600); m2, _ = divmod(r2, 60)
        t_str = f"{h}h {m2:02d}m"
        typ_icon = "⚡" if i["type"] == "fuel" else "💰"
        partner  = i["to"] if i["from"] == uname else i["from"]
        col_btn  = ""
        if show_collect and left == 0:
            col_btn = (f'<form method="POST" action="/energy/invest_collect" style="display:inline">'
                      f'<input type="hidden" name="inv_id" value="{i["created_at"]}">'
                      f'<button class="btn-buy" style="border-color:#39ff6a;color:#39ff6a;font-size:.8em">Vybrať</button></form>')
        return (f'<div class="row"><span class="lbl">{typ_icon} {partner}</span>'
                f'<span class="val">{i["amount"]} → {i["return_amount"]} &nbsp; ⏱{t_str}</span>'
                f'{col_btn}</div>')

    out_html = "".join(_inv_row(i, show_collect=True) for i in my_invs) or f'<div style="color:#2a7a45">—</div>'
    in_html  = "".join(_inv_row(i) for i in incoming) or f'<div style="color:#2a7a45">—</div>'
    msg_html = f'<div style="color:{"#39ff6a" if msg.startswith("✅") else "#ff3a3a"};margin-bottom:8px">{msg}</div>' if msg else ""

    # Zoznam hráčov s profilom
    edata   = load_jf(KB_ENERGY, {})
    pl_opts = "".join(
        f'<option value="{u}">{u}</option>'
        for u in sorted(edata.keys()) if u != uname
    )
    my_cr = career.get(uname, {}).get("career_cr", 0)

    css = """<style>
body{background:#000;color:#cfffcf;font-family:'VT323',monospace;font-size:1.05rem;padding:12px}
.card{border:1px solid #1a3a1a;background:#020d02;max-width:680px;margin:0 auto 10px;padding:10px 14px}
.card-title{color:#39ff6a;font-size:1.1rem;margin-bottom:8px}
.row{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #0a1a0a}
.lbl{color:#2a7a45}.val{color:#cfffcf}
.btn-back{display:inline-block;margin-bottom:10px;color:#39ff6a;border:1px solid #39ff6a44;padding:3px 10px;font-family:inherit}
.btn-buy{background:#010d01;border:1px solid #39ff6a;color:#39ff6a;font-family:inherit;padding:2px 8px;cursor:pointer}
input,select{background:#000;border:1px solid #2a7a45;color:#cfffcf;font-family:inherit;font-size:.95rem;padding:2px 5px}
h1{color:#39ff6a;font-size:1.3rem;margin:4px 0}
</style><link href="https://fonts.googleapis.com/css2?family=VT323&display=swap" rel="stylesheet">"""

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Investície — KB</title>{css}</head><body>
<a href="/energy" class="btn-back">← Energia</a>
<h1>💰 INVESTÍCIE — ENERGETICKÁ MINIHRA</h1>
<div style="color:#2a7a45;font-size:.85rem;margin-bottom:8px">
  Investuj CR alebo energiu do iných hráčov. Návrat: ×{INV_RETURN_RATE} po {INV_DURATION_H}h.
  Max {INV_MAX_ACTIVE} aktívnych. Tvoje CR: {my_cr:,}
</div>
{msg_html}
<div class="card" style="border-color:#ff990044">
  <div class="card-title" style="color:#ff9900">💸 Nová investícia</div>
  <form method="POST" style="display:grid;gap:8px;max-width:400px">
    <div class="row"><span class="lbl">Hráč:</span><select name="target">{pl_opts}</select></div>
    <div class="row"><span class="lbl">Typ:</span>
      <select name="inv_type">
        <option value="cr">💰 CR (kredity)</option>
        <option value="fuel">⚡ Energia (minihra)</option>
      </select></div>
    <div class="row"><span class="lbl">Suma:</span>
      <input type="number" name="amount" value="{INV_MIN_CR}" min="1" style="width:100px"></div>
    <button type="submit" class="btn-buy" style="margin-top:4px">💸 Investovať</button>
  </form>
</div>
<div class="card">
  <div class="card-title">📤 Moje investície (posielam)</div>{out_html}
</div>
<div class="card" style="border-color:#38d1ff44">
  <div class="card-title" style="color:#38d1ff">📥 Investície do mňa (prijímam)</div>{in_html}
</div>
</body></html>"""


@app.route("/energy/invest_collect", methods=["POST"])
def energy_invest_collect():
    """Vyber späť investíciu po vypršaní + výnos."""
    if not _require_session() or not _energy_allowed():
        return redirect("/")
    uname  = _uname()
    try:
        inv_id = float(request.form.get("inv_id", 0))
    except ValueError:
        return redirect("/energy/invest")
    invs = load_jf(KB_INVESTMENTS, {})
    now  = time.time()
    for i in invs.get(uname, []):
        if abs(i["created_at"] - inv_id) < 1 and not i["paid"] and i["expires_at"] <= now:
            i["paid"] = True
            if i["type"] == "cr":
                career = load_jf(KB_CAREER, {})
                career.setdefault(uname, {})["career_cr"] = \
                    career[uname].get("career_cr", 0) + i["return_amount"]
                save_jf(KB_CAREER, career)
            else:
                edata = load_jf(KB_ENERGY, {})
                if uname in edata:
                    edata[uname]["energy"] = round(
                        edata[uname].get("energy", 0) + i["return_amount"], 1)
                    save_jf(KB_ENERGY, edata)
            break
    save_jf(KB_INVESTMENTS, invs)
    return redirect("/energy/invest")


# ── Štart ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n  KOZMICKÉ BANE v4.7 — Web Server")
    print(f"  Otvor: http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
