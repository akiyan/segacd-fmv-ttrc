# Pattern supply replay

This harness independently verifies the v10 boot-preloaded pattern supplies.
It does not import the production packer, scheduler, or planner.

The player has four pattern sources:

- `PrgBuf`: the existing streamed PRG-RAM circular buffer.
- `WordBuf0`: immutable patterns in the physical Word-RAM bank handed to Main
  for even movie frames.
- `WordBuf1`: the corresponding bank for odd movie frames.
- `MainBuf`: immutable boot-preloaded patterns copied once into Main RAM.

On-disc cold entries use the otherwise unused name-table flip bits to identify
`Prg`, `Wr`, or `Main`. Cold-run descriptor count words carry the same source.
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
log. It also requires every preload and every useful Prg pattern to be consumed
exactly once with only zero sector padding left.

The full 6,576-frame Bad Apple H40/30 ADPCM22 proof consumed one frame-0 HEADER
pattern, all 880 Wr0, 880 Wr1, and 208 Main patterns, then 762,861 timed Prg
patterns without an under-run. It matched every source-coded transfer and every
reconstructed VRAM cell against the decision log.
