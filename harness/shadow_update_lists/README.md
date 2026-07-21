# Shadow update-list verifier

This harness verifies the TTRC v11 per-frame choice between the legacy
bitmap/source-entry representation and completed shadow-update pairs.

It checks every packed frame against `decisions.pkl`, replays the resulting
name-table shadow, recomputes the nominal MC68000 cycle model, rejects any
selected list that is not faster, and requires the selected whole-stream
PrgBuf and control-readiness minima to match or exceed the all-legacy baseline.
It also exhaustively proves that the Main player's `offset & 0x0FFE` guard maps
every corrupt 16-bit offset to an even word inside the padded 4 KiB shadow.

Run it after a clean sim and verified pack:

```sh
tools/python.sh harness/shadow_update_lists/verify.py \
  --header out/PROFILE/HEADER.DAT \
  --body out/PROFILE/BODY.DAT \
  --decisions videos/STEM/tmp/decisions.pkl
```
