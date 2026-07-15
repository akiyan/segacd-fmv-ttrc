# DEBUG HUD OCR proof

The movie player writes one contiguous 22-cell row on the VDP Window plane:

```text
FxxxxPxxSxxDxxRxxLxxxx
```

`F` and `L` contain four hexadecimal digits. `P`, `S`, `D`, and `R` contain
their low byte as two hexadecimal digits. There is no H32/H40-specific pitch;
the same row occupies 176 native pixels in either mode.

Run the in-memory synthetic-image proof with:

```sh
python3 harness/hud_ocr/verify.py
```

It renders the actual generated font onto H32- and H40-sized frames, verifies
all six fields and their widths, covers `00`/`FF` byte values, and confirms that
the older `read_frameno()` API still reads an isolated `Fxxxx` field without
requiring the rest of the HUD. The synthetic source is deliberately bright and
noisy; the proof also models the full-width cleared top row produced by the
player's branch-free DEBUG blit and verifies OCR against that black diagnostic
bar.
