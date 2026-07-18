# Player constants build matrix

This harness verifies issue #21's disc-specific Main/Sub assembly path without
reusing stale packed movies. It creates current TTRC v8 headers for H32 and H40
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
