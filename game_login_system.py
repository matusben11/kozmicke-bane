"""
╔══════════════════════════════════════════════════════════╗
║   GAME LOGIN SYSTEM  v2.0                               ║
║   Prepojený s KOZMICKÉ BANE v3.0                        ║
╚══════════════════════════════════════════════════════════╝
"""

import hashlib
import json
import os
import random
import getpass
import time
import subprocess
import sys
import pathlib
from datetime import datetime

# ─────────────────────────────────────────
#  CESTY
# ─────────────────────────────────────────
BASE_DIR  = pathlib.Path(__file__).parent.resolve()
DATA_FILE = BASE_DIR / "game_users.json"
KB_CAREER = BASE_DIR / "kb_career.json"
APP_PY    = BASE_DIR / "app.py"
HTML_FILE = BASE_DIR / "kozmicke_bane.html"

# ─────────────────────────────────────────
#  FARBY
# ─────────────────────────────────────────
Y = "\033[93m"
G = "\033[92m"
R = "\033[91m"
B = "\033[96m"
D = "\033[90m"
W = "\033[97m"
X = "\033[0m"

def clr():
    os.system("cls" if os.name == "nt" else "clear")

def choose_version():
    clr()
    print(f"""
{Y}  ╔══════════════════════════════════════════════╗
  ║        🎮  KOZMICKÉ BANE v3.0               ║
  ║        Vyber spôsob spustenia               ║
  ╚══════════════════════════════════════════════╝{X}

  {Y}[1]{X}  🖥️   Desktop          {D}(natívna aplikácia){X}
  {Y}[2]{X}  🌐  Web lokálne      {D}(v prehliadači, len doma){X}
  {Y}[3]{X}  🌍  Web verejný      {D}(ngrok — potrebuje účet){X}
  {Y}[4]{X}  🔥  Web Cloudflare   {D}(BEZ účtu, automaticky){X}
""")
    while True:
        c = input("  Voľba [1/2/3/4]: ").strip()
        if c in ("1", "2", "3", "4"):
            return c
        print(f"  {R}Zadaj 1, 2, 3 alebo 4.{X}")

def slow(text, delay=0.025):
    for ch in text:
        print(ch, end="", flush=True)
        time.sleep(delay)
    print()

def hdr(title):
    print("═" * 50)
    print(f"  {W}{title}{X}")
    print("─" * 50)

# ─────────────────────────────────────────
#  DATABÁZA
# ─────────────────────────────────────────
def load_users():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=4, ensure_ascii=False)

# ─────────────────────────────────────────
#  KB KARIÉRNE DÁTA
# ─────────────────────────────────────────
def load_kb_career():
    if KB_CAREER.exists():
        try:
            with open(KB_CAREER, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def get_kb_stats(username):
    career = load_kb_career()
    empty = {"career_cr": 0, "sessions": 0, "rank": 1,
             "rank_name": "Baník", "best_session": 0,
             "total_mined": 0, "last_seen": "–"}
    return career.get(username.upper(), empty)

def kb_rank(cr):
    for threshold, r, name in [
        (10_000_000, 5, "Legenda"),
        (2_000_000,  4, "Veliteľ"),
        (500_000,    3, "Veterán"),
        (100_000,    2, "Prospektér"),
        (0,          1, "Baník"),
    ]:
        if cr >= threshold:
            return r, name
    return 1, "Baník"

# ─────────────────────────────────────────
#  BEZPEČNOSŤ
# ─────────────────────────────────────────
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def validate_pw(pw):
    if len(pw) < 6:
        return False, "Heslo musí mať aspoň 6 znakov."
    if not any(c.isdigit() for c in pw):
        return False, "Heslo musí obsahovať aspoň jednu číslicu."
    return True, "OK"

# ─────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────
def register(users):
    hdr("📝  REGISTRÁCIA")
    username = input(f"  {W}Meno:{X} ").strip()
    if not username:
        print(f"  {R}✗ Meno nemôže byť prázdne.{X}"); return False
    if username in users:
        print(f"  {R}✗ Meno '{username}' je obsadené.{X}"); return False
    pw = getpass.getpass(f"  {W}Heslo:{X} ")
    ok, msg = validate_pw(pw)
    if not ok:
        print(f"  {R}✗ {msg}{X}"); return False
    if getpass.getpass(f"  {W}Potvrď heslo:{X} ") != pw:
        print(f"  {R}✗ Heslá sa nezhodujú.{X}"); return False
    users[username] = {
        "password": hash_pw(pw),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "score": 0, "games_played": 0, "kb_sessions": 0,
    }
    save_users(users)
    print(f"\n  {G}✓ Účet '{username}' bol vytvorený!{X}")
    return True

def login(users):
    hdr("🔐  PRIHLÁSENIE")
    username = input(f"  {W}Meno:{X} ").strip()
    pw = getpass.getpass(f"  {W}Heslo:{X} ")
    if username not in users:
        print(f"  {R}✗ Používateľ '{username}' neexistuje.{X}"); return None
    if users[username]["password"] != hash_pw(pw):
        print(f"  {R}✗ Nesprávne heslo.{X}"); return None
    print(f"\n  {G}✓ Vitaj, {username}!{X}"); time.sleep(0.5)
    return username

def reset_password(users):
    hdr("🔑  RESET HESLA")
    username = input(f"  {W}Meno:{X} ").strip()
    if username not in users:
        print(f"  {R}✗ Neexistuje.{X}"); return
    created = users[username].get("created_at", "")
    ans = input(f"  {W}Dátum registrácie (YYYY-MM-DD):{X} ").strip()
    if ans not in created:
        print(f"  {R}✗ Nesprávna odpoveď.{X}"); return
    new_pw = getpass.getpass(f"  {W}Nové heslo:{X} ")
    ok, msg = validate_pw(new_pw)
    if not ok:
        print(f"  {R}✗ {msg}{X}"); return
    if getpass.getpass(f"  {W}Potvrď:{X} ") != new_pw:
        print(f"  {R}✗ Nezhodujú sa.{X}"); return
    users[username]["password"] = hash_pw(new_pw)
    save_users(users)
    print(f"  {G}✓ Heslo zmenené.{X}")

# ─────────────────────────────────────────
#  MINI HRA 1: Číslo
# ─────────────────────────────────────────
def play_guess(users, username):
    hdr("🔢  HÁDANIE ČÍSLA (1–100)")
    secret = random.randint(1, 100)
    score = 0
    for attempt in range(1, 8):
        try:
            guess = int(input(f"  Pokus {attempt}/7 → "))
        except ValueError:
            print(f"  {R}Zadaj číslo!{X}"); continue
        if guess == secret:
            score = max(100 - (attempt - 1) * 12, 10)
            print(f"\n  {G}🎉 Správne za {attempt} pokusov!  +{score} bodov{X}"); break
        print(f"  {'📈 Viac!' if guess < secret else '📉 Menej!'}")
    else:
        print(f"\n  {R}😢 Číslo bolo {secret}.{X}")
    users[username]["score"] = users[username].get("score", 0) + score
    users[username]["games_played"] = users[username].get("games_played", 0) + 1
    save_users(users)
    print(f"  {D}Celkové skóre: {users[username]['score']}{X}")

# ─────────────────────────────────────────
#  MINI HRA 2: Obesenec
# ─────────────────────────────────────────
WORDS = [
    ("python", "🐍 Programovací jazyk"),
    ("astronaut", "🚀 Cestovateľ vesmírom"),
    ("planety", "🪐 Kozmické telesá"),
    ("detektor", "🔍 Hľadacie zariadenie"),
    ("robotika", "🤖 Veda o robotoch"),
    ("gravitacia", "🌍 Príťažlivosť"),
    ("kristal", "💎 Priehľadný minerál"),
    ("vrtacka", "⛏ Ťažobné zariadenie"),
    ("asteroid", "☄️ Kozmická hornina"),
    ("kyslitanove", "💊 Chem. zlúčenina"),
]
STAGES = ["""
   ┌───┐
   │   │
       │
       │
   ════════""","""
   ┌───┐
   │   │
   O   │
       │
   ════════""","""
   ┌───┐
   │   │
   O   │
   │   │
   ════════""","""
   ┌───┐
   │   │
   O   │
  /│   │
   ════════""","""
   ┌───┐
   │   │
   O   │
  /│\\  │
   ════════""","""
   ┌───┐
   │   │
   O   │
  /│\\  │
  /    │
   ════════""","""
   ┌───┐
   │   │
   O   │
  /│\\  │
  / \\  │
   ════════"""]

def play_hangman(users, username):
    hdr("🪢  OBESENEC")
    word, hint = random.choice(WORDS)
    guessed, wrong = set(), set()
    while True:
        print(STAGES[len(wrong)])
        disp = " ".join(c if c in guessed else "_" for c in word)
        print(f"\n  Slovo: {W}{disp}{X}  |  Nápoveda: {D}{hint}{X}")
        print(f"  Chyby ({len(wrong)}/6): {R}{' '.join(sorted(wrong)) or '—'}{X}")
        if all(c in guessed for c in word):
            sc = max(80 - len(wrong) * 10, 10)
            print(f"\n  {G}🎉 {word.upper()}!  +{sc} bodov{X}")
            users[username]["score"] = users[username].get("score", 0) + sc
            users[username]["games_played"] = users[username].get("games_played", 0) + 1
            save_users(users); return
        if len(wrong) >= 6:
            print(f"\n  {R}💀 Slovo bolo: {word.upper()}{X}")
            users[username]["games_played"] = users[username].get("games_played", 0) + 1
            save_users(users); return
        g = input("\n  Hádaj písmeno: ").strip().lower()
        if not g or len(g) != 1 or not g.isalpha():
            print(f"  {R}Zadaj jedno písmeno!{X}"); continue
        if g in guessed or g in wrong:
            print(f"  {Y}Už si hádal '{g}'!{X}"); continue
        if g in word:
            guessed.add(g); print(f"  {G}✓ '{g}' je v slove!{X}")
        else:
            wrong.add(g); print(f"  {R}✗ '{g}' nie je v slove.{X}")

# ─────────────────────────────────────────
#  KOZMICKÉ BANE — LAUNCH
# ─────────────────────────────────────────
def launch_kb(users, username):
    clr()
    kb_before = get_kb_stats(username)

    print(f"""{Y}
  ██╗  ██╗ ██████╗ ███████╗███╗   ███╗██╗ ██████╗██╗  ██╗███████╗
  ██║ ██╔╝██╔═══██╗╚══███╔╝████╗ ████║██║██╔════╝██║ ██╔╝██╔════╝
  █████╔╝ ██║   ██║  ███╔╝ ██╔████╔██║██║██║     █████╔╝ █████╗
  ██╔═██╗ ██║   ██║ ███╔╝  ██║╚██╔╝██║██║██║     ██╔═██╗ ██╔══╝
  ██║  ██╗╚██████╔╝███████╗██║ ╚═╝ ██║██║╚██████╗██║  ██╗███████╗
  ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝     ╚═╝╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝{X}
  {Y}PILOT: {W}{username.upper()}{X}
""")
    r, rname = kb_rank(kb_before.get("career_cr", 0))
    print(f"  {D}── KARIÉRA ─────────────────────────────────{X}")
    print(f"  {Y}Kariérne CR:{X}  {kb_before.get('career_cr', 0):,}")
    print(f"  {Y}Rang:{X}         {r} — {rname}")
    print(f"  {Y}Sessioni:{X}     {kb_before.get('sessions', 0)}")
    print(f"  {Y}Najlepší run:{X} {kb_before.get('best_session', 0):,} CR")
    print()

    # Desktop verzia
    if not APP_PY.exists():
        print(f"  {R}✗ Súbor 'app.py' nenájdený v {BASE_DIR}{X}")
        input("\n  [ ENTER ]"); return
    if not HTML_FILE.exists():
        print(f"  {R}✗ Súbor 'kozmicke_bane.html' nenájdený v {BASE_DIR}{X}")
        input("\n  [ ENTER ]"); return

    print(f"  {G}✓ Spúšťam KOZMICKÉ BANE Desktop...{X}")
    print(f"  {D}Zatvor hernné okno pre návrat do menu.{X}\n")
    time.sleep(1)

    try:
        subprocess.run([sys.executable, str(APP_PY), username], cwd=str(BASE_DIR))
    except Exception as e:
        print(f"  {R}✗ Chyba: {e}{X}"); input("\n  [ ENTER ]"); return

    # Synchonizuj skóre späť
    kb_after = get_kb_stats(username)
    earned = kb_after.get("career_cr", 0) - kb_before.get("career_cr", 0)
    clr()
    r2, rname2 = kb_rank(kb_after.get("career_cr", 0))
    print(f"\n  {G}✓ Vitaj späť z Zyrax-9, {username}!{X}")
    if earned > 0:
        print(f"  {Y}Táto session: +{earned:,} CR{X}")
    print(f"  {Y}Kariérne CR: {kb_after.get('career_cr', 0):,}   Rang: {r2} — {rname2}{X}")

    users[username]["games_played"] = users[username].get("games_played", 0) + 1
    users[username]["kb_sessions"]  = users[username].get("kb_sessions", 0) + 1
    users[username]["score"]        = users[username].get("score", 0) + max(0, earned // 100)
    save_users(users)

    if earned > 0:
        print(f"  {D}+{max(0, earned // 100)} bodov pridaných do login systému{X}")
    input(f"\n  {D}[ ENTER ]{X}")

# ─────────────────────────────────────────
#  ŠTATISTIKY
# ─────────────────────────────────────────
def show_stats(users, username):
    clr(); hdr(f"👤  {username.upper()}  —  ŠTATISTIKY")
    u = users[username]
    kb = get_kb_stats(username)
    r, rname = kb_rank(kb.get("career_cr", 0))
    print(f"  {Y}Meno:           {X}{username}")
    print(f"  {Y}Registrácia:    {X}{u.get('created_at', '–')}")
    print(f"  {Y}Celkové skóre:  {X}{u.get('score', 0)} bodov")
    print(f"  {Y}Zahrané hry:    {X}{u.get('games_played', 0)}")
    print(f"\n  {D}── KOZMICKÉ BANE ──────────────────────{X}")
    print(f"  {Y}Kariérne CR:    {X}{kb.get('career_cr', 0):,}")
    print(f"  {Y}Rang:           {X}{r} — {rname}")
    print(f"  {Y}Sessioni:       {X}{kb.get('sessions', 0)}")
    print(f"  {Y}Najlepší run:   {X}{kb.get('best_session', 0):,} CR")
    print(f"  {Y}Celkom ťažby:   {X}{kb.get('total_mined', 0):,} ks")
    print(f"  {Y}Posledná hra:   {X}{kb.get('last_seen', '–')}")

# ─────────────────────────────────────────
#  LEADERBOARD
# ─────────────────────────────────────────
def show_leaderboard(users, current_user):
    clr(); hdr("🏆  LEADERBOARD")
    career = load_kb_career()
    medals = ["🥇", "🥈", "🥉"]

    print(f"\n  {W}── CELKOVÉ SKÓRE (minihriy + KB) ───────────{X}")
    ranked = sorted(users.items(), key=lambda x: x[1].get("score", 0), reverse=True)
    for i, (uname, data) in enumerate(ranked[:10]):
        m = medals[i] if i < 3 else f" {i+1}."
        you = f" {Y}◀ TY{X}" if uname == current_user else ""
        print(f"  {m} {W}{uname:<16}{X}{data.get('score', 0):>8} bodov{you}")

    print(f"\n  {Y}── KOZMICKÉ BANE — KARIÉRNE CR ─────────────{X}")
    entries = [(u, career.get(u.upper(), {}).get("career_cr", 0)) for u in users]
    entries.sort(key=lambda x: -x[1])
    shown = False
    for i, (uname, cr) in enumerate(entries[:10]):
        if cr == 0:
            continue
        shown = True
        m = medals[i] if i < 3 else f" {i+1}."
        _, rname = kb_rank(cr)
        you = f" {Y}◀ TY{X}" if uname.upper() == current_user.upper() else ""
        print(f"  {m} {W}{uname:<16}{X}{cr:>12,} CR  [{rname}]{you}")
    if not shown:
        print(f"  {D}  – zatiaľ žiadne KB záznamy –{X}")

# ─────────────────────────────────────────
#  GAME HUB
# ─────────────────────────────────────────
def game_menu(users, username):
    while True:
        clr()
        kb = get_kb_stats(username)
        r, rname = kb_rank(kb.get("career_cr", 0))
        print(f"""
{Y}  ╔══════════════════════════════════════════════╗
  ║  🎮  GAME HUB  {X}│ {W}{username.upper():<14}{Y}│ {rname:<14}║
  ╚══════════════════════════════════════════════╝{X}
  {Y}[1]{X} 🔢  Hádanie čísla
  {Y}[2]{X} 🪢  Obesenec
  {Y}[3]{X} 🚀  {W}KOZMICKÉ BANE v3.0{X}  {D}(desktop){X}
  {D}──────────────────────────────────────────────{X}
  {Y}[4]{X} 👤  Moje štatistiky
  {Y}[5]{X} 🏆  Leaderboard
  {Y}[6]{X} 🔑  Zmeniť heslo
  {Y}[7]{X} 🚪  Odhlásiť
  {D}──────────────────────────────────────────────{X}
  {D}KB: {kb.get('career_cr', 0):,} CR  │  Rang {r}: {rname}  │  Sessioni: {kb.get('sessions', 0)}{X}
""")
        c = input("  Voľba: ").strip()

        if c == "1":
            clr(); play_guess(users, username); input(f"\n  {D}[ ENTER ]{X}")
        elif c == "2":
            clr(); play_hangman(users, username); input(f"\n  {D}[ ENTER ]{X}")
        elif c == "3":
            launch_kb(users, username); users = load_users()
        elif c == "4":
            show_stats(users, username); input(f"\n  {D}[ ENTER ]{X}")
        elif c == "5":
            show_leaderboard(users, username); input(f"\n  {D}[ ENTER ]{X}")
        elif c == "6":
            clr(); hdr("🔑  ZMENA HESLA")
            old = getpass.getpass("  Aktuálne heslo: ")
            if users[username]["password"] != hash_pw(old):
                print(f"  {R}✗ Nesprávne heslo.{X}")
            else:
                new = getpass.getpass("  Nové heslo: ")
                ok, msg = validate_pw(new)
                if not ok:
                    print(f"  {R}✗ {msg}{X}")
                elif getpass.getpass("  Potvrď: ") != new:
                    print(f"  {R}✗ Nezhodujú sa.{X}")
                else:
                    users[username]["password"] = hash_pw(new)
                    save_users(users)
                    print(f"  {G}✓ Heslo zmenené!{X}")
            input(f"\n  {D}[ ENTER ]{X}")
        elif c == "7":
            print(f"\n  {Y}Dovidenia, {username}!{X}\n"); break
        else:
            time.sleep(0.4)

# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────
def _setup_ngrok(port):
    """Spustí ngrok tunel a vráti verejnú URL. Vráti None pri chybe."""
    try:
        from pyngrok import ngrok
    except ImportError:
        print(f"\n  {R}✗ pyngrok nie je nainštalovaný.{X}")
        print(f"  {Y}Spusti:{X}  pip install pyngrok")
        return None

    # Skontroluj či je nastavený auth token
    try:
        tunnel = ngrok.connect(port, "http")
        return tunnel.public_url
    except Exception as e:
        err = str(e)
        if "authtoken" in err.lower() or "auth" in err.lower() or "account" in err.lower():
            clr()
            print(f"""
{Y}  ╔══════════════════════════════════════════════╗
  ║   🔑  NGROK — PRVÉ NASTAVENIE              ║
  ╚══════════════════════════════════════════════╝{X}

  Pre verejný link potrebuješ bezplatný ngrok účet.

  {W}1.{X} Choď na:  {B}https://dashboard.ngrok.com/signup{X}
  {W}2.{X} Zaregistruj sa (zadarmo)
  {W}3.{X} Skopíruj svoj Auth Token z:
     {B}https://dashboard.ngrok.com/get-started/your-authtoken{X}
  {W}4.{X} Vlož token sem:
""")
            token = input(f"  {W}Auth Token:{X} ").strip()
            if not token:
                print(f"  {R}Token nebol zadaný. Spúšťam lokálne.{X}")
                return None
            try:
                from pyngrok import conf as ngrok_conf
                ngrok_conf.get_default().auth_token = token
                ngrok.set_auth_token(token)
                tunnel = ngrok.connect(port, "http")
                print(f"\n  {G}✓ Token uložený!{X}")
                return tunnel.public_url
            except Exception as e2:
                print(f"  {R}✗ Chyba: {e2}{X}")
                print(f"  {Y}Spúšťam lokálne.{X}")
                return None
        else:
            print(f"\n  {R}✗ Ngrok chyba: {e}{X}")
            print(f"  {Y}Spúšťam lokálne.{X}")
            return None


def _setup_cloudflare(port):
    """Cloudflare Tunnel — bez účtu. Automaticky stiahne cloudflared ak chýba."""
    import subprocess
    import re
    import shutil
    import threading
    import urllib.request

    # Nájdi alebo stiahni cloudflared
    cf = shutil.which("cloudflared")
    if not cf:
        cf_local = BASE_DIR / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
        if cf_local.exists():
            cf = str(cf_local)
        else:
            print(f"\n  {D}Sťahujem cloudflared (~30 MB)...{X}", flush=True)
            dl = (
                "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
                if os.name == "nt"
                else "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
            )
            try:
                urllib.request.urlretrieve(dl, str(cf_local))
                if os.name != "nt":
                    os.chmod(str(cf_local), 0o755)
                cf = str(cf_local)
                print(f"  {G}✓ cloudflared stiahnutý{X}")
            except Exception as e:
                print(f"  {R}✗ Stiahnutie zlyhalo: {e}{X}")
                return None, None

    # Spusti tunel
    try:
        proc = subprocess.Popen(
            [cf, "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except Exception as e:
        print(f"  {R}✗ Cloudflare chyba: {e}{X}")
        return None, None

    # Číta output v threade, hľadá trycloudflare.com URL
    found = [None]
    def reader():
        for line in proc.stdout:
            m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
            if m:
                found[0] = m.group(0)
                break
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    th.join(timeout=35)
    return proc, found[0]


def start_web(tunnel="none"):
    """
    tunnel = 'none'       — lokálny server
    tunnel = 'ngrok'      — ngrok verejný link
    tunnel = 'cloudflare' — Cloudflare Tunnel (bez účtu)
    """
    import threading
    import webbrowser as wb
    from web_server import app, PORT

    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False),
        daemon=True,
    )
    t.start()
    time.sleep(1.2)

    local_url  = f"http://localhost:{PORT}"
    public_url = None
    cf_proc    = None

    if tunnel == "ngrok":
        print(f"\n  {D}Spúšťam ngrok tunel...{X}")
        public_url = _setup_ngrok(PORT)

    elif tunnel == "cloudflare":
        print(f"\n  {D}Spúšťam Cloudflare Tunnel...{X}")
        cf_proc, public_url = _setup_cloudflare(PORT)
        if public_url is None:
            print(f"  {Y}Cloudflare tunel sa nepodaril. Bežím lokálne.{X}")

    clr()
    if public_url:
        icon = "🔥" if tunnel == "cloudflare" else "🌍"
        label = "CLOUDFLARE" if tunnel == "cloudflare" else "NGROK"
        print(f"""
{Y}  ╔══════════════════════════════════════════════╗
  ║   {icon}  KOZMICKÉ BANE v3.0 — {label} TUNEL  ║
  ╚══════════════════════════════════════════════╝{X}

  {G}✓ Lokálny link:{X}
    {W}{local_url}{X}

  {G}★ VEREJNÝ LINK (zdieľaj komukoľvek):{X}
    {W}{public_url}{X}

  {D}Link funguje kým beží tento terminál.
  Stlač Ctrl+C pre zastavenie.{X}
""")
        wb.open(public_url)
    else:
        print(f"""
{Y}  ╔══════════════════════════════════════════════╗
  ║   🌐  KOZMICKÉ BANE v3.0 — WEB SERVER      ║
  ╚══════════════════════════════════════════════╝{X}

  {G}✓ Server beží na:{X}
    {W}{local_url}{X}

  {D}Otvor odkaz vyššie v prehliadači.
  Stlač Ctrl+C pre zastavenie servera.{X}
""")
        wb.open(local_url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        if tunnel == "ngrok" and public_url:
            try:
                from pyngrok import ngrok
                ngrok.kill()
            except Exception:
                pass
        if cf_proc:
            try:
                cf_proc.terminate()
            except Exception:
                pass
        print(f"\n  {Y}Server zastavený.{X}\n")


def main():
    version = choose_version()
    if version == "2":
        start_web(tunnel="none")
        return
    if version == "3":
        start_web(tunnel="ngrok")
        return
    if version == "4":
        start_web(tunnel="cloudflare")
        return
    users = load_users()
    while True:
        clr()
        print(f"""
{Y}  ╔══════════════════════════════════════════════╗
  ║        🎮  GAME LOGIN SYSTEM  v2.0          ║
  ║        Prepojený s KOZMICKÉ BANE v3.0       ║
  ╚══════════════════════════════════════════════╝{X}

  {Y}[1]{X}  Prihlásiť sa
  {Y}[2]{X}  Registrovať sa
  {Y}[3]{X}  Zabudnuté heslo
  {Y}[4]{X}  Koniec
""")
        c = input("  Voľba: ").strip()
        if c == "1":
            clr()
            u = login(users)
            if u:
                users = load_users()
                game_menu(users, u)
        elif c == "2":
            clr(); register(users); users = load_users(); input(f"\n  {D}[ ENTER ]{X}")
        elif c == "3":
            clr(); reset_password(users); users = load_users(); input(f"\n  {D}[ ENTER ]{X}")
        elif c == "4":
            print(f"\n  {Y}Ahoj!{X}\n"); break
        else:
            time.sleep(0.4)

if __name__ == "__main__":
    main()
