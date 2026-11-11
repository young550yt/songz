#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simple local music player.  No dependencies, no install, no internet.

Run:
    python music_player.py
    (or double-click  "Play Music.bat")

Then it opens http://127.0.0.1:8390 in your browser, and prints a LAN URL
for your phone (same Wi-Fi) at startup.

Edit MUSIC_DIRS below to point at whatever folders hold your songs.
"""
import sys, os, re, json, struct, threading, webbrowser, socket, time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- config ----
# Folders to scan for music.  Add or remove freely; relative to this file
# works too ("" means the folder this script lives in).
MUSIC_DIRS = [
    r"C:\Users\young\Music\Songz🎶",
]

AUDIO_EXT = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".wma"}
PORTS = [8390, 8391, 8392, 8393]
AUTO_OPEN = True
# 0.0.0.0 = listen on every interface, so a phone on the same Wi-Fi can reach
# the player. Loopback (127.0.0.1) still works on the PC itself.
HOST = "0.0.0.0"

CTYPES = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".opus": "audio/opus", ".wav": "audio/wav",
    ".aac": "audio/aac", ".wma": "audio/x-ms-wma",
}

# ------------------------------------------------------------ librarz -------
_NOISE = re.compile(
    r"\s*\((?:[\w]{2,5}_\d{2,4}K|Official Audio|Official Music Video|Official Video|"
    r"Audio|Video|Lyrics|Visualizer|Remastered|Explicit|Clean)\)", re.I)


def pretty(name):
    """'6LACK - On Me ft. Odeal (Official Audio)(MP3_160K).mp3' -> (artist, title)
    Only reached when a file carries no ID3 title, so '_' -> ' ' is safe here."""
    base = os.path.splitext(name)[0]
    base = _NOISE.sub(" ", base)          # must run before '_' -> ' ' mangles
    base = base.replace("_", " ")         # the  M4A_128K  style markers
    base = re.sub(r"\s{2,}", " ", base).strip(" -_.,")
    if " - " in base:
        a, t = base.split(" - ", 1)
        return a.strip(), t.strip()
    return "", base.strip()


_ID3_ENC = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}
_ID3_MAP = {b"TIT2": "title", b"TPE1": "artist", b"TALB": "album"}


def read_id3(path):
    """Best-effort ID3v2 title/artist.  Seeks *past* frames it doesn't want so
    embedded cover art never gets dragged into memory."""
    with open(path, "rb") as f:
        h = f.read(10)
        if len(h) < 10 or h[:3] != b"ID3":
            return {}
        ver, flags = h[3], h[5]
        total = (h[6] << 21) | (h[7] << 14) | (h[8] << 7) | h[9]
        end = 10 + total
        if flags & 0x40:                                   # extended header
            eh = f.read(4)
            if len(eh) < 4:
                return {}
            ext = ((eh[0] << 21) | (eh[1] << 14) | (eh[2] << 7) | eh[3]) if ver == 4 \
                else struct.unpack(">I", eh)[0]
            if ext < 0 or ext > 4096:
                return {}
            f.seek(ext, 1)

        out = {}
        while f.tell() + 10 <= end:
            hdr = f.read(10)
            if len(hdr) < 10 or hdr[:1] == b"\x00":
                break
            fid = hdr[:4]
            if ver == 4:                                   # synchsafe
                fsz = (hdr[4] << 21) | (hdr[5] << 14) | (hdr[6] << 7) | hdr[7]
            else:                                          # v2.3 plain
                fsz = struct.unpack(">I", hdr[4:8])[0]
            if fsz <= 0 or fsz > 8 * 1024 * 1024 or f.tell() + fsz > end + 16:
                break
            if fid in _ID3_MAP:
                data = f.read(fsz)
                if len(data) != fsz or not data[:1]:
                    continue
                try:
                    txt = data[1:].decode(_ID3_ENC.get(data[0], "latin-1"), "replace")
                except Exception:
                    txt = ""
                out[_ID3_MAP[fid]] = txt.replace("\ufeff", "").replace("\u200b", "") \
                                        .strip("\x00").strip()
            else:
                f.seek(fsz, 1)                             # skip artwork etc.
        return out


def _clean(s):
    """Strip BOMs, video-chapter noise and dangling punctuation."""
    s = (s or "").replace("\ufeff", "").replace("\u200b", "")
    s = s.split("|")[0]                                    # 'Song | Channel Presents...'
    s = _NOISE.sub(" ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -_.,|")


def _meta(path, name):
    """ID3 tags first (343/344 of these are properly tagged), filename as fallback."""
    try:
        tag = read_id3(path)
    except OSError:
        tag = {}
    artist = _clean(tag.get("artist", ""))
    title = _clean(tag.get("title", ""))
    if not title:
        fa, ft = pretty(name)
        title = _clean(ft) or os.path.splitext(name)[0]
        if not artist:
            artist = _clean(fa)
    return artist, title


def folder_label(d):
    """'C:\\Users\\young\\Downloads\\Music' -> 'Downloads\\Music'
    (keeps folders that share a basename distinguishable)"""
    d = os.path.normpath(d)
    name = os.path.basename(d)
    parent = os.path.basename(os.path.dirname(d))
    return (parent + "\\" + name) if parent else name


LIB_LOCK = threading.Lock()          # guards the library against delete races


def _abs_dirs():
    """Configured folders, absolute + resolved.  Used to walk the tree AND as
    the 'is this file really one of ours?' check before deleting anything."""
    base = os.path.dirname(os.path.abspath(__file__))
    return [os.path.realpath(d if os.path.isabs(d) else os.path.join(base, d))
            for d in MUSIC_DIRS]


LIB_REV = 1


def scan():
    out, seen = [], set()
    for d in _abs_dirs():
        if not os.path.isdir(d):
            continue
        for dp, dns, fns in os.walk(d):
            # Never scan App, Bible, or hidden folders
            dns[:] = [dn for dn in dns if dn not in ("App", "Bible") and not dn.startswith(".")]
            for n in fns:
                if n.startswith(".") or ".trashed" in n or ".syncthing" in n or n.endswith(".tmp"):
                    continue
                ext = os.path.splitext(n)[1].lower()
                if ext not in AUDIO_EXT:
                    continue
                p = os.path.join(dp, n)
                key = os.path.normcase(p)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                if sz < 1024:
                    continue
                artist, title = _meta(p, n)
                out.append({
                    "path": p, "size": sz, "ext": ext,
                    "artist": artist, "title": title,
                    "folder": folder_label(d),
                    "sub": os.path.relpath(dp, d) if os.path.normcase(dp) != os.path.normcase(d) else "",
                })
    out.sort(key=lambda s: (s["artist"].lower(), s["title"].lower()))
    return out


LIBRARY = scan()


def refresh():
    global LIBRARY, LYRICS, LIB_REV
    new = scan()
    with LIB_LOCK:
        LIBRARY = new
        LIB_REV += 1
    try:
        with DATA_LOCK:
            LYRICS = _load_lyrics()
    except Exception:
        pass


def _rebuild_offline_bg():
    try:
        import subprocess
        make_offline = os.path.join(os.path.dirname(os.path.abspath(__file__)), "make_offline_player.py")
        if os.path.exists(make_offline):
            subprocess.run([sys.executable, make_offline], capture_output=True, text=True, timeout=15)
    except Exception:
        pass


def _get_fs_signature():
    """Fast snapshot of music folders: file paths, sizes, and mtimes.
    Detects added, deleted, renamed, or modified audio/lyrics/cover files."""
    sig = []
    for d in _abs_dirs():
        if not os.path.isdir(d):
            continue
        try:
            sig.append((d, os.path.getmtime(d)))
        except OSError:
            pass
        for dp, dns, fns in os.walk(d):
            dns[:] = [dn for dn in dns if dn not in ("App", "Bible") and not dn.startswith(".")]
            for n in fns:
                if n.startswith(".") or ".trashed" in n or ".syncthing" in n or n.endswith(".tmp"):
                    continue
                ext = os.path.splitext(n)[1].lower()
                if ext in AUDIO_EXT or ext in (".lrc", ".jpg", ".jpeg", ".png", ".webp"):
                    p = os.path.join(dp, n)
                    try:
                        st = os.stat(p)
                        sig.append((p, st.st_size, st.st_mtime))
                    except OSError:
                        pass
    sig.sort()
    return tuple(sig)


def _folder_watcher():
    last_sig = _get_fs_signature()
    while True:
        time.sleep(1.2)
        try:
            cur_sig = _get_fs_signature()
            if cur_sig != last_sig:
                last_sig = cur_sig
                print("[Watcher] Music folder change detected. Rescanning...")
                refresh()
                threading.Thread(target=_rebuild_offline_bg, daemon=True).start()
        except Exception:
            pass


def start_watcher():
    t = threading.Thread(target=_folder_watcher, daemon=True, name="FolderWatcher")
    t.start()


def api_json():
    labels, seen = [], set()
    with LIB_LOCK:
        for s in LIBRARY:
            lab = s["folder"] + (("\\" + s["sub"]) if s["sub"] else "")
            if lab not in seen:
                seen.add(lab)
                labels.append(lab)
        labels.sort()
        songs = [
            {"i": i, "artist": s["artist"], "title": s["title"],
             "size": s["size"], "folder": s["folder"], "sub": s["sub"],
             "path": s["path"]}
            for i, s in enumerate(LIBRARY)
        ]
        rev = LIB_REV
        count = len(LIBRARY)
    return json.dumps({
        "rev": rev,
        "count": count,
        "folders": labels,
        "songs": songs,
    }, ensure_ascii=False).encode("utf-8")


# ------------------------------------------------------------- album art ----
COVER_NAMES = ("cover", "folder", "album", "front", "artwork")
COVER_EXT = (".jpg", ".jpeg", ".png", ".webp")
IMG_CT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".png": "image/png", ".webp": "image/webp"}
MAX_COVER = 8 * 1024 * 1024


def find_cover(song_path):
    """cover/folder/album.jpg sitting in the song's own folder."""
    d = os.path.dirname(os.path.realpath(song_path))
    try:
        names = os.listdir(d)
    except OSError:
        return None
    lower = {}
    for n in names:
        if os.path.isfile(os.path.join(d, n)):
            lower[n.lower()] = n
    for nm in COVER_NAMES:
        for ext in COVER_EXT:
            hit = lower.get(nm + ext)
            if hit:
                return os.path.join(d, hit)
    return None


COVER_CACHE = {}


def extract_embedded_cover(path):
    """Extract embedded ID3 APIC or MP4 covr artwork bytes and content-type."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        try:
            with open(path, "rb") as f:
                h = f.read(10)
                if len(h) < 10 or h[:3] != b"ID3":
                    return None
                ver, flags = h[3], h[5]
                total = (h[6] << 21) | (h[7] << 14) | (h[8] << 7) | h[9]
                end = 10 + total
                if flags & 0x40:
                    eh = f.read(4)
                    if len(eh) < 4:
                        return None
                    ext_len = ((eh[0] << 21) | (eh[1] << 14) | (eh[2] << 7) | eh[3]) if ver == 4 else struct.unpack(">I", eh)[0]
                    if 0 <= ext_len <= 4096:
                        f.seek(ext_len, 1)
                while f.tell() + 10 <= end:
                    hdr = f.read(10)
                    if len(hdr) < 10 or hdr[:1] == b"\x00":
                        break
                    fid = hdr[:4]
                    fsz = (hdr[4] << 21) | (hdr[5] << 14) | (hdr[6] << 7) | hdr[7] if ver == 4 else struct.unpack(">I", hdr[4:8])[0]
                    if fsz <= 0 or fsz > 16 * 1024 * 1024 or f.tell() + fsz > end + 16:
                        break
                    if fid == b"APIC":
                        data = f.read(fsz)
                        for sig, ct in ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"\x89PNG", "image/png"), (b"RIFF", "image/webp")):
                            idx = data.find(sig)
                            if idx != -1:
                                return data[idx:], ct
                    else:
                        f.seek(fsz, 1)
        except OSError:
            pass
    elif ext in (".m4a", ".mp4", ".aac"):
        try:
            with open(path, "rb") as f:
                raw = f.read(min(os.path.getsize(path), 2 * 1024 * 1024))
                ci = raw.find(b"covr")
                if ci != -1:
                    di = raw.find(b"data", ci)
                    if di != -1 and di - ci < 32:
                        img_data = raw[di+16:]
                        for sig, ct in ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG", "image/png")):
                            si = img_data.find(sig)
                            if si != -1:
                                return img_data[si:], ct
        except OSError:
            pass
    return None


# ------------------------------------------------- favourites + lyrics -------
# Sidecar data lives NEXT TO this script, never inside the music folders, so a
# rescan can never pick it up.  Everything is keyed by the exact path string
# the library hands out, so renumbering tracks on rescan/delete orphans nothing.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAVS_FILE = os.path.join(BASE_DIR, "favourites.json")
LYRICS_FILE = os.path.join(BASE_DIR, "lyrics.json")
MAX_LYRICS = 400 * 1024

DATA_LOCK = threading.Lock()          # guards FAVS + LYRICS


def _load_json(fn):
    try:
        with open(fn, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _dump_json(fn, obj):
    """tmp + os.replace - a crash mid-write can never truncate the real file."""
    tmp = fn + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, fn)


def _split_lines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _clean_times(old, lines):
    """Keep a timestamp only if that line still says exactly what it said when
    the stamp was taken - editing the lyrics can never silently retimestamp
    different words, and blank lines are never allowed a stamp."""
    out = {}
    if not isinstance(old, dict):
        return out
    oldlines = old.get("lines")
    oldtimes = old.get("times")
    if not isinstance(oldlines, list) or not isinstance(oldtimes, dict):
        return out
    for k, v in oldtimes.items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if not (0 <= i < len(lines) and i < len(oldlines)):
            continue
        if oldlines[i] != lines[i] or not lines[i].strip():
            continue
        if 0 <= float(v) < 100000:
            out[str(i)] = round(float(v), 3)
    return out


def parse_lrc(path):
    """Parses a .lrc file into dict(text=..., lines=..., times=...)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw.strip() or len(raw) > MAX_LYRICS:
        return None
    raw_lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned_lines = []
    times = {}
    stamp_re = re.compile(r"^\[(\d{1,2}):(\d{2}(?:\.\d+)?)\](.*)$")
    for raw_l in raw_lines:
        line_str = raw_l.strip()
        m = stamp_re.match(line_str)
        if m:
            mins, secs, txt = m.group(1), m.group(2), m.group(3).strip()
            t = round(int(mins) * 60 + float(secs), 3)
            idx = len(cleaned_lines)
            cleaned_lines.append(txt)
            if txt:
                times[str(idx)] = t
        else:
            if line_str.startswith("[") and ":" in line_str and line_str.endswith("]"):
                continue
            cleaned_lines.append(line_str)
    return {"text": "\n".join(cleaned_lines), "lines": cleaned_lines, "times": times}


def _find_sidecar_lrc(song_path):
    base = os.path.splitext(song_path)[0]
    for cand in (base + ".lrc", base + "_private.lrc"):
        if os.path.isfile(cand):
            return cand
    return None


def fetch_online_lyrics(artist, title):
    """Query LRCLIB or lyrics.ovh for synced or plain lyrics."""
    out = {"text": "", "lines": [], "times": {}}
    if not artist and not title:
        return out
    # 1. Try LRCLIB
    try:
        q_art = urllib.parse.quote(artist or "")
        q_tit = urllib.parse.quote(title or "")
        url = f"https://lrclib.net/api/get?artist_name={q_art}&track_name={q_tit}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("syncedLyrics"):
                raw = data["syncedLyrics"]
                lines, times = [], {}
                stamp_re = re.compile(r"^\[(\d{1,2}):(\d{2}(?:\.\d+)?)\](.*)$")
                for l in raw.splitlines():
                    m = stamp_re.match(l.strip())
                    if m:
                        t = round(int(m.group(1)) * 60 + float(m.group(2)), 3)
                        txt = m.group(3).strip()
                        idx = len(lines)
                        lines.append(txt)
                        if txt:
                            times[str(idx)] = t
                    else:
                        lines.append(l.strip())
                return {"text": "\n".join(lines), "lines": lines, "times": times}
            elif data.get("plainLyrics"):
                lines = [l.strip() for l in data["plainLyrics"].splitlines()]
                return {"text": "\n".join(lines), "lines": lines, "times": {}}
    except Exception:
        pass

    # 2. Try lyrics.ovh
    try:
        url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("lyrics"):
                txt = data["lyrics"].strip()
                lines = [l.strip() for l in txt.splitlines()]
                return {"text": txt, "lines": lines, "times": {}}
    except Exception:
        pass
    return out


def _load_lyrics():
    out = {}
    for p, r in _load_json(LYRICS_FILE).items():
        if not isinstance(p, str) or not isinstance(r, dict):
            continue
        text = r.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_LYRICS:
            continue
        lines = _split_lines(text)
        out[p] = {"text": text, "lines": lines, "times": _clean_times(r, lines)}
    # Populate from sidecar .lrc files if not present
    for s in LIBRARY:
        p = s["path"]
        if p not in out:
            lrc_file = _find_sidecar_lrc(p)
            if lrc_file:
                parsed = parse_lrc(lrc_file)
                if parsed and parsed.get("text"):
                    out[p] = parsed
    return out


FAVS = {}
for _fp, _fv in _load_json(FAVS_FILE).items():
    if isinstance(_fp, str) and _fv:
        FAVS[_fp] = True
LYRICS = _load_lyrics()


def _is_ours(path):
    """Same guard the delete route uses: this really is one of our audio files.
    Checked before ANY lyrics/favourite write lands on disk."""
    if not isinstance(path, str) or not path:
        return False
    real = os.path.realpath(path).lower()
    roots = [d.lower() for d in _abs_dirs()]
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        return False
    return os.path.splitext(real)[1].lower() in AUDIO_EXT


def fav_json():
    with DATA_LOCK:
        paths = sorted(FAVS)
    return json.dumps({"paths": paths}, ensure_ascii=False).encode("utf-8")


def lyr_index_json():
    with DATA_LOCK:
        have = sorted(LYRICS)
        synced = sorted(p for p, r in LYRICS.items() if r["times"])
    return json.dumps({"have": have, "synced": synced},
                      ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------- server -----
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- helpers ----------------------------------------------------------
    def _bytes(self, data, ctype, status=200, extra=None):
        hdrs = {"Cache-Control": "no-store"}
        hdrs.update(extra or {})          # extra may override the cache policy
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_audio(self, path, size, ctype):
        """Send the file honouring HTTP Range so the seek bar works."""
        rng = self.headers.get("Range")
        start, end, status = 0, size - 1, 200

        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:                                   # suffix: last N bytes
                    start = max(0, size - int(m.group(2)))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "max-age=3600")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()

        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            pass

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        if path in ("/", "/index.html"):
            return self._bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")

        if path == "/spectrum":
            return self._bytes(SPECTRUM_PAGE.encode("utf-8"),
                               "text/html; charset=utf-8")

        # PWA files for the service worker (sw.js + manifest + icons)
        if path in ("/sw.js", "/manifest.webmanifest",
                    "/icon-192.png", "/icon-512.png"):
            fn = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              path.lstrip("/"))
            ctype = ("application/javascript; charset=utf-8"
                     if fn.endswith(".js") else
                     "application/manifest+json; charset=utf-8"
                     if fn.endswith(".webmanifest") else "image/png")
            cache = "no-cache" if fn.endswith(".js") else "max-age=86400"
            try:
                with open(fn, "rb") as f:
                    data = f.read()
            except OSError:
                return self._bytes(b"not found", "text/plain; charset=utf-8", 404)
            return self._bytes(data, ctype, extra={"Cache-Control": cache})

        if path == "/api/version":
            with LIB_LOCK:
                v_data = json.dumps({"rev": LIB_REV, "count": len(LIBRARY)}).encode("utf-8")
            return self._bytes(v_data, "application/json; charset=utf-8", extra={"Cache-Control": "no-cache"})

        if path == "/api/library":
            return self._bytes(api_json(), "application/json; charset=utf-8", extra={"Cache-Control": "no-cache"})

        if path == "/api/rescan":
            refresh()
            return self._bytes(api_json(), "application/json; charset=utf-8", extra={"Cache-Control": "no-cache"})

        if path == "/api/favourites":
            return self._bytes(fav_json(), "application/json; charset=utf-8")

        if path == "/api/lyrics":
            return self._bytes(lyr_index_json(), "application/json; charset=utf-8")

        if path == "/api/lyrics/show":
            q = parse_qs(urlparse(self.path).query)
            p = (q.get("path") or [""])[0]
            with DATA_LOCK:
                rec = LYRICS.get(p)
                out = ({"path": p, "text": rec["text"],
                        "lines": list(rec["lines"]), "times": dict(rec["times"])}
                       if rec else {})
            return self._bytes(json.dumps(out, ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")

        if path == "/api/lyrics/fetch":
            q = parse_qs(urlparse(self.path).query)
            artist = (q.get("artist") or [""])[0]
            title = (q.get("title") or [""])[0]
            res = fetch_online_lyrics(artist, title)
            return self._bytes(json.dumps(res, ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")

        m = re.match(r"^/audio/(\d+)$", path)
        if m:
            q = parse_qs(urlparse(self.path).query)
            req_p = (q.get("p") or q.get("path") or [None])[0]
            song = None
            with LIB_LOCK:
                if req_p:
                    for s in LIBRARY:
                        if s["path"] == req_p:
                            song = s
                            break
                if not song:
                    i = int(m.group(1))
                    if 0 <= i < len(LIBRARY):
                        song = LIBRARY[i]
            if song:
                return self._serve_audio(song["path"], song["size"],
                                         CTYPES.get(song["ext"], "application/octet-stream"))
            self.send_error(404, "no such track")
            return

        m = re.match(r"^/api/cover/(-?\d+)$", path)
        if m:
            return self._cover(int(m.group(1)))

        self.send_error(404)

    def _cover(self, i):
        with LIB_LOCK:
            if not (0 <= i < len(LIBRARY)):
                return self.send_error(404, "no such track")
            song_path = LIBRARY[i]["path"]

        # 1. Look for loose file on disk
        f = find_cover(song_path)
        if f:
            try:
                if os.path.getsize(f) <= MAX_COVER:
                    with open(f, "rb") as fh:
                        data = fh.read()
                    ctype = IMG_CT.get(os.path.splitext(f)[1].lower(), "image/jpeg")
                    return self._bytes(data, ctype, extra={"Cache-Control": "max-age=86400"})
            except OSError:
                pass

        # 2. Extract embedded cover from tag (APIC / covr)
        if song_path in COVER_CACHE:
            cov = COVER_CACHE[song_path]
        else:
            cov = extract_embedded_cover(song_path)
            COVER_CACHE[song_path] = cov

        if cov:
            data, ctype = cov
            return self._bytes(data, ctype, extra={"Cache-Control": "max-age=86400"})

        return self.send_error(404, "no cover art")

    def do_HEAD(self):
        self.do_GET()

    # ---- permanent delete -------------------------------------------------
    def _json_err(self, msg, code):
        return self._bytes(json.dumps({"error": msg}).encode("utf-8"),
                           "application/json; charset=utf-8", code)

    def do_POST(self):
        p = unquote(urlparse(self.path).path)
        if p == "/api/delete":
            return self._delete()
        if p == "/api/favourite":
            return self._favourite()
        if p == "/api/lyrics/save":
            return self._lyrics_save()
        if p == "/api/lyrics/sync":
            return self._lyrics_sync()
        if p == "/api/lyrics/forget":
            return self._lyrics_forget()
        self.send_error(404)

    def _body(self):
        """POST payload as a dict, or None if it isn't valid JSON."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            d = json.loads(raw.decode("utf-8") or "{}")
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    # ---- favourites --------------------------------------------------------
    def _favourite(self):
        req = self._body()
        if req is None:
            return self._json_err("malformed request", 400)
        path, on = req.get("path"), req.get("on")
        if not isinstance(on, bool):
            return self._json_err("'on' must be true or false", 400)
        if not _is_ours(path):
            return self._json_err("refusing: outside the configured music folders", 403)

        with DATA_LOCK:
            if on:
                FAVS[path] = True
            else:
                FAVS.pop(path, None)
            try:
                _dump_json(FAVS_FILE, FAVS)
            except OSError as e:
                if on:
                    FAVS.pop(path, None)
                return self._json_err("could not save: %s" % e, 500)
        return self._bytes(fav_json(), "application/json; charset=utf-8")

    # ---- lyrics ------------------------------------------------------------
    @staticmethod
    def _lyr_out(path, rec):
        if not rec:
            return {}
        return {"path": path, "text": rec["text"],
                "lines": list(rec["lines"]), "times": dict(rec["times"])}

    def _lyrics_save(self):
        """Paste/type lyrics in.  Blank text clears them."""
        req = self._body()
        if req is None:
            return self._json_err("malformed request", 400)
        path, text = req.get("path"), req.get("text")
        if not isinstance(text, str):
            return self._json_err("'text' must be a string", 400)
        if len(text) > MAX_LYRICS:
            return self._json_err("lyrics are too long", 400)
        if not _is_ours(path):
            return self._json_err("refusing: outside the configured music folders", 403)

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        with DATA_LOCK:
            prev = LYRICS.get(path)
            if not text.strip():
                LYRICS.pop(path, None)
            else:
                lines = _split_lines(text)
                LYRICS[path] = {"text": text, "lines": lines,
                                "times": _clean_times(prev, lines)}
            try:
                _dump_json(LYRICS_FILE, LYRICS)
            except OSError as e:
                if prev is None:
                    LYRICS.pop(path, None)
                else:
                    LYRICS[path] = prev
                return self._json_err("could not save: %s" % e, 500)
            out = self._lyr_out(path, LYRICS.get(path))
        return self._bytes(json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

    def _lyrics_sync(self):
        """Stamp (or with time:null, clear) the moment one line starts."""
        req = self._body()
        if req is None:
            return self._json_err("malformed request", 400)
        path, idx, t = req.get("path"), req.get("index"), req.get("time")
        if not isinstance(path, str) or not path:
            return self._json_err("bad path", 400)
        if not isinstance(idx, int) or isinstance(idx, bool):
            return self._json_err("'index' must be a line number", 400)
        if t is not None:
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                return self._json_err("'time' must be seconds or null", 400)
            if not (0 <= float(t) < 100000):
                return self._json_err("bad time", 400)

        with DATA_LOCK:
            rec = LYRICS.get(path)
            if rec is None:
                return self._json_err("save the lyrics first", 404)
            if not (0 <= idx < len(rec["lines"])):
                return self._json_err("no such line", 400)
            if not rec["lines"][idx].strip():
                return self._json_err("blank lines cannot be synced", 400)

            before = dict(rec["times"])
            if t is None:
                rec["times"].pop(str(idx), None)
            else:
                rec["times"][str(idx)] = round(float(t), 3)
            try:
                _dump_json(LYRICS_FILE, LYRICS)
            except OSError as e:
                rec["times"].clear()
                rec["times"].update(before)
                return self._json_err("could not save: %s" % e, 500)
            out = self._lyr_out(path, rec)
        return self._bytes(json.dumps(out, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

    def _lyrics_forget(self):
        req = self._body()
        if req is None:
            return self._json_err("malformed request", 400)
        path = req.get("path")
        if not isinstance(path, str) or not path:
            return self._json_err("bad path", 400)
        if req.get("confirm") is not True:
            return self._json_err("explicit confirmation required", 400)

        with DATA_LOCK:
            if path not in LYRICS:
                return self._json_err("no lyrics stored for this song", 404)
            prev = LYRICS.pop(path)
            try:
                _dump_json(LYRICS_FILE, LYRICS)
            except OSError as e:
                LYRICS[path] = prev
                return self._json_err("could not save: %s" % e, 500)
        return self._bytes(b"{}", "application/json; charset=utf-8")

    def _delete(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads((self.rfile.read(n) if n else b"{}").decode("utf-8") or "{}")
        except Exception:
            return self._json_err("malformed request", 400)

        i = req.get("i")
        if not isinstance(i, int) or isinstance(i, bool) or i < 0:
            return self._json_err("bad index", 400)
        if req.get("confirm") is not True:
            return self._json_err("explicit confirmation required", 400)

        with LIB_LOCK:
            if i >= len(LIBRARY):
                return self._json_err("library changed - reload the page", 409)
            song = LIBRARY[i]

            # 1) client must echo back the exact file it thinks it is deleting,
            #    so a stale page can never take out the wrong row.
            if req.get("path") != song["path"]:
                return self._json_err(
                    "library changed since this page loaded - reload the page", 409)

            # 2) refuse anything that isn't actually inside a music folder
            real = os.path.realpath(song["path"]).lower()
            roots = [d.lower() for d in _abs_dirs()]
            if not any(real == r or real.startswith(r + os.sep) for r in roots):
                return self._json_err(
                    "refusing: outside the configured music folders", 403)

            # 3) ... and only if it's a recognised audio file
            if os.path.splitext(real)[1].lower() not in AUDIO_EXT:
                return self._json_err("refusing: not an audio file", 403)

            if not os.path.isfile(real):
                return self._json_err("file is already gone - reload the page", 404)

            try:
                os.remove(real)
            except OSError as e:
                return self._json_err("could not delete: %s" % e, 500)

            del LIBRARY[i]
            payload = api_json()

        return self._bytes(payload, "application/json; charset=utf-8")


# ------------------------------------------------------------------ page ----
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8E%B5%3C/text%3E%3C/svg%3E">
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#0e1016">
<title>Music</title>
<style>
  :root{
    --bg:#0e1016; --card:#161a23; --card2:#1b2030; --line:#262c3b;
    --fg:#e6e9f0; --mut:#8b93a7; --acc:#5eead4; --acc2:#3e8dff;
    --now:#3ecf8e; --hot:#ff6b6b;
    --acc-r:94; --acc-g:234; --acc-b:212;
    --now-r:62; --now-g:207; --now-b:142;
    --bg-r:14; --bg-g:16; --bg-b:22;
    --card-r:22; --card-g:26; --card-b:35;
    transition:
      background 2s cubic-bezier(.25,1,.3,1), background-color 2s cubic-bezier(.25,1,.3,1), color 2s cubic-bezier(.25,1,.3,1),
      border-color 2s cubic-bezier(.25,1,.3,1), box-shadow 2s cubic-bezier(.25,1,.3,1);
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{
    background:var(--bg); color:var(--fg);
    font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif;
    display:flex; flex-direction:column;
    transition: background-color 2s cubic-bezier(.25,1,.3,1), color 2s cubic-bezier(.25,1,.3,1);
  }
  header{
    position:sticky; top:0; z-index:5;
    background:rgba(var(--bg-r),var(--bg-g),var(--bg-b),.94); backdrop-filter:blur(8px);
    border-bottom:1px solid var(--line); padding:14px 18px;
    display:flex; align-items:center; gap:14px; flex-wrap:wrap;
    transition: background 2s cubic-bezier(.25,1,.3,1), border-color 2s cubic-bezier(.25,1,.3,1);
  }
  h1{margin:0; font-size:17px; letter-spacing:.02em; display:flex; align-items:center; gap:8px}
  h1 .dot{width:9px;height:9px;border-radius:50%;background:var(--now);
    box-shadow:0 0 10px var(--now)}
  .spacer{flex:1}
  input[type=search]{
    background:var(--card); border:1px solid var(--line); color:var(--fg);
    border-radius:9px; padding:8px 12px; width:240px; outline:none; font-size:13px;
  }
  input[type=search]:focus{border-color:var(--acc2)}
  button{
    background:var(--card); border:1px solid var(--line); color:var(--fg);
    border-radius:9px; padding:8px 12px; cursor:pointer; font-size:13px;
    transition:.15s;
  }
  button:hover{border-color:var(--mut); background:var(--card2)}
  button.on{border-color:var(--acc); color:var(--acc); background:rgba(var(--acc-r),var(--acc-g),var(--acc-b),.08)}

  /* ---- mood toggle: All | <subfolder> | <subfolder> ----
     labels come from the folders themselves; hidden until you create one */
  .moods{display:flex; gap:3px; background:var(--card);
    border:1px solid var(--line); border-radius:11px; padding:3px}
  .moods[hidden]{display:none}
  .mood{background:transparent; border:none; border-radius:8px; padding:5px 13px;
    font-size:12.5px; color:var(--mut); white-space:nowrap}
  .mood:hover{background:var(--card2); color:var(--fg)}
  .mood.on{background:rgba(var(--acc-r),var(--acc-g),var(--acc-b),.14); color:var(--acc); font-weight:700}
  .mood.on:hover{background:rgba(var(--acc-r),var(--acc-g),var(--acc-b),.14); color:var(--acc)}
  .vtoggle.on{border-color:var(--acc); color:var(--acc);
    background:rgba(var(--acc-r),var(--acc-g),var(--acc-b),.08); font-weight:700}

  /* ---- sticky mini-player: bottom bar ---- */
  .deck{
    flex:0 0 auto;
    position:sticky; bottom:0; z-index:6;
    background:rgba(var(--card-r),var(--card-g),var(--card-b),.97);
    backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
    border-top:1px solid var(--line);
    padding:11px 18px;
    box-shadow:0 -14px 34px rgba(0,0,0,.5);
    transition: background 2s cubic-bezier(.25,1,.3,1), border-color 2s cubic-bezier(.25,1,.3,1);
  }
  .np{display:flex; align-items:center; gap:11px; margin-bottom:9px; min-height:40px}
  .np .art{
    width:40px; height:40px; flex:0 0 40px; border-radius:10px;
    background:linear-gradient(140deg, var(--acc) 0%, var(--acc2) 100%);
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 6px 18px rgba(0,0,0,.45), inset 0 1px 0 rgba(255,255,255,.18);
  }
  .np .art svg{width:21px; height:21px; fill:rgba(255,255,255,.92);
    filter:drop-shadow(0 1px 2px rgba(0,0,0,.35))}
  .np .art img{width:100%; height:100%; object-fit:cover; border-radius:10px}
  .np .art img[hidden], .np .art svg[hidden]{display:none}
  body:not(.paused) .np .art svg{animation:bob 1.7s ease-in-out infinite}
  @keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-2px)}}
  .np .info{flex:1 1 auto; min-width:0; display:flex; flex-direction:column; gap:1px}
  .np .t{font-size:14.5px; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .np .a{color:var(--mut); font-size:12.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .np .badge{flex:0 0 auto; font-size:11px; color:var(--mut);
    background:var(--card2); border:1px solid var(--line);
    padding:3px 8px; border-radius:20px; white-space:nowrap}
  #npFullLyr{display:none}          /* the lyrics toggle lives in fullscreen only */
  #npSrc:empty{display:none}        /* no floating empty pill when nothing is loaded */

  /* ---- now-playing fullscreen (Spotify-style) ---- */
  #npLyrs{display:none}
  .npLyrs .npLyrBody{padding:0 8px; overflow-y:auto; max-height:52vh;}
  body.np-fs .deck{
    position:fixed; top:0; right:0; bottom:0; left:0; height:100%; max-width:none;
    z-index:60; display:flex; flex-direction:column; padding:0;
    background:radial-gradient(140% 120% at 50% 36%, var(--card2) 0%, var(--bg) 78%);
    border-top:none; border-radius:0; }
  body.np-fs .deck .np{
    flex:1 1 auto; width:100%; max-width:1080px; min-height:0;
    display:flex; flex-direction:column; align-items:center; justify-content:center; gap:18px; }
  body.np-fs .deck .np .art{
    flex:0 0 auto; width:min(56vh,500px); height:min(56vh,500px); border-radius:18px;
    box-shadow:0 34px 100px rgba(0,0,0,.7); }
  body.np-fs .deck .np .info{flex:0 0 auto; text-align:center}
  body.np-fs .deck .np .badge{display:none}
  body.np-fs .deck .np .npb{display:none}
  body.np-fs .deck .np .fsToggles{
    position:absolute; top:26px; right:26px; display:flex; gap:10px; z-index:5; }
  body.np-fs .deck .np #npFS,
  body.np-fs .deck .np #npFullLyr{
    display:inline-flex; align-items:center; justify-content:center; gap:7px;
    background:rgba(var(--card-r),var(--card-g),var(--card-b),.72); border:1px solid var(--line);
    border-radius:12px; min-height:46px; padding:0 16px; white-space:nowrap; }
  body.np-fs .deck .seekrow{
    position:absolute; left:44px; right:44px;
    bottom:calc(104px + env(safe-area-inset-bottom,0px)); }
  body.np-fs .deck .bar{
    position:absolute; left:0; right:0; bottom:0;
    background:linear-gradient(transparent, rgba(0,0,0,.7));
    padding:34px 26px calc(26px + env(safe-area-inset-bottom,0px));
    gap:14px; flex-wrap:nowrap; }
  body.np-fs .deck .speed{display:none}
  body.np-fs.np-lyr #npLyrs{display:block}
  body.np-lyr #npLyrs .npLyrBody{max-height:56vh}
  body.np-lyr body.np-fs .deck .np{display:none}
  body.np-lyr body.np-fs .deck .bar{display:none}
  .bar{display:flex; align-items:center; gap:14px; flex-wrap:wrap}
  .ctrls{display:flex; gap:7px; align-items:center}
  .ctrls button{min-width:40px; font-size:15px; padding:7px 11px;
    transition:transform .13s, box-shadow .18s, background .15s, border-color .15s}
  .ctrls button:hover{transform:translateY(-1px)}
  .ctrls button:active{transform:translateY(0) scale(.96)}
  .ctrls .play{
    background:var(--acc); color:#000; border-color:var(--acc);
    font-weight:700; min-width:56px;
    box-shadow:0 3px 12px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.18);
  }
  .ctrls .play:hover{
    filter:brightness(1.1); transform:translateY(-2px) scale(1.07);
    box-shadow:0 0 20px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.6), 0 0 46px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.3);
  }
  .ctrls .play:active{transform:translateY(0) scale(.96)}
  body:not(.paused) .ctrls .play{
    box-shadow:0 0 15px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.45), 0 3px 12px rgba(0,0,0,.3);
  }
  .seekwrap{flex:1; min-width:220px; display:flex; align-items:center; gap:10px;
    position:relative}
  .time{font-variant-numeric:tabular-nums; font-size:12px; color:var(--mut); min-width:38px}
  /* floating readout while you scrub or arrow-nudge - milliseconds, because
     a lyric stamp lives or dies on a few hundredths of a second */
  .seektip{
    position:absolute; bottom:100%; margin-bottom:7px; transform:translateX(-50%);
    background:var(--card2); border:1px solid var(--acc); color:var(--fg);
    border-radius:8px; padding:4px 9px; font-size:12.5px; font-weight:700;
    font-variant-numeric:tabular-nums; white-space:nowrap; pointer-events:none;
    box-shadow:0 8px 24px rgba(0,0,0,.6); z-index:9;
  }
  .seektip[hidden]{display:none}
  .seektip b{font-weight:800; color:var(--acc)}
  .seektip i{font-style:normal; color:var(--mut); font-weight:700; font-size:11px;
    margin-left:8px; padding-left:8px; border-left:1px solid var(--line)}
  .seektip i:empty{display:none}
  .time.r{text-align:right}
  input[type=range]{
    -webkit-appearance:none; appearance:none; height:5px; border-radius:5px;
    outline:none; flex:1; cursor:pointer;
    /* --p = played, --b = buffered; both painted from JS */
    background:linear-gradient(to right,
      var(--acc) 0%, var(--acc) var(--p,0%),
      rgba(var(--acc-r),var(--acc-g),var(--acc-b),.34) var(--p,0%), rgba(var(--acc-r),var(--acc-g),var(--acc-b),.34) var(--b,0%),
      var(--card2) var(--b,0%), var(--card2) 100%);
  }
  input[type=range]::-webkit-slider-thumb{
    -webkit-appearance:none; width:14px; height:14px; border-radius:50%;
    background:var(--acc); cursor:pointer; box-shadow:0 0 8px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.5);
    transition:transform .15s ease, box-shadow .18s ease;
  }
  input[type=range]:hover::-webkit-slider-thumb{
    transform:scale(1.4);
    box-shadow:0 0 15px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.9), 0 0 30px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.35);
  }
  input[type=range]:active::-webkit-slider-thumb{transform:scale(1.6)}
  input[type=range]::-moz-range-thumb{
    width:14px;height:14px;border:none;border-radius:50%;background:var(--acc);
    box-shadow:0 0 8px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.5);
    transition:transform .15s ease, box-shadow .18s ease;
  }
  input[type=range]:hover::-moz-range-thumb{
    transform:scale(1.4);
    box-shadow:0 0 15px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.9), 0 0 30px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.35);
  }
  .vol{display:flex; align-items:center; gap:7px; width:130px}
  .vol input{width:90px}

  /* ---- list ---- */
  .meta{padding:9px 18px; color:var(--mut); font-size:12px;
    border-bottom:1px solid var(--line); display:flex; gap:12px; flex-wrap:wrap}
  .list{flex:1; overflow-y:auto; padding:6px 10px 18px;
    --cols:40px minmax(0,1.5fr) minmax(0,1fr) minmax(0,.8fr) 74px 126px}
  /* one single source == a column that says the same thing 344 times.
     Hide it and hand the width back to Title/Artist (auto-restores if you
     ever add a second folder or split Songz into subfolders). */
  .list.nosrc{--cols:40px minmax(0,1.7fr) minmax(0,1.2fr) 74px 126px}
  .list.nosrc .row .src{display:none}
  .list[hidden]{display:none}
  .row{
    display:grid; grid-template-columns:var(--cols); gap:14px;
    align-items:center; padding:7px 10px; border-radius:9px; cursor:pointer;
    border:1px solid transparent; border-left:3px solid transparent;
    transition:background .13s, border-color .13s, box-shadow .13s;
  }
  .row:hover{background:var(--card); border-color:var(--line); border-left-color:var(--line)}
  .row.playing{
    background:rgba(var(--now-r),var(--now-g),var(--now-b),.09);
    border-color:rgba(var(--now-r),var(--now-g),var(--now-b),.38);
    border-left-color:var(--now);
    box-shadow:0 0 20px rgba(var(--now-r),var(--now-g),var(--now-b),.14);
  }
  .row .n{color:var(--mut); font-size:12px; text-align:right; font-variant-numeric:tabular-nums}
  .row.playing .n{color:var(--now); font-weight:700}
  .row .ti{font-size:13.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .row .ar{color:var(--mut); font-size:12.5px; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis}
  .row.playing .ti{color:var(--now); font-weight:600}
  .row.playing .ar{color:var(--now)}
  .row .src{color:var(--mut); font-size:11.5px; opacity:.68; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis}
  .row .sz{color:var(--mut); font-size:11.5px; font-variant-numeric:tabular-nums;
    text-align:right}
  .row .dim{opacity:.4}
  /* queue + lyrics + favourite + delete — the first three are revealed on
     hover so 276 rows stay clean, but a favourited row keeps its heart lit */
  .row .acts{
    display:flex; align-items:center; justify-content:flex-end; gap:4px;
  }
  .row .acts .add, .row .acts .del, .row .acts .lyrics{
    opacity:0; transition:opacity .13s;
  }
  .row.playing .acts .add, .row.playing .acts .del, .row.playing .acts .lyrics{opacity:.6}
  .row:hover .acts .add, .row:hover .acts .del, .row:hover .acts .lyrics{opacity:1}
  .row .acts .fav{opacity:0; transition:opacity .13s, color .13s}
  .row.playing .acts .fav{opacity:0}
  .row .acts .fav.on{opacity:1; color:#ff6b6b}
  .row:hover .acts .fav{opacity:1}
  .acts button{
    width:26px; height:26px; padding:0; border-radius:7px;
    display:flex; align-items:center; justify-content:center;
    background:transparent; border:1px solid transparent; color:var(--mut);
    font-size:15px; line-height:1; font-weight:700;
    transition:color .13s, background .13s, border-color .13s;
  }
  .acts .del svg{width:15px; height:15px; display:block}
  .acts .del:hover{color:#ff6b6b; border-color:rgba(255,107,107,.55);
    background:rgba(255,107,107,.13)}
  .acts .add:hover{color:var(--acc); border-color:rgba(var(--acc-r),var(--acc-g),var(--acc-b),.5);
    background:rgba(var(--acc-r),var(--acc-g),var(--acc-b),.12)}
  .acts .add.on{color:var(--acc); border-color:rgba(var(--acc-r),var(--acc-g),var(--acc-b),.45);
    background:rgba(var(--acc-r),var(--acc-g),var(--acc-b),.1)}
  .acts .lyrics:hover{color:var(--acc2); border-color:rgba(62,141,255,.55);
    background:rgba(62,141,255,.13)}
  .acts .lyrics.on{color:var(--acc2); border-color:rgba(62,141,255,.5);
    background:rgba(62,141,255,.12)}
  .acts .fav:hover{color:#ff6b6b; border-color:rgba(255,107,107,.55);
    background:rgba(255,107,107,.13)}
  .acts .fav.on{border-color:rgba(255,107,107,.5); background:rgba(255,107,107,.12)}

  /* ---- now-playing: heart + lyrics shortcut, and the live lyric line ---- */
  .npb{flex:0 0 auto; font-size:12.5px; padding:6px 12px; color:var(--mut)}
  .npb[disabled]{opacity:.35; cursor:default}
  .npb[disabled]:hover{border-color:var(--line); background:var(--card)}
  .npb.on{color:#ff6b6b; border-color:rgba(255,107,107,.5);
    background:rgba(255,107,107,.12)}
  .lyline{
    color:var(--fg); font-size:13px; line-height:1.35; padding:0 0 9px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    border-left:2px solid var(--acc); padding-left:9px; margin-bottom:2px;
  }
  .lyline[hidden]{display:none}

  /* ---- lyrics sheet: replaces the list, deck + header stay usable ---- */
  .lyrsheet{flex:1; overflow-y:auto; padding:14px 18px 34px}
  .lyrsheet[hidden]{display:none}
  .lhead{display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:14px}
  .lhead .back{font-size:13px}
  .lname{display:flex; flex-direction:column; min-width:0; gap:1px}
  .lname b{font-size:15px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .lname span{color:var(--mut); font-size:12.5px; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis}
  .lstep{
    font-size:12px; font-weight:700; letter-spacing:.09em; text-transform:uppercase;
    color:var(--acc); margin:2px 0 10px;
  }
  textarea#lyrText{
    width:100%; min-height:46vh; background:var(--card); border:1px solid var(--line);
    color:var(--fg); border-radius:11px; padding:13px 15px; outline:none; resize:vertical;
    font:14px/1.75 ui-monospace,Consolas,"Courier New",monospace;
  }
  textarea#lyrText:focus{border-color:var(--acc2)}
  textarea#lyrText[disabled]{opacity:.5}
  .lhint{color:var(--mut); font-size:12px; margin:9px 0 0}
  .lact{display:flex; gap:9px; justify-content:flex-end; margin-top:14px}
  button.pri{
    background:var(--acc); border-color:var(--acc); color:#06211c; font-weight:700;
  }
  button.pri:hover{background:#7df3e0; border-color:#7df3e0;
    box-shadow:0 0 16px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.4)}
  button.pri.on{background:var(--card2); color:var(--acc); border-color:var(--acc)}
  .lbar{
    display:flex; align-items:center; gap:9px; flex-wrap:wrap;
    padding:10px 12px; background:var(--card); border:1px solid var(--line);
    border-radius:11px;
  }
  .lstat{color:var(--mut); font-size:12.5px; font-variant-numeric:tabular-nums}

  .syncbar{
    position:sticky; top:0; z-index:3;
    display:flex; align-items:center; gap:18px; flex-wrap:wrap;
    margin-top:11px; padding:13px 15px;
    background:var(--card2); border:1px solid var(--acc);
    border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,.5);
  }
  .syncbar[hidden]{display:none}
  .sleft{display:flex; flex-direction:column; gap:2px}
  .snow{font-size:27px; font-weight:800; line-height:1;
    color:var(--acc); font-variant-numeric:tabular-nums}
  .ssub{font-size:11.5px; color:var(--mut); font-variant-numeric:tabular-nums}
  .smid{flex:1; min-width:220px; display:flex; flex-direction:column; gap:4px}
  .ssel{font-size:13.5px; font-weight:600; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis}
  .shint{font-size:11.5px; color:var(--mut)}
  .shint b{color:var(--acc)}
  .sspeed{
    flex:1 0 100%; display:flex; align-items:center; gap:6px; flex-wrap:wrap;
    border-top:1px solid var(--line); padding-top:10px;
  }
  .sspeed .lbl{font-size:10.5px; font-weight:800; letter-spacing:.1em;
    text-transform:uppercase; color:var(--mut); margin-right:4px}
  .sspeed .shint{margin-left:7px}
  #stamp{
    font-size:17px; font-weight:800; padding:15px 32px; letter-spacing:.05em;
    box-shadow:0 5px 22px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.35);
  }
  #stamp:hover{box-shadow:0 0 26px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.7), 0 0 60px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.3)}
  #stamp:active{transform:scale(.97)}
  #stamp[disabled]{opacity:.45; cursor:default; box-shadow:none}
  #stamp[disabled]:hover{box-shadow:none}

  .llines{
    margin-top:11px; border:1px solid var(--line); border-radius:11px;
    overflow:hidden; background:var(--card);
  }
  .ln{
    display:grid; grid-template-columns:66px 1fr; gap:12px; align-items:baseline;
    padding:6px 13px; cursor:pointer; font-size:13.5px;
    border-bottom:1px solid rgba(38,44,59,.75);
    transition:background .1s;
  }
  .ln:last-child{border-bottom:none}
  .ln .lt{color:var(--mut); font-size:12px; text-align:right;
    font-variant-numeric:tabular-nums}
  .ln .lx{white-space:pre-wrap; overflow-wrap:anywhere}
  .ln:hover{background:var(--card2)}
  .ln.hit .lt{color:var(--acc); font-weight:700}
  .ln.sel{background:rgba(var(--acc-r),var(--acc-g),var(--acc-b),.16); outline:1px solid var(--acc);
    outline-offset:-1px}
  .ln.sel .lx{font-weight:700}
  .ln.cur{background:rgba(var(--now-r),var(--now-g),var(--now-b),.18); box-shadow:inset 3px 0 0 var(--now)}
  .ln.gap{height:13px; padding:0; cursor:default; background:transparent}
  .ln.gap:hover{background:transparent}

  /* ---- delete confirmation ---- */
  .modal{position:fixed; inset:0; z-index:50; padding:20px;
    background:rgba(0,0,0,.62); backdrop-filter:blur(3px);
    display:flex; align-items:center; justify-content:center}
  .modal[hidden]{display:none}
  .sheet{background:var(--card); border:1px solid var(--line); border-radius:14px;
    width:min(540px,100%); padding:18px 20px;
    box-shadow:0 24px 64px rgba(0,0,0,.65)}
  .mhead{font-size:16px; font-weight:700; margin-bottom:14px}
  .msong .mt{display:block; font-size:14.5px; font-weight:600;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .msong .ma{display:block; color:var(--mut); font-size:13px; margin-top:2px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis}
  .mpath{margin-top:12px; font-size:11.5px; color:var(--mut); background:var(--bg);
    border:1px solid var(--line); border-radius:8px; padding:8px 10px;
    word-break:break-all; font-family:ui-monospace,Consolas,monospace}
  .msize{margin-top:8px; font-size:12px; color:var(--mut)}
  .mwarn{margin-top:14px; font-size:12.5px; color:#ffc0c0;
    background:rgba(255,107,107,.1); border:1px solid rgba(255,107,107,.32);
    border-radius:8px; padding:9px 11px}
  .mact{display:flex; gap:9px; justify-content:flex-end; margin-top:16px}
  #dGo{background:#ff5c5c; border-color:#ff5c5c; color:#2b0505; font-weight:700}
  #dGo:hover{background:#ff7373; border-color:#ff7373;
    box-shadow:0 0 18px rgba(255,92,92,.45)}
  #dGo[disabled]{opacity:.6; cursor:progress}
  .eq{display:none; gap:2px; align-items:flex-end; height:13px}
  .row.playing .eq{display:flex}
  .row.playing .num{display:none}
  .eq i{width:3px; background:var(--now); border-radius:2px; animation:b .9s infinite ease-in-out}
  .eq i:nth-child(1){height:5px; animation-delay:0s}
  .eq i:nth-child(2){height:12px; animation-delay:.2s}
  .eq i:nth-child(3){height:8px; animation-delay:.4s}
  .eq i:nth-child(4){height:11px; animation-delay:.1s}
  @keyframes b{0%,100%{transform:scaleY(.4)}50%{transform:scaleY(1)}}
  body.paused .eq i{animation-play-state:paused}
  /* ---- up-next chip (click to clear) ---- */
  .qchip{display:flex; align-items:center; gap:7px; cursor:pointer;
    background:var(--card2); border:1px solid var(--line); border-radius:20px;
    padding:5px 11px; font-size:11.5px; color:var(--mut); white-space:nowrap;
    transition:border-color .15s, color .15s}
  .qchip[hidden]{display:none}
  .qchip b{color:var(--acc); font-weight:700; font-variant-numeric:tabular-nums}
  .qchip .qx{font-size:13px}
  .qchip:hover{border-color:var(--mut); color:var(--fg)}

  /* ---- group-by-artist ---- */
  .grp{margin:5px 4px}
  .grp summary{
    list-style:none; cursor:pointer; display:flex; align-items:center; gap:9px;
    padding:8px 13px; border-radius:9px; background:var(--card);
    border:1px solid var(--line); font-weight:700; font-size:13px;
  }
  .grp summary::-webkit-details-marker{display:none}
  .grp summary::before{content:"\25B8"; color:var(--mut); font-size:11px;
    transition:transform .15s}
  .grp[open] summary::before{transform:rotate(90deg)}
  .grp summary:hover{border-color:var(--mut); background:var(--card2)}
  .grp .gcount{margin-left:auto; color:var(--mut); font-weight:400; font-size:11.5px}
  .grp .grows{padding-top:4px}

  .empty{padding:40px; text-align:center; color:var(--mut)}
  ::-webkit-scrollbar{width:10px}
  ::-webkit-scrollbar-thumb{background:#2a3141; border-radius:10px}
  ::-webkit-scrollbar-track{background:transparent}
  kbd{background:var(--card2); border:1px solid var(--line); border-radius:4px;
    padding:1px 5px; font-size:11px; font-family:inherit}
  .help{color:var(--mut); font-size:11.5px}


  /* ---- visualizer & limiter ---- */
  #viz{position:fixed; inset:0; z-index:0; pointer-events:none}

  /* ---- Bar Reactivity Modal ---- */
  #barModal{position:fixed; top:58px; right:12px; width:285px; max-height:calc(100vh - 80px); overflow-y:auto; z-index:35;
    background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:16px; box-shadow:0 12px 42px rgba(0,0,0,.6); display:none}
  #barModal.on{display:block}
  .bar-seg{display:flex; border:1px solid var(--line); border-radius:8px; overflow:hidden; margin-bottom:14px}
  .bar-seg button{flex:1; background:var(--card2); border:none; color:var(--mut); padding:7px 4px; font-size:12px; font-weight:700; cursor:pointer; border-radius:0}
  .bar-seg button+button{border-left:1px solid var(--line)}
  .bar-seg button.on{background:var(--acc); color:#000; font-weight:800}

  #knobs{position:fixed; top:58px; right:12px; width:260px; max-height:calc(100vh - 80px); overflow-y:auto; z-index:20;
    background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:14px; box-shadow:0 10px 40px rgba(0,0,0,.5); display:none}
  #knobs.on{display:block}
  .knob{margin-bottom:12px}
  .kh{display:flex; justify-content:space-between; font-size:11px; color:var(--mut)}
  .knob input{width:100%}
  .lim{font-variant-numeric:tabular-nums; font-size:12px; color:var(--mut); min-width:84px; text-align:right}
  .lim.hot{color:var(--hot)}
  .ksec{font-size:11px; font-weight:700; color:var(--acc); text-transform:uppercase; letter-spacing:.06em; margin:14px 0 8px; border-top:1px solid var(--line); padding-top:10px}
  .ksec:first-child{margin-top:0; border-top:none; padding-top:0}
  select.kselect{width:100%; background:var(--card2); border:1px solid var(--line); color:var(--fg); border-radius:8px; padding:6px 9px; font-size:12px; outline:none; margin-bottom:10px}

  /* ---- up next drawer ---- */
  .qdrawer{position:fixed; bottom:74px; right:16px; width:340px; max-width:calc(100vw - 32px);
    max-height:55vh; background:var(--card); border:1px solid var(--line); border-radius:12px;
    box-shadow:0 16px 48px rgba(0,0,0,.65); z-index:25; display:flex; flex-direction:column; overflow:hidden}
  .qdrawer[hidden]{display:none}
  .qdhead{display:flex; align-items:center; gap:8px; padding:10px 14px; border-bottom:1px solid var(--line); background:var(--card2)}
  .qdlist{overflow-y:auto; flex:1; padding:6px 0}
  .qdrow{display:flex; align-items:center; gap:10px; padding:7px 12px; font-size:12.5px; cursor:pointer}
  .qdrow:hover{background:var(--card2)}
  .qdrow .qti{flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .qdrow .qti b{display:block; font-size:13px; color:var(--fg); overflow:hidden; text-overflow:ellipsis}
  .qdrow .qti span{color:var(--mut); font-size:11.5px}
  .qdrow .qdel{color:var(--mut); background:transparent; border:none; padding:4px 7px; border-radius:6px; cursor:pointer; font-size:12px}
  .qdrow .qdel:hover{color:var(--hot); background:rgba(255,107,107,.15)}
  .qempty{padding:20px; text-align:center; color:var(--mut); font-size:12px}

  /* ---- Fullscreen Now Playing Modal ---- */
  #fsnp{
    position:fixed; inset:0; z-index:70; background:rgba(var(--bg-r),var(--bg-g),var(--bg-b),.96);
    backdrop-filter:blur(24px); -webkit-backdrop-filter:blur(24px);
    display:flex; flex-direction:column; overflow:hidden;
    transition:transform .28s cubic-bezier(.16,1,.3,1), opacity .25s ease;
    transform:translateY(100%); opacity:0; pointer-events:none;
  }
  #fsnp.on{transform:translateY(0); opacity:1; pointer-events:auto}
  .fsbg{
    position:absolute; inset:-20%; z-index:-1; filter:blur(60px) brightness(.32) saturate(1.6);
    background-size:cover; background-position:center; transform:scale(1.15); opacity:.75;
  }
  .fshead{display:flex; align-items:center; justify-content:space-between; padding:18px 24px; flex-shrink:0}
  .fshead .fsclose{background:rgba(255,255,255,.08); border:none; border-radius:50%; width:36px; height:36px; font-size:20px; color:var(--fg); cursor:pointer; display:flex; align-items:center; justify-content:center}
  .fshead .fsclose:hover{background:rgba(255,255,255,.16)}
  .fssub{font-size:11px; text-transform:uppercase; letter-spacing:.12em; color:var(--mut); font-weight:700; text-align:center}
  .fstabs{display:flex; gap:6px; background:rgba(255,255,255,.07); border-radius:18px; padding:3px}
  .fstab{background:transparent; border:none; border-radius:15px; padding:4px 12px; font-size:12px; color:var(--mut); cursor:pointer}
  .fstab.on{background:var(--card); color:var(--fg); font-weight:700}

  .fsbody{flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:16px 24px; overflow:hidden; position:relative}

  /* Tab 1: Art view */
  .fsartwrap{display:flex; flex-direction:column; align-items:center; justify-content:center; width:100%; max-width:440px}
  .fsart{
    width:min(68vw, 340px); height:min(68vw, 340px); border-radius:20px;
    overflow:hidden; box-shadow:0 24px 70px rgba(0,0,0,.75), 0 4px 16px rgba(0,0,0,.4);
    background:linear-gradient(140deg, var(--acc) 0%, var(--acc2) 100%);
    display:flex; align-items:center; justify-content:center; margin-bottom:28px;
  }
  .fsart img{width:100%; height:100%; object-fit:cover}
  .fsart svg{width:80px; height:80px; fill:rgba(255,255,255,.85)}
  .fsmeta{width:100%; display:flex; align-items:flex-start; justify-content:space-between; gap:14px}
  .fsmeta .fstitle{font-size:22px; font-weight:800; line-height:1.2; word-break:break-word}
  .fsmeta .fsartist{font-size:16px; color:var(--mut); margin-top:4px}
  .fsfav{font-size:24px; background:transparent; border:none; cursor:pointer; color:var(--mut); padding:4px}
  .fsfav.on{color:var(--hot)}

  /* Tab 2: Karaoke Lyrics view */
  .fslyrwrap{width:100%; max-width:620px; height:100%; overflow-y:auto; padding:30px 10px; scroll-behavior:smooth}
  .fslyrwrap::-webkit-scrollbar{display:none}
  .fsln{font-size:22px; font-weight:600; line-height:1.8; color:rgba(255,255,255,.35); cursor:pointer; padding:6px 12px; border-radius:10px; transition:all .25s ease}
  .fsln:hover{color:rgba(255,255,255,.75)}
  .fsln.active{color:var(--acc); font-size:28px; font-weight:800; transform:scale(1.03); transform-origin:left center; text-shadow:0 0 24px rgba(var(--acc-r),var(--acc-g),var(--acc-b),.45)}
  .fsln.gap{height:16px}

  /* Footer controls */
  .fsfoot{padding:14px 24px 34px; width:100%; max-width:540px; margin:0 auto; flex-shrink:0}
  .fsseek{width:100%; margin-bottom:8px}
  .fstime{display:flex; justify-content:space-between; font-size:12px; color:var(--mut); font-variant-numeric:tabular-nums; margin-top:4px}
  .fsctrls{display:flex; align-items:center; justify-content:space-between; margin-top:16px}
  .fsctrls button{background:transparent; border:none; color:var(--fg); font-size:22px; cursor:pointer; padding:8px 12px; border-radius:50%}
  .fsctrls .fsplay{background:var(--fg); color:#0b0e14; width:64px; height:64px; font-size:26px; border-radius:50%; display:flex; align-items:center; justify-content:center; box-shadow:0 8px 30px rgba(255,255,255,.25)}
  .fsctrls .fsplay:hover{transform:scale(1.06)}
  .fsctrls button.on{color:var(--acc)}

  /* ---- speed slider (sync sheet) ---- */
  .sspeed .spval{font-size:13px; font-weight:800; color:var(--acc);
    min-width:58px; text-align:right; font-variant-numeric:tabular-nums}
  #speed{flex:1 1 240px; min-width:150px}
  #speedNum{
    background:var(--card); border:1px solid var(--line); color:var(--fg);
    border-radius:9px; padding:6px 9px; width:74px; outline:none;
    font-size:13px; font-variant-numeric:tabular-nums;
  }
  #speedNum:focus{border-color:var(--acc2)}
  #speedNum::-webkit-outer-spin-button,#speedNum::-webkit-inner-spin-button{
    -webkit-appearance:none; margin:0}
  #speedNum{-moz-appearance:textfield}

  /* ---- phones: one column, thumb-sized targets --------------------------
     The desktop grid crams seven columns into ~390px, so on a phone the row
     collapses to title/artist on the left with duration and the action
     buttons stacked on the right. Desktop is untouched - this only applies
     under 780px wide. */
  @media (max-width:780px){
    header{padding:10px 12px; flex-wrap:wrap; gap:8px}
    header h1{font-size:17px}
    .spacer{display:none}
    .moods{order:3; width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch;
      padding-bottom:2px; scrollbar-width:none}
    .moods::-webkit-scrollbar{display:none}
    .mood{min-height:34px}                       /* taller = tappable */
    .vtoggle{min-height:34px; padding:0 11px}

    .meta{padding:8px 12px; gap:9px}

    /* two-line row instead of the 7-column grid */
    .list{--cols:minmax(0,1fr) auto}
    .list.nosrc{--cols:minmax(0,1fr) auto}
    .row{padding:9px 4px; gap:10px; align-items:center}
    .row .n{display:none}                        /* track number: no room */
    .row .ar{display:none}                       /* folded under the title */
    .row .src{display:none}
    .row .sz{display:none}
    .row .ti{font-size:14.5px}
    .row .ti::after{content:attr(data-artist); display:block; font-size:12px;
      color:var(--mut); font-weight:400; margin-top:1px; overflow:hidden;
      text-overflow:ellipsis; white-space:nowrap}
    .row .acts{gap:7px}
    .acts button{min-width:44px; min-height:44px}  /* iOS tap-target minimum */

    /* now-playing deck: full-width controls, bigger hit areas */
    .deck{padding:9px 11px; gap:9px; grid-template-columns:auto minmax(0,1fr) auto}
    .deck .np{min-width:0}
    .npb{min-width:46px; min-height:46px}
    .ctrls button{min-width:48px; min-height:48px}  /* thumb-sized transport */
    input[type=range]{height:34px}        /* fat seek bar: no precision tapping */
    /* the title opens the same sheet, so the standalone lyrics button only
       duplicates it on a phone - keep the favourite heart */
    #npLyrics{display:none}
    .info{cursor:pointer}
    .info .t{font-size:13.5px}
    #play{min-width:50px; min-height:50px; font-size:17px}
    .time{font-size:11.5px; min-width:34px}

    /* help text and keyboard hints are desktop-only */
    .help, kbd{display:none}

    /* knobs panel becomes a bottom sheet so it isn't off-screen */
    #knobs{top:auto; right:8px; left:8px; bottom:78px; width:auto}

    .lyrsheet textarea{min-height:52vh}
  }
  /* short phones (landscape or small) keep the deck reachable */
  @media (max-width:780px) and (max-height:520px){
    .lyrsheet textarea{min-height:34vh}
  }
</style>
</head>
<body class="paused">

<canvas id="viz" aria-hidden="true"></canvas>

<header>
  <h1><span class="dot"></span>Music</h1>
  <div class="moods" id="moods" hidden></div>
  <div class="spacer"></div>
  <button id="favfilter" class="vtoggle" type="button"
          title="Show only your favourites">&#9829; Favourites</button>
  <button id="group" class="vtoggle" type="button"
          title="Group the list by artist">&#9776; Artist</button>
  <input type="search" id="q" placeholder="Search songs…  ( / )" autocomplete="off">
  <button id="rescan" title="Rescan the music folders">&#8635; Rescan</button>
  <button id="barbtn" title="Tune reactivity of the 3 background bars">⚡ Bars</button>
  <button id="knobbtn">Knobs</button>
  <button id="limon" class="on">Limiter: on</button>
  <span class="lim" id="limrd">lim 0.0 dB</span>
</header>

<aside id="barModal">
  <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px">
    <b style="font-size:13px; color:var(--fg); display:flex; align-items:center; gap:6px">⚡ Bar Reactivity</b>
    <button id="barClose" style="background:none; border:none; color:var(--mut); font-size:16px; cursor:pointer; padding:0 4px">&times;</button>
  </div>
  <div style="font-size:11px; color:var(--mut); margin-bottom:8px; text-transform:uppercase; font-weight:700">Select Bar to Edit:</div>
  <div class="bar-seg" id="barPick">
    <button data-b="0" class="on">1: Low (Bass)</button>
    <button data-b="1">2: Mid</button>
    <button data-b="2">3: High</button>
  </div>
  <div id="barControls"></div>
</aside>

<aside id="knobs">
  <div id="kbody"></div>
</aside>

<!-- Up Next Drawer -->
<div id="qdrawer" class="qdrawer" hidden>
  <div class="qdhead">
    <b>Up Next (<span id="qdcount">0</span>)</b>
    <span class="spacer"></span>
    <button id="qdclear" type="button" style="padding:4px 9px; font-size:12px">Clear All</button>
    <button id="qdclose" class="back" type="button" style="padding:4px 9px; font-size:12px">&#10005;</button>
  </div>
  <div id="qdlist" class="qdlist"></div>
</div>

<div class="meta">
  <span id="count">loading…</span>
  <span class="help">
    <kbd>Space</kbd> play &nbsp; <kbd>&#8592;</kbd><kbd>&#8594;</kbd> seek 5s &nbsp;
    <kbd>&#8593;</kbd><kbd>&#8595;</kbd> volume &nbsp;
    <kbd>N</kbd>/<kbd>P</kbd> next/prev &nbsp; <kbd>/</kbd> search
  </span>
</div>

<div class="list" id="list"></div>

<!-- ===== lyrics: step 1 paste, step 2 sync ===== -->
<div class="lyrsheet" id="lyr" hidden>
  <div class="lhead">
    <button id="lyrBack" class="back" type="button">&#8592; Songs</button>
    <div class="lname"><b id="lyrT"></b><span id="lyrA"></span></div>
    <span class="spacer"></span>
    <button id="lyrPlay" type="button">&#9654; Play this song</button>
    <span class="badge" id="lyrDur">&ndash;:&ndash;&ndash;</span>
  </div>

  <div id="lyrEdit">
    <div class="lstep">Step 1 &mdash; paste the lyrics</div>
    <textarea id="lyrText" spellcheck="false" autocomplete="off"
      placeholder="Paste the lyrics here…&#10;One line of text per line of the song."></textarea>
    <p class="lhint">Leave a blank line wherever you want a gap. Blank lines can&rsquo;t be
      timed &mdash; every other line can.</p>
    <div class="lact">
      <button id="lyrCancel" type="button">Cancel</button>
      <button id="lyrFetchBtn" type="button" title="Auto-fetch lyrics online">&#9889; Auto-fetch</button>
      <button id="lyrImportBtn" type="button" title="Load a .lrc or .txt file">&#128194;&#65038; Import .lrc</button>
      <input type="file" id="lyrFileInput" accept=".lrc,.txt" hidden>
      <button id="lyrSave" class="pri" type="button">Save lyrics</button>
    </div>
  </div>

  <div id="lyrView" hidden>
    <div class="lstep">Step 2 &mdash; press Sync, then time each line</div>
    <div class="lbar">
      <button id="lyrEditBtn" type="button">&#9998; Edit lyrics</button>
      <button id="lyrSyncBtn" class="pri" type="button">&#9201; Sync</button>
      <button id="lyrExportBtn" type="button" title="Download .lrc file">&#128190;&#65038; Export .lrc</button>
      <button id="lyrForget" type="button">Delete lyrics</button>
      <span class="spacer"></span>
      <span class="lstat" id="lyrStat"></span>
    </div>

    <div class="syncbar" id="syncBar" hidden>
      <div class="sleft">
        <div class="snow" id="sNow">0:00.000</div>
        <div class="ssub">of <span id="sDur">&ndash;:&ndash;&ndash;</span>
          &middot; <span id="sProg"></span></div>
      </div>
      <div class="smid">
        <div class="ssel" id="sSel">Click a line below to select it</div>
        <div class="shint">1. click a line &nbsp;&rarr;&nbsp; 2. let it play &nbsp;&rarr;&nbsp;
          3. hit <b>SYNC</b> the instant it starts</div>
      </div>
      <button id="stamp" class="pri big" type="button" disabled>&#9201; SYNC</button>
      <button id="syncDone" type="button">Done</button>
      <div class="sspeed" id="sspeed">
        <span class="lbl">Speed</span>
        <input type="range" id="speed" min="0.1" max="2" step="0.01" value="1"
               aria-label="Playback speed" title="Slide to slow down or speed up">
        <input type="number" id="speedNum" min="0.1" max="2" step="0.01" value="1"
               aria-label="Exact playback speed" title="Type an exact speed">
        <span class="spval" id="spVal">1.00&times;</span>
        <span class="shint">Below 1&times; the pitch drops with the speed, so it stays
          deep and clear instead of thin. Above 1&times; the pitch is kept. Your
          timestamps are always real song time.</span>
      </div>
    </div>

    <div class="llines" id="lyrLines"></div>
  </div>
</div>

<div class="deck">
  <div class="np">
    <div class="art" aria-hidden="true">
      <img id="art" alt="" hidden>
      <svg id="artGlyph" viewBox="0 0 24 24"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>
    </div>
    <div class="info" id="info" title="Lyrics and sync">
      <span class="t" id="npTitle">Nothing playing</span>
      <span class="a" id="npArtist"></span>
    </div>
    <span class="badge" id="npSrc"></span>
    <button id="npFav" class="npb" type="button" disabled
            title="Favourite this song">&#9825;</button>
    <button id="npLyrics" class="npb" type="button" disabled
            title="Add or edit lyrics">&#9834; Lyrics</button>
    <div class="fsToggles">
      <button id="npFS" class="npb" type="button" title="Fullscreen now playing">&#10530;</button>
      <button id="npFullLyr" class="npb" type="button" title="Lyrics">&#9834; Lyrics</button>
    </div>
  </div>
  <div class="npLyrs" id="npLyrs">
    <div class="npLyrBody"></div>
  </div>
  <div class="lyline" id="lyLine" hidden></div>
  <div class="bar">
    <div class="ctrls">
      <button id="shuffle" title="Shuffle">&#128256;&#65038;</button>
      <button id="prev" title="Previous (P)">&#9198;</button>
      <button id="play" class="play" title="Play / Pause (Space)">&#9654;</button>
      <button id="next" title="Next (N)">&#9197;</button>
      <button id="repeat" title="Repeat">&#128257;&#65038;</button>
    </div>
    <span class="qchip" id="qchip" hidden title="Click to clear the queue">
      Up next <b id="qn">0</b> <span class="qx">&#10005;</span>
    </span>
    <div class="seekwrap" id="seekwrap">
      <span class="time" id="cur">0:00</span>
      <input type="range" id="seek" min="0" max="1000" value="0">
      <span class="time r" id="dur">0:00</span>
      <div class="seektip" id="seekTip" hidden><b id="tipT">0:00.000</b><i id="tipD"></i></div>
    </div>
    <div class="vol">
      <span title="Volume" style="font-size:15px">&#128266;&#65038;</span>
      <input type="range" id="vol" min="0" max="100" value="85">
    </div>
  </div>
</div>

<div class="modal" id="modal" hidden>
  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="mHead">
    <div class="mhead" id="mHead">Delete permanently?</div>
    <div class="msong">
      <span class="mt" id="dTitle"></span>
      <span class="ma" id="dArtist"></span>
    </div>
    <div class="mpath" id="dPath"></div>
    <div class="msize" id="dSize"></div>
    <div class="mwarn">&#9888; This removes the file from your disk. It cannot be undone.</div>
    <div class="mact">
      <button id="dCancel" type="button">Cancel</button>
      <button id="dGo" type="button">Delete permanently</button>
    </div>
  </div>
</div>

<!-- Fullscreen Now Playing View -->
<div id="fsnp">
  <div class="fsbg" id="fsbg"></div>
  <div class="fshead">
    <button class="fsclose" id="fsclose" type="button" title="Close">&#8964;</button>
    <div class="fssub" id="fssub">NOW PLAYING</div>
    <div class="fstabs">
      <button class="fstab on" id="tabArt" type="button">Art</button>
      <button class="fstab" id="tabLyr" type="button">Lyrics</button>
    </div>
  </div>
  <div class="fsbody">
    <!-- Art View -->
    <div class="fsartwrap" id="fsArtWrap">
      <div class="fsart">
        <img id="fsImg" alt="" hidden>
        <svg id="fsGlyph" viewBox="0 0 24 24"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>
      </div>
      <div class="fsmeta">
        <div style="flex:1; min-width:0">
          <div class="fstitle" id="fsTitle">Nothing playing</div>
          <div class="fsartist" id="fsArtist"></div>
        </div>
        <button class="fsfav" id="fsFav" type="button" title="Favourite">&#9825;</button>
      </div>
    </div>
    <!-- Karaoke Lyrics View -->
    <div class="fslyrwrap" id="fsLyrWrap" hidden>
      <div id="fsLyrList"></div>
    </div>
  </div>
  <div class="fsfoot">
    <div class="fsseek">
      <input type="range" id="fsSeekSlider" min="0" max="1000" value="0">
      <div class="fstime">
        <span id="fsCur">0:00</span>
        <span id="fsDur">0:00</span>
      </div>
    </div>
    <div class="fsctrls">
      <button id="fsShuffle" type="button" title="Shuffle">&#128256;&#65038;</button>
      <button id="fsPrev" type="button" title="Previous">&#9198;</button>
      <button id="fsPlay" class="fsplay" type="button" title="Play / Pause">&#9654;</button>
      <button id="fsNext" type="button" title="Next">&#9197;</button>
      <button id="fsRepeat" type="button" title="Repeat">&#128257;&#65038;</button>
    </div>
  </div>
</div>

<audio id="au" preload="metadata"></audio>
<audio id="au2" preload="metadata"></audio>

<script>
"use strict";
var LIB=[], order=[], pos=-1, shuffle=false, repeat=0; // 0 off, 1 all, 2 one
var MOOD="", MOODS=[], GROUP=false, QUEUE=[], FOLDERS=0;
var FAV={}, FAVONLY=false, LYRHAVE={}, LYRSYNC={};   // filled by the API
var au=document.getElementById("au");
var $=function(id){return document.getElementById(id);};
var TRASH='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" '+
          'stroke-linecap="round" stroke-linejoin="round">'+
          '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>';

function fmt(s){ if(!isFinite(s)||s<0)s=0; var m=Math.floor(s/60),
  x=Math.floor(s%60); return m+":"+(x<10?"0":"")+x; }
function fmtms(s){                 /* m:ss.mmm - positions, never durations.
  All-integer maths so 12.9999 never rolls over into :1000. */
  if(!isFinite(s)||s<0) s=0;
  var t=Math.round(s*1000),
      m=Math.floor(t/60000), x=Math.floor((t%60000)/1000), ms=t%1000;
  return m+":"+(x<10?"0":"")+x+"."+(ms<100?(ms<10?"00":"0"):"")+ms;
}
function mb(b){ return (b/1048576).toFixed(1)+" MB"; }

/* ---------- load ---------- */
/* ---------- mood: top-level subfolders of the music folder ---------- */
function moodOf(s){
  var sub=s.sub||"";
  return sub? sub.split(/[\\\/]/)[0] : "";
}
function lsGet(k){ try{ return localStorage.getItem(k); }catch(e){ return null; } }
function lsSet(k,v){ try{ localStorage.setItem(k,v); }catch(e){} }

function load(data){
  if(data && data.rev) _libRev = data.rev;
  var currentSongPath = (pos>=0 && LIB[pos]) ? LIB[pos].path : null;
  var qPaths = QUEUE.map(function(idx){ return LIB[idx] ? LIB[idx].path : null; }).filter(Boolean);

  LIB=data.songs;
  FOLDERS=data.folders.length;
  MOODS=[];
  var seen={};
  for(var i=0;i<LIB.length;i++){
    var m=moodOf(LIB[i]);
    if(!m||seen["#"+m]) continue;
    seen["#"+m]=1; MOODS.push(m);
  }
  MOODS.sort(function(a,b){
    a=a.toLowerCase(); b=b.toLowerCase();
    return a<b?-1:(a>b?1:0);
  });
  if(MOOD && MOODS.indexOf(MOOD)<0) MOOD="";   // folder vanished -> back to All
  buildMoods();
  rebuildOrder();

  if(currentSongPath){
    var newPos = -1;
    for(var k=0; k<LIB.length; k++){
      if(LIB[k].path === currentSongPath){ newPos = k; break; }
    }
    pos = newPos;
    if(pos < 0){
      var curT = $("npTitle").textContent.replace(" (Deleted)", "");
      $("npTitle").textContent = curT + " (Deleted)";
    }
  }

  var newQ = [];
  qPaths.forEach(function(qp){
    for(var k=0; k<LIB.length; k++){
      if(LIB[k].path === qp){ newQ.push(k); break; }
    }
  });
  QUEUE = newQ;
  paintQueue();

  var host=$("list");
  var scr = host ? host.scrollTop : 0;
  render();
  if(host) host.scrollTop = scr;
  mark();

  restoreState();                      // come back to the same song + spot
}
function buildMoods(){
  var host=$("moods");
  if(!MOODS.length){ host.hidden=true; host.innerHTML=""; return; }
  var html='<button class="mood" type="button" data-mood="">All</button>';
  for(var i=0;i<MOODS.length;i++)
    html+='<button class="mood" type="button" data-mood="'+esc(MOODS[i])+'">'+
          esc(MOODS[i])+'</button>';
  host.innerHTML=html;
  host.hidden=false;
  paintMoods();
}
function paintMoods(){
  var bs=document.querySelectorAll("#moods .mood");
  for(var i=0;i<bs.length;i++)
    bs[i].classList.toggle("on", bs[i].dataset.mood===MOOD);
}
function rebuildOrder(){
  order=[];
  for(var i=0;i<LIB.length;i++){
    if(MOOD && moodOf(LIB[i])!==MOOD) continue;
    if(FAVONLY && !FAV[LIB[i].path]) continue;
    order.push(i);
  }
  if(shuffle) order=shuffleArr(order);
}
function setMood(m){
  MOOD=m||"";
  lsSet("player-mood", MOOD);
  rebuildOrder();
  paintMoods();
  render();
}
function shuffleArr(a){
  a=a.slice();
  for(var i=a.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1));
    var t=a[i];a[i]=a[j];a[j]=t;}
  return a;
}
function fetchLib(){ return fetch("/api/library").then(function(r){return r.json();}); }

/* ---------- render ---------- */
function render(){
  var q=$("q").value.trim().toLowerCase();
  var host=$("list"), n=0, srcset={}, vis=[];
  var words=q? q.split(/\s+/).filter(Boolean):[];   // "6lack on me" -> 3 words

  for(var k=0;k<order.length;k++){
    var s=LIB[order[k]]; if(!s) continue;
    var hay=(s.artist+" "+s.title+" "+s.folder+" "+s.sub).toLowerCase();
    var hit=true;
    for(var w=0;w<words.length;w++)
      if(hay.indexOf(words[w])<0){ hit=false; break; }
    if(!hit) continue;
    vis.push(order[k]); n++;
    srcset[s.folder+"|"+s.sub]=1;
  }
  /* a filtered-down view that lands on one folder doesn't need the column */
  host.classList.toggle("nosrc", Object.keys(srcset).length<=1);

  host.innerHTML = !n
    ? '<div class="empty">'+
      (FAVONLY ? "No favourites yet &mdash; hover a row and tap &#9829;."
       : words.length ? "No songs match." : "Nothing here yet.")+'</div>'
    : (GROUP? groupedHtml(vis) : flatHtml(vis));

  $("count").textContent =
    (n<LIB.length ? n+" of "+LIB.length+" songs" : LIB.length+" songs")+
    "  ·  "+FOLDERS+" source"+(FOLDERS===1?"":"s");

  markQueued();
}
function rowHtml(i, num){
  var s=LIB[i];
  return '<div class="row'+(i===pos?" playing":"")+
         '" data-i="'+i+'" title="'+esc(s.folder+(s.sub?" / "+s.sub:""))+'">'+
    '<div class="n"><span class="num">'+num+'</span>'+
      '<span class="eq"><i></i><i></i><i></i><i></i></span></div>'+
    '<div class="ti" data-artist="'+esc(s.artist||"")+'">'+esc(s.title)+'</div>'+
    '<div class="ar">'+(s.artist?esc(s.artist):'<span class="dim">&#8212;</span>')+'</div>'+
    '<div class="src">'+esc(s.sub||s.folder)+'</div>'+
    '<div class="sz">'+mb(s.size)+'</div>'+
    '<div class="acts">'+
      '<button class="fav'+(FAV[s.path]?" on":"")+'" type="button" data-i="'+i+
        '" title="'+(FAV[s.path]?"Remove from favourites":"Favourite this song")+'">'+
        (FAV[s.path]?"&#9829;":"&#9825;")+'</button>'+
      '<button class="lyrics'+(LYRHAVE[s.path]?" on":"")+'" type="button" data-i="'+i+
        '" title="'+(LYRHAVE[s.path]?"Edit or re-sync lyrics":"Add lyrics for this song")+
        '">&#9834;</button>'+
      '<button class="add" type="button" data-i="'+i+
        '" title="Add to the queue">&#43;</button>'+
      '<button class="del" type="button" data-i="'+i+
        '" title="Delete this song">'+TRASH+'</button>'+
    '</div></div>';
}
function flatHtml(vis){
  var html="";
  for(var k=0;k<vis.length;k++) html+=rowHtml(vis[k], k+1);
  return html;
}
function groupedHtml(vis){
  var groups={}, keys=[], num={};
  for(var k=0;k<vis.length;k++) num[vis[k]]=k+1;
  for(var k=0;k<vis.length;k++){
    var g=LIB[vis[k]].artist||"Unknown artist";
    var key=g.toLowerCase();
    if(!groups[key]){ groups[key]={name:g,items:[]}; keys.push(key); }
    groups[key].items.push(vis[k]);
  }
  keys.sort();
  var html="";
  for(var t=0;t<keys.length;t++){
    var grp=groups[keys[t]];
    html+='<details class="grp" open><summary><span>'+esc(grp.name)+
          '</span><span class="gcount">'+grp.items.length+'</span></summary>'+
          '<div class="grows">';
    for(var j=0;j<grp.items.length;j++) html+=rowHtml(grp.items[j], num[grp.items[j]]);
    html+='</div></details>';
  }
  return html;
}
function esc(s){ return String(s).replace(/[&<>"]/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); }

/* ---------- playback ---------- */
/* ---------- album art: cover.jpg next to the song, else the glyph ---------- */
var coverTok=0, lastCover=-1;
/* ---- dynamic album-art color theming ---- */
var _themeCanvas=document.createElement('canvas');
var _themeCtx=_themeCanvas.getContext('2d');
var _curAcc=[94,234,212];
var _themeDefault={
  bg:'#0e1016',card:'#161a23',card2:'#1b2030',line:'#262c3b',
  fg:'#e6e9f0',mut:'#8b93a7',acc:'#5eead4',acc2:'#3e8dff',now:'#3ecf8e',hot:'#ff6b6b',
  'acc-r':'94','acc-g':'234','acc-b':'212',
  'now-r':'62','now-g':'207','now-b':'142',
  'bg-r':'14','bg-g':'16','bg-b':'22',
  'card-r':'22','card-g':'26','card-b':'35'
};

function _luma(r,g,b){return 0.299*r+0.587*g+0.114*b;}
function _sat(r,g,b){var M=Math.max(r,g,b),m=Math.min(r,g,b);return M===0?0:(M-m)/M;}
function _hex(r,g,b){var h=function(n){var s=Math.max(0,Math.min(255,Math.round(n))).toString(16);return s.length<2?'0'+s:s;};return '#'+h(r)+h(g)+h(b);}
function _darken(r,g,b,f){return[Math.round(r*f),Math.round(g*f),Math.round(b*f)];}
function _lighten(r,g,b,f){return[Math.min(255,Math.round(r+(255-r)*f)),Math.min(255,Math.round(g+(255-g)*f)),Math.min(255,Math.round(b+(255-b)*f))];}
function _mix(r,g,b,r2,g2,b2,t){return[Math.round(r+(r2-r)*t),Math.round(g+(g2-g)*t),Math.round(b+(b2-b)*t)];}

var _themeAnimRaf = null;
var _themeCurrent = {
  acc: [94,234,212],
  now: [62,207,142],
  bg:  [14,16,22],
  c1:  [22,26,35],
  c2:  [27,32,48],
  line:[38,44,59],
  acc2:[62,141,255],
  fg:  [230,233,240],
  mut: [139,147,167]
};

function applyThemeColor(r,g,b){
  var sat = _sat(r,g,b);
  if(sat < 0.1){
    r=120; g=160; b=200;
  }

  /* Compute target palette */
  var bg = _darken(r,g,b, 0.08);
  bg = [Math.max(6, bg[0]), Math.max(7, bg[1]), Math.max(10, bg[2])];
  var c1   = _mix(bg[0],bg[1],bg[2], r,g,b, 0.12);
  var c2   = _mix(bg[0],bg[1],bg[2], r,g,b, 0.20);
  var line = _mix(bg[0],bg[1],bg[2], r,g,b, 0.35);

  var lum = _luma(r,g,b);
  var boost = lum < 160 ? (190 / Math.max(lum, 1)) : 1.05;
  var acc = [Math.min(255, Math.round(r*boost)), Math.min(255, Math.round(g*boost)), Math.min(255, Math.round(b*boost))];
  var acc2 = [Math.round(acc[0]*0.4 + acc[2]*0.6), Math.round(acc[1]*0.6 + acc[0]*0.4), Math.min(255, Math.round(acc[2]*1.2))];
  var now = acc;
  var fg = _lighten(acc[0],acc[1],acc[2], 0.88);
  var mut = _mix(line[0],line[1],line[2], fg[0],fg[1],fg[2], 0.50);

  var targets = {
    acc: acc, now: now, bg: bg, c1: c1, c2: c2, line: line, acc2: acc2, fg: fg, mut: mut
  };

  /* Smooth 2-second ease-out glide for all colors (including canvas and rgba vars) */
  if(_themeAnimRaf) cancelAnimationFrame(_themeAnimRaf);
  var startTime = performance.now();
  var duration = 2000; /* exactly 2.0s transition */
  var startColors = {};
  for(var k in _themeCurrent){
    startColors[k] = _themeCurrent[k].slice();
  }

  var root = document.documentElement;
  var mt = document.querySelector('meta[name=theme-color]');

  function updatePaletteStep(nowTime){
    var elapsed = nowTime - startTime;
    var progress = Math.min(1.0, elapsed / duration);
    /* Cubic ease-out: smooth, gradual arrival without harsh stop */
    var ease = 1 - Math.pow(1 - progress, 3);

    for(var key in targets){
      var sc = startColors[key], tc = targets[key];
      _themeCurrent[key] = [
        Math.round(sc[0] + (tc[0] - sc[0]) * ease),
        Math.round(sc[1] + (tc[1] - sc[1]) * ease),
        Math.round(sc[2] + (tc[2] - sc[2]) * ease)
      ];
    }
    _curAcc = _themeCurrent.acc;

    /* Write current interpolated colors */
    function cv(n, a){ root.style.setProperty(n, _hex(a[0],a[1],a[2])); }
    function num(n, a){
      root.style.setProperty(n+'-r', a[0]);
      root.style.setProperty(n+'-g', a[1]);
      root.style.setProperty(n+'-b', a[2]);
    }

    cv('--bg', _themeCurrent.bg);
    cv('--card', _themeCurrent.c1);
    cv('--card2', _themeCurrent.c2);
    cv('--line', _themeCurrent.line);
    cv('--fg', _themeCurrent.fg);
    cv('--mut', _themeCurrent.mut);
    cv('--acc', _themeCurrent.acc);
    cv('--acc2', _themeCurrent.acc2);
    cv('--now', _themeCurrent.now);

    num('--acc', _themeCurrent.acc);
    num('--now', _themeCurrent.now);
    num('--bg', _themeCurrent.bg);
    num('--card', _themeCurrent.c1);

    if(mt) mt.content = _hex(_themeCurrent.bg[0], _themeCurrent.bg[1], _themeCurrent.bg[2]);

    if(progress < 1.0){
      _themeAnimRaf = requestAnimationFrame(updatePaletteStep);
    } else {
      _themeAnimRaf = null;
    }
  }

  _themeAnimRaf = requestAnimationFrame(updatePaletteStep);
}

function extractThemeColor(imgEl){
  var S=48; _themeCanvas.width=_themeCanvas.height=S;
  try{ _themeCtx.drawImage(imgEl,0,0,S,S); }catch(e){return;}
  var data; try{ data=_themeCtx.getImageData(0,0,S,S).data; }catch(e){return;}
  var best=null, bScore=-1;
  for(var p=0; p<data.length; p+=4){
    var r=data[p], g=data[p+1], b=data[p+2], a=data[p+3];
    if(a<200) continue;
    var lum=_luma(r,g,b);
    if(lum<10 || lum>248) continue;
    var sat=_sat(r,g,b);
    var score = sat*3.2 - Math.abs(lum-128)/128*0.35;
    if(score > bScore){ bScore=score; best=[r,g,b]; }
  }
  if(!best){
    var ar=0,ag=0,ab=0,cnt=0;
    for(var q=0; q<data.length; q+=4){
      if(data[q+3]>=200){ ar+=data[q]; ag+=data[q+1]; ab+=data[q+2]; cnt++; }
    }
    if(cnt) best=[Math.round(ar/cnt), Math.round(ag/cnt), Math.round(ab/cnt)];
  }
  if(!best) return;
  applyThemeColor(best[0], best[1], best[2]);
}

function resetTheme(){
  _curAcc = [94,234,212];
  var d=_themeDefault, root=document.documentElement;
  Object.keys(d).forEach(function(k){ root.style.setProperty('--'+k, d[k]); });
  var mt=document.querySelector('meta[name=theme-color]');
  if(mt) mt.content=d.bg;
}

function setCover(i){
  var img=$("art"), svg=$("artGlyph");
  if(i<0 || !LIB[i]){
    lastCover=-1; coverTok++;
    img.hidden=true; img.removeAttribute("src"); svg.hidden=false;
    resetTheme();
    return;
  }
  if(i===lastCover) return;
  lastCover=i;
  var tok=++coverTok;
  img.hidden=true; svg.hidden=false;               // assume no art until it lands
  img.onload=function(){
    if(tok!==coverTok) return;
    img.hidden=false; svg.hidden=true;
    var fsbg=$("fsbg");
    if(fsbg) fsbg.style.backgroundImage='url("'+img.src+'")';
    var fsImg=$("fsImg");
    if(fsImg){fsImg.src=img.src;fsImg.hidden=false;}
    extractThemeColor(img);
  };
  img.onerror=function(){
    if(tok!==coverTok) return;
    img.hidden=true; svg.hidden=false;
    resetTheme();
  };
  img.src="/api/cover/"+i;
}

function updateMediaSession(s, i){
  if(!("mediaSession" in navigator) || !s) return;
  try{
    navigator.mediaSession.metadata = new MediaMetadata({
      title: s.title || "Unknown Title",
      artist: s.artist || "Unknown Artist",
      album: s.sub || s.folder || "Songz",
      artwork: [
        { src: "/api/cover/" + i, sizes: "512x512", type: "image/jpeg" },
        { src: "/icon-512.png", sizes: "512x512", type: "image/png" }
      ]
    });
  }catch(e){}
}
if("mediaSession" in navigator){
  try{
    navigator.mediaSession.setActionHandler("play", function(){ if(au.paused) au.play(); });
    navigator.mediaSession.setActionHandler("pause", function(){ au.pause(); });
    navigator.mediaSession.setActionHandler("previoustrack", function(){ step(-1); });
    navigator.mediaSession.setActionHandler("nexttrack", function(){ step(1); });
    navigator.mediaSession.setActionHandler("seekto", function(d){
      if(d.seekTime!=null && isFinite(au.duration)){
        au.currentTime = Math.max(0, Math.min(au.duration, d.seekTime));
        paintSeek();
      }
    });
    navigator.mediaSession.setActionHandler("seekforward", function(d){
      nudgeSeek(d.seekOffset || 10);
    });
    navigator.mediaSession.setActionHandler("seekbackward", function(d){
      nudgeSeek(-(d.seekOffset || 10));
    });
  }catch(e){}
}

function playIndex(i){
  if(i<0||i>=LIB.length) return;
  pos=i;
  var s=LIB[i];
  au.src="/audio/"+i+"?p="+encodeURIComponent(s.path);
  au.play().catch(function(){});
  saveState(true, 0);              // remember the song; it starts at 0:00
  $("npTitle").textContent=s.title;
  $("npArtist").textContent=s.artist||"";
  $("npSrc").textContent=s.sub||s.folder;
  setCover(i);
  updateMediaSession(s, i);
  document.body.classList.remove("paused");
  $("play").innerHTML="&#10074;&#10074;";
  mark();
  paintDeck();
  if(!order.length) return;
  try{
    var el=document.querySelector('.row[data-i="'+i+'"]');
    if(el) el.scrollIntoView({block:"center", behavior:"smooth"});
  }catch(e){}
}
function mark(){
  /* pos is a LIBRARY index - compare straight against data-i so a shuffled
     or mood-filtered order can never highlight the wrong row */
  var rows=document.querySelectorAll(".row");
  for(var i=0;i<rows.length;i++)
    rows[i].classList.toggle("playing", +rows[i].dataset.i===pos);
}
function curPosInOrder(){
  for(var k=0;k<order.length;k++) if(order[k]===pos) return k;
  return -1;
}
function step(dir){
  /* an explicit "next" drains the queue first */
  if(dir>0 && QUEUE.length){
    var nx=QUEUE.shift();
    paintQueue();
    if(nx>=0 && nx<LIB.length){ playIndex(nx); return; }
  }
  if(!order.length) return;
  var k=curPosInOrder();
  if(k<0){ playIndex(order[0]); return; }
  var next;
  if(dir>0){
    next=k+1;
    if(next>=order.length){
      if(repeat!==1){ pause(); return; }
      /* end of the playlist + repeat all: a shuffled list gets a fresh
         shuffle and starts again from song #1, so the cycle never dies */
      if(shuffle){
        rebuildOrder();
        if(!order.length){ pause(); return; }
        if(order[0]===pos && order.length>1){   /* don't replay the song that just ended */
          var sw=order[0]; order[0]=order[1]; order[1]=sw;
        }
        playIndex(order[0]); return;
      }
      next=0;
    }
  }else{
    next=k-1;
    if(next<0) next = repeat===1 ? order.length-1 : 0;
  }
  playIndex(order[next]);
}
function toggle(){ if(pos<0){ playIndex(order[0]!==undefined?order[0]:0); return; }
  if(au.paused) au.play(); else au.pause(); }
function pause(){ au.pause(); }

/* ---------- queue (up next) ---------- */
function enqueue(i){
  if(!LIB[i]) return;
  QUEUE.push(i);
  paintQueue();
}
function clearQueue(){ QUEUE=[]; paintQueue(); }
function paintQueue(){
  $("qchip").hidden = QUEUE.length===0;
  $("qn").textContent = QUEUE.length;
  $("qdcount").textContent = QUEUE.length;
  markQueued();
  renderQDrawer();
}
function renderQDrawer(){
  var box = $("qdlist");
  if(!box) return;
  box.innerHTML = "";
  if(!QUEUE.length){
    box.innerHTML = '<div class="qempty">No songs in queue.<br>Click <b>+</b> on any song to add.</div>';
    return;
  }
  for(var q=0; q<QUEUE.length; q++){
    (function(idx, songIdx){
      var s = LIB[songIdx];
      if(!s) return;
      var row = document.createElement("div");
      row.className = "qdrow";
      row.innerHTML = '<span style="color:var(--mut); font-size:11px; min-width:18px">'+(idx+1)+'</span>' +
        '<div class="qti"><b>'+esc(s.title)+'</b><span>'+esc(s.artist||"Unknown")+'</span></div>' +
        '<button class="qdel" title="Remove">&#10005;</button>';
      row.querySelector(".qti").addEventListener("click", function(){
        QUEUE.splice(idx, 1);
        paintQueue();
        playIndex(songIdx);
      });
      row.querySelector(".qdel").addEventListener("click", function(e){
        e.stopPropagation();
        QUEUE.splice(idx, 1);
        paintQueue();
      });
      box.appendChild(row);
    })(q, QUEUE[q]);
  }
}
function markQueued(){
  var bs=document.querySelectorAll(".acts .add");
  for(var k=0;k<bs.length;k++)
    bs[k].classList.toggle("on", QUEUE.indexOf(+bs[k].dataset.i)>=0);
}
$("qchip").addEventListener("click", function(e){
  e.stopPropagation();
  var d = $("qdrawer");
  d.hidden = !d.hidden;
});
$("qdclose").addEventListener("click", function(){ $("qdrawer").hidden = true; });
$("qdclear").addEventListener("click", function(){ clearQueue(); $("qdrawer").hidden = true; });

/* ---------- session state: reopening returns to the same song ---------- */
var stLast=0, stDone=false;
function saveState(force, tOverride){
  var now=Date.now();
  if(!force && now-stLast<4000) return;     // timeupdate: one write per ~4s
  stLast=now;
  lsSet("player-state", JSON.stringify({
    path: (pos>=0 && LIB[pos]) ? LIB[pos].path : "",
    t: (tOverride!=null ? tOverride : au.currentTime) || 0,
    shuffle: shuffle, repeat: repeat
  }));
}
function restoreState(){
  if(stDone) return; stDone=true;           // load() also runs after a rescan
  var s; try{ s=JSON.parse(lsGet("player-state")||"null"); }catch(e){}
  if(!s) return;
  shuffle=!!s.shuffle; repeat=(+s.repeat)||0;
  $("shuffle").classList.toggle("on", shuffle);
  $("repeat").classList.toggle("on", repeat>0);
  $("repeat").textContent = repeat===2 ? "\u{1F501}\uFE0E\u20091" : "\u{1F501}\uFE0E";
  rebuildOrder(); render();
  if(!s.path) return;
  var i=-1;
  for(var k=0;k<LIB.length;k++) if(LIB[k].path===s.path){ i=k; break; }
  if(i<0) return;
  var t=+s.t||0;
  pos=i;
  au.src="/audio/"+i;                       // paused: pick up where we left off
  if(t>1) au.addEventListener("loadedmetadata", function once(){
    au.removeEventListener("loadedmetadata", once);
    try{ au.currentTime=t; }catch(e){}
    paintSeek();
  });
  $("npTitle").textContent=LIB[i].title;
  $("npArtist").textContent=LIB[i].artist||"";
  $("npSrc").textContent=LIB[i].sub||LIB[i].folder;
  setCover(i); mark(); paintDeck(); paintSeek();
  updateMediaSession(LIB[i], i);
}
au.addEventListener("timeupdate", function(){
  saveState();
  if("mediaSession" in navigator && "setPositionState" in navigator.mediaSession && isFinite(au.duration) && au.duration > 0){
    try{
      navigator.mediaSession.setPositionState({
        duration: au.duration,
        playbackRate: au.playbackRate || 1,
        position: Math.min(au.currentTime, au.duration)
      });
    }catch(e){}
  }
});
au.addEventListener("pause", function(){
  saveState(true);
  if("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
});
addEventListener("pagehide", function(){ saveState(true); });
document.addEventListener("visibilitychange", function(){
  if(document.hidden) saveState(true);      // tab closed from the tab switcher
});

/* swipe the now-playing deck: left = next, right = previous */
(function(){
  var deck=document.querySelector(".deck"); if(!deck) return;
  var x0=0, y0=0, live=false;
  deck.addEventListener("touchstart", function(e){
    if(e.touches.length!==1) return;
    var t=e.target;
    if(t.closest && t.closest("input,button,a")) return;  // sliders keep their touch
    x0=e.touches[0].clientX; y0=e.touches[0].clientY; live=true;
  }, {passive:true});
  deck.addEventListener("touchend", function(e){
    if(!live) return; live=false;
    var c=e.changedTouches[0], dx=c.clientX-x0, dy=c.clientY-y0;
    if(Math.abs(dx)<48 || Math.abs(dx)<Math.abs(dy)*1.5) return;  // a real swipe
    step(dx<0?1:-1);
  }, {passive:true});
})();

/* ---------- wiring ---------- */
au.addEventListener("play", function(){
  if("mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
  document.body.classList.remove("paused");
  $("play").innerHTML="&#10074;&#10074;";
});
au.addEventListener("pause", function(){
  document.body.classList.add("paused");
  $("play").innerHTML="&#9654;";
});
au.addEventListener("ended", function(){
  if(repeat===2){
    au.currentTime=0;
    au.play().catch(function(){          /* transient abort: retry once */
      setTimeout(function(){ if(au.paused) au.play().catch(function(){}); }, 300);
    });
    return;
  }
  step(1);
});
/* a song that fails to load or decode must never kill the session: skip
   ahead instead of sitting there paused. A run of broken files gives up
   after a few tries rather than spinning through the whole library. */
var errStreak=0;
au.addEventListener("error", function(){
  if(pos<0 || !au.getAttribute("src")) return;   // cleared deck, not a bad song
  if(++errStreak>3){ errStreak=0; pause(); return; }
  step(1);
});
au.addEventListener("playing", function(){ errStreak=0; });
/* ---------- seek / volume painting ---------- */
var seeking=false, bufPct=0;
function paintSeek(){
  var el=$("seek"), p=0, b=0;
  if(isFinite(au.duration) && au.duration>0){
    p=Math.max(0, Math.min(1, au.currentTime/au.duration));
    try{
      if(au.buffered && au.buffered.length)
        b=Math.max(0, Math.min(1, au.buffered.end(au.buffered.length-1)/au.duration));
    }catch(e){ b=0; }
  }
  if(b<p) b=p;                       // never let buffered trail the playhead
  bufPct=b*100;
  el.style.setProperty("--p", (p*100).toFixed(2)+"%");
  el.style.setProperty("--b", bufPct.toFixed(2)+"%");
}
function paintVol(){
  var v=Math.max(0, Math.min(100, +$("vol").value));
  var el=$("vol");
  el.style.setProperty("--p", v+"%");
  el.style.setProperty("--b", v+"%");
}

au.addEventListener("loadedmetadata", function(){
  $("dur").textContent=fmt(au.duration);
  paintSeek();
});
au.addEventListener("timeupdate", function(){
  $("cur").textContent=fmt(au.currentTime);
  if(seeking) return;                 // don't fight the finger on the slider
  if(isFinite(au.duration) && au.duration>0)
    $("seek").value=Math.round(au.currentTime/au.duration*1000);
  paintSeek();
});
au.addEventListener("progress", paintSeek);

/* ---------- seek indicator: "how far into the song am I going?" ---------- */
var tipTimer=0;
function hideSeekTip(){
  if(tipTimer){ clearTimeout(tipTimer); tipTimer=0; }
  $("seekTip").hidden=true;
}
function showSeekTip(pct, timeStr, deltaStr, autoHide){
  var tip=$("seekTip"), wrap=$("seekwrap"), el=$("seek");
  $("tipT").textContent=timeStr;
  $("tipD").textContent=deltaStr||"";
  tip.hidden=false;
  var wr=wrap.getBoundingClientRect(), er=el.getBoundingClientRect();
  if(!er.width || !wr.width){ tip.hidden=true; return; }
  var half=tip.offsetWidth/2,
      left=(er.left-wr.left)+Math.max(0, Math.min(1, pct))*er.width;
  left=Math.max(half, Math.min(wr.width-half, left));
  tip.style.left=left.toFixed(1)+"px";
  if(tipTimer) clearTimeout(tipTimer);
  tipTimer=setTimeout(hideSeekTip, autoHide||2500);
}
function seekTipAt(pct, autoHide){          /* the spot the pointer sits on */
  if(!isFinite(au.duration) || au.duration<=0) return;
  showSeekTip(pct, fmtms(pct*au.duration), "", autoHide);
}
function nudgeSeek(d){                      /* arrow keys: show where you landed */
  if(!isFinite(au.duration) || au.duration<=0) return;
  var before=au.currentTime,
      t=Math.max(0, Math.min(au.duration, before+d)),
      delta=t-before;
  au.currentTime=t;
  showSeekTip(t/au.duration, fmtms(t),
              (delta>=0?"+":"")+delta.toFixed(1)+"s", 2200);
}

$("seek").addEventListener("input", function(){
  seeking=true;
  var p=(+this.value/1000)*100;
  this.style.setProperty("--p", p.toFixed(2)+"%");
  this.style.setProperty("--b", Math.max(p, bufPct).toFixed(2)+"%");
  seekTipAt(+this.value/1000, 2500);        // live readout while you drag
});
$("seek").addEventListener("change", function(){
  var frac=+this.value/1000;
  if(isFinite(au.duration)) au.currentTime=frac*au.duration;
  seeking=false;
  paintSeek();
  seekTipAt(frac, 2000);                    // where you just dropped it
});
$("seek").addEventListener("mousemove", function(e){
  var r=this.getBoundingClientRect();
  if(!r.width) return;
  seekTipAt((e.clientX-r.left)/r.width, 2500);
});
$("seek").addEventListener("mouseleave", function(){ if(!seeking) hideSeekTip(); });
$("vol").addEventListener("input", function(){
  /* The element stays at volume 1 and the slider drives the gain node:
     MediaElementSource honours element volume, so element volume 0 would
     feed the analyser pure silence and kill the bars. */
  au.volume=1;
  if(MASTER) MASTER.gain.value=this.value/100;
  lsSet("player-vol", this.value);    // remember it across restarts
  paintVol();
});

$("play").addEventListener("click", toggle);
$("next").addEventListener("click", function(){ step(1); });
$("prev").addEventListener("click", function(){
  if(au.currentTime>3){ au.currentTime=0; return; } step(-1);
});
$("shuffle").addEventListener("click", function(){
  shuffle=!shuffle; this.classList.toggle("on", shuffle);
  rebuildOrder();                     // keep the mood filter when reshuffling
  render();
  saveState(true);
});
$("repeat").addEventListener("click", function(){
  repeat=(repeat+1)%3;
  this.classList.toggle("on", repeat>0);
  this.textContent = repeat===2 ? "\u{1F501}\uFE0E\u20091" : "\u{1F501}\uFE0E";
  this.title = ["Repeat off","Repeat all","Repeat one"][repeat];
  saveState(true);
});
$("rescan").addEventListener("click", function(){
  var b=this; b.disabled=true; b.textContent="…";
  fetch("/api/rescan").then(function(r){return r.json();}).then(function(d){
    load(d); b.disabled=false; b.innerHTML="&#8635; Rescan";
  });
});
$("q").addEventListener("input", render);
$("list").addEventListener("click", function(e){
  var del=e.target.closest(".del");
  if(del){ askDelete(+del.dataset.i); return; }
  var add=e.target.closest(".add");
  if(add){ enqueue(+add.dataset.i); return; }
  var fav=e.target.closest(".fav");
  if(fav){ toggleFav(+fav.dataset.i); return; }
  var lir=e.target.closest(".lyrics");
  if(lir){ openLyrics(+lir.dataset.i); return; }
  var row=e.target.closest(".row"); if(!row) return;
  playIndex(+row.dataset.i);
});
$("moods").addEventListener("click", function(e){
  var b=e.target.closest(".mood"); if(!b) return;
  setMood(b.dataset.mood);
});
$("group").addEventListener("click", function(){
  GROUP=!GROUP;
  this.classList.toggle("on", GROUP);
  lsSet("player-group", GROUP?"1":"");
  render();
});
$("qchip").addEventListener("click", clearQueue);

document.addEventListener("keydown", function(e){
  var t=e.target.tagName;
  /* dialog open: swallow every shortcut (Enter/Space still reach its buttons) */
  if(!$("modal").hidden){
    if(e.key==="Escape") closeModal();
    return;
  }
  /* lyrics sheet open: Escape closes it and typing stays in the textarea, but
     the transport keeps working - you play/pause and seek while syncing.
     S stamps the selected line at the playhead. */
  if(!$("lyr").hidden){
    if(e.key==="Escape"){ closeLyrics(); return; }
    if(t==="INPUT" || t==="TEXTAREA") return;
    if(t==="BUTTON" && (e.key===" " || e.key==="Enter")) return;
    if(e.key==="s" || e.key==="S"){ e.preventDefault(); stamp(); return; }
  } else if(t==="INPUT" && e.key!=="Escape"){ if(e.key==="Escape") e.target.blur(); return; }
  if(e.key===" "){ e.preventDefault(); toggle(); }
  else if(e.key==="ArrowRight"){ e.preventDefault(); nudgeSeek(5); }
  else if(e.key==="ArrowLeft"){ e.preventDefault(); nudgeSeek(-5); }
  else if(e.key==="ArrowUp"){ e.preventDefault(); $("vol").value=Math.min(100,+$("vol").value+5); $("vol").dispatchEvent(new Event("input")); }
  else if(e.key==="ArrowDown"){ e.preventDefault(); $("vol").value=Math.max(0,+$("vol").value-5); $("vol").dispatchEvent(new Event("input")); }
  else if(e.key==="n"||e.key==="N"){ step(1); }
  else if(e.key==="p"||e.key==="P"){ step(-1); }
  else if(e.key==="/"){ e.preventDefault(); $("q").focus(); }
  else if(e.key==="Escape"){ $("q").blur(); }
});

/* ---------- delete ---------- */
function resetDeck(){
  pos=-1;
  au.pause(); au.removeAttribute("src"); au.load();
  setCover(-1);
  $("npTitle").textContent="Nothing playing";
  $("npArtist").textContent="";
  $("npSrc").textContent="";
  document.body.classList.add("paused");
  $("play").innerHTML="&#9654;";
  $("cur").textContent="0:00"; $("dur").textContent="0:00"; $("seek").value=0;
  hideSeekTip();
  paintDeck();
}

var pendingIdx=-1;

function askDelete(i){
  var s=LIB[i]; if(!s) return;
  pendingIdx=i;
  $("dTitle").textContent=s.title;
  $("dArtist").textContent=s.artist||"";
  $("dPath").textContent=s.path;
  $("dSize").textContent=mb(s.size);
  $("dGo").disabled=false;
  $("modal").hidden=false;
  $("dCancel").focus();
}
function closeModal(){ $("modal").hidden=true; pendingIdx=-1; }

function commitDelete(){
  if(pendingIdx<0) return;
  var idx=pendingIdx, s=LIB[idx];
  if(!s){ closeModal(); return; }

  var wasPlaying=(idx===pos);
  var keepKey=null, keepTime=au.currentTime, keepPlaying=!au.paused;
  if(!wasPlaying && idx<pos && LIB[pos]) keepKey=LIB[pos].path;

  $("dGo").disabled=true;
  fetch("/api/delete",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({i:idx, path:s.path, confirm:true})})
  .then(function(r){
    return r.json().then(function(d){
      if(!r.ok) throw new Error(d.error||("HTTP "+r.status));
      return d;
    });
  })
  .then(function(d){
    closeModal();
    if(wasPlaying){ resetDeck(); }
    load(d);
    /* a delete at/before the playing row shifts its index, and /audio/N is
       index-based — re-point so a later seek can't pull the wrong file. */
    if(keepKey){
      for(var k=0;k<LIB.length;k++){
        if(LIB[k].path===keepKey){
          pos=k;
          au.src="/audio/"+k;
          var onMeta=function(){
            au.removeEventListener("loadedmetadata", onMeta);
            try{ au.currentTime=keepTime; }catch(e){}
          };
          au.addEventListener("loadedmetadata", onMeta);
          if(keepPlaying) au.play().catch(function(){});
          break;
        }
      }
    }
    mark();
  })
  .catch(function(err){
    $("dGo").disabled=false;
    alert("Could not delete:\n\n"+(err&&err.message?err.message:String(err)));
  });
}

$("dCancel").addEventListener("click", closeModal);
$("dGo").addEventListener("click", commitDelete);
$("modal").addEventListener("click", function(e){ if(e.target===this) closeModal(); });

/* ================= favourites + lyrics ================= */
function jget(u){ return fetch(u).then(function(r){ return r.json(); }); }
function jpost(u,b){
  return fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},
                  body:JSON.stringify(b)})
    .then(function(r){ return r.json().then(function(d){
      if(!r.ok) throw new Error(d.error||("HTTP "+r.status));
      return d;
    }); });
}
function setFavMap(paths){
  FAV={};
  for(var i=0;i<(paths||[]).length;i++) FAV[paths[i]]=1;
}
function rerender(){                       /* repaint without losing your place */
  var y=$("list").scrollTop;
  render();
  $("list").scrollTop=y;
}

/* ---------- favourites ---------- */
function paintFavBtn(){
  var on = pos>=0 && !!LIB[pos] && !!FAV[LIB[pos].path];
  var b=$("npFav");
  b.innerHTML = on ? "&#9829;" : "&#9825;";
  b.classList.toggle("on", on);
}
function paintDeckLyricBtn(){
  var live = pos>=0 && !!LIB[pos];
  var b=$("npLyrics");
  b.classList.toggle("on", live && !!LYRSYNC[LIB[pos].path]);
  b.title = (live && LYRHAVE[LIB[pos].path])
          ? "Edit or re-sync these lyrics" : "Add lyrics for this song";
}
function paintDeck(){
  var live = pos>=0 && !!LIB[pos];
  $("npFav").disabled=!live;
  $("npLyrics").disabled=!live;
  paintFavBtn();
  paintDeckLyricBtn();
  ensureDeckLyric(live? pos : -1);
  fillNpLyrs();
}
function toggleFav(i){
  var s=LIB[i]; if(!s) return;
  var on=!FAV[s.path];
  jpost("/api/favourite",{path:s.path,on:on}).then(function(d){
    setFavMap(d.paths);
    if(FAVONLY) rebuildOrder();
    rerender(); paintFavBtn();
  }).catch(function(e){
    alert("Could not save that favourite:\n\n"+(e&&e.message?e.message:String(e)));
  });
}
function loadFavs(){
  jget("/api/favourites").then(function(d){
    setFavMap(d.paths);
    paintFavBtn();
    if(LIB.length) rerender();
  }).catch(function(){});
}
function loadLyricIndex(){
  jget("/api/lyrics").then(function(d){
    var i, h=d.have||[], y=d.synced||[];
    LYRHAVE={}; LYRSYNC={};
    for(i=0;i<h.length;i++) LYRHAVE[h[i]]=1;
    for(i=0;i<y.length;i++) LYRSYNC[y[i]]=1;
    if(LIB.length) rerender();
    paintDeckLyricBtn();
  }).catch(function(){});
}
$("favfilter").addEventListener("click", function(){
  FAVONLY=!FAVONLY;
  this.classList.toggle("on", FAVONLY);
  lsSet("player-fav", FAVONLY?"1":"");
  rebuildOrder(); render();
});
$("npFav").addEventListener("click", function(){ if(pos>=0) toggleFav(pos); });
/* On a phone the deck has no spare room for a full seek bar, so the title
   block doubles as the way into the lyrics/seek sheet. */
$("info").addEventListener("click", function(){ if(pos>=0) openLyrics(pos); });
$("npLyrics").addEventListener("click", function(){ if(pos>=0) openLyrics(pos); });

/* ---------- lyrics ---------- */
var LYR_I=-1, LYR=null, LYR_LINES=[], LYR_SEL=-1, LYR_ON=false;
var DECK_LYR=null, DECK_LYR_I=-1;

function lyricIndexAt(rec, t){
  /* the line with the latest stamp at or before the playhead */
  if(!rec || !rec.times) return -1;
  var best=-1, bt=-Infinity;
  for(var k in rec.times){
    var v=rec.times[k], i=+k;
    if(typeof v!=="number") continue;
    if(v<=t+0.02 && (v>bt || (v===bt && i>best))){ bt=v; best=i; }
  }
  return best;
}
function ensureDeckLyric(i){
  if(i===DECK_LYR_I){ paintLyricLine(); return; }
  DECK_LYR_I=i; DECK_LYR=null; paintLyricLine();
  if(i<0 || !LIB[i]) return;
  var path=LIB[i].path;
  if(!LYRHAVE[path]) return;
  jget("/api/lyrics/show?path="+encodeURIComponent(path)).then(function(d){
    if(DECK_LYR_I!==i) return;
    DECK_LYR=(d && typeof d.text==="string") ? d : null;
    paintLyricLine();
  }).catch(function(){});
}
function paintLyricLine(){
  var el=$("lyLine");
  if(!DECK_LYR || !DECK_LYR.times || !Object.keys(DECK_LYR.times).length){
    el.hidden=true; el.textContent=""; return;
  }
  var i=lyricIndexAt(DECK_LYR, au.currentTime);
  el.hidden=false;
  el.textContent = i>=0 ? (DECK_LYR.lines[i]||"") : "";
}

function fillNpLyrs(){
  var body=document.querySelector("#npLyrs .npLyrBody");   /* $ is getElementById */
  if(!body) return;
  var h="";
  if(pos<0){ body.innerHTML=""; return; }
  var L=LYR;
  if(!L || !L.lines || !L.lines.length){
    h='<div class="lnote">No lyrics for this song yet.</div>';
  }else{
    L.lines.forEach(function(t, ix){
      h+='<div class="ln" data-i="'+ix+'">'+
         '<span class="txt">'+esc(t||"")+'</span></div>';
    });
  }
  body.innerHTML=h;
}
$("npFS").addEventListener("click", function(){
  if(document.fullscreenElement){ document.exitFullscreen().catch(function(){}); return; }
  document.body.classList.add("np-fs");
  var d=document.querySelector(".deck");
  if(d && d.requestFullscreen){ try{ d.requestFullscreen(); }catch(e2){} }
});
document.addEventListener("fullscreenchange", function(){
  if(document.fullscreenElement){ }
  else{ document.body.classList.remove("np-fs"); document.body.classList.remove("np-lyr"); }
});
$("npFullLyr").addEventListener("click", function(){ document.body.classList.toggle("np-lyr"); });
function openLyrics(i){
  if(!LIB[i]) return;
  LYR_I=i; LYR_SEL=-1; LYR_ON=false; followLyric.last=-1;
  var s=LIB[i];
  $("lyrT").textContent=s.title;
  $("lyrA").textContent=s.artist||"";
  $("lyrDur").textContent=(pos===i && isFinite(au.duration))? fmt(au.duration):"–:––";
  $("syncBar").hidden=true;
  $("lyrSyncBtn").innerHTML="&#9201; Sync";
  $("lyrSyncBtn").classList.remove("on");
  $("list").hidden=true; $("lyr").hidden=false;
  $("lyrEdit").hidden=false; $("lyrView").hidden=true;
  $("lyrText").disabled=true;
  $("lyrText").value = LYRHAVE[s.path] ? "loading…" : "";
  jget("/api/lyrics/show?path="+encodeURIComponent(s.path)).then(function(d){
    if(LYR_I!==i) return;
    LYR=(d && typeof d.text==="string") ? d : null;
    LYR_LINES = LYR? LYR.lines : [];
    $("lyrText").disabled=false;
    if(LYR){ $("lyrText").value=LYR.text; showLyricsView(); }
    else { $("lyrText").value=""; $("lyrText").focus(); }
  }).catch(function(){
    if(LYR_I!==i) return;
    $("lyrText").disabled=false; $("lyrText").value="";
  });
}
function closeLyrics(){
  $("lyr").hidden=true; $("list").hidden=false;
  LYR_ON=false; LYR_SEL=-1; LYR_I=-1; LYR=null; LYR_LINES=[];
  setSpeed(1);
  rerender();
}
function showLyricsView(){
  $("lyrEdit").hidden=true; $("lyrView").hidden=false;
  setSyncOn(false);
  renderLines(); paintLyrStat();
}
function showLyricsEdit(){
  $("lyrEdit").hidden=false; $("lyrView").hidden=true;
  LYR_ON=false; LYR_SEL=-1; $("syncBar").hidden=true;
  $("lyrText").value = LYR? LYR.text : "";
  $("lyrText").focus();
}
/* ---------- slow motion while timing ---------- */
/* au.playbackRate only changes how FAST the timeline advances - au.currentTime
   stays real song time, so a stamp taken at 0.25x is still the true position. */
var LYR_RATE=1, RAMP=null, SP_MIN=0.1, SP_MAX=2;

function clampRate(v){
  v=parseFloat(v);
  if(!isFinite(v)) return 1;
  return Math.min(SP_MAX, Math.max(SP_MIN, v));
}
function paintSpeed(v, keepNum){
  $("speed").value=v;
  if(!keepNum) $("speedNum").value=+v.toFixed(2);
  $("spVal").innerHTML=+v.toFixed(2)+"&times;";
  $("speed").style.setProperty("--p", ((v-SP_MIN)/(SP_MAX-SP_MIN)*100)+"%");
}
/* Slowing down drops the pitch as well as the tempo - a tape slowdown, which
   keeps the sound deep and clear instead of thin and phasey. Speeding up
   keeps the pitch, so 2x stays listenable. */
function applyRate(v){
  var keep = v>=1;
  au.preservesPitch=keep; au.mozPreservesPitch=keep; au.webkitPreservesPitch=keep;
  try{ au.playbackRate=v; }catch(e){}
}
function setSpeed(v, instant, keepNum){
  v=clampRate(v);
  var from=LYR_RATE;
  LYR_RATE=v; paintSpeed(v, keepNum);
  if(RAMP){ cancelAnimationFrame(RAMP); RAMP=null; }
  if(instant || Math.abs(v-from)<1e-6){ applyRate(v); return; }
  var t0=performance.now(), DUR=260;
  (function step(now){
    var k=Math.min(1,(now-t0)/DUR);
    var e=1-Math.pow(1-k,3);
    applyRate(from+(v-from)*e);
    if(k<1) RAMP=requestAnimationFrame(step); else RAMP=null;
  })(performance.now());
}
$("speed").addEventListener("input", function(){ setSpeed(this.value); });
$("speedNum").addEventListener("input", function(){
  var s=this.value.trim(), n=parseFloat(s);
  if(s==="" || isNaN(n) || n<SP_MIN) return;   // half-typed value: wait for more
  setSpeed(n, false, true);
});
$("speedNum").addEventListener("change", function(){ setSpeed(this.value); });
$("speedNum").addEventListener("blur", function(){ setSpeed(this.value); });

/* ---------- classic 3-band bars + limiter ----------
   Bar 1 = low (kick and bass, ~30-250 Hz), bar 2 = mid (vocals and body,
   ~250 Hz-4 kHz), bar 3 = high (hats and cymbals, ~4-16 kHz). Each bar reads
   its own slice of the FFT, so they move independently - a track with a big
   kick and no hats pushes bar 1 only. */
var OPAC=1;                                       /* bar opacity, knob-driven */
var MAXDB=-6, RATIO=12, LIMIT_ON=true;            /* the limiter */

var VIZ=null, VX=null, VAC=null, VSRC=null, VAN=null, VFFT=null;
var COMP=null, MASTER=null;
var vlev=[0,0,0], vtar=[0,0,0], vpeak=[0.12,0.10,0.08], vprev=0;
var vrun=false, vinit=false, vpaint=null;
var VRANGE=[[30,250],[250,4000],[4000,16000]];   /* low, mid, high, in Hz */

/* Per-bar customizable reactivity settings */
var BAR_SETTINGS = [
  { atk: 14, rel: 5.2, ceil: 1.35, curve: 2.1 }, /* Bar 0: Low */
  { atk: 14, rel: 5.2, ceil: 1.35, curve: 2.1 }, /* Bar 1: Mid */
  { atk: 14, rel: 5.2, ceil: 1.35, curve: 2.1 }  /* Bar 2: High */
];
try{
  var savedBarS = JSON.parse(lsGet("player_bar_settings")||"null");
  if(Array.isArray(savedBarS) && savedBarS.length===3){
    for(var bi=0; bi<3; bi++){
      if(savedBarS[bi]){
        if(savedBarS[bi].atk) BAR_SETTINGS[bi].atk = +savedBarS[bi].atk;
        if(savedBarS[bi].rel) BAR_SETTINGS[bi].rel = +savedBarS[bi].rel;
        if(savedBarS[bi].ceil) BAR_SETTINGS[bi].ceil = +savedBarS[bi].ceil;
        if(savedBarS[bi].curve) BAR_SETTINGS[bi].curve = +savedBarS[bi].curve;
      }
    }
  }
}catch(e){}

function vizSize(){
  VIZ.width =innerWidth *devicePixelRatio;
  VIZ.height=innerHeight*devicePixelRatio;
  VIZ.style.width =innerWidth +"px";
  VIZ.style.height=innerHeight+"px";
}
function vizDraw(){
  if(!VIZ) return;
  VX.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);
  VX.clearRect(0,0,innerWidth,innerHeight);
  if(pos<0) return;                     /* nothing playing: no idle boxes over the list */
  var bw=Math.min(112, innerWidth*0.085), gap=bw*0.62;
  var total=bw*3+gap*2, x0=(innerWidth-total)/2;
  var base=innerHeight*0.54, maxH=innerHeight*0.3;
  VX.globalAlpha=OPAC;
  for(var k=0;k<3;k++){
    var p=Math.max(0, Math.min(1, vlev[k]));
    /* Keep bar height contained so it feels grounded and doesn't constantly ceiling out */
    var h=Math.max(4, p*maxH);
    var x=x0+k*(bw+gap);
    VX.strokeStyle="rgba(143,214,255,.07)"; VX.lineWidth=1;
    VX.strokeRect(x+.5, base-maxH+.5, bw-1, maxH-1);
    
    var ar=_curAcc[0], ag=_curAcc[1], ab=_curAcc[2];
    /* Calculate dynamic highlight color: as p approaches 1, lighten nicely toward white without blinding */
    var hr=Math.min(255, Math.round(ar + (255-ar)*p*0.58));
    var hg=Math.min(255, Math.round(ag + (255-ag)*p*0.58));
    var hb=Math.min(255, Math.round(ab + (255-ab)*p*0.58));

    /* Gradient gets richer and brighter with height */
    var g=VX.createLinearGradient(0, base-h, 0, base);
    var topAlpha=0.15 + 0.65*Math.pow(p, 1.25);
    g.addColorStop(0, "rgba("+hr+","+hg+","+hb+","+topAlpha.toFixed(3)+")");
    g.addColorStop(0.4, "rgba("+ar+","+ag+","+ab+","+(topAlpha*0.55).toFixed(3)+")");
    g.addColorStop(1, "rgba("+ar+","+ag+","+ab+",.02)");
    
    /* Subtle glow shadow when reaching high levels */
    if(p > 0.45){
      VX.shadowColor="rgba("+hr+","+hg+","+hb+","+(0.35*(p-0.45)/0.55).toFixed(2)+")";
      VX.shadowBlur=Math.round(8*p);
    } else {
      VX.shadowColor="transparent";
      VX.shadowBlur=0;
    }
    
    VX.fillStyle=g; VX.fillRect(x, base-h, bw, h);
    
    /* Top cap: vibrant glowing cap that lights up with intensity */
    var capAlpha=Math.min(1.0, 0.25 + 0.70*Math.pow(p, 0.85));
    VX.fillStyle="rgba("+hr+","+hg+","+hb+","+capAlpha.toFixed(3)+")";
    VX.fillRect(x, base-h, bw, Math.max(3, Math.round(3 + 2.5*p)));
    VX.shadowBlur=0;
  }
  VX.globalAlpha=1;
  VX.fillStyle="rgba(143,214,255,.10)";
  VX.fillRect(x0-24, base, total+48, 1);
}
/* Sampling runs on its own clock, not inside the paint loop, so a short
   transient is not missed whenever the frame rate drops. */
function vizSample(){
  if(!VAN || !VFFT) return;
  if(au.paused){ vtar[0]=vtar[1]=vtar[2]=0; return; }
  VAN.getByteFrequencyData(VFFT);
  var nyq=VAC.sampleRate/2, nb=VFFT.length;
  for(var b=0;b<3;b++){
    var lo=Math.max(1, Math.floor(VRANGE[b][0]/nyq*nb));
    var hi=Math.min(nb, Math.ceil(VRANGE[b][1]/nyq*nb));
    var s=0, n=0;
    for(var i=lo;i<hi;i++){ s+=VFFT[i]; n++; }
    var v=n? (s/n)/255 : 0;
    /* Bass carries far more energy than treble, so each band is divided by its
       own peak - but that peak must drift VERY slowly (~1 min half life) or
       every bar normalises to its own recent maximum and they all sit pinned
       at the top looking identical. A gamma then spreads the mid-range out so
       quiet passages actually read as quiet. */
    var cfg = BAR_SETTINGS[b];
    if(v>vpeak[b]) vpeak[b]=v;
    else vpeak[b]=Math.max(0.08, vpeak[b]*0.9996);
    var ceilingFactor = Math.max(0.10, vpeak[b] * cfg.ceil);
    var ratio = Math.min(1.0, v / ceilingFactor);
    vtar[b]=Math.pow(ratio, cfg.curve);
  }
}
function vizFrame(){
  if(!vrun){ vpaint=null; return; }
  var now=performance.now();
  var dt=vprev? Math.min(0.25,(now-vprev)/1000) : 1/60;
  vprev=now;
  var quiet=true;
  for(var b=0;b<3;b++){
    /* snap up fast, settle back slowly - reads as impact rather than mush */
    var cfg = BAR_SETTINGS[b];
    var rate=(vtar[b]>vlev[b])? cfg.atk : cfg.rel;
    vlev[b]+= (vtar[b]-vlev[b])*(1-Math.exp(-rate*dt));
    if(vlev[b]>0.002) quiet=false;
  }
  vizDraw();
  paintLimiter();
  /* stop burning frames once everything has settled and nothing is playing */
  if(quiet && au.paused){ vrun=false; vpaint=null; return; }
  vpaint=requestAnimationFrame(vizFrame);
}
/* Audio graph: source -> analyser -> limiter -> master -> speakers.
   The analyser sits BEFORE the limiter, so the bars show the true level of
   the song and you can see exactly what the limiter is holding down.
   createMediaElementSource can only ever be called once per element, so the
   graph is wired exactly once, on the first play (a user gesture, which is
   also what lets the AudioContext start un-suspended). */
var EQ_FREQS = [60, 250, 1000, 4000, 12000];
var EQ_LABELS = ['Bass 60Hz', 'Low-Mid 250Hz', 'Mid 1kHz', 'High-Mid 4kHz', 'Treble 12kHz'];
var EQ_NODES = [];
var EQ_GAINS = [0, 0, 0, 0, 0];
var EQ_PRESETS = {
  "Flat": [0, 0, 0, 0, 0],
  "Bass Boost": [6, 4, 1, 0, -1],
  "Vocal Boost": [-2, 1, 4, 3, 1],
  "Treble Boost": [-2, 0, 1, 4, 6],
  "Rock": [4, 2, -1, 2, 4],
  "Electronic": [5, 3, -1, 2, 4],
  "Chill / Lofi": [4, 2, -2, -2, -4]
};
try{
  var eqSaved = JSON.parse(lsGet("player-eq")||"null");
  if(Array.isArray(eqSaved) && eqSaved.length===5) EQ_GAINS = eqSaved;
}catch(e){}

function applyEQ(){
  if(!EQ_NODES.length) return;
  for(var i=0;i<5;i++){
    if(EQ_NODES[i]) EQ_NODES[i].gain.value = EQ_GAINS[i];
  }
}

/* Sleep timer */
var sleepTimerId=null, sleepTarget=0, sleepMode="0";
function setSleepTimer(val){
  sleepMode = val;
  if(sleepTimerId){ clearInterval(sleepTimerId); sleepTimerId = null; }
  var stat = $("sleepstat");
  if(val==="0"){
    if(stat) stat.textContent = "Off";
    return;
  }
  if(val==="end"){
    if(stat) stat.textContent = "At track end";
    return;
  }
  var mins = parseInt(val, 10);
  sleepTarget = Date.now() + mins * 60 * 1000;
  sleepTimerId = setInterval(updateSleepCountdown, 1000);
  updateSleepCountdown();
}
function updateSleepCountdown(){
  var stat = $("sleepstat");
  var diff = Math.max(0, Math.round((sleepTarget - Date.now())/1000));
  if(diff<=0){
    clearInterval(sleepTimerId); sleepTimerId = null;
    au.pause();
    sleepMode = "0";
    if($("sleeppick")) $("sleeppick").value = "0";
    if(stat) stat.textContent = "Off";
    if(MASTER) MASTER.gain.value = Math.max(0, Math.min(1, (+$("vol").value||0)/100));
    return;
  }
  if(diff <= 15 && MASTER){
    var baseVol = Math.max(0, Math.min(1, (+$("vol").value||0)/100));
    MASTER.gain.value = baseVol * (diff / 15);
  }
  var m = Math.floor(diff/60), s = diff % 60;
  if(stat) stat.textContent = m + ":" + (s < 10 ? "0" : "") + s;
}
au.addEventListener("ended", function(){
  if(sleepMode === "end"){
    au.pause();
    sleepMode = "0";
    if($("sleeppick")) $("sleeppick").value = "0";
    if($("sleepstat")) $("sleepstat").textContent = "Off";
  }
});

function initGraph(){
  if(vinit) return; vinit=true;
  try{
    VAC=new (window.AudioContext||window.webkitAudioContext)();
    VSRC=VAC.createMediaElementSource(au);
    VAN=VAC.createAnalyser();
    VAN.fftSize=2048; VAN.smoothingTimeConstant=0.55;
    VFFT=new Uint8Array(VAN.frequencyBinCount);
    COMP=VAC.createDynamicsCompressor();
    MASTER=VAC.createGain();
    au.volume=1;                       /* loudness lives on MASTER, not here */
    MASTER.gain.value=Math.max(0, Math.min(1, (+$("vol").value||0)/100));

    EQ_NODES = [];
    for(var b=0; b<5; b++){
      var filter = VAC.createBiquadFilter();
      if(b===0){
        filter.type = "lowshelf";
        filter.frequency.value = 60;
      } else if(b===4){
        filter.type = "highshelf";
        filter.frequency.value = 12000;
      } else {
        filter.type = "peaking";
        filter.frequency.value = EQ_FREQS[b];
        filter.Q.value = 1.0;
      }
      filter.gain.value = EQ_GAINS[b];
      EQ_NODES.push(filter);
    }
    VSRC.connect(VAN);
    var prevNode = VAN;
    for(var b=0; b<5; b++){
      prevNode.connect(EQ_NODES[b]);
      prevNode = EQ_NODES[b];
    }
    prevNode.connect(COMP);
    COMP.connect(MASTER);
    MASTER.connect(VAC.destination);   /* without this: silence */
    applyLimit();
  }catch(e){ VAC=VAN=COMP=MASTER=null; return; }
  setInterval(vizSample, 25);
}
function applyLimit(){
  if(!COMP) return;
  COMP.threshold.value=MAXDB;
  COMP.knee.value=LIMIT_ON? 6 : 0;
  COMP.ratio.value=LIMIT_ON? RATIO : 1;
  COMP.attack.value=0.003;           /* fast enough to catch a spike */
  COMP.release.value=0.18;
}
function paintLimiter(){
  var el=$('limrd');
  if(!COMP){ el.textContent='lim n/a'; return; }
  var red=COMP.reduction||0;
  el.textContent='lim '+red.toFixed(1)+' dB';
  el.classList.toggle('hot', red<-0.5);
}
function vizPlay(){
  initGraph();
  if(VAC && VAC.state==="suspended") VAC.resume();
  if(!VIZ) return;                          /* reduced motion: nothing to draw */
  if(!vrun){ vrun=true; vprev=0; vpaint=requestAnimationFrame(vizFrame); }
}
au.addEventListener("play", vizPlay);
setInterval(function(){ if(COMP) paintLimiter(); }, 250);
/* no pause listener needed: vizFrame lets the bars fall, then parks itself
   as soon as they are flat and the audio has stopped. */
(function(){
  if(matchMedia("(prefers-reduced-motion: reduce)").matches){
    var c=$("viz"); if(c) c.remove();
    VIZ=null; return;                      /* motion is off - do not animate */
  }
  VIZ=$("viz"); VX=VIZ.getContext("2d"); vizSize();
  addEventListener("resize", function(){ if(VIZ){ vizSize(); if(!vrun) vizDraw(); } });
  vizDraw();                                  /* parked at rest until first play */
})();


/* ---------- Bar Reactivity UI Controller ---------- */
(function(){
  var curBar = 0;
  var barModal = $('barModal');
  var barBtn = $('barbtn');
  var barClose = $('barClose');
  var barPick = $('barPick');
  var container = $('barControls');

  function saveSettings(){
    lsSet("player_bar_settings", JSON.stringify(BAR_SETTINGS));
  }

  function renderControls(){
    if(!container) return;
    var cfg = BAR_SETTINGS[curBar];
    var names = ["1: Low (Bass)", "2: Mid", "3: High"];
    var html = '<div style="font-size:12px; font-weight:700; color:var(--acc); margin-bottom:12px; display:flex; justify-content:space-between">' +
      '<span>Editing: ' + names[curBar] + '</span>' +
      '<button id="barResetCur" style="background:none; border:none; color:var(--mut); font-size:11px; cursor:pointer; text-decoration:underline">Reset</button>' +
      '</div>';

    // 1. Attack / Reactivity speed
    html += '<div class="knob"><div class="kh"><span>Hit Speed (Attack)</span><span class="kv">' + cfg.atk.toFixed(1) + '</span></div>' +
      '<input type="range" min="4" max="30" step="0.5" value="' + cfg.atk + '" id="b_atk"></div>';

    // 2. Release / Decay speed
    html += '<div class="knob"><div class="kh"><span>Drop Speed (Release)</span><span class="kv">' + cfg.rel.toFixed(1) + '</span></div>' +
      '<input type="range" min="1" max="15" step="0.5" value="' + cfg.rel + '" id="b_rel"></div>';

    // 3. Absolute Limit / Ceiling headroom
    html += '<div class="knob"><div class="kh"><span>Ceiling Limit (Resistance)</span><span class="kv">' + cfg.ceil.toFixed(2) + 'x</span></div>' +
      '<input type="range" min="0.8" max="2.5" step="0.05" value="' + cfg.ceil + '" id="b_ceil"></div>';

    // 4. Heaviness / Progressive resistance curve
    html += '<div class="knob"><div class="kh"><span>Weight / Curve</span><span class="kv">' + cfg.curve.toFixed(2) + '</span></div>' +
      '<input type="range" min="1.0" max="3.5" step="0.1" value="' + cfg.curve + '" id="b_curve"></div>';

    container.innerHTML = html;

    $('b_atk').addEventListener('input', function(){
      cfg.atk = +this.value;
      this.previousElementSibling.querySelector('.kv').textContent = cfg.atk.toFixed(1);
      saveSettings();
    });
    $('b_rel').addEventListener('input', function(){
      cfg.rel = +this.value;
      this.previousElementSibling.querySelector('.kv').textContent = cfg.rel.toFixed(1);
      saveSettings();
    });
    $('b_ceil').addEventListener('input', function(){
      cfg.ceil = +this.value;
      this.previousElementSibling.querySelector('.kv').textContent = cfg.ceil.toFixed(2) + 'x';
      saveSettings();
    });
    $('b_curve').addEventListener('input', function(){
      cfg.curve = +this.value;
      this.previousElementSibling.querySelector('.kv').textContent = cfg.curve.toFixed(2);
      saveSettings();
    });
    $('barResetCur').addEventListener('click', function(){
      BAR_SETTINGS[curBar] = { atk: 14, rel: 5.2, ceil: 1.35, curve: 2.1 };
      saveSettings();
      renderControls();
    });
  }

  if(barPick){
    barPick.addEventListener('click', function(e){
      var b = e.target.closest('button[data-b]');
      if(!b) return;
      curBar = +b.dataset.b;
      barPick.querySelectorAll('button').forEach(function(el){
        el.classList.toggle('on', +el.dataset.b === curBar);
      });
      renderControls();
    });
  }

  if(barBtn){
    barBtn.addEventListener('click', function(){
      var open = !barModal.classList.contains('on');
      barModal.classList.toggle('on', open);
      barBtn.classList.toggle('on', open);
      if(open){
        $('knobs').classList.remove('on');
        $('knobbtn').classList.remove('on');
        renderControls();
      }
    });
  }

  if(barClose){
    barClose.addEventListener('click', function(){
      barModal.classList.remove('on');
      barBtn.classList.remove('on');
    });
  }
})();

/* ---------- knobs panel ---------- */
var KNOBS=[
 {id:'opac',  label:'Bar opacity', min:15, max:100, step:1, dec:0,
  get:function(){return Math.round(OPAC*100);}, set:function(v){OPAC=v/100;}},
 {id:'maxdb', label:'Limiter max level', min:-30, max:0, step:1, dec:0, suf:' dB',
  get:function(){return MAXDB;}, set:function(v){MAXDB=v; applyLimit();}},
 {id:'ratio', label:'Limiter ratio', min:1, max:20, step:1, dec:0, suf:':1',
  get:function(){return RATIO;}, set:function(v){RATIO=v; applyLimit();}}
];
(function(){
  var body=$('kbody');
  var html = '';

  // 1. Equalizer Section
  html += '<div class="ksec">5-Band Equalizer</div>';
  html += '<select id="eqpreset" class="kselect"><option value="">-- Choose EQ Preset --</option>';
  for(var pr in EQ_PRESETS){
    html += '<option value="'+pr+'">'+pr+'</option>';
  }
  html += '</select>';
  for(var e=0; e<5; e++){
    html += '<div class="knob"><div class="kh"><span>'+EQ_LABELS[e]+'</span><span class="kv" id="kv_eq_'+e+'">'+(EQ_GAINS[e]>0?'+':'')+EQ_GAINS[e]+' dB</span></div>' +
      '<input type="range" id="k_eq_'+e+'" min="-12" max="12" step="1" value="'+EQ_GAINS[e]+'" data-eq="'+e+'"></div>';
  }

  // 2. Sleep Timer Section
  html += '<div class="ksec">Sleep Timer <span id="sleepstat" style="float:right; font-weight:400; color:var(--mut)">Off</span></div>';
  html += '<select id="sleeppick" class="kselect">' +
    '<option value="0">Off</option>' +
    '<option value="15">15 minutes</option>' +
    '<option value="30">30 minutes</option>' +
    '<option value="45">45 minutes</option>' +
    '<option value="60">60 minutes</option>' +
    '<option value="end">At track end</option>' +
    '</select>';

  // 3. Dynamics Limiter Section
  html += '<div class="ksec">Dynamics & Display</div>';
  for(var i=0;i<KNOBS.length;i++){
    var k=KNOBS[i];
    html+='<div class="knob"><div class="kh"><span>'+k.label+
          '</span><span class="kv" id="kv_'+k.id+'"></span></div>'+
          '<input type="range" id="k_'+k.id+'" min="'+k.min+'" max="'+k.max+
          '" step="'+k.step+'" data-k="'+i+'"></div>';
  }
  body.innerHTML = html;

  // Listeners
  $('eqpreset').addEventListener('change', function(){
    var p = EQ_PRESETS[this.value];
    if(!p) return;
    EQ_GAINS = p.slice();
    lsSet("player-eq", JSON.stringify(EQ_GAINS));
    for(var e=0; e<5; e++){
      $('k_eq_'+e).value = EQ_GAINS[e];
      $('kv_eq_'+e).textContent = (EQ_GAINS[e]>0?'+':'')+EQ_GAINS[e]+' dB';
    }
    applyEQ();
  });

  body.addEventListener('input', function(e){
    var t=e.target;
    if(t.dataset && t.dataset.eq!==undefined){
      var idx = +t.dataset.eq;
      var val = +t.value;
      EQ_GAINS[idx] = val;
      $('kv_eq_'+idx).textContent = (val>0?'+':'')+val+' dB';
      lsSet("player-eq", JSON.stringify(EQ_GAINS));
      applyEQ();
      $('eqpreset').value = "";
      return;
    }
    if(t.dataset && t.dataset.k!==undefined){
      var kk=KNOBS[+t.dataset.k];
      kk.set(+t.value); paint();
      if(!vrun) vizDraw();
    }
  });

  $('sleeppick').addEventListener('change', function(){
    setSleepTimer(this.value);
  });

  function paint(){
    for(var j=0;j<KNOBS.length;j++){
      var kk=KNOBS[j], el=$('k_'+kk.id);
      if(el){
        el.value=kk.get();
        $('kv_'+kk.id).textContent=kk.get()+(kk.suf||'');
      }
    }
  }
  window.paintKnobs=paint;
  paint();
})();
$('knobbtn').addEventListener('click', function(){
  var p=$('knobs');
  var open=!p.classList.contains('on');      /* #knobs is display:none by CSS,
                                                .on reveals it */
  p.classList.toggle('on', open);
  this.classList.toggle('on', open);
  if(open && window.paintKnobs) window.paintKnobs();
});
$('limon').addEventListener('click', function(){
  LIMIT_ON=!LIMIT_ON; applyLimit();
  this.textContent='Limiter: '+(LIMIT_ON?'on':'off');
  this.classList.toggle('on', LIMIT_ON);
});
/* ---------- (end visualizer integration) ---------- */

function setSyncOn(on){
  LYR_ON=on;
  $("syncBar").hidden=!on;
  $("lyrSyncBtn").innerHTML = on? "&#10005; Stop sync" : "&#9201; Sync";
  $("lyrSyncBtn").classList.toggle("on", on);
  if(!on) setSpeed(1);                  // never leave normal listening slowed down
  LYR_SEL=-1;
  renderLines(); paintLyrStat(); paintSync();
}
function renderLines(){
  var times = LYR? LYR.times : {};
  var cur = (LYR && pos===LYR_I)? lyricIndexAt(LYR, au.currentTime) : -1;
  var html="", shown=0;
  for(var i=0;i<LYR_LINES.length;i++){
    var tx=LYR_LINES[i];
    if(!tx.trim()){ html+='<div class="ln gap"></div>'; continue; }
    shown++;
    var ts=times[String(i)];
    html+='<div class="ln'+(ts!=null?" hit":"")+(i===LYR_SEL?" sel":"")+
          (i===cur?" cur":"")+'" data-i="'+i+'">'+
          '<span class="lt">'+(ts!=null? fmt(ts) : "&ndash;:&ndash;&ndash;")+'</span>'+
          '<span class="lx">'+esc(tx)+'</span></div>';
  }
  if(!shown) html='<div class="empty">Nothing to time yet.</div>';
  $("lyrLines").innerHTML=html;
}
function counts(){
  var times=LYR?LYR.times:{}, n=0, tot=0;
  for(var i=0;i<LYR_LINES.length;i++){
    if(!LYR_LINES[i] || !LYR_LINES[i].trim()) continue;
    tot++;
    if(times[String(i)]!=null) n++;
  }
  return [n, tot];
}
function paintLyrStat(){
  var c=counts();
  $("lyrStat").textContent = c[0] ? (c[0]+" of "+c[1]+" lines timed")
                                 : (c[1]+" lines · not timed yet");
}
function paintSync(){
  if($("syncBar").hidden) return;
  var c=counts(), here = pos===LYR_I;
  $("sProg").textContent = c[0]+" of "+c[1]+" timed";
  $("sDur").textContent = (here && isFinite(au.duration))? fmt(au.duration):"–:––";
  $("sNow").textContent = here? fmtms(au.currentTime) : "–:––";
  var msg = LYR_SEL<0 ? "Click a line below to select it"
          : "Line "+(LYR_SEL+1)+": "+(LYR_LINES[LYR_SEL]||"");
  if(!here) msg += "   ·   ▶ play this song first";
  $("sSel").textContent=msg;
  $("stamp").disabled = (LYR_SEL<0);
}
function nextFree(from){
  var times=LYR?LYR.times:{}, i;
  for(i=from+1;i<LYR_LINES.length;i++)
    if(LYR_LINES[i].trim() && times[String(i)]==null) return i;
  for(i=0;i<=from;i++)
    if(times[String(i)]==null && LYR_LINES[i].trim()) return i;
  return -1;
}
function stamp(){
  if(!LYR || LYR_I<0) return;
  if(LYR_SEL<0){ $("sSel").textContent="Click a line first"; return; }
  if(pos!==LYR_I){ $("sSel").textContent="▶ Play this song first, then hit SYNC"; return; }
  var path=LIB[LYR_I].path, sel=LYR_SEL, t=Math.max(0, au.currentTime||0);
  jpost("/api/lyrics/sync",{path:path, index:sel, time:t}).then(function(d){
    LYR=d; LYR_LINES=d.lines||[];
    LYRSYNC[path]=1;
    LYR_SEL=nextFree(sel);
    renderLines(); paintLyrStat(); paintSync(); paintDeckLyricBtn();
    if(pos===LYR_I){ DECK_LYR=LYR; DECK_LYR_I=pos; paintLyricLine(); }
    /* leave the button so Space/Enter can't stamp the NEXT line by accident */
    if(document.activeElement && document.activeElement.blur)
      document.activeElement.blur();
  }).catch(function(e){
    $("sSel").textContent = (e&&e.message)? e.message : "could not save that timestamp";
  });
}
function followLyric(){
  if($("lyr").hidden || !LYR || pos!==LYR_I) return;
  var i=lyricIndexAt(LYR, au.currentTime);
  if(i===followLyric.last) return;
  followLyric.last=i;
  renderLines();
  if(i>=0){
    try{
      var el=document.querySelector('#lyrLines .ln[data-i="'+i+'"]');
      if(el) el.scrollIntoView({block:"center", behavior:"smooth"});
    }catch(e){}
  }
}
followLyric.last=-1;

function saveLyrics(){
  if(LYR_I<0) return;
  var s=LIB[LYR_I]; if(!s) return;
  var b=$("lyrSave"), txt=$("lyrText").value;
  b.disabled=true; b.textContent="Saving…";
  jpost("/api/lyrics/save",{path:s.path, text:txt}).then(function(d){
    b.disabled=false; b.innerHTML="Save lyrics";
    LYR=(d && typeof d.text==="string") ? d : null;
    LYR_LINES = LYR? LYR.lines : [];
    LYR_SEL=-1; followLyric.last=-1;
    if(LYR && LYR.times && Object.keys(LYR.times).length) LYRSYNC[s.path]=1;
    else delete LYRSYNC[s.path];
    if(LYR) LYRHAVE[s.path]=1; else delete LYRHAVE[s.path];
    if(pos===LYR_I){ DECK_LYR=LYR; DECK_LYR_I=pos; }
    if(LYR){ showLyricsView(); rerender(); }
    else closeLyrics();
    paintDeck();
  }).catch(function(e){
    b.disabled=false; b.innerHTML="Save lyrics";
    alert("Could not save the lyrics:\n\n"+(e&&e.message?e.message:String(e)));
  });
}
function forgetLyrics(){
  if(LYR_I<0 || !LYR) return;
  var s=LIB[LYR_I];
  if(!window.confirm("Delete the lyrics AND every timestamp for\n\n"+
                     s.title+"\n\nThis cannot be undone.")) return;
  jpost("/api/lyrics/forget",{path:s.path, confirm:true}).then(function(){
    LYR=null; LYR_LINES=[]; LYR_SEL=-1;
    delete LYRHAVE[s.path]; delete LYRSYNC[s.path];
    if(pos===LYR_I){ DECK_LYR=null; DECK_LYR_I=pos; }
    closeLyrics();
    paintDeck();
  }).catch(function(e){
    alert("Could not delete the lyrics:\n\n"+(e&&e.message?e.message:String(e)));
  });
}

$("lyrBack").addEventListener("click", closeLyrics);
$("lyrCancel").addEventListener("click", function(){
  if(LYR) showLyricsView(); else closeLyrics();
});
$("lyrPlay").addEventListener("click", function(){
  if(LYR_I<0) return;
  if(pos===LYR_I){ if(au.paused) au.play().catch(function(){}); return; }
  playIndex(LYR_I);
});
$("lyrSave").addEventListener("click", saveLyrics);
$("lyrEditBtn").addEventListener("click", showLyricsEdit);
$("lyrForget").addEventListener("click", forgetLyrics);
$("lyrSyncBtn").addEventListener("click", function(){ setSyncOn(!LYR_ON); });
$("syncDone").addEventListener("click", function(){ setSyncOn(false); });
$("stamp").addEventListener("click", stamp);
$("lyrFetchBtn").addEventListener("click", function(){
  if(LYR_I<0 || !LIB[LYR_I]) return;
  var s = LIB[LYR_I];
  var btn = this;
  btn.textContent = "Fetching…";
  fetch("/api/lyrics/fetch?artist="+encodeURIComponent(s.artist||"")+"&title="+encodeURIComponent(s.title||""))
    .then(function(r){ return r.json(); })
    .then(function(res){
      btn.innerHTML = "&#9889; Auto-fetch";
      if(res && res.text){
        $("lyrText").value = res.text;
        if(res.times && Object.keys(res.times).length > 0){
          jpost("/api/lyrics/save", {path: s.path, text: res.text}).then(function(){
            var promises = [];
            for(var k in res.times){
              promises.push(jpost("/api/lyrics/sync", {path: s.path, index: +k, time: res.times[k]}));
            }
            Promise.all(promises).then(function(){
              loadLyricIndex();
              openLyrics(LYR_I);
            });
          });
        }
      } else {
        alert("No online lyrics found for this track. You can paste or type them manually!");
      }
    }).catch(function(){
      btn.innerHTML = "&#9889; Auto-fetch";
      alert("Could not reach lyrics service.");
    });
});
$("lyrExportBtn").addEventListener("click", function(){
  if(LYR_I<0 || !LIB[LYR_I] || !LYR) return;
  var s = LIB[LYR_I];
  var lrcContent = "";
  var lines = LYR_LINES || [];
  var times = LYR.times || {};
  for(var i=0; i<lines.length; i++){
    var t = times[String(i)];
    if(t!=null){
      var m = Math.floor(t/60), sec = (t%60).toFixed(2);
      lrcContent += "[" + (m<10?"0":"") + m + ":" + (sec<10?"0":"") + sec + "]" + lines[i] + "\n";
    } else {
      lrcContent += lines[i] + "\n";
    }
  }
  var blob = new Blob([lrcContent], {type: "text/plain;charset=utf-8"});
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (s.title || "song") + ".lrc";
  a.click();
});
$("lyrImportBtn").addEventListener("click", function(){
  $("lyrFileInput").click();
});
$("lyrFileInput").addEventListener("change", function(){
  var file = this.files[0];
  if(!file) return;
  var reader = new FileReader();
  reader.onload = function(e){
    var content = e.target.result;
    var raw_lines = content.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split("\n");
    var stampRe = /^\[(\d{1,2}):(\d{2}(?:\.\d+)?)\](.*)$/;
    var cleanLines = [];
    for(var l=0; l<raw_lines.length; l++){
      var str = raw_lines[l].trim();
      var m = stampRe.exec(str);
      if(m){
        cleanLines.push(m[3].trim());
      } else {
        if(str.startsWith("[") && str.indexOf(":")>0 && str.endsWith("]")) continue;
        cleanLines.push(str);
      }
    }
    $("lyrText").value = cleanLines.join("\n");
  };
  reader.readAsText(file);
});
$("lyrLines").addEventListener("click", function(e){
  var ln=e.target.closest(".ln");
  if(!ln || ln.dataset.i===undefined) return;
  var i=+ln.dataset.i;
  if(LYR_ON){ LYR_SEL=i; renderLines(); paintSync(); return; }
  var ts=(LYR && LYR.times)? LYR.times[String(i)] : null;
  if(ts==null) return;
  if(pos!==LYR_I) playIndex(LYR_I);
  try{ au.currentTime=ts; }catch(err){}
});
au.addEventListener("timeupdate", function(){
  paintLyricLine();
  if($("lyr").hidden) return;
  if(!$("syncBar").hidden) paintSync();
  followLyric();
});
au.addEventListener("loadedmetadata", function(){
  setSpeed(LYR_RATE);              // a new src must never silently reset the rate
  if(pos===LYR_I) $("lyrDur").textContent=fmt(au.duration);
  if(!$("syncBar").hidden) paintSync();
  paintLyricLine();
});

/* ---- restore preferences ---- */
GROUP   = lsGet("player-group")==="1";
MOOD    = lsGet("player-mood") || "";
FAVONLY = lsGet("player-fav")==="1";
$("group").classList.toggle("on", GROUP);
$("favfilter").classList.toggle("on", FAVONLY);
var savedVol=parseInt(lsGet("player-vol"),10);
if(isNaN(savedVol) || savedVol<0 || savedVol>100) savedVol=85;
$("vol").value=savedVol;
au.volume=1;                       /* loudness is applied by MASTER on play */
if(MASTER) MASTER.gain.value=savedVol/100;
paintVol();
paintSeek();

var _libRev = -1;
function checkLibraryWatcher(){
  fetch("/api/version")
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(_libRev === -1){
        _libRev = d.rev;
        return;
      }
      if(d.rev !== _libRev){
        _libRev = d.rev;
        fetchLib().then(function(libData){
          load(libData);
        }).catch(function(){});
      }
    })
    .catch(function(){});
}
setInterval(checkLibraryWatcher, 1200);

fetchLib().then(function(d){
  if(d && d.rev) _libRev = d.rev;
  load(d);
}).catch(function(){
  $("count").textContent="failed to load library";
});
loadFavs();
loadLyricIndex();
/* cache the shell so the player still opens when the laptop is unreachable
   (sw.js). Needs a secure context - on a plain http://IP address that means
   the Chrome flag; registration failure is harmless. */
if("serviceWorker" in navigator &&
   (location.protocol==="http:"||location.protocol==="https:")){
  navigator.serviceWorker.register("/sw.js").catch(function(){});
}
</script>
</body>
</html>
"""


# ---- spectrum demo: /spectrum -------------------------------------------------
# A standalone playground for the visualiser ideas. Same origin as the player, so
# it plays the real library through /audio/{i} - no copies, no CORS.
SPECTRUM_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Spectrum - test bench</title>
<style>
  :root{
    --bg:#0e1016; --card:#161a23; --card2:#1d2230; --line:#252b3a;
    --fg:#e6e9f0; --mut:#8b93a7; --acc:#5eead4; --now:#3ecf8e;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--fg);display:flex;flex-direction:column;
    font:13px/1.5 ui-sans-serif,system-ui,"Segoe UI",Roboto,sans-serif}

  header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
    padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card)}
  h1{margin:0;font-size:15px;display:flex;align-items:center;gap:8px}
  h1 .dot{width:8px;height:8px;border-radius:50%;background:var(--now);
    box-shadow:0 0 8px var(--now)}
  .spacer{flex:1}
  .seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .seg button{background:var(--card2);border:none;color:var(--mut);
    padding:5px 11px;font-size:12px;cursor:pointer;font:inherit}
  .seg button+button{border-left:1px solid var(--line)}
  .seg button.on{background:var(--acc);color:#06211c;font-weight:800}
  .lbl{font-size:10.5px;font-weight:800;letter-spacing:.09em;
    text-transform:uppercase;color:var(--mut)}

  main{flex:1;display:flex;min-height:0}
  aside{width:290px;flex:none;overflow-y:auto;border-right:1px solid var(--line);
    background:var(--card)}
  .row{display:flex;gap:9px;align-items:center;padding:7px 12px;cursor:pointer;
    border-bottom:1px solid rgba(255,255,255,.02)}
  .row:hover{background:var(--card2)}
  .row.on{background:rgba(var(--acc-r),var(--acc-g),var(--acc-b),.1)}
  .row .n{color:var(--mut);font-size:11px;width:20px;text-align:right;flex:none}
  .row .m{min-width:0;flex:1}
  .row .t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .row .a{color:var(--mut);font-size:11px;white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis}

  .stage{flex:1;position:relative;min-width:0}
  #cv{position:absolute;inset:0;display:block}

  footer{border-top:1px solid var(--line);background:var(--card);padding:11px 16px;
    display:flex;align-items:center;gap:13px}
  .np{width:250px;flex:none;min-width:0}
  .np .t{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .np .a{color:var(--mut);font-size:11px}
  button.tr{background:var(--card2);border:1px solid var(--line);color:var(--fg);
    border-radius:8px;padding:7px 13px;cursor:pointer;font:inherit;font-size:13px}
  button.tr:hover{border-color:var(--mut)}
  button.tr.main{background:var(--acc);border-color:var(--acc);color:#06211c;
    font-weight:800;min-width:64px}
  .seek{flex:1;height:5px;border-radius:5px;background:var(--card2);position:relative;
    cursor:pointer}
  .seek i{position:absolute;left:0;top:0;bottom:0;width:0;border-radius:5px;
    background:var(--acc)}
  .tm{color:var(--mut);font-variant-numeric:tabular-nums;font-size:12px;flex:none}
  input[type=range]{width:96px;accent-color:var(--acc)}
  .empty{padding:26px;color:var(--mut)}
</style>
</head>
<body>

<header>
  <h1><span class="dot"></span>Spectrum bench</h1>
  <span class="lbl">bars</span>
  <div class="seg" id="bars">
    <button data-n="16">16</button><button data-n="32" class="on">32</button>
    <button data-n="48">48</button><button data-n="64">64</button>
  </div>
  <button class="tr" id="glow" type="button">Glow: on</button>
  <button class="tr" id="caps" type="button">Peak caps: on</button>
  <span class="spacer"></span>
  <span class="lbl" id="stat">loading&hellip;</span>
</header>

<main>
  <aside id="list"><div class="empty">Loading your library&hellip;</div></aside>
  <div class="stage"><canvas id="cv"></canvas></div>
</main>

<footer>
  <button class="tr main" id="pp" type="button">&#9654;</button>
  <div class="np"><div class="t" id="npt">nothing playing</div>
    <div class="a" id="npa">click a song on the left</div></div>
  <span class="tm" id="tcur">0:00</span>
  <div class="seek" id="seek"><i id="seeki"></i></div>
  <span class="tm" id="tdur">0:00</span>
  <span class="lbl">vol</span>
  <input type="range" id="vol" min="0" max="1" step="0.01" value="0.8">
</footer>

<script>
"use strict";
var $=function(id){return document.getElementById(id);};

/* ---- tuning ---- */
var NB=32, GLOW=true, CAPS=true;
var FLOOR_DB=26;      /* dB range mapped to full bar height. The real signal
                         spans roughly -5..-27 dB once tilt is applied, so a
                         wide window (60+ dB) just pins every bar at the top
                         and nothing appears to move. */
var TILT=0.4;         /* 0 = raw spectrum (bass crushes everything), 1 = flat */
var ATK=26, REL=9;    /* bar attack / release, per second */
var CAP_HOLD=0.16;    /* seconds a peak floats before it starts falling */
var CAP_GRAV=0.85;    /* peak fall speed, full-heights per second */

var au=new Audio(); au.preload="metadata"; au.volume=0.8;
var AC=null, SRC=null, AN=null, FFT=null, BANDS=[];
var lev=[], tgt=[], peak=[], hold=[];
var LIB=[], cur=-1, vrun=false, vprev=0;

/* ---------- canvas + bloom ----------
   The bloom is a real downscale-and-re-add: bars are drawn small into an
   offscreen canvas, then drawn back up to full size with 'lighter', which is
   one drawImage instead of 32 shadowBlur fills. Cheap, and it actually bleeds
   light into the background the way a glow should. */
var cv=$("cv"), cx=cv.getContext("2d");
var blm=document.createElement("canvas"), bx=blm.getContext("2d");
var W=0,H=0,DPR=1;
function size(){
  var r=cv.parentNode.getBoundingClientRect();
  DPR=Math.max(1, window.devicePixelRatio||1);
  W=Math.max(1, Math.round(r.width)); H=Math.max(1, Math.round(r.height));
  cv.width=W*DPR; cv.height=H*DPR; cv.style.width=W+"px"; cv.style.height=H+"px";
  blm.width=Math.max(1, Math.round(W*DPR/5));
  blm.height=Math.max(1, Math.round(H*DPR/5));
}
addEventListener("resize", function(){ size(); if(!vrun) draw(); });

/* ---------- log-spaced bands ----------
   Equal ratios, not equal Hz: bar 1 might cover 32-36 Hz while the last covers
   ~11-16 kHz. That is how ears hear, so bass and treble get an equal share of
   the width instead of the bottom three bars eating the whole screen. */
function buildBands(){
  if(!AC||!FFT) return;
  var nyq=AC.sampleRate/2, nb=FFT.length;
  var fmin=32, fmax=Math.min(16000, nyq*0.95);
  BANDS=[];
  for(var k=0;k<NB;k++){
    var f0=fmin*Math.pow(fmax/fmin, k/NB);
    var f1=fmin*Math.pow(fmax/fmin, (k+1)/NB);
    var lo=Math.max(1, Math.floor(f0/nyq*nb));
    var hi=Math.min(nb, Math.max(lo+1, Math.ceil(f1/nyq*nb)));
    /* spectra fall roughly 6 dB per octave; partial tilt compensation keeps the
       treble bars audible without flipping the balance entirely */
    var fc=Math.sqrt(f0*f1);
    BANDS.push({lo:lo, hi:hi, tilt:TILT*10*Math.log(fc/fmin)/Math.LN10});
  }
  lev.length=tgt.length=peak.length=hold.length=NB;
  for(var i=0;i<NB;i++){ lev[i]=0; tgt[i]=0; peak[i]=0; hold[i]=0; }
}

function sample(){
  if(!AN||!FFT) return;
  if(au.paused){ for(var k=0;k<NB;k++) tgt[k]=0; return; }
  AN.getByteFrequencyData(FFT);
  for(var k=0;k<NB;k++){
    var b=BANDS[k], s=0, n=0;
    for(var i=b.lo;i<b.hi;i++){ s+=FFT[i]; n++; }
    var v=n? s/n/255 : 0;
    if(v<1e-5) v=1e-5;
    var db=20*Math.log(v)/Math.LN10;            /* about -100..0 dB */
    var lv=(db + b.tilt + FLOOR_DB)/FLOOR_DB;   /* -62 dB .. 0 dB -> 0..1 */
    tgt[k]=Math.max(0, Math.min(1, lv));
  }
}

function frame(){
  if(!vrun){ vprev=0; return; }
  var now=performance.now();
  var dt=vprev? Math.min(0.1, (now-vprev)/1000) : 1/60;
  vprev=now;
  var quiet=true;
  for(var k=0;k<NB;k++){
    /* snap up hard, settle back slowly - reads as impact, not mush */
    var r=(tgt[k]>lev[k])? ATK : REL;
    lev[k]+=(tgt[k]-lev[k])*(1-Math.exp(-r*dt));
    /* peak caps: jump with the bar, hang, then fall under gravity */
    if(lev[k]>=peak[k]){ peak[k]=lev[k]; hold[k]=CAP_HOLD; }
    else{
      hold[k]-=dt;
      if(hold[k]<=0) peak[k]=Math.max(0, peak[k]-CAP_GRAV*dt);
    }
    if(lev[k]>0.004 || peak[k]>0.004) quiet=false;
  }
  draw();
  if(quiet && au.paused){ vrun=false; vprev=0; return; }
  requestAnimationFrame(frame);
}

function geometry(){
  var pad=Math.max(18, W*0.03);
  var inner=W-pad*2, gap=Math.max(1, inner/(NB*2.6));
  var bw=(inner-gap*(NB-1))/NB;
  var base=H*0.9, maxH=H*0.74;
  return {pad:pad, gap:gap, bw:bw, base:base, maxH:maxH};
}

/* bars into a canvas, in CSS-pixel coordinates. The CALLER sets the transform
   (DPR for the main canvas, the downscale ratio for the bloom) - if this
   function reset the transform itself the sharp bars would be drawn at CSS
   scale on a device-pixel canvas and come out the wrong size. */
function drawBars(c, g){
  if(!BANDS.length) return;          /* first paint can land before the bands
                                        exist, and NaN gradients throw */
  for(var k=0;k<NB;k++){
    var lv=lev[k]||0;
    var bh=Math.max(1.5, lv*g.maxH);
    var x=g.pad + k*(g.bw+g.gap);
    var y=g.base-bh;
    var grad=c.createLinearGradient(0, y, 0, g.base);
    grad.addColorStop(0, "rgba(143,214,255,.95)");
    grad.addColorStop(.35, "rgba(var(--acc-r),var(--acc-g),var(--acc-b),.85)");
    grad.addColorStop(1, "rgba(var(--acc-r),var(--acc-g),var(--acc-b),.20)");
    c.fillStyle=grad;
    c.fillRect(x, y, g.bw, bh);
  }
}

function draw(){
  if(!W) return;
  var g=geometry();
  cx.setTransform(DPR,0,0,DPR,0,0);
  cx.globalCompositeOperation="source-over";
  cx.globalAlpha=1;
  cx.clearRect(0,0,W,H);

  if(GLOW && NB<=48){
    bx.setTransform(blm.width/W, 0, 0, blm.height/H, 0, 0);
    bx.globalCompositeOperation="source-over";
    bx.globalAlpha=1;
    bx.clearRect(0,0,blm.width,blm.height);
    drawBars(bx, g);
    cx.globalCompositeOperation="lighter";
    cx.globalAlpha=0.55;
    cx.filter="blur("+(W>900?7:5)+"px)";
    cx.drawImage(blm, 0, 0, W, H);
    cx.filter="none";
    cx.globalAlpha=1;
    cx.globalCompositeOperation="source-over";
  }

  /* crisp bars on top of the bloom */
  drawBars(cx, g);

  /* peak caps */
  if(CAPS){
    cx.shadowColor="rgba(255,255,255,.85)";
    cx.shadowBlur=GLOW? 12 : 0;
    cx.fillStyle="rgba(230,255,252,.92)";
    for(var k=0;k<NB;k++){
      if(peak[k]<0.02) continue;
      var y=g.base-peak[k]*g.maxH;
      cx.fillRect(g.pad+k*(g.bw+g.gap), y-1.5, g.bw, 2.5);
    }
    cx.shadowBlur=0;
  }

  /* a floor so the bars sit on something */
  cx.fillStyle="rgba(143,214,255,.12)";
  cx.fillRect(g.pad-10, g.base, W-(g.pad-10)*2, 1);
}

/* ---------- audio graph (wired once, on first play) ---------- */
function initGraph(){
  if(AC) return;
  try{
    AC=new (window.AudioContext||window.webkitAudioContext)();
    SRC=AC.createMediaElementSource(au);        /* once per element, ever */
    AN=AC.createAnalyser();
    AN.fftSize=4096; AN.smoothingTimeConstant=0.5;
    FFT=new Uint8Array(AN.frequencyBinCount);
    SRC.connect(AN); AN.connect(AC.destination); /* without this: silence */
    buildBands();
    /* sampling on its own clock, not in the paint loop: a transient can be
       shorter than a frame, so painting at a low fps would just miss it */
    setInterval(sample, 25);
  }catch(e){ AC=AN=null; }
}
function play(i){
  initGraph();
  if(AC && AC.state==="suspended") AC.resume();
  if(i!==cur){ cur=i; au.src="/audio/"+i; }
  au.play();
  if(!vrun){ vrun=true; vprev=0; requestAnimationFrame(frame); }
}
function fmt(s){ if(!isFinite(s)||s<0)s=0;
  var m=Math.floor(s/60), x=Math.floor(s%60); return m+":"+(x<10?"0":"")+x; }

au.addEventListener("play", function(){
  $("pp").innerHTML="&#10074;&#10074;";
  if(!vrun){ vrun=true; vprev=0; requestAnimationFrame(frame); }
});
au.addEventListener("pause", function(){ $("pp").innerHTML="&#9654;"; });
au.addEventListener("ended", function(){ if(cur+1<LIB.length) play(cur+1); });
au.addEventListener("timeupdate", function(){
  $("tcur").textContent=fmt(au.currentTime);
  $("tdur").textContent=fmt(au.duration);
  $("seeki").style.width=(isFinite(au.duration)&&au.duration>0
    ? (au.currentTime/au.duration*100) : 0)+"%";
});

/* ---------- controls ---------- */
$("pp").addEventListener("click", function(){
  if(cur<0){ if(LIB.length) play(0); return; }
  if(au.paused) au.play(); else au.pause();
});
$("seek").addEventListener("click", function(e){
  if(!isFinite(au.duration)||au.duration<=0) return;
  var r=this.getBoundingClientRect();
  au.currentTime=Math.max(0, Math.min(1,(e.clientX-r.left)/r.width))*au.duration;
});
$("vol").addEventListener("input", function(){ au.volume=+this.value; });
$("glow").addEventListener("click", function(){
  GLOW=!GLOW; this.textContent="Glow: "+(GLOW?"on":"off");
  this.classList.toggle("on", false); draw();
});
$("caps").addEventListener("click", function(){
  CAPS=!CAPS; this.textContent="Peak caps: "+(CAPS?"on":"off"); draw();
});
$("bars").addEventListener("click", function(e){
  var b=e.target.closest("button[data-n]"); if(!b) return;
  NB=+b.dataset.n;
  var bs=this.querySelectorAll("button");
  for(var i=0;i<bs.length;i++) bs[i].classList.toggle("on", bs[i]===b);
  buildBands(); draw();
});
addEventListener("keydown", function(e){
  if(e.code==="Space" && e.target.tagName!=="INPUT"){
    e.preventDefault();
    if(cur<0){ if(LIB.length) play(0); return; }
    if(au.paused) au.play(); else au.pause();
  }
});

/* ---------- library ---------- */
fetch("/api/library").then(function(r){return r.json();}).then(function(j){
  LIB=j.songs||[];
  var box=$("list"); box.innerHTML="";
  if(!LIB.length){ box.innerHTML='<div class="empty">No songs found.</div>'; }
  var frag=document.createDocumentFragment();
  for(var i=0;i<LIB.length;i++){
    (function(i){
      var s=LIB[i];
      var d=document.createElement("div");
      d.className="row"; d.dataset.i=i;
      var n=document.createElement("span"); n.className="n"; n.textContent=(i+1);
      var m=document.createElement("div"); m.className="m";
      var t=document.createElement("div"); t.className="t"; t.textContent=s.title;
      var a=document.createElement("div"); a.className="a"; a.textContent=s.artist;
      m.appendChild(t); m.appendChild(a);
      d.appendChild(n); d.appendChild(m);
      d.addEventListener("click", function(){
        var rows=document.querySelectorAll(".row.on");
        for(var r=0;r<rows.length;r++) rows[r].classList.remove("on");
        d.classList.add("on");
        $("npt").textContent=s.title;
        $("npa").textContent=s.artist;
        play(i);
      });
      frag.appendChild(d);
    })(i);
  }
  box.appendChild(frag);
  $("stat").textContent=LIB.length+" songs";
}).catch(function(){ $("stat").textContent="failed to load"; });

size(); draw();
</script>
</body>
</html>
"""


class QuietServer(ThreadingHTTPServer):
    """Browsers routinely drop audio streams (seeking, stopping, closing the
    tab).  Those aborts are normal - don't dump a traceback for them."""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, TimeoutError, OSError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def lan_ip():
    """Best-effort LAN address, used to build the URL you open on the phone.
    Falls back to the hostname if the route probe fails."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))          # no packets sent; just picks a route
        return s.getsockname()[0]
    except OSError:
        return socket.gethostname()
    finally:
        s.close()


def main():
    port = None
    for p in PORTS:
        try:
            srv = QuietServer((HOST, p), Handler)
            port = p
            break
        except OSError:
            continue
    if port is None:
        print("All ports busy - close any other copy of the player and retry.")
        return 1

    url = "http://127.0.0.1:%d" % port
    print("Music player on %s" % url)
    if HOST == "0.0.0.0":
        print("On your phone (same Wi-Fi): http://%s:%d" % (lan_ip(), port))
    print("Scanned %d songs from %d folder(s)." % (len(LIBRARY), len(MUSIC_DIRS)))
    print("Ctrl+C to stop.")
    start_watcher()

    if AUTO_OPEN:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
