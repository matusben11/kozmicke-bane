"""
KOZMICKÉ BANE v4.8 — Web Server
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
KB_ENERGY   = DATA_DIR / "kb_energy.json"
KB_MARKET   = DATA_DIR / "kb_market.json"
KB_AUCTIONS = DATA_DIR / "kb_auctions.json"

# ── Upstash Redis — voliteľné perzistentné KV úložisko ─────────────────────
# Nastav UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN v Render env vars.
# Ak nie sú nastavené, používajú sa lokálne súbory (pre vývoj).
_KV_URL   = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
_KV_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
print(f"[KV] URL set: {bool(_KV_URL)} | TOKEN set: {bool(_KV_TOKEN)} | URL prefix: {_KV_URL[:30] if _KV_URL else 'NONE'}")

# Mapovanie cesty súboru → Redis kľúč
_KV_KEYS = {
    DATA_FILE:   "game_users",
    KB_CAREER:   "kb_career",
    KB_SAVES:    "kb_saves",
    KB_LB:       "kb_leaderboard",
    KB_ENERGY:   "kb_energy",
    KB_MARKET:   "kb_market",
    KB_AUCTIONS: "kb_auctions",
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
        "id": "energy_minigame",
        "public": False,
        "name_sk": "Energetická minihra",
        "desc_sk": "Stav elektrárne, vyrábaj energiu — základ pre budúci trh",
        "name_en": "Energy minigame",
        "desc_en": "Build power plants, produce energy — foundation for the future market",
    },
]

# ── Energetická minihra — konštanty ─────────────────────────────────────────

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
}

FUEL_SHOP = [
    {
        "id": "coal", "icon": "⛏",
        "name_sk": "Uhlie", "name_en": "Coal",
        "pack_qty": 20, "pack_cost": 800,
        "unit_sk": "ton", "unit_en": "tons",
    },
    {
        "id": "uranium", "icon": "☢",
        "name_sk": "Urán (palivové články)", "name_en": "Uranium (fuel rods)",
        "pack_qty": 5, "pack_cost": 6000,
        "unit_sk": "ks", "unit_en": "rods",
    },
]

MAX_ENERGY        = 1000  # maximálna kapacita zásobníka energie
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
    "gold":    {"liq": 25,  "rev": 0.10, "min_b": 150, "max_b": 3000,"min_s": 160, "max_s": 3200},
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
     "qty": 5,   "start_bid": 1800,  "source": "commodity_gold", "duration_min": 60},
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

def _atomic_write(path, text):
    """Write text to a temp file then rename — prevents corruption on crash."""
    path = pathlib.Path(path)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)

def load_users():
    try:
        if DATA_FILE.exists():
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[WARN] game_users.json corrupted ({e}), starting fresh.")
    return {}

def save_users(u):
    _atomic_write(DATA_FILE, json.dumps(u, indent=4, ensure_ascii=False))
    _kv_set("game_users", u)  # dual-write do Upstash (ticho zlyhá ak nie je nakonfigurovaný)

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
    """Načítaj zo lokálneho súboru (po startup warm-up je vždy aktuálny)."""
    try:
        p = pathlib.Path(path)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}

def save_jf(path, data):
    """Zapiš do lokálneho súboru A zároveň do Upstash (dual-write)."""
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    key = _KV_KEYS.get(pathlib.Path(path), pathlib.Path(path).stem)
    _kv_set(key, data)  # ticho zlyhá ak Upstash nie je nakonfigurovaný


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
    profile["fuel"].setdefault("uranium", 0.0)
    profile.setdefault("commodities", {})
    profile["commodities"].setdefault("oil", 0.0)
    profile["commodities"].setdefault("gold", 0.0)
    profile.setdefault("active_events", [])
    profile.setdefault("last_event", None)
    profile.setdefault("last_event_at", 0.0)
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

    elif effect == "solar_boost" or effect == "sell_bonus":
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
    solar_mult = 1.0
    failed_plant_idx = None
    for ae in active_events:
        if ae["effect"] == "solar_boost":
            solar_mult = max(solar_mult, ae["value"])
        elif ae["effect"] == "plant_fail":
            failed_plant_idx = ae.get("plant_idx")
    profile["active_events"] = active_events

    for idx, plant_id in enumerate(profile["plants"]):
        if idx == failed_plant_idx:
            continue
        pt = PLANT_TYPES.get(plant_id)
        if not pt:
            continue
        if pt["fuel_type"] is None:
            energy += pt["energy_per_hr"] * elapsed_hrs * solar_mult
        else:
            avail = fuel.get(pt["fuel_type"], 0.0)
            if avail <= 0:
                continue
            hrs = min(elapsed_hrs, avail / pt["fuel_per_hr"])
            energy += pt["energy_per_hr"] * hrs
            fuel[pt["fuel_type"]] = max(0.0, avail - hrs * pt["fuel_per_hr"])

    profile["energy"] = round(min(energy, MAX_ENERGY), 1)
    profile["fuel"] = {k: round(v, 2) for k, v in fuel.items()}
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
    """Odhadne hodnotu firmy (súčet build_cost elektrární) pre bankrotovú ponuku."""
    total = sum(PLANT_TYPES.get(p, {}).get("build_cost", 0) for p in profile.get("plants", []))
    fuel = profile.get("fuel", {})
    comm = profile.get("commodities", {})
    total += int(fuel.get("coal", 0)) * 45
    total += int(fuel.get("uranium", 0)) * 1300
    total += int(comm.get("oil", 0)) * 58
    total += int(comm.get("gold", 0)) * 500
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
    for pid, cnt in plant_counts.items():
        pt = PLANT_TYPES.get(pid)
        if not pt:
            continue
        if pt["fuel_type"] is None:
            total_rate += pt["energy_per_hr"] * cnt
        else:
            if fuel.get(pt["fuel_type"], 0) > 0:
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
                fstock = fuel.get(pt["fuel_type"], 0)
                if fstock > 0:
                    status   = f'<span class="active">▶ {Lp("AKTÍVNA","ACTIVE")}</span>'
                    rate_str = f'+{pt["energy_per_hr"] * cnt} E/hod'
                else:
                    status   = f'<span class="idle">⏸ {Lp("NEČINNÁ — bez paliva","IDLE — no fuel")}</span>'
                    rate_str = "+0 E/hod"
                fuel_name = Lp(next(f for f in FUEL_SHOP if f["id"] == pt["fuel_type"])["name_sk"],
                               next(f for f in FUEL_SHOP if f["id"] == pt["fuel_type"])["name_en"])
                unit      = Lp(next(f for f in FUEL_SHOP if f["id"] == pt["fuel_type"])["unit_sk"],
                               next(f for f in FUEL_SHOP if f["id"] == pt["fuel_type"])["unit_en"])
                fuel_str  = f'<span style="color:#2a7a45;font-size:.85em"> — {fuel_name}: {fstock:.1f} {unit}</span>'
            plants_html += (
                f'<div class="plant-row">'
                f'<span>{pt["icon"]} {name} ×{cnt}{fuel_str}</span>'
                f'<span>{status} &nbsp; <span class="val">{rate_str}</span></span>'
                f'</div>'
            )

    # ── Elektrárne na kúpu
    buy_plants_html = ""
    for pid, pt in PLANT_TYPES.items():
        name = Lp(pt["name_sk"], pt["name_en"])
        desc = Lp(pt["desc_sk"], pt["desc_en"])
        cnt  = plant_counts.get(pid, 0)
        can_afford = cr >= pt["build_cost"]
        at_max     = cnt >= pt["max_count"]
        disabled   = "disabled" if (not can_afford or at_max) else ""
        note       = f'({Lp("max","max")} {pt["max_count"]})' if at_max else ""
        warn       = f'<span class="warn"> — {Lp("nedostatok CR","not enough CR")}</span>' if (not can_afford and not at_max) else ""
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

    # ── Eventy ──────────────────────────────────────────────────
    now = time.time()
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

    if cr < pt["build_cost"] or cnt >= pt["max_count"]:
        return redirect("/energy")

    entry["career_cr"] = cr - pt["build_cost"]
    career[uname] = entry
    save_jf(KB_CAREER, career)

    data = load_jf(KB_ENERGY, {})
    data[uname]["plants"].append(plant_id)
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
        "plants":      list(profile.get("plants", [])),
        "energy":      round(profile.get("energy", 0), 1),
        "fuel":        dict(profile.get("fuel", {})),
        "commodities": dict(profile.get("commodities", {})),
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


# ── Štart ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n  KOZMICKÉ BANE v4.7 — Web Server")
    print(f"  Otvor: http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
