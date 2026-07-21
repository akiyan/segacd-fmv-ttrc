# Pattern supply replay

This harness independently verifies the v12 boot-preloaded pattern supplies.
It does not import the production packer, scheduler, or planner.

The player has four pattern sources:

- `PrgBuf`: the existing streamed PRG-RAM circular buffer.
- `WordBuf0`: immutable patterns in the physical Word-RAM bank handed to Main
  for even movie frames.
- `WordBuf1`: the corresponding bank for odd movie frames.
- `DicBuf`: a persistent 256-entry dictionary copied once into Main RAM.

On-disc cold entries use the otherwise unused name-table flip bits to identify
`Prg`, `Wr`, or `Dic`. Cold-run descriptors carry the source and an 8-bit
DicBuf start index.
`Wr` resolves to `Wr0` or `Wr1` from movie-frame parity.

Run the independent whole-stream proof against an already packed profile:

```sh
tools/python.sh harness/pattern_supply/verify.py \
  --header out/bad-apple-h40/HEADER.DAT \
  --body out/bad-apple-h40/BODY.DAT \
  --decisions videos/BadApple_H40_320x224_adpcm22/tmp/decisions.pkl
```

The verifier parses every HEADER and BODY sector, reproduces rate-slot
boundaries, validates source-coded runs, consumes each physical source in exact
player order, and checks every cold and reused VRAM pattern against the decision
log. It also requires every consumptive preload and every useful Prg pattern to
be consumed exactly once, while DicBuf entries may be reused by index.

Run this proof again after a current v12 full encode; older consumptive-preload
counts are not evidence for indexed DicBuf.
