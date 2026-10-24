#!/usr/bin/env python3
"""
Share recordings - a small web page that lets any browser on the same network
play, download and delete the Steam Frame's recordings.

Python standard library only. The recorder's dashboard tab turns it on and off:

    server = ShareServer("~/Videos/framecorder")
    server.start()
    print(server.urls, server.pin)   # shown in the headset
    ...
    server.stop()

It can also be run by hand from Konsole for testing:

    python3 share_server.py --dir ~/Videos/framecorder

Recordings in the folder and in its subfolders one level down (such as clips/)
are listed. Files still being written end in .part and are left out until the
recorder renames them.

Safety: it only listens while sharing is on, asks for a 4-digit PIN once per
browser, only serves video files inside the recordings folder, and turns itself
off after IDLE_MINUTES with no requests.
"""

import argparse
import hmac
import html
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import zipfile
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_DIR = "~/Videos/framecorder"
DEFAULT_PORT = 8642
IDLE_MINUTES = 30
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".mov")
# A file written to within this many seconds is treated as still recording.
SETTLE_SECONDS = 5.0
MAX_FAILED_PINS = 5
LOCKOUT_SECONDS = 30.0
COPY_CHUNK = 1024 * 1024
COOKIE_NAME = "frame_share"
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                         "frame-fps-overlay", "thumbs")
MIME_TYPES = {".mp4": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
              ".mov": "video/quicktime"}


def log(message):
    print(message, flush=True)


def lan_ip():
    """The address other devices on the Wi-Fi reach us at (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024


class ShareServer:
    def __init__(self, recordings_dir=DEFAULT_DIR, port=DEFAULT_PORT, idle_minutes=IDLE_MINUTES,
                 on_stop=None):
        self.recordings_dir = os.path.realpath(os.path.expanduser(recordings_dir))
        self.port = port
        self.idle_seconds = int(idle_minutes * 60)
        # Called with no arguments when sharing stops by itself (idle timeout).
        self.on_stop = on_stop
        self.pin = ""
        self._sessions = set()
        self._failed = {}       # client ip -> (failures, locked_until)
        self._last_request = 0.0
        self._httpd = None
        self._thumb_lock = threading.Lock()
        self._lock = threading.Lock()

    @property
    def active(self):
        return self._httpd is not None

    @property
    def urls(self):
        """Addresses to show in the headset, the friendly name first."""
        return [f"http://{socket.gethostname()}.local:{self.port}", f"http://{lan_ip()}:{self.port}"]

    def start(self):
        with self._lock:
            if self._httpd:
                return
            os.makedirs(self.recordings_dir, exist_ok=True)
            self.pin = f"{secrets.randbelow(10000):04d}"
            self._sessions.clear()
            self._failed.clear()
            self._last_request = time.monotonic()
            handler = type("Handler", (_Handler,), {"share": self})
            self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
            self._httpd.daemon_threads = True
            threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
            threading.Thread(target=self._watch_idle, args=(self._httpd,), daemon=True).start()
        log(f"Sharing {self.recordings_dir} at {', '.join(self.urls)} (PIN {self.pin})")

    def stop(self):
        with self._lock:
            httpd, self._httpd = self._httpd, None
            self._sessions.clear()
        if httpd:
            httpd.shutdown()
            httpd.server_close()
            log("Sharing stopped")

    def _watch_idle(self, httpd):
        while self._httpd is httpd:
            time.sleep(10)
            if time.monotonic() - self._last_request > self.idle_seconds and self._httpd is httpd:
                log(f"No requests for {self.idle_seconds // 60} minutes")
                self.stop()
                if self.on_stop:
                    self.on_stop()
                return

    # --- recordings ---

    def recordings(self):
        """Finished recordings, newest first. A name is the path inside the folder, e.g. clips/x.mp4."""
        found = []
        now = time.time()
        folders = [("", self.recordings_dir)]
        try:
            folders += [(e.name + "/", e.path) for e in os.scandir(self.recordings_dir)
                        if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
        except FileNotFoundError:
            return found
        for prefix, folder in folders:
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith(".") or not entry.name.lower().endswith(VIDEO_EXTENSIONS):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                if now - st.st_mtime < SETTLE_SECONDS:
                    continue
                found.append({"name": prefix + entry.name, "folder": prefix.rstrip("/"),
                              "size": st.st_size, "size_text": human_size(st.st_size), "mtime": st.st_mtime})
        found.sort(key=lambda f: f["mtime"], reverse=True)
        return found

    def path_for(self, name):
        """The recording's path, or None if the name isn't a finished recording in the folder."""
        if not name or "\\" in name or any(part in ("", ".", "..") or part.startswith(".")
                                            for part in name.split("/")):
            return None
        path = os.path.realpath(os.path.join(self.recordings_dir, name))
        if not path.startswith(self.recordings_dir + os.sep):
            return None
        if not any(f["name"] == name for f in self.recordings()):
            return None
        return path

    @staticmethod
    def _thumb_prefix(name):
        return name.replace("/", "__") + "."

    def thumbnail(self, name):
        """A cached JPEG still for the recording, or None when ffmpeg isn't available."""
        path = self.path_for(name)
        ffmpeg = shutil.which("ffmpeg")
        if not path or not ffmpeg:
            return None
        st = os.stat(path)
        thumb = os.path.join(CACHE_DIR, f"{self._thumb_prefix(name)}{int(st.st_mtime)}.jpg")
        with self._thumb_lock:
            if not os.path.exists(thumb):
                os.makedirs(CACHE_DIR, exist_ok=True)
                tmp = thumb + ".tmp.jpg"
                # 1 s in skips a black first frame; fall back to the start for very short clips.
                for seek in ("1", "0"):
                    result = subprocess.run(
                        [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-ss", seek, "-i", path,
                         "-frames:v", "1", "-vf", "scale=480:-2", tmp],
                        capture_output=True, timeout=30)
                    if result.returncode == 0 and os.path.exists(tmp):
                        os.replace(tmp, thumb)
                        break
            if not os.path.exists(thumb):
                return None
        with open(thumb, "rb") as f:
            return f.read()

    def delete(self, name):
        path = self.path_for(name)
        if not path:
            return False
        os.remove(path)
        for cached in os.listdir(CACHE_DIR) if os.path.isdir(CACHE_DIR) else []:
            if cached.startswith(self._thumb_prefix(name)):
                os.remove(os.path.join(CACHE_DIR, cached))
        log(f"Deleted {name}")
        return True

    # --- PIN ---

    def check_pin(self, ip, pin):
        """Returns a session token, or None. Locks a client out for a while after repeated misses."""
        failures, locked_until = self._failed.get(ip, (0, 0.0))
        if time.monotonic() < locked_until:
            return None
        if hmac.compare_digest(pin.strip(), self.pin):
            self._failed.pop(ip, None)
            token = secrets.token_urlsafe(24)
            self._sessions.add(token)
            return token
        failures += 1
        if failures >= MAX_FAILED_PINS:
            self._failed[ip] = (0, time.monotonic() + LOCKOUT_SECONDS)
        else:
            self._failed[ip] = (failures, 0.0)
        return None

    def locked_out(self, ip):
        return time.monotonic() < self._failed.get(ip, (0, 0.0))[1]


class _Handler(BaseHTTPRequestHandler):
    share = None    # set per server in ShareServer.start()
    protocol_version = "HTTP/1.1"
    server_version = "FrameShare"

    def log_message(self, fmt, *args):
        pass

    # --- plumbing ---

    def _authed(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        token = jar[COOKIE_NAME].value if COOKIE_NAME in jar else ""
        return token in self.share._sessions

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status=200):
        self._send(status, json.dumps(data), "application/json")

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(length, 64 * 1024)) if length else b""

    def _route(self):
        self.share._last_request = time.monotonic()
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        parts = parsed.path.split("/", 2)
        return parts[1] if len(parts) > 1 else "", urllib.parse.unquote(parts[2]) if len(parts) > 2 else "", query

    # --- GET / HEAD ---

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        section, name, query = self._route()
        if not self._authed():
            if section == "":
                return self._send(200, PIN_PAGE.replace("{error}", ""), "text/html; charset=utf-8")
            return self._send(401, "Enter the PIN first")
        if section == "":
            return self._send(200, MAIN_PAGE, "text/html; charset=utf-8")
        if section == "api" and name == "files":
            return self._json({"files": self.share.recordings(), "folder": self.share.recordings_dir})
        if section == "files":
            return self._send_file(name, download="download" in query)
        if section == "thumb":
            data = self.share.thumbnail(name)
            if data is None:
                return self._send(404, "No thumbnail")
            return self._send(200, data, "image/jpeg", {"Cache-Control": "max-age=86400"})
        if section == "zip":
            return self._send_zip(query.get("name", []))
        return self._send(404, "Not found")

    def _send_file(self, name, download):
        path = self.share.path_for(name)
        if not path:
            return self._send(404, "Not found")
        size = os.path.getsize(path)
        start, end = 0, size - 1
        status = 200
        byte_range = self.headers.get("Range", "")
        if byte_range.startswith("bytes="):
            first, _, last = byte_range[6:].split(",")[0].strip().partition("-")
            try:
                if first:
                    start, end = int(first), int(last) if last else size - 1
                else:
                    start = max(0, size - int(last))
                end = min(end, size - 1)
            except ValueError:
                start, end = 0, size - 1
            if start > end or start >= size:
                return self._send(416, "", headers={"Content-Range": f"bytes */{size}"})
            status = 206
        disposition = "attachment" if download else "inline"
        self.send_response(status)
        self.send_header("Content-Type", MIME_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{urllib.parse.quote(os.path.basename(name))}")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    chunk = f.read(min(COPY_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                    self.share._last_request = time.monotonic()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_zip(self, names):
        paths = [(n, self.share.path_for(n)) for n in names]
        if not paths or any(p is None for _, p in paths):
            return self._send(404, "Not found")
        # Video doesn't compress further, so files are stored as-is and streamed; the size isn't
        # known up front, so the connection closes at the end instead.
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f"attachment; filename=\"frame-recordings-{time.strftime('%Y%m%d-%H%M')}.zip\"")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        try:
            with zipfile.ZipFile(_TouchingWriter(self.wfile, self.share), "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for name, path in paths:
                    zf.write(path, arcname=name)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # --- POST ---

    def do_POST(self):
        section, name, _ = self._route()
        ip = self.client_address[0]
        if section == "login":
            form = urllib.parse.parse_qs(self._read_body().decode(errors="replace"))
            token = self.share.check_pin(ip, form.get("pin", [""])[0])
            if token:
                return self._send(303, "", headers={
                    "Location": "/",
                    "Set-Cookie": f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict"})
            error = ("Too many wrong PINs. Wait 30 seconds and try again." if self.share.locked_out(ip)
                     else "Wrong PIN. Check the number shown in the headset.")
            page = PIN_PAGE.replace("{error}", f'<p class="error">{html.escape(error)}</p>')
            return self._send(403, page, "text/html; charset=utf-8")
        if not self._authed():
            return self._send(401, "Enter the PIN first")
        # Same-origin JSON only: a form on another site can't send this content type without CORS.
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(415, "Expected JSON")
        if section == "api" and name == "delete":
            try:
                names = json.loads(self._read_body() or b"{}").get("names", [])
            except (ValueError, AttributeError):
                return self._send(400, "Bad request")
            deleted = [n for n in names if isinstance(n, str) and self.share.delete(n)]
            return self._json({"deleted": deleted})
        return self._send(404, "Not found")


class _TouchingWriter:
    """Write-only stream for zipfile that keeps the idle timer fresh during long downloads."""

    def __init__(self, out, share):
        self.out, self.share = out, share

    def write(self, data):
        self.share._last_request = time.monotonic()
        return self.out.write(data)

    def flush(self):
        self.out.flush()


STYLE = """
:root { color-scheme: dark; --bg:#111418; --card:#1b2027; --line:#2b323c; --text:#e8ecf1; --muted:#9aa4b1; --accent:#4fa3ff; --danger:#ff6b6b; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.4 system-ui, sans-serif; background:var(--bg); color:var(--text); }
header { position:sticky; top:0; z-index:2; display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:12px 16px; background:var(--bg); border-bottom:1px solid var(--line); }
h1 { font-size:18px; margin:0 auto 0 0; }
button, .button { font:inherit; padding:8px 14px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--text); cursor:pointer; text-decoration:none; }
button.primary { background:var(--accent); border-color:var(--accent); color:#04121f; }
button.danger { color:var(--danger); }
button:disabled { opacity:.4; cursor:default; }
main { padding:16px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(240px, 1fr)); gap:14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
.card.selected { outline:2px solid var(--accent); }
.thumb { position:relative; aspect-ratio:16/9; background:#000 center/cover no-repeat; cursor:pointer; display:flex; align-items:center; justify-content:center; color:var(--muted); }
.thumb input { position:absolute; top:8px; left:8px; width:20px; height:20px; }
.info { padding:10px 12px; }
.name { font-weight:600; word-break:break-all; }
.meta { color:var(--muted); font-size:13px; margin:2px 0 8px; }
.actions { display:flex; gap:6px; }
.actions > * { flex:1; text-align:center; padding:6px; font-size:14px; }
.empty { color:var(--muted); text-align:center; padding:60px 16px; }
dialog { padding:0; border:0; background:#000; max-width:96vw; }
dialog video { display:block; max-width:96vw; max-height:85vh; }
dialog::backdrop { background:rgba(0,0,0,.8); }
.pin { max-width:320px; margin:12vh auto; padding:0 16px; text-align:center; }
.pin input { font-size:32px; letter-spacing:12px; width:100%; text-align:center; padding:10px; border-radius:10px; border:1px solid var(--line); background:var(--card); color:var(--text); margin:16px 0; }
.error { color:var(--danger); }
"""

PIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Frame recordings</title>
<style>""" + STYLE + """</style></head><body>
<form class="pin" method="post" action="/login">
<h1>Frame recordings</h1>
<p>Enter the PIN shown in the headset.</p>
{error}
<input name="pin" inputmode="numeric" autocomplete="off" maxlength="4" autofocus required>
<button class="primary" type="submit">Open</button>
</form></body></html>"""

MAIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Frame recordings</title>
<style>""" + STYLE + """</style></head><body>
<header>
<h1>Frame recordings</h1>
<button id="all">Select all</button>
<button id="zip" class="primary" disabled>Download selected</button>
<button id="del" class="danger" disabled>Delete selected</button>
<button id="refresh">Refresh</button>
</header>
<main><div id="grid" class="grid"></div><p id="empty" class="empty" hidden>No recordings yet.</p></main>
<dialog id="player"><video id="video" controls autoplay></video></dialog>
<script>
const grid = document.getElementById("grid"), selected = new Set();
const enc = encodeURIComponent;
function esc(s) { return s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function updateButtons() {
  document.getElementById("zip").disabled = document.getElementById("del").disabled = !selected.size;
  document.getElementById("zip").textContent = selected.size ? `Download selected (${selected.size})` : "Download selected";
}
async function load() {
  const res = await fetch("/api/files");
  if (res.status === 401) return location.reload();
  const {files} = await res.json();
  for (const n of [...selected]) if (!files.some(f => f.name === n)) selected.delete(n);
  document.getElementById("empty").hidden = files.length > 0;
  grid.innerHTML = files.map(f => `
    <div class="card${selected.has(f.name) ? " selected" : ""}" data-name="${esc(f.name)}">
      <div class="thumb" style="background-image:url('/thumb/${enc(f.name)}')">
        <input type="checkbox" ${selected.has(f.name) ? "checked" : ""} aria-label="Select">&#9654;</div>
      <div class="info"><div class="name">${esc(f.name.split("/").pop())}</div>
        <div class="meta">${f.folder ? esc(f.folder) + " &middot; " : ""}${new Date(f.mtime * 1000).toLocaleString()} &middot; ${f.size_text}</div>
        <div class="actions"><a class="button" href="/files/${enc(f.name)}?download">Download</a>
          <button class="danger" data-delete>Delete</button></div></div>
    </div>`).join("");
  updateButtons();
}
async function remove(names) {
  if (!confirm(names.length === 1 ? `Delete ${names[0]}?` : `Delete ${names.length} recordings?`)) return;
  await fetch("/api/delete", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({names})});
  names.forEach(n => selected.delete(n));
  load();
}
grid.addEventListener("click", e => {
  const card = e.target.closest(".card"); if (!card) return;
  const name = card.dataset.name;
  if (e.target.matches("input[type=checkbox]")) {
    e.target.checked ? selected.add(name) : selected.delete(name);
    card.classList.toggle("selected", e.target.checked); updateButtons();
  } else if (e.target.matches("[data-delete]")) {
    remove([name]);
  } else if (e.target.closest(".thumb")) {
    const video = document.getElementById("video");
    video.src = "/files/" + enc(name); document.getElementById("player").showModal();
  }
});
document.getElementById("player").addEventListener("click", e => {
  if (e.target.id === "player") { e.target.close(); }
});
document.getElementById("player").addEventListener("close", () => {
  const video = document.getElementById("video"); video.pause(); video.removeAttribute("src"); video.load();
});
document.getElementById("all").onclick = () => {
  const cards = [...grid.querySelectorAll(".card")];
  const all = cards.length && cards.every(c => selected.has(c.dataset.name));
  cards.forEach(c => all ? selected.delete(c.dataset.name) : selected.add(c.dataset.name));
  load();
};
document.getElementById("zip").onclick = () => {
  if (selected.size === 1) location.href = "/files/" + enc([...selected][0]) + "?download";
  else location.href = "/zip?" + [...selected].map(n => "name=" + enc(n)).join("&");
};
document.getElementById("del").onclick = () => remove([...selected]);
document.getElementById("refresh").onclick = load;
load();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description="Share recordings with browsers on the same network.")
    parser.add_argument("--dir", default=DEFAULT_DIR, help=f"recordings folder (default {DEFAULT_DIR})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    args = parser.parse_args()
    stopped = threading.Event()
    server = ShareServer(args.dir, args.port, on_stop=stopped.set)
    server.start()
    print(f"\nOpen one of these on your PC:\n  " + "\n  ".join(server.urls) + f"\nPIN: {server.pin}\n"
          "Press Ctrl+C to stop.", flush=True)
    try:
        stopped.wait()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
