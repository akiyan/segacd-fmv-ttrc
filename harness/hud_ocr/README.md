# DEBUG HUD OCR proof

The movie player writes one contiguous 32-cell row on the VDP Window plane:

```text
FxxxxPxxSxxDxxRxxLxxCxxWxxMxxAxx
```

`F` contains four hexadecimal digits. `L` is the high byte of the audio lead;
`P`, `S`, `D`, `R`, `C`, `W`, `M`, and `A` show two hexadecimal digits. There
is no H32/H40-specific pitch; the same row occupies 256 native pixels in either
mode.

Run the in-memory synthetic-image proof with:

```sh
python3 harness/hud_ocr/verify.py
```

It renders the actual generated font onto H32- and H40-sized frames, verifies
all ten fields and their widths, covers `00`/`FF` byte values, and confirms that
the older `read_frameno()` API still reads an isolated `Fxxxx` field without
requiring the rest of the HUD. The synthetic source is deliberately bright and
noisy; the proof also models the full-width cleared top row produced by the
player's branch-free DEBUG blit and verifies OCR against that black diagnostic
bar.
