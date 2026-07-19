# Player constants build matrix

This harness verifies the disc-specific Main/Sub assembly path without
reusing stale packed movies. It creates current TTRC v9 headers for H32 and H40
at 15, 24 and 30 fps, generates `player_constants.inc`, then assembles and links
both the generic and specialized DEBUG players.

For every case it requires:

- specialized IP and SP binaries do not grow relative to the generic build;
- the specialized SP binary stays within the 4,096-byte boot area;
- the specialized SP contains the exact HEADER signature immediate and the
  `0xBAD1` mismatch diagnostic;
- Main's specialized flip branches cannot escape the `bf_doflip` control-flow
  region before `do_flip`;
- all six geometry/timing combinations assemble and link successfully.

Run it with the project Python environment:

```sh
tools/python.sh harness/player_constants/verify.py
```

The script uses a temporary directory under `tmp/` and removes it after the
matrix completes. It does not depend on copyrighted source video or a prior
simulation.

Measure the conservative instruction-cycle saving over a real fixed-N2 packed
stream with:

```sh
tools/python.sh harness/player_constants/measure_cycles.py \
  --header out/sonic-jam-op-h32/HEADER.DAT \
  --body out/sonic-jam-op-h32/BODY.DAT
```

The cycle model uses the MC68000 User's Manual Section 8 timings. It counts all
real packed cold runs but deliberately excludes variable extra savings from
CDC polling, wave-chunk boundaries, DMA-budget refills and palette switches.
The result is therefore a lower bound for the current player, not the stale
1,400-cycles-per-frame estimate from before Main code generation was added.
