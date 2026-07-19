# Buffering and whole-movie tank planning

This document describes how the encoder moves virtual byte capacity between
frames, how that differs from the physical payload RING, and why the current
whole-movie planner is shaped around concentrated Miss bursts.

## Scope and terminology

Two different objects have similar capacities but different jobs:

| Object | Domain | Job |
|---|---|---|
| virtual tank / VBV budget | encoder model | Decides how many quality bytes a frame may spend. |
| payload RING | Sub-CPU PRG-RAM | Holds prefetched 32-byte cold patterns delivered in CD sectors. |

The virtual tank is not a second player buffer. Its level is an encoder-side
accounting value. The payload RING is physical and is scheduled independently
by `stream_schedule.py`, then verified again by `pack_stream.py`.

Both currently use the 388 KiB usable capacity derived from the 428 KiB
physical RING minus its 40 KiB delivery-jitter margin. Equal capacities do not
make their occupancy curves interchangeable.

## Objective

The planner's primary quality objective is to avoid a large number of Miss
tiles arriving together. A small error distributed across the picture is less
damaging than a frame where hundreds of cells cannot be updated.

The planner therefore protects future changes likely to fall through to Flbk
or Miss. It does not optimize one whole-frame pixel-error score, and it does
not try to force starvation to zero by reducing the raster or frame rate.

## End-to-end flow

The planner runs after palette selection and quantization but before the final
per-frame codec decisions:

1. Render the exact quantized target for every frame.
2. Mark changed cells whose visual change exceeds the existing Coa bound.
3. Dry-run the complete exact target through the shared `TileAllocator`.
4. Record complete exact demand and the narrower Coa-exceeding Miss-risk
   demand for every frame.
5. Walk both demand traces backwards to build two reserve curves.
6. Run the normal encoder pass, keeping the Miss-risk reserve intact.
7. Spend optional Raw/Buf upgrade bytes only above the complete-exact reserve.
8. Update the virtual tank from actual spending. Both reserve curves naturally
   reach zero at the movie end.

The old occupancy-percentage lanes, separate recovery holdback, and terminal
drain ramp are not alternate modes. They have been removed; the whole-movie
plan is the only virtual-budget allocation path when VBV is enabled.

## Predicting demand

`upgrade_planner.predict_update_demands()` receives the already-quantized
pattern and palette assignment arrays. It advances one shared `TileAllocator`
through the exact target, preserving the same residency and eviction model
used by the final encoder.

For each frame after frame 0, complete exact demand consists of:

- 2 bytes for every cell whose exact pattern or palette assignment changed;
- 32 bytes once for every distinct changed pattern that is not resident;
- a cold-pattern count clipped to the mode/fps hardware limit.

Repeated cells that need the same newly loaded pattern share its 32-byte cold
cost. An exact pattern already resident in VRAM costs only the 2-byte
name-table update.

Frame 0 has zero streaming demand because it is loaded from `HEADER.DAT` during
boot. It still seeds the predictive VRAM state so frame 1 residency is correct.

### Palette boundaries

The comparison uses the palette active in the target frame. At a CRAM segment
boundary, the previous exact pattern indices are rendered through the new
palette before visual distance is measured. This models the fact that retained
tiles immediately change colour when CRAM changes.

The current PALTAB path preloads segment palettes, so `PAL_WRITE_BYTES` is zero.
The supply calculation still supports subtracting an in-stream palette write
if that format choice changes later.

## The two demand and reserve curves

| Curve | Demand included | Used by |
|---|---|---|
| upgrade exact | every exact changed cell and predicted cold pattern | Optional correction of Near, Coa, Flbk, Miss, and carried approximations to Raw/Buf. |
| main Miss-risk | only changed cells whose exact-target visual change exceeds the Coa luma/chroma bounds | The normal per-frame allocation pass. |

The exact curve is deliberately strict for optional work: an early cosmetic
upgrade must not consume bytes that the exact dry run predicts a future burst
will need.

The main curve is deliberately narrower. A change within Coa can degrade to a
resident approximation without becoming an empty Miss. Reserving against all
exact changes in the main pass over-protects the future and can starve the
present. In a 10-second Lunar check, applying the complete exact curve to the
main pass produced 13,757 Miss tiles with a peak of 695, so that variant was
rejected. Protecting only Coa-exceeding changes keeps the plan focused on the
failure class it is meant to prevent.

## Building a reserve backwards

Each frame has a predictable fresh supply:

```text
frame supply = FRAME_BYTES
             - audio/control bytes
             - fixed 2-byte name-table allowance
             - any in-stream palette bytes
```

The reserve is built from the end of the movie towards the beginning. The last
frame's required reserve after display is zero. For each earlier frame, the
planner asks how many bytes the next frame still needs after using that next
frame's own supply:

```text
reserve after this frame =
    clamp(next reserve + next demand - next supply, 0, tank capacity)
```

This is the minimum amount worth retaining for the predicted future trace. A
light run before a burst grows the reserve; a light tail after the last burst
releases it. No end-of-movie percentage or artificial ramp is needed.

Clipping at tank capacity is intentional. If a predicted burst needs more than
one full tank plus its own supply, optional restraint cannot make that burst
fully feasible. The ordinary priority, approximation, carry-over, and Miss
logic still decides the best achievable result.

## Applying the reserve

For a normal frame, the total spending limit is:

```text
spendable = tank before frame + frame supply - reserve after frame
```

The result is never negative. Work already committed by an earlier stage is
also never undone: `planned_spend_limit()` returns at least
`already_spent`.

The normal allocation pass calls this with the main Miss-risk reserve and no
prior spending. It then processes changed cells in the existing visual
priority order.

The optional upgrade pass runs after normal allocation. It calls the same
function with the upgrade exact reserve and the bytes already spent. Candidate
approximations retain their quality ordering: persistent approximations are
promoted to the highest correction priority, followed by the existing
Flbk/Coa/Near and carry ordering. All candidates share the one planned limit;
there are no separate percentage lanes.

After the frame, the virtual tank becomes:

```text
tank after frame =
    clamp(tank before frame + frame supply - actual spending,
          0, tank capacity)
```

Frame 0 is the exception: boot loads it outside BODY streaming, so it leaves
the virtual tank full for frame 1.

## Physical payload RING remains independent

The planner changes which exact patterns the encoder chooses, but it does not
replace the physical delivery proof. After decisions are known:

1. `stream_schedule.py` calculates control and payload delivery in whole CD
   sectors.
2. The schedule applies the prebuffer, routing table, delivery cadence, and
   physical RING capacity.
3. `pack_stream.py --verify` replays every delivery and consumption event.
4. A stream is accepted only when the decoded output matches the sim and the
   payload RING never under-runs or overflows.

`Tank` in the analysis overlay is physical payload-RING occupancy. It must not
be replaced by either virtual reserve curve.

## Diagnostics

When VBV is enabled, `sim.py` writes these arrays to
`buffer_remaining.npz`:

| Array | Unit | Meaning |
|---|---:|---|
| `vbv_remaining` | 32-byte pattern slots | Actual virtual-tank level after each frame. |
| `upgrade_demand_bytes` | bytes | Complete exact demand predicted by the dry run. |
| `upgrade_reserve_bytes` | bytes | Reserve protecting optional upgrades. |
| `main_risk_demand_bytes` | bytes | Coa-exceeding predicted demand. |
| `main_risk_reserve_bytes` | bytes | Reserve protecting the normal allocation pass. |
| `remaining` | 32-byte pattern slots | Physical payload-RING occupancy used by the analysis Tank meter. |

The sim report also prints the start, peak, and end of both reserve curves.
The end value must be zero. A nonzero physical `remaining` value at the end can
still be valid sector padding and is not unused virtual quality budget.

## Validation

### Bad Apple H40, full 6576 frames

The full H40/30 fps comparison used the previous fixed policy as the baseline
and prioritized concentrated Miss measurements:

| Metric | Previous fixed policy | Whole-movie planner | Change |
|---|---:|---:|---:|
| total Miss tiles | 57,058 | 46,075 | -19.2% |
| maximum Miss in one frame | 555 | 442 | -20.4% |
| 99th percentile | 340.50 | 207.25 | -39.1% |
| sum of squared per-frame Miss | 17,015,668 | 8,315,207 | -51.1% |
| frames with 300 or more Miss | 81 | 12 | -85.2% |
| frames with 400 or more Miss | 41 | 4 | -90.2% |
| frames with 450 or more Miss | 20 | 0 | eliminated |
| frames with 500 or more Miss | 11 | 0 | eliminated |
| starved frames | 523 | 512 | 11 fewer |

The selected run averaged 137,539 B/s, below CD 1x. The packed routing-v9
stream reproduced all 6576 sim frames exactly, with zero undelivered pattern
pops and zero physical payload-RING under-runs.

### Lunar H32, 10-second check

The shorter sanity check moved total Miss from 131 to 110 and starved frames
from 8 to 7. Its single-frame peak moved from 50 to 61. This is a useful guard
against claiming that every local maximum must improve; the planner is chosen
for its large-burst behavior across demanding full-length material.

## Source locations and tests

- `tools/upgrade_planner.py`: exact/risk demand prediction, backwards reserve,
  and spending limit.
- `tools/sim.py`: Coa-risk mask construction and integration into normal and
  optional allocation.
- `tools/test_upgrade_planner.py`: residency costs, cold-cap clipping,
  protected demand, terminal drain, and spending-limit tests.
- `CONFIG.md`: capacities and current encoder behavior.
- `ANALYSIS.md`: overlay meanings and saved diagnostic arrays.

Changes to this planner affect encoder output and require an encoder build
version bump in `tools/av_version.txt`, a clean sim, packed-stream verification,
and representative full-length validation.
