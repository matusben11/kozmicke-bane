"""
KOZMICKÉ BANE v3.0 — Desktop Application
Career Edition

Spustenie:
  python app.py              (lobby — výber pilota)
  python app.py MENO_PILOTA  (priamo zo login systému)
"""

import webview
import os
import sys
import json
import pathlib
import tkinter as tk
from tkinter import messagebox
from datetime import datetime

# ─── CESTY ─────────────────────────────────────────────────────
BASE    = pathlib.Path(__file__).parent.resolve()
GAME    = BASE / "kozmicke_bane.html"
SAVES   = BASE / "kb_saves.json"
LB_FILE = BASE / "kb_leaderboard.json"
CAREER  = BASE / "kb_career.json"

AMBER = "#ffb000"
ADIM  = "#a07000"
ADRK  = "#3a2800"
BG    = "#0b0900"
PANEL = "#0f0c00"
RED   = "#ff3a3a"
GREEN = "#39ff6a"

# ─── POMOCNÉ ───────────────────────────────────────────────────
def load_json(path, default=None):
    try:
        p = pathlib.Path(path)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[KB] Save error: {e}")

def fmt_date(ts):
    try:
        d = datetime.fromtimestamp(ts / 1000)
        return d.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return "–"

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

DEPTHS = {1:"Povrch", 2:"Litosféra", 3:"Hlboká", 4:"Magma", 5:"Jadro"}

# ─── CAREER SYNC ───────────────────────────────────────────────
def sync_career(username, session_cr, mined, session_win):
    career = load_json(CAREER, {})
    key = username.upper()
    e = career.get(key, {"career_cr":0,"sessions":0,"best_session":0,
                          "total_mined":0,"wins":0,"last_seen":"–"})
    e["career_cr"]    += int(session_cr)
    e["sessions"]     += 1
    e["total_mined"]  += int(mined)
    e["best_session"]  = max(e["best_session"], int(session_cr))
    e["last_seen"]     = datetime.now().strftime("%Y-%m-%d %H:%M")
    if session_win:
        e["wins"] = e.get("wins", 0) + 1
    r, rname = kb_rank(e["career_cr"])
    e["rank"] = r; e["rank_name"] = rname
    career[key] = e
    save_json(CAREER, career)
    return e

# ─── JS API ────────────────────────────────────────────────────
_pilot = "PILOT"

class GameAPI:
    """Volateľné z HTML cez window.pyapi.*"""

    def save_game(self, slot, data_json):
        saves = load_json(SAVES, {})
        saves[str(slot)] = json.loads(data_json)
        save_json(SAVES, saves)
        return True

    def load_game(self, slot):
        saves = load_json(SAVES, {})
        d = saves.get(str(slot))
        return json.dumps(d) if d else "null"

    def delete_save(self, slot):
        saves = load_json(SAVES, {})
        saves.pop(str(slot), None)
        save_json(SAVES, saves)
        return True

    def get_startup_data(self):
        """Hra zavolá toto raz pri štarte — vráti saves + leaderboard z disku."""
        return json.dumps({
            "saves":       load_json(SAVES, {}),
            "leaderboard": load_json(LB_FILE, []),
            "pilot":       _pilot,
        })

    def add_leaderboard(self, entry_json):
        lb = load_json(LB_FILE, [])
        lb.append(json.loads(entry_json))
        lb.sort(key=lambda x: -x.get("score", 0))
        save_json(LB_FILE, lb[:20])
        return True

    def get_leaderboard(self):
        return json.dumps(load_json(LB_FILE, []))

    def clear_leaderboard(self):
        save_json(LB_FILE, [])
        return True

    def report_session_end(self, credits_earned, mined, win):
        entry = sync_career(_pilot, credits_earned, mined, win)
        return json.dumps(entry)

    def get_career(self):
        career = load_json(CAREER, {})
        return json.dumps(career.get(_pilot.upper(), {}))

    def minimize_window(self):
        if _win: _win.minimize()

    def toggle_fullscreen(self):
        if _win: _win.toggle_fullscreen()


# ─── BRIDGE JS — iba disk sync, žiadne scene volania ───────────
# Injektuje sa po načítaní stránky.
# Prepojí localStorage.setItem → pyapi.save_game, atď.
BRIDGE_JS = """
(function() {
  if (window.__KB_BRIDGE_LOADED__) return;
  window.__KB_BRIDGE_LOADED__ = true;

  // Helper — pywebview API moze byt na roznych cestach
  function getApi() {
    if (window.pywebview && window.pywebview.api) return window.pywebview.api;
    if (window.pyapi) return window.pyapi;
    return null;
  }

  // Nacitaj data zo suborov pri starte — opakuje sa kym API nie je dostupne
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
          console.log('[KB Bridge] Data loaded from disk.');
        } catch(e) { console.warn('[KB Bridge] init error:', e); }
      });
    } else if (_t++ < 50) { setTimeout(tryInit, 100); }
  }
  setTimeout(tryInit, 200);

  // localStorage.setItem -> disk sync
  var _origSet = Storage.prototype.setItem;
  Storage.prototype.setItem = function(key, value) {
    _origSet.call(this, key, value);
    var api = getApi(); if (!api) return;
    if (key === 'kb_saves') {
      try {
        var saves = JSON.parse(value);
        Object.keys(saves).forEach(function(slot) {
          api.save_game(slot, JSON.stringify(saves[slot]));
        });
      } catch(e) {}
    }
    if (key === 'kb_leaderboard') {
      try {
        JSON.parse(value).forEach(function(e) { api.add_leaderboard(JSON.stringify(e)); });
      } catch(e) {}
    }
  };

  // localStorage.removeItem -> disk sync
  var _origRemove = Storage.prototype.removeItem;
  Storage.prototype.removeItem = function(key) {
    _origRemove.call(this, key);
    var api = getApi(); if (!api) return;
    if (key === 'kb_saves') { for (var i=1;i<=4;i++) api.delete_save(i); }
    if (key === 'kb_leaderboard') { api.clear_leaderboard(); }
  };

  console.log('[KB Bridge v3] Active.');
})();
"""

# ─── WEBVIEW ───────────────────────────────────────────────────
_win = None

def launch_game(pilot, slot=None):
    """
    pilot : meno pilota
    slot  : None = nová hra,  1-4 = načítaj uloženie
    """
    global _win, _pilot
    _pilot = pilot

    if not GAME.exists():
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("Chyba", f"Nenájdený súbor:\n{GAME}")
        return

    api  = GameAPI()
    # URL s parametrami — http_server=True spustí lokálny server
    url  = f"kozmicke_bane.html?pilot={pilot}"
    if slot:
        url += f"&slot={slot}"

    _win = webview.create_window(
        title             = f"KOZMICKÉ BANE v3.0  —  {pilot.upper()}",
        url               = url,
        js_api            = api,
        width             = 1150,
        height            = 800,
        min_size          = (900, 650),
        resizable         = True,
        background_color  = "#0b0900",
    )

    def on_loaded():
        # Iba injektuj bridge — žiadne scene volania, žiadne timeouty
        _win.evaluate_js(BRIDGE_JS)

    _win.events.loaded += on_loaded

    # http_server=True: pywebview spustí lokálny HTTP server,
    # URL parametre (?pilot=...) fungujú korektne
    webview.start(
        http_server = True,
        debug       = False,
    )


# ─── LOBBY OKNO ────────────────────────────────────────────────
class LobbyWindow(tk.Tk):
    def __init__(self, pilot):
        super().__init__()
        self.result       = None   # None | "new" | "slot_N"
        self.pilot        = pilot
        self.title("KOZMICKÉ BANE v3.0  —  LOBBY")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._build()

    def _build(self):
        saves  = load_json(SAVES, {})
        career = load_json(CAREER, {})
        kb     = career.get(self.pilot.upper(), {})
        cr     = kb.get("career_cr", 0)
        r, rname = kb_rank(cr)

        # ─ Header ─────────────────────────────────────────
        hf = tk.Frame(self, bg=BG, highlightbackground=AMBER, highlightthickness=1)
        hf.pack(fill="x", padx=12, pady=(12, 5))

        ascii_art = (
            "██╗  ██╗ ██████╗ ███████╗███╗   ███╗██╗ ██████╗██╗  ██╗███████╗\n"
            "██║ ██╔╝██╔═══██╗╚══███╔╝████╗ ████║██║██╔════╝██║ ██╔╝██╔════╝\n"
            "█████╔╝ ██║   ██║  ███╔╝ ██╔████╔██║██║██║     █████╔╝ █████╗  \n"
            "██╔═██╗ ██║   ██║ ███╔╝  ██║╚██╔╝██║██║██║     ██╔═██╗ ██╔══╝  \n"
            "██║  ██╗╚██████╔╝███████╗██║ ╚═╝ ██║██║╚██████╗██║  ██╗███████╗\n"
            "╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝     ╚═╝╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝"
        )
        tk.Label(hf, text=ascii_art, bg=BG, fg=AMBER,
                 font=("Courier New", 7, "bold"), justify="center").pack(pady=(7,2))
        tk.Label(hf, text="B A N E  v3.0  —  CAREER EDITION",
                 bg=BG, fg=AMBER, font=("Courier New", 11, "bold")).pack()
        tk.Label(hf,
                 text=f"PILOT: {self.pilot.upper()}   │   RANG {r}: {rname}   │   {cr:,} CR",
                 bg=BG, fg=ADIM, font=("Courier New", 9)).pack(pady=(0,7))

        _sep(self)

        # ─ Nová hra ───────────────────────────────────────
        _ABtn(self,
              "▶   NOVÁ HRA  —  Začni od nuly",
              self._new, AMBER, big=True).pack(fill="x", padx=12, pady=(5,3))

        _sep(self)

        # ─ Save sloty ─────────────────────────────────────
        tk.Label(self, text="📂  POKRAČOVAŤ  —  Vyber uloženie",
                 bg=BG, fg=ADIM, font=("Courier New", 10, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(3,2))

        has_any = False
        for s in range(1, 5):
            d = saves.get(str(s))
            if not d:
                tk.Label(self,
                         text=f"  #{s}  – prázdny slot –",
                         bg=BG, fg="#333300",
                         font=("Courier New", 9), anchor="w").pack(fill="x", padx=12)
                continue
            has_any = True
            dep  = DEPTHS.get(d.get("depth", 1), "?")
            cr_s = d.get("credits", 0)
            pct  = min(100, round(cr_s / max(1, d.get("goal", 15000)) * 100))
            lbl  = (f"  #{s}  {d.get('username','?'):<12}"
                    f"  {cr_s:>6,} CR ({pct}%)"
                    f"  Táh {d.get('turn',0):<4}"
                    f"  [{dep}]  {fmt_date(d.get('ts',0))}")
            row = tk.Frame(self, bg=BG)
            row.pack(fill="x", padx=12, pady=2)
            _ABtn(row, lbl, lambda x=s: self._load(x), GREEN).pack(
                side="left", fill="x", expand=True, padx=(0,3))
            _ABtn(row, "🗑", lambda x=s: self._del(x), RED).pack(side="left")

        if not has_any:
            tk.Label(self, text="  – žiadne uloženia –",
                     bg=BG, fg=ADIM, font=("Courier New", 9)).pack(anchor="w", padx=12)

        _sep(self)

        # ─ Kariéra top 3 ──────────────────────────────────
        tk.Label(self, text="🏆  KARIÉRA",
                 bg=BG, fg=ADIM, font=("Courier New", 10, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(3,1))

        career_data = load_json(CAREER, {})
        entries = sorted(career_data.items(),
                         key=lambda x: -x[1].get("career_cr", 0))
        medals = ["🥇", "🥈", "🥉"]
        shown = 0
        for i, (uname, d) in enumerate(entries[:5]):
            c = d.get("career_cr", 0)
            if c == 0: continue
            _, rn = kb_rank(c)
            m   = medals[i] if i < 3 else f" {i+1}."
            col = AMBER if uname.upper() == self.pilot.upper() else ADIM
            tk.Label(self,
                     text=f"  {m} {uname:<12} {c:>10,} CR  [{rn}]  {d.get('sessions',0)} sess.",
                     bg=BG, fg=col, font=("Courier New", 9), anchor="w").pack(fill="x", padx=12)
            shown += 1
        if shown == 0:
            tk.Label(self, text="  – zatiaľ žiadne KB záznamy –",
                     bg=BG, fg=ADIM, font=("Courier New", 9)).pack(anchor="w", padx=12)

        _sep(self)
        _ABtn(self, "✗  Zrušiť", self._cancel, ADIM).pack(
            fill="x", padx=12, pady=(3,12))

        self.update_idletasks()
        self.geometry(f"660x{self.winfo_reqheight()}")

    def _new(self):   self.result = "new";      self.destroy()
    def _load(self, s): self.result = f"slot_{s}"; self.destroy()
    def _cancel(self): self.result = None;       self.destroy()

    def _del(self, slot):
        saves = load_json(SAVES, {})
        saves.pop(str(slot), None)
        save_json(SAVES, saves)
        messagebox.showinfo("Vymazané", f"Slot #{slot} bol vymazaný.")
        # Rebuild
        for w in self.winfo_children(): w.destroy()
        self._build()


def _sep(parent):
    tk.Frame(parent, bg=ADIM, height=1).pack(fill="x", padx=12, pady=3)

class _ABtn(tk.Button):
    def __init__(self, parent, text, cmd, color=AMBER, big=False):
        font = ("Courier New", 11, "bold") if big else ("Courier New", 9)
        super().__init__(parent, text=text, command=cmd,
            bg=PANEL, fg=color, activebackground=ADRK,
            activeforeground="#fff8e0", font=font,
            bd=1, relief="flat",
            highlightbackground=color, highlightthickness=1,
            cursor="hand2", pady=7 if big else 4)
        self.bind("<Enter>", lambda e: self.configure(bg=ADRK, fg="#fff8e0"))
        self.bind("<Leave>", lambda e: self.configure(bg=PANEL, fg=color))


# ─── MAIN ──────────────────────────────────────────────────────
def main():
    pilot = (sys.argv[1].strip() if len(sys.argv) > 1 else "PILOT") or "PILOT"
    pilot = pilot[:18]

    if not GAME.exists():
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("Chyba",
            f"Nenájdený súbor kozmicke_bane.html!\n\nPriečinok:\n{BASE}")
        sys.exit(1)

    lobby = LobbyWindow(pilot)
    lobby.mainloop()

    if lobby.result is None:
        return

    if lobby.result == "new":
        launch_game(pilot, slot=None)

    elif lobby.result.startswith("slot_"):
        s = int(lobby.result.split("_")[1])
        saves = load_json(SAVES, {})
        saved_pilot = saves.get(str(s), {}).get("username", pilot)
        launch_game(saved_pilot, slot=s)


if __name__ == "__main__":
    main()
