"""
KOZMICKÉ BANE v4.5 — Web Server
Spustenie: python web_server.py
           alebo cez game_login_system.py → [2] Web
"""

import hashlib
import json
import os
import pathlib
from datetime import datetime
from flask import Flask, request, redirect, session, make_response

BASE_DIR  = pathlib.Path(__file__).parent.resolve()
DATA_FILE = BASE_DIR / "game_users.json"
KB_CAREER = BASE_DIR / "kb_career.json"
KB_SAVES  = BASE_DIR / "kb_saves.json"
KB_LB     = BASE_DIR / "kb_leaderboard.json"
HTML_FILE = BASE_DIR / "kozmicke_bane.html"

PORT       = int(os.environ.get("PORT", 5000))
ADMIN_CODE = os.environ.get("ADMIN_CODE", "")   # nastav v env premenných na Render

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "kb-web-secret-xyrax9-2024")


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
        "password": hashlib.sha256(password.encode()).hexdigest(),
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

def validate_pw(pw):
    if len(pw) < 6:
        return False, "Heslo musí mať aspoň 6 znakov."
    if not any(c.isdigit() for c in pw):
        return False, "Heslo musí obsahovať aspoň jednu číslicu."
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


# ── Login page HTML ────────────────────────────────────────────────────────

LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KOZMICKÉ BANE v4.5 — Login</title>
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
</style>
</head>
<body>
<pre class="logo">
 ██╗  ██╗ ██████╗ ███████╗███╗   ███╗██╗ ██████╗██╗  ██╗███████╗
 ██║ ██╔╝██╔═══██╗╚══███╔╝████╗ ████║██║██╔════╝██║ ██╔╝██╔════╝
 █████╔╝ ██║   ██║  ███╔╝ ██╔████╔██║██║██║     █████╔╝ █████╗
 ██╔═██╗ ██║   ██║ ███╔╝  ██║╚██╔╝██║██║██║     ██╔═██╗ ██╔══╝
 ██║  ██╗╚██████╔╝███████╗██║ ╚═╝ ██║██║╚██████╗██║  ██╗███████╗
 ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝     ╚═╝╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝</pre>
<div class="subtitle">B A N E &nbsp; v4.5 &mdash; WEB EDITION</div>

<div class="card">
  <div class="tabs">
    <button class="tab __ON_LOGIN__" onclick="show('login',this)">&#128272; Prihlásenie</button>
    <button class="tab __ON_REG__"   onclick="show('register',this)">&#128221; Registrácia</button>
    <button class="tab __ON_RESET__" onclick="show('reset',this)">&#128273; Reset hesla</button>
  </div>

  <div id="login" class="panel __ON_LOGIN__">
    __FLASH_LOGIN__
    <form method="POST" action="/login">
      <label>MENO</label>
      <input type="text" name="username" autocomplete="username" autofocus>
      <label>HESLO</label>
      <input type="password" name="password" autocomplete="current-password">
      <button class="btn" type="submit">&#9654; &nbsp; PRIHLÁSIŤ SA</button>
    </form>
  </div>

  <div id="register" class="panel __ON_REG__">
    __FLASH_REG__
    <form method="POST" action="/register">
      <label>MENO</label>
      <input type="text" name="username">
      <label>HESLO &nbsp;<span style="color:#555;font-size:0.85em">(min. 6 znakov, aspoň 1 číslica)</span></label>
      <input type="password" name="password">
      <label>POTVRĎ HESLO</label>
      <input type="password" name="password2">
      <button class="btn" type="submit">&#10003; &nbsp; VYTVORIŤ ÚČET</button>
    </form>
  </div>

  <div id="reset" class="panel __ON_RESET__">
    __FLASH_RESET__
    <form method="POST" action="/reset">
      <label>MENO</label>
      <input type="text" name="username">
      <label>DÁTUM REGISTRÁCIE &nbsp;<span style="color:#555;font-size:0.85em">(YYYY-MM-DD)</span></label>
      <input type="text" name="reg_date" placeholder="napr. 2024-03-10">
      <label>NOVÉ HESLO</label>
      <input type="password" name="new_password">
      <label>POTVRĎ HESLO</label>
      <input type="password" name="new_password2">
      <button class="btn" type="submit">&#128273; &nbsp; ZMENIŤ HESLO</button>
    </form>
  </div>

  <p class="hint">KOZMICKÉ BANE v4.5 &mdash; Web Edition &mdash; localhost:__PORT__</p>
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

    return (LOGIN_HTML
        .replace("__ON_LOGIN__",   on_login)
        .replace("__ON_REG__",     on_reg)
        .replace("__ON_RESET__",   on_reset)
        .replace("__FLASH_LOGIN__", flash(err_login, ok_login))
        .replace("__FLASH_REG__",   flash(err_reg, ok_reg))
        .replace("__FLASH_RESET__", flash(err_reset, ""))
        .replace("__PORT__",        str(PORT))
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

def render_lobby(pilot):
    all_saves = load_jf(KB_SAVES, {})
    saves  = all_saves.get(pilot.upper(), {})
    career = load_jf(KB_CAREER, {})
    kb     = career.get(pilot.upper(), {})
    cr     = kb.get("career_cr", 0)
    r, rname = kb_rank(cr)

    # ── Hlavička
    html  = f"<!DOCTYPE html><html lang='sk'><head><meta charset='UTF-8'>"
    html += f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
    html += f"<title>KOZMICKÉ BANE — Lobby</title>{LOBBY_CSS}</head><body>"
    html += """<pre class="logo">
 ██╗  ██╗ ██████╗ ███████╗███╗   ███╗██╗ ██████╗██╗  ██╗███████╗
 ██║ ██╔╝██╔═══██╗╚══███╔╝████╗ ████║██║██╔════╝██║ ██╔╝██╔════╝
 █████╔╝ ██║   ██║  ███╔╝ ██╔████╔██║██║██║     █████╔╝ █████╗
 ██╔═██╗ ██║   ██║ ███╔╝  ██║╚██╔╝██║██║██║     ██╔═██╗ ██╔══╝
 ██║  ██╗╚██████╔╝███████╗██║ ╚═╝ ██║██║╚██████╗██║  ██╗███████╗
 ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝     ╚═╝╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝</pre>"""
    html += f'<div class="subtitle">B A N E &nbsp; v4.5 &mdash; CAREER EDITION</div>'
    html += f'<div class="pilot">PILOT: {pilot.upper()} &nbsp;|&nbsp; RANG {r}: {rname} &nbsp;|&nbsp; {cr:,} CR</div>'

    # ── Kariéra stats
    html += '<div class="card">'
    html += '<div class="card-title">&#128202; KARIÉRA</div>'
    html += '<div class="stats-grid">'
    html += f'<div class="stat">Kariérne CR: <span>{cr:,}</span></div>'
    html += f'<div class="stat">Rang: <span>{r} &mdash; {rname}</span></div>'
    html += f'<div class="stat">Sessioni: <span>{kb.get("sessions", 0)}</span></div>'
    html += f'<div class="stat">Najlepší run: <span>{kb.get("best_session", 0):,} CR</span></div>'
    html += f'<div class="stat">Celkom ťažby: <span>{kb.get("total_mined", 0):,} ks</span></div>'
    html += f'<div class="stat">Posledná hra: <span>{kb.get("last_seen", "–")}</span></div>'
    html += '</div></div>'

    # ── Mini hry
    html += '<div class="card">'
    html += '<div class="card-title">&#127918; MINI HRY</div>'
    html += '<a href="/mini/cislo" class="btn">&#128290; &nbsp; H&#193;DANIE &#268;&#205;SLA &nbsp; <span style="color:#a07000;font-size:0.85em">(1&ndash;100, 7 pokusov)</span></a>'
    html += '<a href="/mini/obesenec" class="btn">&#128279; &nbsp; OBESENEC &nbsp; <span style="color:#a07000;font-size:0.85em">(h&#225;daj slovo)</span></a>'
    html += '</div>'

    # ── Nová hra KB
    html += '<div class="card">'
    html += '<div class="card-title">&#128640; KOZMICK&#201; BANE v4.5</div>'
    html += '<a href="/game" class="btn btn-green">&#9654; &nbsp; NOV&#193; HRA &mdash; Za&#269;ni od nuly</a>'
    html += '</div>'

    # ── Save sloty
    html += '<div class="card">'
    html += '<div class="card-title">&#128193; POKRAČOVAŤ &mdash; Vyber uloženie</div>'
    for s in range(1, 5):
        d = saves.get(str(s))
        if not d:
            html += f'<div class="slot-row"><div class="slot-info">#{s} &nbsp; &ndash; prázdny slot &ndash;</div></div>'
        else:
            dep  = DEPTHS.get(d.get("depth", 1), "?")
            crs  = d.get("credits", 0)
            goal = max(1, d.get("goal", 15000))
            pct  = min(100, round(crs / goal * 100))
            date = fmt_date_ts(d.get("ts", 0))
            uname = d.get("username", "?")
            lbl  = f"#{s} &nbsp; {uname} &nbsp; {crs:,} CR ({pct}%) &nbsp; Táh {d.get('turn',0)} &nbsp; [{dep}] &nbsp; {date}"
            html += f'<div class="slot-row">'
            html += f'<a href="/game?slot={s}" class="btn" style="margin:0;flex:1">{lbl}</a>'
            del_js = (
                f"if(confirm('Vymazať slot #{s}?'))"
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
    html += '<div class="card-title">&#127942; KARIÉRA &mdash; TOP HRÁČI</div>'
    medals = ["&#129351;", "&#129352;", "&#129353;"]
    shown = 0
    for i, (uname, d) in enumerate(entries[:5]):
        c = d.get("career_cr", 0)
        if c == 0:
            continue
        _, rn = kb_rank(c)
        m   = medals[i] if i < 3 else f"{i+1}."
        cls = "lb-row me" if uname.upper() == pilot.upper() else "lb-row"
        html += f'<div class="{cls}">{m} &nbsp; <span>{uname}</span> &nbsp; {c:,} CR &nbsp; [{rn}] &nbsp; {d.get("sessions",0)} sess.</div>'
        shown += 1
    if shown == 0:
        html += '<div class="lb-row">&ndash; zatiaľ žiadne záznamy &ndash;</div>'
    html += '</div>'

    # ── Import / Export
    html += '<div class="card">'
    html += '<div class="card-title">&#128228; PRENOS DÁT &mdash; Import / Export</div>'
    html += '<a href="/import_data" class="btn" style="text-align:center">&#8597; Preniesť dáta z PC na server (alebo naopak)</a>'
    html += '</div>'

    # ── Admin prístup priamo v lobby
    html += '<div style="width:100%;max-width:700px;margin-bottom:6px">'
    html += '<details style="border:1px solid #2a1500;padding:8px 14px;background:#0b0900">'
    html += '<summary style="cursor:pointer;color:#555;font-size:0.92em;letter-spacing:0.06em;list-style:none">&#9881; ADMIN PR&#205;STUP</summary>'
    html += '<form method="POST" action="/admin" style="margin-top:10px;display:flex;gap:8px;align-items:center">'
    html += '<input type="password" name="code" placeholder="Admin k&#243;d" autocomplete="off" '
    html += 'style="background:#000;border:1px solid #3a2800;color:#fff8e0;font-family:\'VT323\',monospace;'
    html += 'font-size:1.1em;padding:6px 10px;flex:1;outline:none;">'
    html += '<button type="submit" style="background:#1a0000;border:1px solid #ff4444;color:#ff4444;'
    html += 'padding:6px 16px;cursor:pointer;font-family:\'VT323\',monospace;font-size:1.1em;white-space:nowrap">Vstúpiť</button>'
    html += '</form></details></div>'

    # ── Logout
    html += '<div style="width:100%;max-width:700px">'
    html += '<a href="/logout" class="btn btn-logout">&#10007; &nbsp; Odhlásiť sa</a>'
    html += '</div>'
    # ── Auto-sync script (localStorage → server pri každom otvorení lobby)
    html += """<script>
(function(){
  try{
    var saves=JSON.parse(localStorage.getItem('kb_saves')||'{}');
    var lb=JSON.parse(localStorage.getItem('kb_leaderboard')||'[]');
    var hasData=Object.keys(saves).length>0||lb.length>0;
    if(!hasData)return;
    fetch('/api/sync_local_saves',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({saves:saves,leaderboard:lb})
    }).then(function(r){return r.json();}).then(function(d){
      if(d.synced>0){
        console.log('[sync] Synchronizovaných slotov: '+d.synced);
        window.location.reload();
      }
    }).catch(function(e){console.warn('[sync]',e);});
  }catch(e){console.warn('[sync] Chyba:',e);}
})();
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

MINI_CISLO_HTML = (
    MINI_BASE_CSS
    + """
<div class="card">
  <h2>&#128290; H&#193;DANIE &#268;&#205;SLA</h2>
  <div class="info" id="info">Had&#225;m &#269;&#237;slo od 1 do 100. M&#225;&#353; 7 pokusov.</div>
  <div id="msg"></div>
  <div id="gameArea">
    <input type="number" id="inp" min="1" max="100" placeholder="Zadaj &#269;&#237;slo 1-100" onkeydown="if(event.key==='Enter')guess()">
    <button class="btn" onclick="guess()">&#9654; H&#193;DA&#356;</button>
  </div>
  <div id="result" style="display:none">
    <div class="score-msg" id="scoreMsg"></div>
    <a href="/mini/cislo" class="btn" style="margin-top:14px">&#8635; Hr&#225;&#357; znova</a>
    <a href="/lobby" class="btn btn-back" style="margin-top:6px">&#8592; Lobby</a>
  </div>
</div>
<script>
const secret = Math.floor(Math.random()*100)+1;
let attempts = 0, done = false;
function guess() {
  if (done) return;
  const v = parseInt(document.getElementById('inp').value);
  if (!v || v<1 || v>100) { setMsg('Zadaj &#269;&#237;slo 1-100!','err'); return; }
  attempts++;
  if (v === secret) {
    const sc = Math.max(100-(attempts-1)*12, 10);
    done = true;
    setMsg('&#127881; Spr&#225;vne za '+attempts+' pokusov! +'+sc+' bodov','ok');
    endGame(sc);
  } else if (attempts >= 7) {
    done = true;
    setMsg('&#128128; &#268;&#237;slo bolo '+secret+'. Nahr&#225;&#353; znova?','err');
    endGame(0);
  } else {
    const hint = v < secret ? '&#128200; Viac!' : '&#128201; Menej!';
    setMsg('Pokus '+attempts+'/7 &mdash; '+hint,'hint');
  }
  document.getElementById('inp').value='';
  document.getElementById('inp').focus();
}
function setMsg(t,cls){const m=document.getElementById('msg');m.innerHTML='<div class="msg '+cls+'">'+t+'</div>';}
function endGame(sc){
  document.getElementById('gameArea').style.display='none';
  const r=document.getElementById('result'); r.style.display='block';
  document.getElementById('scoreMsg').innerHTML=(sc>0?'+'+sc+' bodov pridan&#253;ch!':'&#381;iadne body tentokr&#225;t.');
  if(sc>0) fetch('/api/update_score',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({score:sc,game:'cislo'})});
}
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
    ("python", "Programovaci jazyk"),
    ("astronaut", "Cestovatel vesmírom"),
    ("planety", "Kozmicke telesa"),
    ("detektor", "Hladacie zariadenie"),
    ("robotika", "Veda o robotoch"),
    ("gravitacia", "Pritazlivost"),
    ("kristal", "Priehladny mineral"),
    ("vrtacka", "Tazobne zariadenie"),
    ("asteroid", "Kozmicka hornina"),
]

import json as _json


def build_obesenec_html():
    import random
    word, hint = random.choice(HANGMAN_WORDS)
    stages_js = _json.dumps(HANGMAN_STAGES)
    return (
        MINI_BASE_CSS
        + f"""
<div class="card">
  <h2>&#128279; OBESENEC</h2>
  <pre class="hang" id="hang"></pre>
  <div class="word" id="word"></div>
  <div class="info">N&#225;poveda: <span style="color:#a07000">{hint}</span></div>
  <div class="wrong" id="wrong">Chyby (0/6): &mdash;</div>
  <div id="msg"></div>
  <div class="letters" id="letters"></div>
  <div id="result" style="display:none">
    <div class="score-msg" id="scoreMsg"></div>
    <a href="/mini/obesenec" class="btn" style="margin-top:14px">&#8635; Hr&#225;&#357; znova</a>
    <a href="/lobby" class="btn btn-back" style="margin-top:6px">&#8592; Lobby</a>
  </div>
</div>
<script>
const WORD = {_json.dumps(word)};
const STAGES = {stages_js};
let guessed = new Set(), wrong = new Set(), done = false;

function render() {{
  document.getElementById('hang').textContent = STAGES[wrong.size];
  const disp = WORD.split('').map(c => guessed.has(c) ? c : '_').join(' ');
  document.getElementById('word').textContent = disp;
  document.getElementById('wrong').innerHTML =
    'Chyby (' + wrong.size + '/6): ' + (wrong.size ? [...wrong].sort().join(' ') : '&mdash;');
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
      setMsg('&#127881; ' + WORD.toUpperCase() + '! +' + sc + ' bodov', 'ok');
      endGame(sc);
    }}
  }} else {{
    wrong.add(letter);
    render();
    if (wrong.size >= 6) {{
      done = true;
      setMsg('&#128128; Slovo bolo: ' + WORD.toUpperCase(), 'err');
      endGame(0);
    }}
  }}
}}

function setMsg(t,cls){{const m=document.getElementById('msg');m.innerHTML='<div class="msg '+cls+'">'+t+'</div>';}}
function endGame(sc){{
  document.getElementById('letters').style.display='none';
  const r=document.getElementById('result'); r.style.display='block';
  document.getElementById('scoreMsg').innerHTML=(sc>0?'+'+sc+' bodov pridan\u00fdch!':'\u017diadne body tentokr\u00e1t.');
  if(sc>0) fetch('/api/update_score',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{score:sc,game:'obesenec'}})}});
}}

// Build letter buttons
const abc = 'aábcčdďeéfghiíjklľmnňoópqrŕsštťuúvwxyýzž';
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
        return render_login(tab="login", err_login=f"Používateľ '{username}' neexistuje.")
    if users[username]["password"] != hash_pw(password):
        return render_login(tab="login", err_login="Nesprávne heslo.")
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
        return render_login(tab="register", err_reg="Meno nemôže byť prázdne.")
    if username in users:
        return render_login(tab="register", err_reg=f"Meno '{username}' je obsadené.")
    ok, msg = validate_pw(password)
    if not ok:
        return render_login(tab="register", err_reg=msg)
    if password != password2:
        return render_login(tab="register", err_reg="Heslá sa nezhodujú.")
    users[username] = {
        "password":    hash_pw(password),
        "created_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        "score": 0, "games_played": 0, "kb_sessions": 0,
    }
    save_users(users)
    return render_login(tab="login",
                        ok_login=f"Účet '{username}' vytvorený! Prihlás sa.")


@app.route("/reset", methods=["POST"])
def reset():
    username  = request.form.get("username", "").strip()
    reg_date  = request.form.get("reg_date", "").strip()
    new_pw    = request.form.get("new_password", "")
    new_pw2   = request.form.get("new_password2", "")
    users = load_users()
    if username not in users:
        return render_login(tab="reset", err_reset="Používateľ neexistuje.")
    if reg_date not in users[username].get("created_at", ""):
        return render_login(tab="reset", err_reset="Nesprávny dátum registrácie.")
    ok, msg = validate_pw(new_pw)
    if not ok:
        return render_login(tab="reset", err_reset=msg)
    if new_pw != new_pw2:
        return render_login(tab="reset", err_reset="Heslá sa nezhodujú.")
    users[username]["password"] = hash_pw(new_pw)
    save_users(users)
    return render_login(tab="login", ok_login="Heslo bolo zmenené. Prihlás sa.")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


# ── Routes — Mini hry ─────────────────────────────────────────────────────

@app.route("/mini/cislo")
def mini_cislo():
    if "username" not in session:
        return redirect("/")
    return make_response(MINI_CISLO_HTML)


@app.route("/mini/obesenec")
def mini_obesenec():
    if "username" not in session:
        return redirect("/")
    return make_response(build_obesenec_html())


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
    if "username" not in session:
        return redirect("/")
    return render_lobby(session["username"])


@app.route("/delete_save/<int:slot>")
def delete_save_route(slot):
    if "username" not in session:
        return redirect("/")
    saves = load_jf(KB_SAVES, {})
    uname = _uname()
    if uname in saves:
        saves[uname].pop(str(slot), None)
    save_jf(KB_SAVES, saves)
    return redirect("/lobby")


@app.route("/game")
def game():
    if "username" not in session:
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
    return "username" in session

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
    if raw_saves:
        all_saves = load_jf(KB_SAVES, {})
        user_saves = all_saves.get(uname, {})
        for slot, data in raw_saves.items():
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

def _admin_check():
    """Vráti True ak je admin session aktívna."""
    return session.get("admin") is True

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if _admin_check():
        return redirect("/admin/panel")
    err = ""
    if request.method == "POST":
        code = request.form.get("code", "")
        if ADMIN_CODE and code == ADMIN_CODE:
            session["admin"] = True
            return redirect("/admin/panel")
        err = "Nesprávny admin kód."
    return f"""<!DOCTYPE html><html><head><title>Admin</title>{ADMIN_CSS}</head><body>
<h1>🔑 ADMIN PRÍSTUP</h1>
<p style="color:#888;margin-bottom:1rem">Zadaj admin kód nastavený v env premennej <code>ADMIN_CODE</code>.</p>
{"<p class='warn'>"+err+"</p>" if err else ""}
<form method="POST">
  <input type="password" name="code" placeholder="Admin kód" autofocus style="width:220px">
  <button type="submit" style="background:#1a1200;border:1px solid #ffb000;color:#ffb000;
    padding:.25rem .8rem;cursor:pointer;font-family:inherit;border-radius:3px;margin-left:.4rem">
    Vstúpiť
  </button>
</form>
</body></html>"""

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin")

@app.route("/admin/panel")
def admin_panel():
    if not _admin_check():
        return redirect("/admin")
    users   = load_users()
    career  = load_jf(KB_CAREER, {})
    saves   = load_jf(KB_SAVES, {})

    # Zlúč účty z users + career (aby sa zobrazili aj keď je users.json poškodený)
    all_names = set(u.lower() for u in users.keys())
    for ckey in career.keys():
        all_names.add(ckey.lower())

    rows = ""
    for uname_lower in sorted(all_names):
        # nájdi original-case kľúč v users
        u_orig = next((k for k in users if k.lower() == uname_lower), None)
        u  = users.get(u_orig, {}) if u_orig else {}
        c  = career.get(uname_lower.upper(), {})
        sv = saves.get(uname_lower.upper(), {})
        display = u_orig or uname_lower.upper()
        pw_str = (u.get('password','')[:16] + '…') if u.get('password') else '<em style="color:#555">—bez hesla—</em>'
        rows += f"""<tr>
          <td><strong>{display}</strong></td>
          <td style="font-size:.78rem;color:#888;word-break:break-all">{pw_str}</td>
          <td>{u.get('registered','–')}</td>
          <td>{u.get('last_login') or u.get('last_web_login') or c.get('last_seen','–')}</td>
          <td style="color:#ffdd44">{c.get('rank_name','Baník')}</td>
          <td>{c.get('career_cr',0):,} CR</td>
          <td>{c.get('sessions',0)} / {c.get('wins',0)}</td>
          <td>{len(sv)} slotov</td>
          <td>
            <a href="/admin/reset/{display}" class="btn btn-r"
               onclick="return confirm('Reset hesla pre {display}?')">Reset hesla</a>
            <a href="/admin/delete/{display}" class="btn btn-r"
               onclick="return confirm('Vymazať účet {display}? Toto je nevratné!')">Zmazať</a>
          </td>
        </tr>"""

    total_cr = sum(d.get("career_cr", 0) for d in career.values())
    return f"""<!DOCTYPE html><html><head><title>Admin Panel</title>{ADMIN_CSS}</head><body>
<h1>&#9881; ADMIN PANEL &mdash; KOZMICK&#201; BANE v4.5</h1>
<p style="color:#888;font-size:.85rem">
  Účty: <strong style="color:#ffb000">{len(all_names)}</strong> &nbsp;|&nbsp;
  Celkové kariérne CR: <strong style="color:#ffb000">{total_cr:,}</strong> &nbsp;|&nbsp;
  <a href="/admin/logout" class="btn btn-r">Odhlásiť admin</a>
  <a href="/lobby" class="btn">Lobby</a>
</p>
<h2>&#128101; VŠETKY ÚČTY</h2>
<table>
  <tr>
    <th>Používateľ</th><th>Hash hesla (prvých 16 znakov)</th>
    <th>Registrovaný</th><th>Posledné prihlásenie</th>
    <th>Rank</th><th>Kariéra</th><th>Hry / Výhry</th><th>Uloženia</th><th>Akcie</th>
  </tr>
  {rows}
</table>
<h2>🔐 RESET HESLA</h2>
<form method="POST" action="/admin/reset_pw">
  <input type="text"     name="uname"   placeholder="Používateľ" style="width:180px">
  <input type="password" name="new_pw"  placeholder="Nové heslo" style="width:180px">
  <button type="submit" style="background:#1a0000;border:1px solid #ff4444;color:#ff4444;
    padding:.25rem .8rem;cursor:pointer;font-family:inherit;border-radius:3px;margin-left:.3rem">
    Nastaviť heslo
  </button>
</form>
<p style="color:#555;font-size:.8rem;margin-top:.4rem">
  * Heslá sú uložené ako SHA-256 hash — originálne heslá nie sú nikde uložené.
</p>
</body></html>"""

@app.route("/admin/reset_pw", methods=["POST"])
def admin_reset_pw():
    if not _admin_check():
        return redirect("/admin")
    uname  = request.form.get("uname", "").strip()
    new_pw = request.form.get("new_pw", "")
    users  = load_users()
    if uname not in users:
        return redirect("/admin/panel?err=notfound")
    ok, msg = validate_pw(new_pw)
    if not ok:
        return redirect(f"/admin/panel?err={msg}")
    users[uname]["password"] = hash_pw(new_pw)
    save_users(users)
    return redirect("/admin/panel")

@app.route("/admin/reset/<uname>")
def admin_reset_get(uname):
    if not _admin_check():
        return redirect("/admin")
    return f"""<!DOCTYPE html><html><head><title>Reset hesla</title>{ADMIN_CSS}</head><body>
<h1>🔐 Reset hesla — {uname}</h1>
<form method="POST" action="/admin/reset_pw">
  <input type="hidden" name="uname" value="{uname}">
  <input type="password" name="new_pw" placeholder="Nové heslo (min 6 znakov, 1 číslica)"
    style="width:260px" autofocus>
  <button type="submit" style="background:#1a0000;border:1px solid #ff4444;color:#ff4444;
    padding:.25rem .8rem;cursor:pointer;font-family:inherit;border-radius:3px;margin-left:.3rem">
    Uložiť nové heslo
  </button>
</form>
<p style="margin-top:.8rem"><a href="/admin/panel" class="btn">◀ Späť</a></p>
</body></html>"""

@app.route("/admin/delete/<uname>")
def admin_delete_user(uname):
    if not _admin_check():
        return redirect("/admin")
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
    return redirect("/admin/panel")


# ── Štart ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n  KOZMICKÉ BANE v4.5 — Web Server")
    print(f"  Otvor: http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
