# DEBUG HUD OCR proof

The movie player writes hexadecimal values only in a contiguous top row. The
keys below describe the fixed interpretation; their letters are not drawn:

```text
H32: xxxx xx xx xx xx xx xx xx xx xx
H40: xxxx xx xx xx xx xx xx xx xx xx xxxx xx
```

`F/U` contain four hexadecimal digits. `L` is the high byte of the audio lead;
`P`, `S`, `D`, `R`, `C`, `W`, `M`, `A`, and `N` show two hexadecimal digits. There
is no H32/H40-specific pitch; H40 uses its eight additional cells for `U/N`.
`U` is the Main pattern-transfer time in 30.72 us Mega-CD stopwatch ticks, and
`N` is the low byte of the packed cold-run descriptor count (wrapping at 256).
The player formats these values into a 28-word Main-RAM row before display
pacing, then publishes only the first 22 H32 or all 28 H40 words with fixed
longword writes over the first cells of the inactive movie Plane A table. The
final control-port word switches to that complete picture-and-HUD table. Its
VBlank guard rejects terminal V-counter lines `0xFC..0xFF`, keeping the HUD and
picture aligned without extending a 30 fps frame to a third scanout.

Each of the 16 8x8 patterns contains a two-pixel-wide four-bit barcode in its
top row and a compact 6x7 hexadecimal glyph below it. The reader decodes the
barcode directly and uses the lower glyph only as a confidence check. Run the
in-memory synthetic-image proof with:

```sh
tools/python.sh harness/hud_ocr/verify.py
```

It renders the actual generated font onto H32- and H40-sized frames, verifies
all visible fields and their widths, covers `00`/`FF` byte values, and confirms that
the older `read_frameno()` API still reads an isolated `Fxxxx` field without
requiring the rest of the HUD. The synthetic source is deliberately bright and
noisy; the proof models opaque font cells only across the 22-cell H32 or
28-cell H40 HUD and verifies that the unused H40 width remains the original
movie content. This matches the player: no Window transparency or alternate
Plane B is involved.
