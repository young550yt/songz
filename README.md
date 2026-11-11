# Songz 🎵

A fully local music player for PC — no install, no internet, no accounts.
Runs a tiny Python server and plays the music already on your disk, with a
phone-friendly web UI (same Wi-Fi = play from your phone).

## What's inside

| Path | What it is |
|---|---|
| `Music.html` | The player UI (library list, search, moods, favourites, lyrics) |
| `App/music_player.py` | The local server (stdlib only — no pip installs) |
| `App/Play Music.bat` | Double-click launcher: starts the server, opens the browser |
| `App/Music.html` | Player UI copy used by the app build |
| `App/offline_template.html` | Template for the offline/PWA build |
| `App/make_offline_player.py` | Rebuilds the offline player from the template |
| `App/sw.js`, `App/manifest.webmanifest` | PWA bits (installable, offline) |
| `Bible/` | Offline Bible reader sub-app (`Bible.bat`, `bible_server.py`) |
| `Bible/make_bible_data.py` | Rebuilds `bible_data.js` from source text |

## Run it

1. **Player:** double-click `App\Play Music.bat` → opens `http://127.0.0.1:8390`.
2. **Bible:** double-click `Bible\Bible.bat`.

Edit `MUSIC_DIRS` at the top of `App/music_player.py` to point at your music
folders.

## Notes

- **No songs included** — audio files are intentionally not in this repo.
  Only the app code and its metadata/config ship here.
- Python 3.12+ (any modern 3.x works). Standard library only.
