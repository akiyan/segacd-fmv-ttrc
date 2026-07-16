# Palette audit

This harness renders the real segmented CRAM palettes from a simulation
decision log and distinguishes palette-table entries from colours actually
referenced by displayed tiles.

It replays every decision-log update, maintains the current 896-cell pattern
state, and counts palette-index use for every frame. When a `master/` directory
is supplied, it also applies the encoder's exact Bayer RGB333 conversion to all
source frames and reports the complete quantised-source colour set.

Run from the repository root:

```sh
python3 harness/palette_audit/audit.py \
  videos/BadApple_H32_256x224_pcm13/decisions.pkl \
  --master-dir videos/BadApple_H32_256x224_pcm13/master \
  --output-dir videos/BadApple_H32_256x224_pcm13/tmp/palette_audit \
  --upload-offset 19
```

Outputs:

- `palette_by_segment.png`: all CRAM segments, grouped by P0-P3 and index;
- `palette_global.png`: unique colours actually displayed across the movie;
- `palette_slots.csv`: every segment/palette/index slot and its use count;
- `palette_global.csv`: global unique-colour counts and locations;
- `summary.txt`: concise exact counts and colour values.

The near-white dither check compares the Bayer input with the actual codec
display state and counts 8x8 tiles made only from RGB333 `666` and `777`,
grouped by the number of `666` pixels:

```sh
python3 harness/palette_audit/near_white.py \
  videos/BadApple_H32_256x224_pcm13/decisions.pkl \
  videos/BadApple_H32_256x224_pcm13/master
```

The digital RGB labels use the encoder/emulator levels
`0, 36, 72, 108, 144, 180, 216, 252`. Therefore RGB333 `777` is the hardware
maximum, CRAM word `0x0EEE`, and appears as digital `#FCFCFC`; it is not a
missing white level.
