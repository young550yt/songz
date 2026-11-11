# -*- coding: utf-8 -*-
r"""Build the offline phone player: one self-contained Music.html.

Run:  py make_offline_player.py

It reuses the PC player's scanner (music_player.py) for title/artist/folder
parsing, then bakes the whole library, your favourites and your lyrics into a
single HTML file that needs no server and no network. Copy it somewhere on the
phone NEXT TO the folders that hold the songs (Chill, Rageout, ...) and open it.

Outputs (all three are written so you can grab whichever one syncs):
  <Songz>\Music.html                - beside Chill/ and Rageout/
  <Songz>\App\Music.html            - the folder that syncs to the phone
  <CrossDevice mirror>\Music.html   - straight into the phone mirror
"""
import io
import json
import os
import stat
import sys
import importlib.util
import datetime

APP_DIR = os.path.dirname(os.path.abspath(__file__))          # ...\Songz\App
SONGZ = os.path.dirname(APP_DIR)                              # ...\Songz
MIRROR = (r"C:\Users\young\CrossDevice\Young's A06\storage\snaptube"
          r"\download\SnapTube Audio")
TEMPLATE = os.path.join(APP_DIR, "offline_template.html")
PLAYER = os.path.join(APP_DIR, "music_player.py")


def load_player_module():
    """The PC player scans the library at import time; we reuse its parser."""
    spec = importlib.util.spec_from_file_location("mp_scanner", PLAYER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def rel_parts(abs_path):
    """Split an absolute song path into (relative path, basename)."""
    rel = os.path.relpath(abs_path, SONGZ)
    parts = rel.split(os.sep)
    return "/".join(parts), parts[-1]


def load_json(fn):
    try:
        with io.open(fn, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def build_library(m):
    lib = []
    for e in m.LIBRARY:
        rel, bn = rel_parts(e["path"])
        lib.append({
            "p": rel,                      # relative path from the HTML file
            "t": e.get("title", bn),
            "a": e.get("artist", ""),
            "s": e.get("sub", ""),         # mood folder (Chill / Rageout)
            "z": e.get("size", 0),
            "bn": bn,                      # basename, for the folder-relink map
        })
    return lib


def build_favs(m, lib):
    """favourites.json is keyed by absolute path; remap to relative keys."""
    rel_by_abs = {}
    for e, ent in zip(m.LIBRARY, lib):
        rel_by_abs[os.path.realpath(e["path"]).lower()] = ent["p"]
    out = {}
    for k, v in load_json(os.path.join(APP_DIR, "favourites.json")).items():
        if not v:
            continue
        rel = rel_by_abs.get(os.path.realpath(k).lower())
        if rel:
            out[rel] = True
    return out


def build_lyrics(m, lib):
    """Bakes lyrics from m.LYRICS (both lyrics.json and auto-detected sidecar .lrc files)."""
    rel_by_abs = {}
    for e, ent in zip(m.LIBRARY, lib):
        rel_by_abs[os.path.realpath(e["path"]).lower()] = ent["p"]
    out = {}
    for k, v in getattr(m, "LYRICS", {}).items():
        if not isinstance(v, dict):
            continue
        rel = rel_by_abs.get(os.path.realpath(k).lower())
        if not rel:
            continue
        out[rel] = {
            "text": v.get("text", ""),
            "lines": v.get("lines", []) if isinstance(v.get("lines"), list) else [],
            # the PC player stores times as {"0": 21.2, "3": 30.3} (dict keyed by
            # line index); flatten to a dense array so the offline player gets a list
            "times": _times_to_list(v.get("times")),
        }
    return out


def _times_to_list(times):
    if isinstance(times, list):
        return times
    if isinstance(times, dict):
        out = []
        for k, t in times.items():
            try:
                i = int(k)
            except (TypeError, ValueError):
                continue
            if i < 0 or i > 10000:
                continue
            while len(out) <= i:
                out.append(None)
            out[i] = t if isinstance(t, (int, float)) else None
        return out
    return []


def js_json(obj):
    """JSON safe to drop straight into a <script> block."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("</", "<\\/")


def generate(base_prefix):
    tpl = io.open(TEMPLATE, encoding="utf-8").read()
    m = load_player_module()
    lib = build_library(m)
    favs = build_favs(m, lib)
    lyrics = build_lyrics(m, lib)
    gen = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = (tpl
           .replace("__BASE__", json.dumps(base_prefix))
           .replace("__GEN__", json.dumps(gen))
           .replace("__LIB__", js_json(lib))
           .replace("__FAVS__", js_json(favs))
           .replace("__LYR__", js_json(lyrics)))
    leftover = [t for t in ("__BASE__", "__GEN__", "__LIB__", "__FAVS__", "__LYR__")
                if t in out]
    if leftover:
        raise SystemExit("placeholders not replaced: %s" % leftover)
    return out, len(lib), len(favs), len(lyrics)


def write(path, content):
    try:
        if os.path.exists(path):
            # the phone mirror is managed by Syncthing, which can flag files
            # read-only; clear it or the update is rejected with EACCES
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return "%8d B  %s" % (len(content.encode("utf-8")), path)
    except Exception as e:
        return "FAILED    %s (%s)" % (path, e)


def main():
    targets = [
        (os.path.join(SONGZ, "Music.html"), ""),            # beside Chill/
        (os.path.join(APP_DIR, "Music.html"), "../"),       # inside App/
    ]
    if os.path.isdir(MIRROR):
        targets.append((os.path.join(MIRROR, "Music.html"), ""))
    else:
        print("note: phone mirror not found, skipped: %s" % MIRROR)

    for path, prefix in targets:
        content, n, nf, nl = generate(prefix)
        print(write(path, content))
    print("library %d songs, %d favourites, %d lyric sets baked in." % (n, nf, nl))
    return 0


if __name__ == "__main__":
    sys.exit(main())
