---
name: comps
description: Project skill for reading the "Comp = resident reuse" thresholds (Same/Near/Coa/Flbk) from the real delta codec implementation in tools/sim.py, presenting them as a table, and helping tune the values. Use when the user wants to refine comparison thresholds or invokes "/comps". Values are on a 0-255 scale; smaller is stricter.
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
grep -nE "NEAR_F3 = dict|CBRSIM_NEAR_Y|CBRSIM_NEAR_C|CBRSIM_TCOA_|CBRSIM_TMID_|CBRSIM_TFAR_|COA_K =|COA_BW" tools/sim.py
```

Do not rely on values written in older notes or chat history. The values in
`tools/sim.py` are the source of truth. If the code and any document
disagree, report the code value first and mention that the document is stale.

Read these values:

- `NEAR_F3`: Near `Ym` / `Yp` / `C`, defaulted by `CBRSIM_NEAR_YM`,
  `CBRSIM_NEAR_YP`, and `CBRSIM_NEAR_C`.
- `MIDFAR_TIERS` `coa` / `mid` / `far`: each tier's `Ym` / `Yp` / `C`,
  defaulted by `CBRSIM_TCOA_*`, `CBRSIM_TMID_*`, and `CBRSIM_TFAR_*`.
- Search range: `COA_K`, the number of candidates checked inside a bucket, and
  `COA_BW`, the average-color bucket width.
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
| Same | exact match | - | - | Points to the same VRAM tile. Costs 0 bytes. |
| Near | value | value | value | Almost identical. Keeps the existing tile at 0 bytes. |
| Coa | value | value | value | Roughly close. Reuses a resident tile with no CD transfer. |
| Flbk | value | value | value | Fallback for what would be a Miss (merged Mid+Far). Wide threshold, 2-byte entry. Starvation insurance. |

Always add these notes:

- The code checks ranks from strict to loose. A candidate passes a rank only if
  all three measures are within that rank's thresholds.
- The strictest passing rank is used.
- Near and Coa are "good reuse": they save CD bandwidth.
- Flbk is insurance for starvation (merged Mid+Far). It is a lower-quality fallback with a deliberately wide threshold, so it almost always fills a hole.
- If no rank passes, the cell becomes Miss.
- Making thresholds stricter means fewer reused tiles, more Raw transfers,
  better image accuracy, more CD pressure, and often more starvation.
- Making thresholds looser means the reverse.
- Same is exact deduplication. It has no threshold.
- Do not describe Flbk as normal quality. It is an emergency reuse level that
  keeps motion going when bytes are short.
- When explaining this to the user, avoid heavy math language. Say "how much
  the tile differs" before using names like threshold, average, or maximum.

If the user asks "which value should I change first?", start with Coa. Coa has
the biggest practical effect on good resident reuse. Near should stay fairly
strict because it silently keeps the previous tile. Flbk should be tuned
as starvation insurance after Coa is understood.

### 3. Tune Values If Asked

Permanent changes go into `tools/sim.py` defaults:

- `NEAR_F3`, such as the current default strings for `Ym`, `Yp`, and `C`.
- `MIDFAR_TIERS`, with the default Coa / Flbk threshold values.

Temporary experiments should use environment overrides:

```text
CBRSIM_NEAR_YM
CBRSIM_NEAR_YP
CBRSIM_NEAR_C
CBRSIM_TCOA_YM
CBRSIM_TCOA_YP
CBRSIM_TCOA_C
CBRSIM_TMID_YM
CBRSIM_TMID_YP
CBRSIM_TMID_C
CBRSIM_TFAR_YM
CBRSIM_TFAR_YP
CBRSIM_TFAR_C
```

After changing values, run `/sim` or the equivalent simulation to check:

- starvation rate
- category counts
- average Near / Coa counts
- Miss rate
- Flbk usage
- visible quality

Stricter settings commonly increase Raw and Miss. Looser settings commonly
increase reuse and artifacts.

When reporting a tuning result, include both the numbers and what they mean in
plain language:

- "Raw went up" means more CD bytes are being spent on exact tile updates.
- "Coa went up" means more already-resident tiles are being reused.
- "Miss went up" means visible cells are not getting a good update in time.
- "Flbk went up" means the codec is leaning harder on the emergency fallback.
- "Starvation went up" means the requested stream could not always fit the
  available CD supply.

## Notes

- Thresholds are absolute 0-255 values, equivalent to RGB888-style differences.
- Each tile has 8x8 = 64 pixels. The code uses average and maximum values over
  that tile.
- Candidate search first narrows by average-color bucket (`COA_BW`), then checks
  up to `COA_K` recent candidates with the F3 measures, then picks the best one.
- If the bucket misses, there may be no candidates and the result can become
  Miss immediately.
- `COA_K` and `COA_BW` change how many possible resident tiles are considered.
  Bigger search can find better reuse but can slow simulation and may change
  the balance between quality and artifacts.
- Keep H32 / H40 / mode4 playback goals in mind. The same visual thresholds can
  feel different when resolution, tile count, or frame rate changes.
- Detailed logic is in `tools/sim.py`, especially `best_resident`,
  `tier_of`, and `commit_unified`.
- See `[[codec-fps-resolution-policy]]` where relevant.
