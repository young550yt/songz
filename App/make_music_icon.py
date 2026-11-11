# -*- coding: utf-8 -*-
"""Generates icon-192.png / icon-512.png for the music player's manifest.

A teal eighth note on the player's dark background - the same two colors
the page uses (--acc on --bg). Pure standard library, so it runs anywhere
the server does.  Run:  py make_music_icon.py"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
BG = (0x0e, 0x10, 0x16)          # --bg
ACC = (0x5e, 0xea, 0xd4)         # --acc


def on_note(x, y, size):
    u, v = x / size, y / size
    # stem
    if 0.50 <= u <= 0.58 and 0.16 <= v <= 0.78:
        return True
    # note head (tilted oval)
    if ((u - 0.38) / 0.17) ** 2 + ((v - 0.70) / 0.125) ** 2 <= 1.0:
        return True
    # flag
    if 0.58 <= u <= 0.74 and 0.16 <= v <= 0.42 and (u - 0.58) * 1.4 >= v - 0.16:
        return True
    return False


def write_png(path, size):
    raw = bytearray()
    for y in range(size):
        raw.append(0)                                   # scanline filter: none
        for x in range(size):
            raw += bytes(ACC if on_note(x, y, size) else BG)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)   # 8-bit RGB
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
           chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
           chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    print("wrote %s (%d bytes)" % (os.path.basename(path), len(png)))


if __name__ == "__main__":
    write_png(os.path.join(HERE, "icon-192.png"), 192)
    write_png(os.path.join(HERE, "icon-512.png"), 512)
