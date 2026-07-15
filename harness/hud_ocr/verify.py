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
GLYPHS.update({
    char: gen_debugfont.ORDER[16 + i]
    for i, char in enumerate(("R", "W", "M", "P", "U", "L", "I", "S", " ", "+", "-", "N"))
})


def _draw_cell(dst, x, y, rows):
    for dy, row in enumerate(rows):
        for dx, pixel in enumerate(row):
            if pixel == "#":
                dst[y + dy, x + dx] = 235


def make_hud(width, values, origin=(5, 4), complete=True, black_backing=False):
    height = 32
    yy, xx = np.mgrid[:height, :width]
    # Deliberately bright/noisy movie pixels make transparent text difficult to
    # read. The hardware DEBUG path keeps the full-width top Window row clear,
    # modelled by black_backing below.
    image = (150 + (7 * xx + 11 * yy) % 91).astype(np.uint8)
    x, y = origin
    fields = read_frameno.HUD_LAYOUT if complete else read_frameno.HUD_LAYOUT[:1]
    if black_backing:
        image[y:y + read_frameno.CELL, :] = 0
    for name, col, digits in fields:
        gx = x + col * read_frameno.CELL
        _draw_cell(image, gx, y, GLYPHS[name])
        text = f"{values[name] & ((1 << (digits * 4)) - 1):0{digits}X}"
        for j, char in enumerate(text):
            _draw_cell(image, gx + (j + 1) * read_frameno.CELL, y, GLYPHS[char])
    return Image.fromarray(image, "L")


def check_case(width, values, origin):
    image = make_hud(width, values, origin, black_backing=True)
    got = read_frameno.read_hud(image)
    for name, _col, digits in read_frameno.HUD_LAYOUT:
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
    if read_frameno.HUD_CELLS != 22:
        raise SystemExit(f"HUD layout is {read_frameno.HUD_CELLS} cells, expected 22")
    check_case(256, {"F": 0x1234, "P": 0xAB, "S": 0xFF,
                     "D": 0x00, "R": 0x7E, "L": 0xBEEF}, (7, 5))
    check_case(320, {"F": 0x0000, "P": 0xFF, "S": 0x00,
                     "D": 0xFF, "R": 0x00, "L": 0xFFFF}, (2, 3))

    # The longstanding single-purpose API must not depend on later HUD fields.
    only_f = make_hud(
        48, {"F": 0xCAFE}, origin=(3, 6), complete=False,
        black_backing=True)
    frame, confidence = read_frameno.read_frameno(only_f)
    if frame != 0xCAFE or confidence < 0.90:
        raise SystemExit(
            f"standalone F API: got {frame:04X}/{confidence:.3f}, expected CAFE")

    print("HUD OCR proof: OK (full-width black row, H32/H40 22-cell layout, standalone F compatible)")


if __name__ == "__main__":
    main()
