#!/usr/bin/env python3
"""Generate a small ASCII glyph font as Genesis 4bpp tiles for the CD-DA screen.

Output: boot/opfont.bin = len(ORDER) tiles * 32 bytes.
Set pixels use colour index 1, background 0. Glyph i is tile i; the Main CPU
uploads them at VRAM tile index 1 (so ORDER[i] lives at tile 1+i) and writes
name-table entries from the pre-resolved index string in handoff.s.

ORDER fixes the tile assignment; handoff.s's cdda_msg must match it:
  C=1 D=2 -=3 A=4 (space)=5 P=6 L=7 Y=8 I=9 N=10 G=11
"""
from pathlib import Path

ORDER = ["C", "D", "-", "A", " ", "P", "L", "Y", "I", "N", "G"]

# 8x8 bitmaps, '#' = colour 1. Bold strokes so they survive headless downscaling.
GLYPHS = {
    "C": ["..####..", ".##..##.", "##......", "##......", "##......", ".##..##.", "..####..", "........"],
    "D": ["######..", ".##..##.", ".##...##", ".##...##", ".##...##", ".##..##.", "######..", "........"],
    "A": ["..####..", ".##..##.", "##....##", "##....##", "########", "##....##", "##....##", "........"],
    "P": ["######..", "##...##.", "##...##.", "######..", "##......", "##......", "##......", "........"],
    "L": ["##......", "##......", "##......", "##......", "##......", "##......", "#######.", "........"],
    "Y": ["##...##.", "##...##.", ".##.##..", "..###...", "...##...", "...##...", "...##...", "........"],
    "I": [".#####..", "...##...", "...##...", "...##...", "...##...", "...##...", ".#####..", "........"],
    "N": ["##...##.", "###..##.", "####.##.", "##.####.", "##..###.", "##...##.", "##...##.", "........"],
    "G": ["..####..", ".##..##.", "##......", "##.####.", "##...##.", ".##..##.", "..####..", "........"],
    "-": ["........", "........", "........", ".#####..", "........", "........", "........", "........"],
    " ": ["........", "........", "........", "........", "........", "........", "........", "........"],
}


def tile_bytes(rows):
    out = bytearray()
    for row in rows:
        px = [1 if c == "#" else 0 for c in row.ljust(8, ".")[:8]]
        for i in range(0, 8, 2):
            out.append((px[i] << 4) | px[i + 1])
    return bytes(out)


def main():
    data = bytearray()
    for ch in ORDER:
        data += tile_bytes(GLYPHS[ch])
    out = Path(__file__).resolve().parent.parent / "boot" / "opfont.bin"
    out.write_bytes(data)
    print(f"wrote {out} ({len(ORDER)} tiles, {len(data)} bytes)")


if __name__ == "__main__":
    main()
