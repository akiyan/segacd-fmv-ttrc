---
name: priorities
description: Project skill for reading and presenting the "which tile updates first" priority weights from the real delta codec implementation in tools/sim.py. Use when the user wants to tune update priority, coefficients, or priority layers, or invokes "/priorities".
---

# /priorities: Update-Tile Priority Weights and Tuning

The per-frame quality allowance is limited, so the encoder cannot always update every
changed tile. It must choose which changed tiles go first. This skill reads the
real scoring coefficients and layers from code, presents them in a plain table,
and helps tune them. Avoid dense terminology; explain in everyday language
first, following AGENTS.md.

## Procedure

### 1. Read Current Values From the Real Code

```sh
grep -nE "DETAIL_ALPHA =|AGING_ALPHA =|WAIT_CAP =|BORDER_TILES =|BORDER_WEIGHT =|score =|order =|PRIOLAYER|cell_tier" tools/sim.py
```

Use the code as the source of truth. Do not quote old chat notes unless they
match the current implementation. If the user asks for the current behavior,
read `tools/sim.py` first, then explain it.

Also look near the places where `score`, `order`, `lexsort`, `cell_tier`, and
`commit_unified` are used. The constants alone do not fully explain the order;
the sort keys decide how the constants are applied.

### 2. Present the Values in This Shape

**Base score**: bigger scores update earlier.

```text
score = visual change * (1 + DETAIL_ALPHA * detail) * aging * border weight
```

| Element | Meaning | Default coefficient | Effect |
|---|---|---|---|
| `diff` visual change | Sum of RGB difference from the currently displayed tile. | - | Tiles that changed more go first. |
| `detail` | How detailed the tile is, measured by tile standard deviation. | `DETAIL_ALPHA=1.5` | Detailed artwork gets extra priority; flat fills wait. |
| `aging` | How many consecutive frames the tile has waited. | `AGING_ALPHA=0.6`, `WAIT_CAP=10` | Waiting adds priority. Max multiplier is `1 + 0.6 * 10 = 7`. Helps clear old misses. |
| `border` | De-emphasizes screen-edge tiles. | `BORDER_TILES=2`, `BORDER_WEIGHT=0.4` | Outer two tile bands get 0.4x priority, favoring the center. |
| tie break | What happens when scores tie. | `CBRSIM_CENTERTIE` | If enabled, cells closer to the center go first through `lexsort`. |

There is a single ordering: changed tiles are sorted by the base score above
(with the optional center tie-break). There is no separate priority layer that
promotes degraded cells ahead of the score — an earlier `CBRSIM_PRIOLAYER`
layer was removed because, on 8x8 tiles, force-promoting Miss/Flbk/Coa cells
caused block-noise artifacts. Degraded cells still rise naturally through the
`aging` term (they keep waiting, so their score grows each frame).

Plain explanation for the user:

- The encoder has a short list of changed tiles and cannot always send them all.
- It gives each tile an urgency score.
- Big visible changes, detailed art, and tiles that waited too long move up.
- Edge tiles are pushed down so the center of the picture tends to stay cleaner.
- Rough tiles are not force-jumped to the front; they climb as they wait.

### 3. Tune Values If Asked

Permanent coefficient changes go into `tools/sim.py`:

- `DETAIL_ALPHA`
- `AGING_ALPHA`
- `WAIT_CAP`
- `BORDER_TILES`
- `BORDER_WEIGHT`

Tie behavior can be overridden by an environment variable:

```text
CBRSIM_CENTERTIE
```

After changing values, run `/sim` or the equivalent simulation and inspect:

- starvation rate
- category counts
- carried-over age
- visible quality
- how often degraded tiers are upgraded

When reporting a tuning result, include what changed in user-visible terms:

- Higher `DETAIL_ALPHA`: more attention to detailed art, less to flat areas.
- Lower `DETAIL_ALPHA`: flatter and simpler behavior, but detailed parts may
  smear or wait longer.
- Higher `AGING_ALPHA` or `WAIT_CAP`: old skipped tiles are rescued more
  aggressively.
- Lower `AGING_ALPHA` or `WAIT_CAP`: the encoder favors fresh large changes,
  but old rough cells can remain rough longer.
- Wider or heavier border discount: center quality improves, edges can lag.
- Weaker border discount: edges get treated more evenly, center may lose some
  protection when the scene is busy.

Do not treat one metric as enough. A setting that lowers starvation can still
look worse if it upgrades the wrong tiles first. Always inspect actual frames
or the rendered analysis video when the change is meant to improve quality.

## Notes

- Priority sorting only applies to changed tiles that are not Near-skipped.
- Near and Same cost 0 bytes and do not compete for Raw transfer budget.
- Each cell's current degradation level is tracked in `cell_tier`:
  `0=Miss`, `1=Flbk`, `2=Coa`, `3=Near`, `9=good`.
- The priority logic decides who receives scarce Raw update bytes. It does not
  change Same/Near zero-byte reuse before that point.
- Because frame rate, composition, and aspect can change by source, useful
  coefficients may vary by content. A high-motion 30 fps clip and a
  lower-motion 15 fps clip can need different priorities even when the CD rate
  is the same.
- For H40 and mode4 experiments, re-check these priorities. A larger or
  differently shaped tile grid changes which mistakes are most visible.
- Detailed logic is in `tools/sim.py`, especially score and order
  generation plus `commit_unified`.
- `[[comps]]` covers the resident-reuse thresholds.
