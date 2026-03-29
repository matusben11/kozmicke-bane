"""
KOZMICKÉ BANE v4.8 — Web Server
Spustenie: python web_server.py
           alebo cez game_login_system.py → [2] Web
"""

import hashlib
import json
import os
import pathlib
import smtplib
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
    try:
        p = pathlib.Path(path)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}

def save_jf(path, data):
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))

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

def get_sp_ranks(user_dict):
    """Vráti list špeciálnych rankov (max 2). Kompatibilné so starým special_rank stringom."""
    sr = user_dict.get("special_ranks")
    if isinstance(sr, list):
        return [s for s in sr if s][:2]
    old = user_dict.get("special_rank")
    return [old] if old else []

RANKS = [
    (1, "Baník",      0),
    (2, "Prospektér", 100_000),
    (3, "Veterán",    500_000),
    (4, "Veliteľ",    2_000_000),
    (5, "Legenda",    10_000_000),
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
    if uname not in users:
        return
    notifs = users[uname].setdefault("notifications", [])
    notifs.append({"text": text, "from": from_role,
                   "ts": datetime.now().strftime("%Y-%m-%d %H:%M"), "read": False})
    save_users(users)
    email = users[uname].get("email", "").strip()
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
        rank_opts = "".join(
            f'<option value="{t}" {"selected" if t==cur_tier else ""}>{name}</option>'
            for t, name, _ in RANKS
        )
        spr     = get_sp_ranks(u)
        sp_cell = (" ".join(f"<span style='color:#ffd700'>&#9733;{s}</span>" for s in spr)
                   if spr else "<span style='color:#444'>—</span>")
        is_adm  = u.get("is_admin") is True
        adm_cell = ("<span style='color:#00ccff'>&#9679; Admin</span>" if is_adm
                    else "<span style='color:#444'>—</span>")
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
          <td>{len(sv)}</td>
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
            <a href="/owner/delete/{display}" style="color:#ff4444;font-size:.8em"
               onclick="return confirm('Vymazat {display}?')">Del</a>
            &nbsp;
            <form method="POST" action="/owner/set_rank_tier" style="display:inline">
              <input type="hidden" name="uname" value="{display}">
              <select name="tier" style="background:#000b1a;border:1px solid #00ccff;
                color:#00ccff;font-family:inherit;font-size:.8em;padding:2px">
                {rank_opts}
              </select>
              <button type="submit" style="background:#000b1a;border:1px solid #00ccff;
                color:#00ccff;padding:2px 6px;cursor:pointer;font-family:inherit;font-size:.8em">
                Rank
              </button>
            </form>
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
    <th>Rank</th><th>Kariera</th><th>Spec. rank</th><th>Ban</th><th>Admin</th><th>Sloty</th><th>Akcie</th>
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
        rank_opts_a = "".join(
            f'<option value="{t}" {"selected" if t==cur_tier_a else ""}>{name}</option>'
            for t, name, _ in RANKS
        )
        spr0 = spr[0] if len(spr) > 0 else ''
        spr1 = spr[1] if len(spr) > 1 else ''
        rows += f"""<tr>
          <td><strong>{uname_orig}</strong></td>
          <td style="color:#ffdd44">{rank_name}</td>
          <td>{spr_html}</td>
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
            &nbsp;
            <form method="POST" action="/adminpanel/set_rank_tier" style="display:inline">
              <input type="hidden" name="uname" value="{uname_orig}">
              <select name="tier" style="background:#000b1a;border:1px solid #00ccff;
                color:#00ccff;font-family:inherit;font-size:.85em;padding:2px">
                {rank_opts_a}
              </select>
              <button type="submit" style="background:#000b1a;border:1px solid #00ccff;
                color:#00ccff;padding:2px 8px;cursor:pointer;font-family:inherit;font-size:.85em">
                Rank
              </button>
            </form>
            &nbsp;
            <form method="POST" action="/adminpanel/message" style="display:inline">
              <input type="hidden" name="uname" value="{uname_orig}">
              <input type="text" name="msg_text" placeholder="správa..."
                style="width:110px;background:#000;border:1px solid #7788cc;color:#aabbff;
                font-family:inherit;font-size:.85em;padding:2px 4px">
              <button type="submit" style="background:#000;border:1px solid #7788cc;
                color:#aabbff;padding:2px 8px;cursor:pointer;font-family:inherit;font-size:.85em">
                &#9993;
              </button>
            </form>
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
  <tr><th>Hrac</th><th>Rang</th><th>Spec. ranky</th><th>Nastav</th></tr>
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
