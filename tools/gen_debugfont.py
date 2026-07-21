#!/usr/bin/env python3
"""Generate the 16-glyph movie DEBUG HUD font as Genesis 4bpp tiles.

Each 8x8 glyph encodes its nibble twice:

* row 0 is a machine-readable four-bit barcode, two pixels per bit;
* rows 1..7 contain a compact 6x7 human-readable hexadecimal glyph with one
  blank column on each side.

Only 0..F exist.  Source pixels are indices 0/1; the movie player expands set
pixels to palette 0 index 15 and background pixels to palette 0 index 1.
"""
from pathlib import Path


_DIGITS_6X7 = {
    0x0: [".####.", "##..##", "##..##", "##..##", "##..##", "##..##", ".####."],
    0x1: ["..##..", ".###..", "..##..", "..##..", "..##..", "..##..", "######"],
    0x2: ["#####.", "....##", "...##.", "..##..", ".##...", "##....", "######"],
    0x3: ["#####.", "....##", "...##.", "....##", "....##", "##..##", ".####."],
    0x4: ["...###", "..####", ".##.##", "##..##", "######", "....##", "....##"],
    0x5: ["######", "##....", "#####.", "....##", "....##", "##..##", ".####."],
    0x6: [".####.", ".##...", "##....", "#####.", "##..##", "##..##", ".####."],
    0x7: ["######", "....##", "...##.", "..##..", ".##...", ".##...", ".##..."],
    0x8: [".####.", "##..##", "##..##", ".####.", "##..##", "##..##", ".####."],
    0x9: [".####.", "##..##", "##..##", ".#####", "....##", "...##.", ".####."],
    0xA: [".####.", "##..##", "##..##", "######", "##..##", "##..##", "##..##"],
    0xB: ["#####.", "##..##", "##..##", "#####.", "##..##", "##..##", "#####."],
    0xC: [".####.", "##..##", "##....", "##....", "##....", "##..##", ".####."],
    0xD: ["#####.", "##..##", "##..##", "##..##", "##..##", "##..##", "#####."],
    0xE: ["######", "##....", "##....", "#####.", "##....", "##....", "######"],
    0xF: ["######", "##....", "##....", "#####.", "##....", "##....", "##...."],
}


def glyph_rows(value: int) -> list[str]:
    """Return one barcode plus compact hex glyph as eight 8-pixel rows."""
    barcode = "".join("##" if value & (1 << bit) else ".."
                      for bit in range(3, -1, -1))
    return [barcode] + ["." + row + "." for row in _DIGITS_6X7[value]]


ORDER = [glyph_rows(value) for value in range(16)]


def tile_bytes(rows):
    out = bytearray()
    for row in rows:
        px = [1 if c == "#" else 0 for c in row.ljust(8, ".")[:8]]
        for i in range(0, 8, 2):
            out.append((px[i] << 4) | px[i + 1])
    return bytes(out)


def main():
    data = bytearray()
    for rows in ORDER:
        data += tile_bytes(rows)
    Path("boot/dbgfont.bin").write_bytes(bytes(data))
    print(f"wrote boot/dbgfont.bin = {len(ORDER)} tiles * 32 = {len(data)} bytes")


if __name__ == "__main__":
    main()
