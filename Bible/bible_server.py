# -*- coding: utf-8 -*-
r"""Server for the daily-verse app.

Run:  py bible_server.py      (or double-click Bible.bat)

Serves this folder on the network and owns data.js, the one file that must
not be written by two devices at once.

GET  /api/record        -> the whole record as JSON
POST /api/record        -> partial update, merged in, written atomically

Merging is per-day and per-verse, last-writer-wins by timestamp, so a
change made on the phone hours ago is not clobbered by a stale laptop copy.

The browser page works without this server too (open bible.html straight
off disk); in that mode it queues changes locally and posts them here the
next time it can reach it.
"""
import io
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REC_FILE = os.path.join(HERE, "data.js")
HOST = "0.0.0.0"                    # 0.0.0.0 so the phone can reach it
PORTS = [8395, 8396, 8397]
AUTO_OPEN = True

DEFAULT_SETTINGS = {"books": ["Psalms", "Proverbs", "Matthew"], "perDay": 2}

_REC_RE = re.compile(r"window\.RECORD\s*=\s*(\{.*\})\s*;?\s*$", re.S)


_LAST_GOOD = None         # most recent record that parsed
_QUARANTINED = False      # one backup per run, not one per request


def _quarantine_record():
    """An unreadable data.js (half-written by a power cut, or a bad sync)
    is never silently thrown away: the bytes are copied aside and the
    problem is printed loudly, so the record can be recovered by hand."""
    global _QUARANTINED
    if _QUARANTINED or not os.path.exists(REC_FILE):
        return
    _QUARANTINED = True
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copyfile(REC_FILE, REC_FILE + ".corrupt-" + stamp)
        print("[warn] data.js is unreadable - kept as data.js.corrupt-%s" % stamp,
              flush=True)
        print("[warn] serving the last record that parsed instead.", flush=True)
    except OSError:
        pass


def _parse_record(text):
    """The record out of data.js's text, or None if it is not a usable
    record. Structure is checked, not just syntax: a file whose days or
    settings came through as something other than an object counts as
    corrupt too."""
    m = _REC_RE.search(text)
    if not m:
        return None
    try:
        rec = json.loads(m.group(1))
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None
    if not isinstance(rec.get("days", {}), dict):
        return None
    if not isinstance(rec.get("settings", {}), dict):
        return None
    return rec


def load_record():
    global _LAST_GOOD
    try:
        with io.open(REC_FILE, encoding="utf-8") as f:
            rec = _parse_record(f.read())
        if rec is not None:
            rec.setdefault("v", 1)
            rec.setdefault("settings", dict(DEFAULT_SETTINGS))
            rec.setdefault("days", {})
            _LAST_GOOD = rec
            return rec
    except Exception:
        pass
    _quarantine_record()
    if _LAST_GOOD is not None:
        return _LAST_GOOD            # a bad read must not wipe the record
    return {"v": 1, "settings": dict(DEFAULT_SETTINGS), "days": {}}


_REC_LOCK = threading.Lock()


def _record_body(rec):
    return ("/* Your daily-verse record. Written by bible_server.py.\n"
            "   Do not edit while the app is open. */\n"
            "window.RECORD=" + json.dumps(rec, ensure_ascii=False,
                                          separators=(",", ":")) + ";\n")


def write_record(rec):
    """Write data.js via a temp file then replace, so a reader (or Syncthing)
    never sees a half-written file. Also clears the read-only flag, which the
    sync client can set."""
    tmp = REC_FILE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(_record_body(rec))
    try:
        if os.path.exists(REC_FILE):
            os.chmod(REC_FILE, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    os.replace(tmp, REC_FILE)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _merge_verse(old, new):
    """Field-level merge: done-flag and note are tracked separately, each
    with its own timestamp, so ticking 'done' on the phone cannot erase a
    note typed on the laptop."""
    if not isinstance(old, dict):
        old = {}
    if not isinstance(new, dict):
        return old
    out = dict(old)
    if new.get("done") is not None and _num(new.get("doneAt")) >= _num(old.get("doneAt")):
        out["done"] = bool(new["done"])
        out["doneAt"] = _num(new.get("doneAt"))
    if isinstance(new.get("note"), str) and _num(new.get("noteAt")) >= _num(old.get("noteAt")):
        out["note"] = new["note"]
        out["noteAt"] = _num(new.get("noteAt"))
    return out


def merge_record(rec, incoming):
    """Merge an incoming partial update into the stored record, in place."""
    if not isinstance(incoming, dict):
        return rec
    s = incoming.get("settings")
    if isinstance(s, dict):
        cur = rec.setdefault("settings", dict(DEFAULT_SETTINGS))
        if _num(s.get("updatedAt")) >= _num(cur.get("updatedAt")):
            if isinstance(s.get("books"), list) and s["books"]:
                cur["books"] = [str(b) for b in s["books"][:66]]
            try:
                cur["perDay"] = max(1, min(3, int(s.get("perDay", cur.get("perDay", 2)))))
            except (TypeError, ValueError):
                pass
            cur["updatedAt"] = _num(s.get("updatedAt"))
    days = incoming.get("days")
    if isinstance(days, dict):
        rdays = rec.setdefault("days", {})
        for key, day in days.items():
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(key)) or not isinstance(day, dict):
                continue
            cur = rdays.get(key)
            if not isinstance(cur, dict):
                rdays[key] = day
                continue
            # A day's verse states belong to THAT day's verse list. If the
            # incoming patch was made against different refs (a stale tab, or
            # settings changed before any work was done), it must not merge
            # state by index onto unrelated verses.
            incoming_refs = day.get("refs")
            cur_refs = cur.get("refs")
            refs_match = (not incoming_refs) or (list(incoming_refs) == list(cur_refs or []))
            if refs_match:
                merged_verses = {i: v for i, v in
                                 ((str(j), v) for j, v in enumerate(cur.get("verses", []))
                                  if isinstance(v, dict))}
                for i, nv in enumerate(day.get("verses", []) or []):
                    if not isinstance(nv, dict):
                        continue
                    merged_verses[str(i)] = _merge_verse(merged_verses.get(str(i), {}), nv)
                if merged_verses:
                    cur["verses"] = [merged_verses[str(i)] for i in range(len(merged_verses))]
            # The frozen verse list for a day: keep whichever is newer.
            # If the refs genuinely CHANGED, the tasks are different verses -
            # the newer freeze wins outright and brings its own states.
            if day.get("refs") and _num(day.get("frozenAt")) >= _num(cur.get("frozenAt")):
                if list(day["refs"]) != list(cur_refs or []):
                    cur["verses"] = [v if isinstance(v, dict) else {}
                                     for v in (day.get("verses") or [])]
                cur["refs"] = day["refs"]
                cur["frozenAt"] = _num(day.get("frozenAt"))
            cur["updatedAt"] = max(_num(cur.get("updatedAt")), _num(day.get("updatedAt")))
    return rec


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):          # quiet, like the music player
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              cache="no-store"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)   # default: never serve a stale record
        # The phone opens bible.html as a file:// page, whose origin is
        # "null" - without these headers the browser blocks its POST to us
        # (same lesson the music server needed).
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Chrome blocks a file:// page from POSTing to a private LAN IP
        # (Private Network Access) unless the server explicitly opts in.
        # Loopback tests never trigger this - only the real phone does.
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def do_OPTIONS(self):                    # CORS preflight
        self._send(204, b"")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/record":
            print("[rec] GET  from %s" % self.client_address[0], flush=True)
            with _REC_LOCK:
                rec = load_record()
            self._send(200, json.dumps(rec, ensure_ascii=False))
            return
        if path == "/config.js":
            # served fresh, so the page always knows the address the phone
            # should post to even after the laptop's IP changes
            body = ("window.APP_HOST=%s;\nwindow.APP_PORT=%d;\n"
                    % (json.dumps(lan_ip()), self.server.server_address[1]))
            self._send(200, body, "application/javascript; charset=utf-8")
            return
        if path == "/":
            path = "/bible.html"
        fn = os.path.normpath(os.path.join(HERE, path.lstrip("/")))
        if not fn.startswith(HERE) or not os.path.isfile(fn):
            self._send(404, '{"error":"not found"}')
            return
        base = os.path.basename(fn)
        ctype = "text/html; charset=utf-8" if fn.endswith(".html") else \
                "application/javascript; charset=utf-8" if fn.endswith(".js") else \
                "application/manifest+json; charset=utf-8" if fn.endswith(".webmanifest") else \
                "image/png" if fn.endswith(".png") else \
                "image/svg+xml" if fn.endswith(".svg") else \
                "text/plain; charset=utf-8"
        # What the browser may keep: the record and the LAN address are
        # never cached, the page and the service worker revalidate so edits
        # show up at once, and the big static files are safe for a day
        # (sw.js revalidates them itself, so the offline copy stays fresh).
        if base in ("data.js", "config.js"):
            cache = "no-store"
        elif fn.endswith(".html") or base == "sw.js":
            cache = "no-cache"
        elif fn.endswith((".js", ".png", ".webmanifest")):
            cache = "max-age=86400"
        else:
            cache = "no-cache"
        try:
            with io.open(fn, "rb") as f:
                data = f.read()
        except OSError:
            self._send(404, '{"error":"not found"}')
            return
        if base == "data.js":
            # A corrupt record (bad sync, half-write) must never reach a
            # browser: it would hard-error there and the service worker
            # would cache it. Serve the last record that parsed instead
            # (load_record quarantines the bad file on its way).
            if _parse_record(data.decode("utf-8", "replace")) is None:
                data = _record_body(load_record()).encode("utf-8")
        self._send(200, data, ctype, cache)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/record":
            self._send(404, '{"error":"not found"}')
            return
        print("[rec] POST from %s" % self.client_address[0], flush=True)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 2_000_000:
                self._send(400, '{"error":"bad length"}')
                return
            incoming = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._send(400, '{"error":"bad json"}')
            return
        with _REC_LOCK:
            rec = merge_record(load_record(), incoming)
            try:
                write_record(rec)
            except OSError as e:
                self._send(500, json.dumps({"error": "write failed: %s" % e}))
                return
        self._send(200, json.dumps({"ok": True}))


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))          # no packets sent, just picks a route
        return s.getsockname()[0]
    except OSError:
        return socket.gethostname()
    finally:
        s.close()


def main():
    port = None
    srv = None
    for p in PORTS:
        try:
            srv = ThreadingHTTPServer((HOST, p), Handler)
            srv.daemon_threads = True
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print("All ports busy - close any other copy and retry.")
        return 1

    print("Daily verses on http://127.0.0.1:%d" % port)
    print("On your phone (same Wi-Fi): http://%s:%d" % (lan_ip(), port))
    print("Ctrl+C to stop.")
    if AUTO_OPEN:
        threading.Timer(0.6, lambda: webbrowser.open(
            "http://127.0.0.1:%d" % port)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
