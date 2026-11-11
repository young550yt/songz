# -*- coding: utf-8 -*-
"""Generates icon-192.png / icon-512.png for manifest.webmanifest.

A gold cross on the app's dark background - the same two colors the page
uses. Pure standard library, so it runs anywhere the server does.
Run:  py make_icon.py"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
BG = (0x12, 0x10, 0x0c)          # --bg
GOLD = (0xe0, 0xb2, 0x5c)        # --gold


def on_cross(x, y, size):
    u, v = x / size, y / size
    return (0.44 <= u <= 0.56 and 0.14 <= v <= 0.86) or \
           (0.20 <= u <= 0.80 and 0.30 <= v <= 0.42)


def write_png(path, size):
    raw = bytearray()
    for y in range(size):
        raw.append(0)                                   # scanline filter: none
        for x in range(size):
            raw += bytes(GOLD if on_cross(x, y, size) else BG)

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
