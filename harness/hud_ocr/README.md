# DEBUG HUD OCR proof

The movie player writes a contiguous row on the VDP Window plane:

```text
H32: FxxxxPxxSxxDxxRxxLxxCxxWxxMxxAxx
H40: FxxxxPxxSxxDxxRxxLxxCxxWxxMxxAxxUxxxxNxx
```

`F/U` contain four hexadecimal digits. `L` is the high byte of the audio lead;
`P`, `S`, `D`, `R`, `C`, `W`, `M`, `A`, and `N` show two hexadecimal digits. There
is no H32/H40-specific pitch; H40 uses its eight additional cells for `U/N`.
`U` is the Main pattern-transfer time in 30.72 us Mega-CD stopwatch ticks, and
`N` is the low byte of the packed cold-run descriptor count (wrapping at 256).

Run the in-memory synthetic-image proof with:

```sh
python3 harness/hud_ocr/verify.py
```

It renders the actual generated font onto H32- and H40-sized frames, verifies
all visible fields and their widths, covers `00`/`FF` byte values, and confirms that
the older `read_frameno()` API still reads an isolated `Fxxxx` field without
requiring the rest of the HUD. The synthetic source is deliberately bright and
noisy; the proof also models the full-width cleared top row produced by the
player's branch-free DEBUG blit and verifies OCR against that black diagnostic
bar.
