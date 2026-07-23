---
name: comps
description: Project skill for reading the "Comp = resident reuse" thresholds (Same/Near/Flbk) from the real delta codec implementation in tools/sim.py, presenting them as a table, and helping tune the values. Use when the user wants to refine comparison thresholds or invokes "/comps". Values are on a 0-255 scale; smaller is stricter.
---

# /comps: Resident-Reuse Threshold Table and Tuning

The delta codec decides whether a tile already resident in VRAM is close enough
to reuse. It checks that with three plain measures: brightness average error,
brightness peak error, and color average error. This skill reads the actual
thresholds from code, presents them in an easy table, and helps the user tune
them. Avoid dense terminology; explain in everyday language first, following
AGENTS.md.

## Procedure

### 1. Read Current Values From the Real Code

```sh
grep -nE "NEAR_F3 = dict|CBRSIM_NEAR_Y|CBRSIM_NEAR_C|CBRSIM_TFLBK_|RESIDENT_K =|RESIDENT_BW" tools/sim.py
```

Do not rely on values written in older notes or chat history. The values in
`tools/sim.py` are the source of truth. If the code and any document
disagree, report the code value first and mention that the document is stale.

Read these values:

- `NEAR_F3`: Near `Ym` / `Yp` / `C`, defaulted by `CBRSIM_NEAR_YM`,
  `CBRSIM_NEAR_YP`, and `CBRSIM_NEAR_C`.
- `MIDFAR_TIERS` `flbk`: `Ym` / `Yp` / `C`, defaulted by
  `CBRSIM_TFLBK_*`. These bounds apply when improve-only mode is disabled.
- Search range: `RESIDENT_K`, the number of candidates checked inside the
  target bucket, and `RESIDENT_BW`, the average-color bucket width. Deferred
  Flbk cells get one bounded second chance from the newest eligible resident
  in each adjacent bucket when the target bucket cannot improve the display.
- Any local environment values if the user is clearly running a temporary
  experiment. These override the defaults only for that run.

### 2. Present the Table in This Shape

Three measures, all on a 0-255 scale. `0` means identical. Smaller values are
stricter:

- **Brightness average drift Ym**: average brightness difference over the 64
  pixels in a tile.
- **Brightness peak drift Yp**: the biggest brightness difference at any one
  pixel. This catches shape breakage.
- **Color average drift C**: average color difference.

| Rank | Ym | Yp | C | How it is used |
|---|---:|---:|---:|---|
| Same | exact match | - | - | Reuses the exact resident pattern. No pattern transfer; a changed name entry costs 2 bytes. |
| Near | value | value | value | Almost identical. Keeping the current tile costs 0 bytes; pointing to another resident costs a 2-byte name entry. |
| Flbk | value | value | value | Used only after an exact load cannot be funded. Default improve-only mode accepts the best resident only when it improves the current display; absolute mode uses these wide bounds. |

Always add these notes:

- The code checks ranks from strict to loose. A candidate passes a rank only if
  all three measures are within that rank's thresholds.
- Near is accepted as normal resident reuse. A candidate outside Near first
  gets an exact cold-load attempt.
- Flbk is insurance for starvation. It is considered only when that exact load
  cannot fit, and the default mode requires an actual improvement over the
  currently displayed tile.
- If no rank passes, the cell becomes Miss.
- Making thresholds stricter means fewer reused tiles, more Raw transfers,
  better image accuracy, more CD pressure, and often more starvation.
- Making thresholds looser means the reverse.
- Same is exact deduplication. It has no threshold.
- Do not describe Flbk as normal quality. It is an emergency reuse level that
  keeps motion going when bytes are short.
- When explaining this to the user, avoid heavy math language. Say "how much
  the tile differs" before using names like threshold, average, or maximum.

If the user asks "which value should I change first?", start with Near only
when normal resident reuse is the target. Near should stay fairly strict
because it can silently keep the previous tile. Tune Flbk separately as
starvation insurance.

### 3. Tune Values If Asked

Permanent changes go into `tools/sim.py` defaults:

- `NEAR_F3`, such as the current default strings for `Ym`, `Yp`, and `C`.
- `MIDFAR_TIERS`, with the default Flbk threshold values.

Temporary experiments should use environment overrides:

```text
CBRSIM_NEAR_YM
CBRSIM_NEAR_YP
CBRSIM_NEAR_C
CBRSIM_TFLBK_YM
CBRSIM_TFLBK_YP
CBRSIM_TFLBK_C
```

After changing values, run `/sim` or the equivalent simulation to check:

- starvation rate
- category counts
- average Near / Flbk counts
- Miss rate
- Flbk usage
- visible quality

Stricter settings commonly increase Raw and Miss. Looser settings commonly
increase reuse and artifacts.

When reporting a tuning result, include both the numbers and what they mean in
plain language:

- "Raw went up" means more CD bytes are being spent on exact tile updates.
- "Near went up" means more close resident tiles are being reused.
- "Miss went up" means visible cells are not getting a good update in time.
- "Flbk went up" means the codec is leaning harder on the emergency fallback.
- "Starvation went up" means the requested stream could not always fit the
  available CD supply.

## Notes

- Thresholds are absolute 0-255 values, equivalent to RGB888-style differences.
- Each tile has 8x8 = 64 pixels. The code uses average and maximum values over
  that tile.
- Candidate search first narrows by average-color bucket (`RESIDENT_BW`), then
  checks up to `RESIDENT_K` recent candidates with the F3 measures, then picks
  the best one. Flbk alone checks the newest eligible candidate from each of
  the 26 adjacent buckets if that first result cannot improve the display.
- If neither the target nor adjacent buckets provide an improving candidate,
  the result can become Miss.
- `RESIDENT_K` and `RESIDENT_BW` change how many possible resident tiles are considered.
  Bigger search can find better reuse but can slow simulation and may change
  the balance between quality and artifacts.
- Keep H32 / H40 / mode4 playback goals in mind. The same visual thresholds can
  feel different when resolution, tile count, or frame rate changes.
- Detailed logic is in `tools/sim.py`, especially `best_resident`,
  `tier_of`, and `commit_unified`.
- See `[[codec-fps-resolution-policy]]` where relevant.
