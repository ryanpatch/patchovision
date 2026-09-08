#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
jvuey-remote - a small web remote for the jvuey Jellyfin TUI.

Serves a single page at http://<box-ip>:8080/remote

This process never touches playback. It is a translator: HTTP from your phone
in one side, Unix-socket commands out the other. Which socket is accepting
connections tells it what is on the TV right now:

    jvuey-tui.sock  accepting  ->  the selector menu is up   (send keys)
    jvuey-mpv.sock  accepting  ->  mpv is playing            (send IPC commands)
    neither                    ->  nothing running

Because the menu lives on the TV and the phone only sends keystrokes, no
Jellyfin credentials or stream URLs are ever sent to the phone.
"""

import os
import sys
import json
import time
import socket
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ============================================================================
# Socket locations - must match jvuey23.py
# ============================================================================

def runtime_dir() -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and Path(xdg).is_dir():
        return Path(xdg)
    fallback = Path(f"/tmp/jvuey-{os.getuid()}")
    fallback.mkdir(mode=0o700, exist_ok=True)
    return fallback


TUI_SOCKET = runtime_dir() / "jvuey-tui.sock"
MPV_SOCKET = runtime_dir() / "jvuey-mpv.sock"

CONNECT_TIMEOUT = 0.4
REPLY_TIMEOUT = 1.5


# ============================================================================
# Talking to mpv
# ============================================================================

class MpvClient:
    """mpv JSON IPC. One connection per call - simple and self-healing."""

    def __init__(self, path: Path):
        self.path = str(path)

    def _connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect(self.path)
        sock.settimeout(REPLY_TIMEOUT)
        return sock

    def alive(self) -> bool:
        # Connect rather than stat the file: mpv can leave a stale socket
        # behind if it is killed, and a stale socket refuses connections.
        try:
            self._connect().close()
            return True
        except OSError:
            return False

    def batch(self, commands: list[list]) -> list | None:
        """Send several commands on one connection, return their results."""
        try:
            sock = self._connect()
        except OSError:
            return None

        results: list = [None] * len(commands)
        try:
            for i, command in enumerate(commands):
                payload = {"command": command, "request_id": i + 1}
                sock.sendall(json.dumps(payload).encode() + b"\n")

            pending = set(range(1, len(commands) + 1))
            buffer = b""
            deadline = time.monotonic() + REPLY_TIMEOUT
            while pending and time.monotonic() < deadline:
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, OSError):
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    # mpv interleaves async events with replies; ignore events.
                    request_id = message.get("request_id")
                    if request_id in pending:
                        pending.discard(request_id)
                        if message.get("error") == "success":
                            results[request_id - 1] = message.get("data")
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return results

    def command(self, command: list):
        result = self.batch([command])
        return result[0] if result else None

    def status(self) -> dict | None:
        props = [
            "media-title", "time-pos", "duration", "pause",
            "volume", "mute", "chapter", "chapters",
        ]
        results = self.batch([["get_property", p] for p in props])
        if results is None:
            return None
        data = dict(zip(props, results))
        return {
            "ok": True,
            "mode": "playing",
            "title": data.get("media-title") or "Playing",
            "position": data.get("time-pos") or 0,
            "duration": data.get("duration") or 0,
            "paused": bool(data.get("pause")),
            "volume": data.get("volume") if data.get("volume") is not None else 100,
            "muted": bool(data.get("mute")),
            "chapter": data.get("chapter"),
            "chapters": data.get("chapters") or 0,
        }


# ============================================================================
# Talking to the TUI
# ============================================================================

class TuiClient:
    """Newline-delimited JSON over a Unix socket."""

    def __init__(self, path: Path):
        self.path = str(path)

    def request(self, payload: dict) -> dict | None:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(self.path)
            sock.settimeout(REPLY_TIMEOUT)
        except OSError:
            return None
        try:
            sock.sendall(json.dumps(payload).encode() + b"\n")
            buffer = b""
            deadline = time.monotonic() + REPLY_TIMEOUT
            while b"\n" not in buffer and time.monotonic() < deadline:
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, OSError):
                    return None
                if not chunk:
                    return None
                buffer += chunk
            line = buffer.split(b"\n", 1)[0]
            return json.loads(line) if line.strip() else None
        except (OSError, ValueError):
            return None
        finally:
            try:
                sock.close()
            except OSError:
                pass


mpv = MpvClient(MPV_SOCKET)
tui = TuiClient(TUI_SOCKET)


def current_status() -> dict:
    """mpv wins if it is up, because that is what is on screen."""
    if mpv.alive():
        status = mpv.status()
        if status:
            return status
    status = tui.request({"action": "status"})
    if status:
        return status
    return {"ok": True, "mode": "idle"}


# ============================================================================
# The page
# ============================================================================

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,maximum-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0d0f13">
<title>jvuey remote</title>
<style>
  :root{
    --bg:#0d0f13; --panel:#171a21; --panel2:#1f232c; --line:#2a2f3a;
    --text:#e8eaee; --dim:#8b93a3; --accent:#4a9eff; --accent-dim:#1d3a5c;
    --ok:#3ddc84;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{height:100%}
  body{
    margin:0;background:var(--bg);color:var(--text);
    font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
    display:flex;flex-direction:column;
    padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
    overscroll-behavior:none;
  }
  header{
    display:flex;align-items:center;gap:10px;padding:12px 16px;
    background:var(--panel);border-bottom:1px solid var(--line);flex:none;
  }
  #dot{width:9px;height:9px;border-radius:50%;background:var(--dim);flex:none;transition:background .3s}
  #dot.live{background:var(--ok)}
  #dot.dead{background:#e0533d}
  #hdr{font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
  #hdrsub{font-size:12px;color:var(--dim);flex:none}
  main{flex:1;min-height:0;display:flex;flex-direction:column}
  .hidden{display:none!important}

  /* ---------- menu ---------- */
  #searchbar{padding:10px 12px;background:var(--panel);border-bottom:1px solid var(--line);flex:none}
  #search{
    width:100%;padding:11px 13px;border-radius:10px;border:1px solid var(--line);
    background:var(--panel2);color:var(--text);font-size:16px;outline:none;
  }
  #search:focus{border-color:var(--accent)}
  #list{flex:1;min-height:0;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:6px 0}
  .row{
    padding:11px 16px;border-left:3px solid transparent;color:var(--dim);
    font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  }
  .row.sel{background:var(--accent-dim);border-left-color:var(--accent);color:var(--text);font-weight:600}
  #count{padding:8px 16px;font-size:12px;color:var(--dim);flex:none;border-top:1px solid var(--line)}

  /* ---------- d-pad ---------- */
  #pad{
    flex:none;padding:12px 16px 10px;background:var(--panel);border-top:1px solid var(--line);
    display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;
  }
  .btn{
    display:flex;align-items:center;justify-content:center;
    background:var(--panel2);border:1px solid var(--line);border-radius:12px;
    color:var(--text);font-size:17px;font-weight:600;padding:13px 8px;
    touch-action:manipulation;user-select:none;cursor:pointer;
    transition:background .08s,transform .08s;
  }
  .btn:active{background:var(--accent);transform:scale(.95)}
  .btn.ok{background:var(--accent-dim);border-color:var(--accent)}
  .btn.sm{font-size:12px;font-weight:500;padding:12px 4px;color:var(--dim)}
  #extras{
    flex:none;display:grid;grid-template-columns:repeat(5,1fr);gap:8px;
    padding:0 16px 16px;background:var(--panel);
  }

  /* ---------- playback ---------- */
  #play{flex:1;display:flex;flex-direction:column;justify-content:center;gap:22px;padding:20px 18px}
  #ptitle{font-size:19px;font-weight:600;text-align:center;line-height:1.3;
          display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
  #chap{text-align:center;font-size:13px;color:var(--dim);margin-top:-14px}
  #seek{width:100%;-webkit-appearance:none;appearance:none;background:transparent;height:30px}
  #seek::-webkit-slider-runnable-track{height:6px;border-radius:3px;background:var(--panel2)}
  #seek::-webkit-slider-thumb{-webkit-appearance:none;width:20px;height:20px;border-radius:50%;
    background:var(--accent);margin-top:-7px}
  #seek::-moz-range-track{height:6px;border-radius:3px;background:var(--panel2)}
  #seek::-moz-range-thumb{width:20px;height:20px;border:0;border-radius:50%;background:var(--accent)}
  #times{display:flex;justify-content:space-between;font-size:13px;color:var(--dim);
         margin-top:-16px;font-variant-numeric:tabular-nums}
  .rowgrid{display:grid;gap:10px}
  .g2{grid-template-columns:1fr 1fr}
  .g4{grid-template-columns:repeat(4,1fr)}
  #bigplay{padding:22px;font-size:26px;background:var(--accent-dim);border-color:var(--accent)}
  #vol{width:100%;-webkit-appearance:none;appearance:none;background:transparent;height:28px}
  #vol::-webkit-slider-runnable-track{height:5px;border-radius:3px;background:var(--panel2)}
  #vol::-webkit-slider-thumb{-webkit-appearance:none;width:17px;height:17px;border-radius:50%;
    background:var(--text);margin-top:-6px}
  #vol::-moz-range-track{height:5px;border-radius:3px;background:var(--panel2)}
  #vol::-moz-range-thumb{width:17px;height:17px;border:0;border-radius:50%;background:var(--text)}
  .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);margin-bottom:-14px}

  /* ---------- idle ---------- */
  #idle{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
        gap:10px;color:var(--dim);text-align:center;padding:30px}
</style>
</head>
<body>

<header>
  <div id="dot"></div>
  <div id="hdr">Connecting...</div>
  <div id="hdrsub"></div>
</header>

<main>
  <!-- MENU -->
  <div id="menu" class="hidden" style="flex:1;min-height:0;display:flex;flex-direction:column">
    <div id="searchbar">
      <input id="search" type="search" placeholder="Search titles..."
             autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false">
    </div>
    <div id="list"></div>
    <div id="count"></div>
    <div id="pad">
      <div></div>
      <div class="btn" data-key="up">&#9650;</div>
      <div></div>
      <div class="btn" data-key="left">&#9664;</div>
      <div class="btn ok" data-key="select">OK</div>
      <div class="btn" data-key="right">&#9654;</div>
      <div></div>
      <div class="btn" data-key="down">&#9660;</div>
      <div></div>
    </div>
    <div id="extras">
      <div class="btn sm" data-key="play">Play</div>
      <div class="btn sm" data-key="info">Info</div>
      <div class="btn sm" data-key="watched">Seen</div>
      <div class="btn sm" data-key="library">Lib</div>
      <div class="btn sm" data-key="settings">Setup</div>
    </div>
  </div>

  <!-- PLAYBACK -->
  <div id="play" class="hidden">
    <div id="ptitle"></div>
    <div id="chap"></div>
    <div>
      <input id="seek" type="range" min="0" max="1000" value="0">
      <div id="times"><span id="tnow">0:00</span><span id="tleft"></span></div>
    </div>
    <div class="btn" id="bigplay" data-mpv='["cycle","pause"]'>&#10074;&#10074;</div>
    <div class="rowgrid g4">
      <div class="btn" data-mpv='["seek",-60,"relative"]'>&minus;60</div>
      <div class="btn" data-mpv='["seek",-10,"relative"]'>&minus;10</div>
      <div class="btn" data-mpv='["seek",10,"relative"]'>+10</div>
      <div class="btn" data-mpv='["seek",60,"relative"]'>+60</div>
    </div>
    <div class="rowgrid g2">
      <div class="btn sm" data-mpv='["add","chapter",-1]'>&#9198; Chapter</div>
      <div class="btn sm" data-mpv='["add","chapter",1]'>Chapter &#9197;</div>
    </div>
    <div>
      <div class="lbl">Volume</div>
      <input id="vol" type="range" min="0" max="100" value="100">
    </div>
    <div class="rowgrid g2">
      <div class="btn sm" data-mpv='["script-binding","stats/display-stats-toggle"]'>&#9432; Info</div>
      <div class="btn sm" data-mpv='["quit"]'>&#9632; Stop</div>
    </div>
  </div>

  <!-- IDLE -->
  <div id="idle" class="hidden">
    <div style="font-size:30px">&#128250;</div>
    <div>Nothing running on the box.</div>
    <div style="font-size:13px">Waiting for the selector to start...</div>
  </div>
</main>

<script>
const $ = id => document.getElementById(id);
let seeking = false, volTouching = false, lastSig = "", mode = null;

function fmt(seconds){
  seconds = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(seconds/3600), m = Math.floor(seconds%3600/60), s = seconds%60;
  return h ? h + ":" + String(m).padStart(2,"0") + ":" + String(s).padStart(2,"0")
           : m + ":" + String(s).padStart(2,"0");
}

async function send(path, body){
  try{
    const response = await fetch(path, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)
    });
    render(await response.json());
  }catch(e){ $("dot").className = "dead"; }
}

function show(which){
  for (const id of ["menu","play","idle"]) $(id).classList.toggle("hidden", id !== which);
}

function render(state){
  if (!state){ $("dot").className = "dead"; return; }
  $("dot").className = "live";

  if (state.mode === "playing"){
    if (mode !== "playing"){ show("play"); mode = "playing"; }
    $("hdr").textContent = "Playing";
    $("hdrsub").textContent = "";
    $("ptitle").textContent = state.title || "";
    $("bigplay").innerHTML = state.paused ? "&#9654;" : "&#10074;&#10074;";

    const duration = state.duration || 0, position = state.position || 0;
    if (!seeking){
      $("seek").value = duration > 0 ? Math.round(position / duration * 1000) : 0;
    }
    $("tnow").textContent  = fmt(position);
    $("tleft").textContent = duration > 0 ? "-" + fmt(duration - position) : "";
    $("chap").textContent =
      (state.chapters > 0 && state.chapter !== null && state.chapter !== undefined)
        ? "Chapter " + (state.chapter + 1) + " of " + state.chapters : "";
    if (!volTouching) $("vol").value = Math.round(state.volume == null ? 100 : state.volume);
    return;
  }

  if (state.mode === "menu"){
    if (mode !== "menu"){ show("menu"); mode = "menu"; }
    $("hdr").textContent = state.subtitle || state.title || "Menu";
    $("hdrsub").textContent = state.view || "";

    // Only rebuild the list when it actually changed, so scrolling survives.
    const signature = state.start + "|" + state.total + "|" + state.index + "|" +
                      (state.items[0] ? state.items[0].label : "");
    if (signature !== lastSig){
      lastSig = signature;
      const list = $("list");
      list.innerHTML = "";
      for (const item of state.items){
        const row = document.createElement("div");
        row.className = "row" + (item.i === state.index ? " sel" : "");
        row.textContent = item.label;
        row.onclick = () => send("/api/goto", {index: item.i});
        list.appendChild(row);
      }
      // Centre the highlight ourselves; scrollIntoView can leave it clipped
      // under the sticky search bar.
      const selected = list.querySelector(".sel");
      if (selected){
        list.scrollTop = selected.offsetTop - (list.clientHeight / 2) + (selected.offsetHeight / 2);
      }
    }
    $("count").textContent =
      state.total ? ((state.index == null ? 0 : state.index) + 1) + " of " + state.total : "Empty";
    if (document.activeElement !== $("search") && $("search").value !== (state.search || "")){
      $("search").value = state.search || "";
    }
    return;
  }

  if (mode !== "idle"){ show("idle"); mode = "idle"; lastSig = ""; }
  $("hdr").textContent = "Idle";
  $("hdrsub").textContent = "";
}

// --- buttons -------------------------------------------------------------
document.addEventListener("click", event => {
  const button = event.target.closest("[data-key],[data-mpv]");
  if (!button) return;
  if (navigator.vibrate) navigator.vibrate(8);
  if (button.dataset.key) send("/api/key", {key: button.dataset.key});
  else send("/api/mpv", {command: JSON.parse(button.dataset.mpv)});
});

// Hold-to-repeat on the up/down arrows.
let repeatTimer = null, repeatDelay = null;
function stopRepeat(){ clearInterval(repeatTimer); clearTimeout(repeatDelay); repeatTimer = repeatDelay = null; }
for (const el of document.querySelectorAll('[data-key="up"],[data-key="down"]')){
  el.addEventListener("touchstart", () => {
    repeatDelay = setTimeout(() => {
      repeatTimer = setInterval(() => send("/api/key", {key: el.dataset.key}), 120);
    }, 420);
  }, {passive:true});
  for (const done of ["touchend","touchcancel","touchmove"]) el.addEventListener(done, stopRepeat, {passive:true});
}

// --- sliders -------------------------------------------------------------
const seek = $("seek");
seek.addEventListener("input",  () => { seeking = true; });
seek.addEventListener("change", () => {
  send("/api/mpv", {command:["seek", seek.value / 10, "absolute-percent"]});
  setTimeout(() => { seeking = false; }, 400);
});
const vol = $("vol");
vol.addEventListener("input",  () => { volTouching = true; });
vol.addEventListener("change", () => {
  send("/api/mpv", {command:["set_property","volume", Number(vol.value)]});
  setTimeout(() => { volTouching = false; }, 400);
});

// --- search --------------------------------------------------------------
let searchTimer = null;
$("search").addEventListener("input", event => {
  clearTimeout(searchTimer);
  const text = event.target.value;
  searchTimer = setTimeout(() => send("/api/search", {text}), 260);
});
$("search").addEventListener("keydown", event => {
  if (event.key === "Enter"){ event.target.blur(); }
});

// --- polling -------------------------------------------------------------
async function poll(){
  try{
    const response = await fetch("/api/status", {cache:"no-store"});
    render(await response.json());
  }catch(e){ $("dot").className = "dead"; }
}
let timer = null;
function startPolling(){
  clearInterval(timer);
  timer = setInterval(poll, mode === "playing" ? 900 : 1400);
  poll();
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearInterval(timer); else startPolling();
});
startPolling();
setInterval(startPolling, 15000);   // re-tune the interval as the mode changes
</script>
</body>
</html>
"""


# ============================================================================
# HTTP
# ============================================================================

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "jvuey-remote"

    def log_message(self, *args):
        pass  # keep the journal to startup lines only

    def handle_one_request(self):
        # Browsers drop keep-alive connections without ceremony, and the phone
        # does it every time the page is backgrounded. Without this, each one
        # prints a ConnectionResetError traceback into the journal.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: dict):
        self._send(200, json.dumps(data).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/remote")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/remote":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return

        if path == "/api/status":
            self._json(current_status())
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._json({"ok": False, "error": "bad json"})
            return

        if path == "/api/key":
            result = tui.request({"action": "key", "key": body.get("key", "")})
            self._json(result or current_status())
            return

        if path == "/api/search":
            result = tui.request({"action": "search", "text": body.get("text", "")})
            self._json(result or current_status())
            return

        if path == "/api/goto":
            # Jump the highlight to a row the phone tapped, by stepping the
            # TUI's own cursor so its scrolling stays consistent.
            status = tui.request({"action": "status"})
            if status and status.get("index") is not None:
                delta = int(body.get("index", 0)) - int(status["index"])
                key = "down" if delta > 0 else "up"
                for _ in range(min(abs(delta), 500)):
                    status = tui.request({"action": "key", "key": key})
            self._json(status or current_status())
            return

        if path == "/api/mpv":
            command = body.get("command")
            if not isinstance(command, list):
                self._json({"ok": False, "error": "bad command"})
                return
            mpv.command(command)
            # Give mpv a beat to apply pause/seek before reading it back.
            time.sleep(0.12)
            self._json(current_status())
            return

        self._send(404, b"not found", "text/plain")


def main():
    port = int(os.environ.get("JVUEY_REMOTE_PORT", "8080"))
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"jvuey-remote listening on http://0.0.0.0:{port}/remote", flush=True)
    print(f"  tui socket: {TUI_SOCKET}", flush=True)
    print(f"  mpv socket: {MPV_SOCKET}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
