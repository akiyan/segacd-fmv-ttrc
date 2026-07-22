#!/usr/bin/env python3
"""Verify OCR for the contiguous movie-player DEBUG HUD."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import gen_debugfont  # noqa: E402
import read_frameno  # noqa: E402


GLYPHS = {format(i, "X"): rows for i, rows in enumerate(gen_debugfont.ORDER[:16])}


def _draw_cell(dst, x, y, rows):
    for dy, row in enumerate(rows):
        for dx, pixel in enumerate(row):
            if pixel == "#":
                dst[y + dy, x + dx] = 235


def make_hud(width, values, origin=(5, 4), complete=True, black_backing=False,
             layout=None):
    height = 32
    yy, xx = np.mgrid[:height, :width]
    # Deliberately bright/noisy movie pixels surround the opaque value cells.
    # Hardware overwrites only the HUD cells in the inactive movie Plane A;
    # the unused H40 width remains the original movie name-table content.
    image = (150 + (7 * xx + 11 * yy) % 91).astype(np.uint8)
    x, y = origin
    if layout is None:
        layout = read_frameno.hud_layout_for_width(width)
    fields = layout if complete else layout[:1]
    if black_backing:
        cells = max(col + digits for _name, col, digits in layout)
        image[y:y + read_frameno.CELL,
              x:x + cells * read_frameno.CELL] = 0
    for name, col, digits in fields:
        gx = x + col * read_frameno.CELL
        text = f"{values[name] & ((1 << (digits * 4)) - 1):0{digits}X}"
        for j, char in enumerate(text):
            _draw_cell(image, gx + j * read_frameno.CELL, y, GLYPHS[char])
    return Image.fromarray(image, "L")


def check_case(width, values, origin, layout=None):
    image = make_hud(width, values, origin, black_backing=True, layout=layout)
    got = read_frameno.read_hud(image, layout=layout)
    for name, _col, digits in (layout
                               or read_frameno.hud_layout_for_width(width)):
        mask = (1 << (digits * 4)) - 1
        expected = values[name] & mask
        if got[name][0] != expected:
            raise SystemExit(
                f"{width}px {name}: read {got[name][0]:X}, expected {expected:X}")
        if got[name][1] < 0.90:
            raise SystemExit(f"{width}px {name}: low confidence {got[name][1]:.3f}")
    frame, confidence = read_frameno.read_frameno(image)
    if frame != values["F"] or confidence < 0.90:
        raise SystemExit(
            f"{width}px F-only API: got {frame:04X}/{confidence:.3f}, "
            f"expected {values['F']:04X}")


def main():
    if len(gen_debugfont.ORDER) != 16:
        raise SystemExit(f"DEBUG font has {len(gen_debugfont.ORDER)} glyphs, expected 16")
    for value, rows in enumerate(gen_debugfont.ORDER):
        expected = "".join("##" if value & (1 << bit) else ".."
                           for bit in range(3, -1, -1))
        if rows[0] != expected:
            raise SystemExit(f"glyph {value:X} barcode {rows[0]!r}, expected {expected!r}")
    if read_frameno.HUD_CELLS != 30:
        raise SystemExit(f"H32 HUD is {read_frameno.HUD_CELLS} cells, expected 30")
    if read_frameno.HUD_H40_CELLS != 30:
        raise SystemExit(
            f"H40 HUD is {read_frameno.HUD_H40_CELLS} cells, expected 30")
    check_case(256, {"F": 0x1234, "P": 0xAB, "S": 0xFF,
                     "D": 0x00, "R": 0x7E, "L": 0x68,
                     "C": 0x02, "W": 0x03, "M": 0x04, "A": 0x1E,
                     "U": 0x2345, "N": 0x17, "J": 0x0A}, (0, 5))
    check_case(320, {"F": 0x0000, "P": 0xFF, "S": 0x00,
                     "D": 0xFF, "R": 0x00, "L": 0x7F,
                     "C": 0x00, "W": 0xFF, "M": 0x02, "A": 0x00,
                     "U": 0x1234, "N": 0x2F, "J": 0x28}, (0, 3))
    if read_frameno.HUD_H40_FLIP_CELLS != 34:
        raise SystemExit(
            f"H40 flip HUD is {read_frameno.HUD_H40_FLIP_CELLS} cells, "
            "expected 34")
    check_case(320, {"F": 0x0A99, "P": 0x0C, "S": 0x00,
                     "D": 0x00, "R": 0x00, "L": 0x38,
                     "C": 0x00, "W": 0x63, "M": 0x01, "A": 0x42,
                     "U": 0x023D, "N": 0x87, "J": 0x0E,
                     "V": 0xF2, "O": 0xFF}, (0, 3),
               layout=read_frameno.HUD_H40_FLIP_LAYOUT)

    h40 = np.asarray(make_hud(
        320, {"F": 0x0000, "P": 0x00, "S": 0x00, "D": 0x00,
              "R": 0x00, "L": 0x00, "C": 0x00, "W": 0x00,
              "M": 0x00, "A": 0x00, "U": 0x0000, "N": 0x00,
              "J": 0x00},
        origin=(0, 3), black_backing=True))
    if np.all(h40[3:11, read_frameno.HUD_H40_CELLS * 8:] == 0):
        raise SystemExit("H40 HUD unused width must remain movie-visible")

    # The longstanding single-purpose API must not depend on later HUD fields.
    only_f = make_hud(
        48, {"F": 0xCAFE}, origin=(3, 6), complete=False,
        black_backing=True)
    frame, confidence = read_frameno.read_frameno(only_f)
    if frame != 0xCAFE or confidence < 0.90:
        raise SystemExit(
            f"standalone F API: got {frame:04X}/{confidence:.3f}, expected CAFE")

    print("HUD OCR proof: OK (values only, common H32/H40 30 cells, "
          "unused H40 width movie-visible, standalone F compatible)")


if __name__ == "__main__":
    main()
