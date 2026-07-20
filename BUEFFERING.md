# Pattern supply and whole-movie quality planning

This document defines how exact 32-byte tile patterns reach VRAM, how the four
physical pattern supplies differ from the encoder's offline quality budget, and
how boot-only memory is assigned to the frames where it prevents concentrated
Miss bursts.

## Names

Use these names for physical pattern storage:

| Public name | Analysis label | Memory | Capacity | Lifetime |
|---|---|---|---:|---|
| `PrgBuf` | `Prg` | Sub-CPU PRG-RAM | 12,416 patterns / 388 KiB usable | Streamed circular buffer; refilled from `BODY.DAT`. |
| `WordBuf0` | `Wr0` | physical 1M Word-RAM bank 0 | 880 patterns / 27.5 KiB | Loaded once from `HEADER.DAT`, then drained by eligible even frames. |
| `WordBuf1` | `Wr1` | physical 1M Word-RAM bank 1 | 880 patterns / 27.5 KiB | Loaded once from `HEADER.DAT`, then drained by eligible odd frames. |
| `MainBuf` | `Main` | Main RAM | 208 patterns / 6.5 KiB | Staged through Word RAM at boot, copied once to Main RAM, then drained by either parity. |

`PrgBuf` is physically implemented as a ring buffer, which is why the player
still has internal assembly constants such as `RING_BASE` and `RING_SIZE`.
“RING” describes the data structure; `PrgBuf` is the public name of that
pattern supply.

The old name **Tank** is retired. There is no Tank object or Tank meter.

The analysis category **Buf** is not a buffer. It is a historical funding
class meaning that an exact load used saved whole-movie quality budget rather
than only the current frame's fresh allowance. It remains one of the seven tile
categories, but there is no separate Buf meter.

## Two separate layers

The design has one offline planning layer and four physical supplies:

| Layer | Exists where | Purpose |
|---|---|---|
| whole-movie quality budget | encoder only | Moves permission to spend bytes from light frames to demanding frames. |
| `PrgBuf` / `WordBuf0` / `WordBuf1` / `MainBuf` | player memory | Hold the exact pattern bytes that the chosen updates consume. |

The quality budget is accounting, not a fifth player buffer. Its trace is kept
for diagnostics but is not shown as a physical-supply meter. Its 388 KiB
ceiling matches the usable `PrgBuf` scheduling ceiling so the encoder cannot
assume more time-shifting freedom than the stream can provide. Equal ceilings
do not make the two traces interchangeable.

## Objective

The primary quality objective is to prevent many Miss cells from arriving in
one frame. A small approximation spread across the picture is usually less
damaging than a frame with hundreds of unchanged holes.

The planner therefore gives priority to future changes that are likely to fall
through to Flbk or Miss. It does not optimize a single whole-frame pixel-error
score, and it does not hide starvation by lowering the selected raster or
frame rate.

## End-to-end planning

Planning happens after palette selection and quantization but before the final
per-frame decisions:

1. Render the exact quantized target for every frame.
2. Mark changed cells whose visual change exceeds the Coa bound. These are the
   narrower Miss-risk set.
3. Dry-run the complete exact target through the same `TileAllocator` used by
   the final encode.
4. Record, per frame, complete exact bytes/cold patterns and protected
   Miss-risk bytes/cold patterns.
5. Allocate the finite `WordBuf0`, `WordBuf1`, and `MainBuf` boot credits to
   predicted bursts.
6. Subtract only the saved 32-byte pattern payload from the future demand. A
   preloaded exact tile still needs its 2-byte name-table entry.
7. Walk the adjusted demand backwards to build the complete-exact and
   Miss-risk reserve curves.
8. Run the normal encoder pass. It consumes boot-preload credits only for cold
   patterns that are actually selected.
9. Freeze one physical source for every update in the decision log.
10. Pack and independently replay that frozen assignment. No later stage is
    allowed to invent a different source choice.

The previous occupancy-percentage lanes, recovery holdback, and terminal drain
ramp are removed. The backwards whole-movie plan is the only quality-budget
allocation policy.

## Exact-demand prediction

`upgrade_planner.predict_update_demand_details()` advances one shared VRAM
allocator through the exact target. For each frame after frame 0, exact demand
contains:

- 2 bytes for every cell whose exact pattern or palette assignment changed;
- 32 bytes once for every distinct changed pattern that is not resident;
- a cold-pattern count clipped to the measured mode/fps/active-area limit.

Repeated cells that use one newly loaded pattern share its 32-byte cost. An
exact pattern already in VRAM costs only the 2-byte name entry. Frame 0 has zero
timed-stream demand because `HEADER.DAT` loads it during boot, but it still
seeds predicted VRAM residency for frame 1.

At a palette boundary, the previous exact indices are rendered through the new
palette before visual distance is measured. Retained tiles therefore reflect
the colour change caused by the real CRAM switch.

The prediction exposes two traces:

| Trace | Includes | Protects |
|---|---|---|
| complete exact | every exact changed cell and predicted cold pattern | Optional correction of Near, Coa, Flbk, Miss, and carried approximations. |
| protected Miss-risk | changed cells whose visual change exceeds the Coa bounds | Normal allocation against future Flbk/Miss bursts. |

The complete trace is deliberately strict for optional upgrades. The protected
trace is narrower so a Coa-safe change can degrade gracefully instead of
starving the current frame to preserve unnecessary bytes for the future.

## Assigning the boot preloads

`pattern_supply.plan_frame_budgets()` assigns one 32-byte credit at a time.
This is a water-fill policy: after every credit, the affected frame's remaining
risk is recomputed before the next credit is selected. It avoids dumping an
entire buffer into the frame that merely started with the largest burst.

Credits are ordered as follows:

1. protected cold demand before unprotected exact demand;
2. largest remaining protected-byte demand;
3. largest remaining exact-byte and cold demand;
4. frame number as the deterministic final tie-break.

Word RAM is allocated first because it is parity-constrained:

- `WordBuf0` can serve even timed frames;
- `WordBuf1` can serve odd timed frames;
- frame 0 is excluded because it already has its own boot block.

`MainBuf` is allocated afterwards and can fill the largest residual demand on
either parity. No frame receives more preload credits than its predicted exact
cold count, and no physical capacity may be exceeded.

The two Word-RAM buffers are not duplicate caches. They hold different pattern
sequences selected for their own frame parity. This preserves the full 1,760
pattern contribution of the two physical banks instead of spending half of it
on an identical copy.

## Building and spending the quality reserve

Each timed frame receives fresh offline spending allowance:

```text
frame supply = target bytes per frame
             - audio/control bytes
             - fixed name-table allowance
             - any in-stream palette bytes
```

For each reserve trace, the encoder walks from the movie end towards frame 0:

```text
reserve after this frame =
    clamp(next reserve + next demand - next supply,
          0, quality-budget capacity)
```

The last reserve is zero. A light run before a burst grows the reserve, while a
light tail releases it naturally. If one burst needs more than a full quality
budget plus its own supply, clipping is intentional: restraint alone cannot
make that burst fully exact, so the normal priority, approximation, carry, and
Miss paths choose the best achievable result.

The per-frame spending limit is:

```text
spendable = quality budget before frame
          + fresh frame supply
          - reserve required after frame
```

Already committed work is never undone. The normal pass uses the protected
Miss-risk reserve. The optional exact-upgrade pass uses the complete-exact
reserve and starts from the bytes already spent by the normal pass. Persistent
approximations retain their high correction priority, but all candidates share
this one planned limit.

After the frame:

```text
quality budget after frame =
    clamp(quality budget before frame
          + fresh frame supply
          - actual spending,
          0, quality-budget capacity)
```

Frame 0 is the exception: boot loads it outside `BODY.DAT`, so frame 1 starts
with the complete quality budget.

## Freezing the physical source

The final encoder may select fewer cold loads than prediction, or select them
in a different priority order. It therefore assigns sources to realized cold
updates, not merely to predicted frame totals.

For each frame, realized preload loads consume that frame's planned Word credit
first, then its Main credit. Remaining cold loads use `Prg`. Non-cold resident
repoints always carry source `Prg` as a neutral value because no pattern source
is consumed.

The decision log stores the source array aligned with the update array. The
packer validates update counts, cold flags, frame-0 restrictions, all three
preload capacities, per-frame source totals, and the source-aware run count.
It then writes four chronological pattern streams:

- the continuously delivered Prg stream;
- the boot-only Wr0 stream;
- the boot-only Wr1 stream;
- the boot-only Main stream.

## Player path

TTRC v10 carries a source code in each cold update and each run descriptor.
The source changes where the Main CPU reads the 32-byte pattern; it does not
change the destination VRAM slot or the displayed name-table value.

- `Prg`: the Sub CPU consumes the next `PrgBuf` pattern and copies it into the
  frame's Word-RAM output area. Main reads that Word-RAM source.
- `Wr0` / `Wr1`: Main reads directly from the immutable preload region in the
  physical Word-RAM bank handed over for that frame. One source code is enough;
  frame parity selects the physical bank.
- `Main`: boot stages the bytes through frame-0 Word RAM and Main copies them
  once to `MainBuf`. Later pattern transfers read Main RAM directly.

Word-RAM sources use the measured VDP DMA first-word correction. `MainBuf` DMA
does not need that correction. One- and two-tile runs retain the direct-CPU
fast path; longer runs use bounded VBlank DMA. Source changes split runs even
when VRAM slots are consecutive.

## Physical PrgBuf scheduling remains independent

Only `Prg` loads consume the timed payload stream. After final decisions are
known:

1. `stream_schedule.py` schedules control and Prg payload in whole CD sectors.
2. It applies the boot prebuffer, routing table, CD cadence, usable `PrgBuf`
   capacity, and rate padding.
3. `pack_stream.py --verify` replays every delivery and consumption event.
4. The packed stream is accepted only when decoded cells match the sim and
   `PrgBuf` neither under-runs nor exceeds its scheduling ceiling.

The underlying PRG-RAM allocation is a 428 KiB circular buffer. Only 388 KiB
is scheduled; the remaining 40 KiB is delivery-jitter and frame-0 staging
headroom, not a fifth supply and not free feature memory.

## Analysis display

The old Tank and Buf gauges are replaced by four independent remaining-pattern
meters:

- `Prg` can rise when `BODY.DAT` prefetches future payload and fall when a
  frame consumes Prg patterns;
- `Wr0`, `Wr1`, and `Main` begin at their actual boot-loaded totals and only
  fall as their patterns are consumed;
- an unused preload capacity is not drawn as if bytes were loaded;
- the middle timeline row stacks the four remaining amounts with distinct
  colours against the sum of their fixed capacities.

The offline quality-budget trace remains available in the data file but has no
meter. This keeps the picture faithful to the four real supplies.

## Diagnostics

Schema 4 `buffer_remaining.npz` contains:

| Array | Unit | Meaning |
|---|---:|---|
| `prg_remaining` | patterns | End-of-frame physical `PrgBuf` occupancy. |
| `wr0_remaining` | patterns | Unconsumed boot patterns in `WordBuf0`. |
| `wr1_remaining` | patterns | Unconsumed boot patterns in `WordBuf1`. |
| `main_remaining` | patterns | Unconsumed boot patterns in `MainBuf`. |
| `prg_capacity`, `wr0_capacity`, `wr1_capacity`, `main_capacity` | patterns | Fixed capacities used to scale the meters. |
| `prg_loads`, `wr0_loads`, `wr1_loads`, `main_loads` | patterns/frame | Realized source consumption. |
| `wr0_preloaded`, `wr1_preloaded`, `main_preloaded` | patterns | Actual boot-loaded totals. |
| `quality_budget_remaining` | 32-byte pattern slots | Offline quality-budget level after each frame; diagnostic only. |
| `exact_demand_bytes`, `protected_demand_bytes` | bytes | Predicted demand before boot-preload credits. |
| `preload_credit_bytes` | bytes | Predicted payload bytes removed by boot assignment. |
| `upgrade_demand_bytes`, `main_risk_demand_bytes` | bytes | Demand after applicable preload credits. |
| `upgrade_reserve_bytes`, `main_risk_reserve_bytes` | bytes | Backwards reserve curves. |
| `body_useful_payload_bytes`, `body_useful_control_bytes`, `body_pad_bytes`, `body_physical_bytes` | bytes/frame slot | Physical `BODY.DAT` delivery accounting. |

The decision log additionally stores schema-1 `pattern_supply` data with the
update-aligned source codes, planned frame credits, realized source loads, and
capacities. `pattern_transfers` schema 2 freezes total tiles, source-aware runs,
and per-source loads for pack-time equality checks.

## Validation gates

Every change to this path must pass all of these gates:

1. allocator and supply-planner unit tests;
2. sim-to-pack equality for every update source, source load, and run count;
3. independent replay of all frames and every VRAM cell;
4. `PrgBuf` delivery proof with no under-run or over-cap event;
5. player constant, memory-overlap, code-generation, and binary-size checks;
6. a full DEBUG ADPCM22 recording for the target profile, including HUD,
   audio, and visual verification.

The full H40/30 ADPCM22 Bad Apple profile covers 6,576 frames at 320x224,
1,120 active tiles, and cold cap 178. It consumed all 880 Wr0, 880 Wr1, and 208
Main boot patterns. Of the 764,830 total cold loads, frame 0 contributed one
HEADER pattern and timed Prg supplied 762,861; the analysis `prg_loads` trace
includes frame 0 and therefore sums to 762,862. Prg never fell below 19 ready
patterns and the physical schedule had no under-run.
Packing matched every frozen source and run; the independent replay matched
every frame and every reconstructed VRAM state.

The corresponding full DEBUG recording kept all 6,575 timed frame intervals at
exactly two 60 Hz scanouts. HUD `S`, `D`, `R`, and `C` stayed zero, Main VBlank
wait `M` stayed at most one, Main transfer time `U` stayed at most 549 stopwatch
ticks, and run count `N` stayed at most 69. The same Replay produced identical
14,801 decoded video frames, 10,893,312 stereo PCM sample frames, packet timing,
and stream metadata in realtime, offline, and repeated-offline captures. The
recorder only requires a structurally valid non-empty audio stream during
routine runs; it no longer applies waveform thresholds.

## Source locations

- `tools/upgrade_planner.py`: exact/protected prediction, backwards reserve,
  and spending limit.
- `tools/pattern_supply.py`: capacities, water-fill allocation, frozen source
  validation, and physical stream materialization.
- `tools/sim.py`: demand construction, final source assignment, diagnostics,
  and decision-log freezing.
- `tools/pack_stream.py`: v10 serialization and exact schedule verification.
- `boot/movieplay_sp.s`: boot loading, Prg consumption, and frame handoff.
- `boot/movieplay_ip.s`: source-aware run construction and VRAM transfer.
- `ANALYSIS.md`: the four meters and stacked timeline.
- `MOVIE.md`: the exact v10 on-disc representation.

Changes affect encoder and player output, so they require both build-version
counters in `tools/av_version.txt` to be reviewed, a clean sim, packed-stream
verification, and representative full-length playback validation.
