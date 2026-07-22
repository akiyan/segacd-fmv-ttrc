# Cold-cap model: why H40/30fps sticks at 175, measured

Goal: identify, with per-frame evidence, which resource actually breaks the
fixed two-VBlank cadence when the H40/30fps/1,120-tile cold cap rises above
175, then rank the fixes. Phase 0 mines the packs and DEBUG HUD OCR series
that already exist on this machine; later phases add a calibrated two-CPU
timeline model and targeted re-measurements.

## Tools

- `extract_frames.py` — parses a packed `HEADER.DAT` + `BODY.DAT` pair
  (TTRC v10-v12) and emits one CSV row per frame: cell updates, physical
  pattern loads by source (Prg/Wr/Dic), cold-run descriptor structure
  (count, short runs, max run length), Pass2 word total, palette-switch
  flag, control bytes, and the CD slot schedule (control/payload sectors,
  rated allowance, delivery lead). With `--hud-csv` it cross-checks the
  parsed per-frame run count against the DEBUG HUD `N` column of a
  recording; a full-length match also proves that recording played exactly
  that stream.

```sh
tools/python.sh harness/cold_cap_model/extract_frames.py \
    out/sonic-jam-op-h40 --csv frames_175.csv \
    --hud-csv videos/SonicJamOp_H40_jitter_final_hud.csv
```

## Specimen inventory (all full-length Sonic Jam OP, H40/30fps/1,120 tiles)

| Stream (realized max loads/frame) | Pack | Recording + HUD series | N cross-check |
|---|---|---|---|
| 175 (v12, shadow lists, current) | `out/sonic-jam-op-h40` | `SonicJamOp_H40_jitter_final(_repeat)` | 2,714/2,714 |
| 178 (v10) | `videos/SonicJamOp_H40_cold195_test/packed` (byte-identical copy in `..._cold200_test/packed`) | `SonicJamOp_H40_cold195_emu`, `SonicJamOp_H40_cold200_emu` | 2,714/2,714 each |
| 190 (v10) | `out/sonic-jam-op-h40-cold190-hudinline` | `SonicJamOp_H40_cold190_hudinline(_repeat)` | 2,714/2,714 |

Every `*_repeat` recording is hash-identical to its first run (see
`videos/*_repeat_compare.json`), so each disc's behaviour is fully
deterministic in the emulator; when two recordings of the *same stream
content* break at different frames, the difference is the player build on
the disc, not chance.

## Phase 0 findings

**1. The "cap 195" and "cap 200" retry recordings replayed a realized-178
stream.** The packs behind them are byte-identical and never exceed 178
loads/frame; the full-length HUD `N` match pins both recordings to that
stream. The historical conclusion "195/200 break the cadence" therefore
actually observed *a realized-178 stream breaking on newer player builds*.
The realized-190 stream is a separate real specimen and broke differently.

**2. Cadence-break census** (a break = a movie frame held for 3 captures in
the 59.94 Hz HUD series; S/D/R stayed 0 everywhere):

| Recording | Breaks (frame held) | Held frame's next-frame workload |
|---|---|---|
| jitter_final, realized 175 | none | max U 544 ticks, max W 99 lines, J ≤ 14 KiB |
| cold195_emu, realized 178 | 1380, 2619 | culprits 1381 (75 loads), 2620 (178-load plateau), both 2-sector slots |
| cold200_emu, same 178 stream, different build | 2621 | culprit 2622 (178-load plateau), 2-sector slot |
| cold190_hudinline, realized 190 | 1191 | culprit 1192 = palette-switch frame (pal seg 7, 60 loads, 16 runs), 2-sector slot |
| cold187_qual (older build) | 31 | culprit in the opening heavy section (U 576-605 ticks, N 107-143) |
| cold178/179/180/181_qual (older build) | none | — |

**3. Three break signatures, none of them raw DMA:**

- *Main-window overrun* (older builds, opening section): U reaches 576-605
  ticks (17.7-18.6 ms), i.e. the pattern-transfer interval itself spills
  past a field. Run-count N 107-143 in the same frames — run overhead, not
  word volume (2.8k words fits one 3,400-word VBlank budget).
- *Palette collision* (realized-190): the only break sits exactly on a
  CRAM-switch frame, which must fit CRAM + flip atomically in one VBlank.
  The same stream survived its 190-load plateaus.
- *Plateau phase loss* (realized-178 on current-era builds): breaks land on
  2-sector slots inside long stretches pinned at exactly 178 loads, with
  modest U (73-375 ticks) and W ~60 lines. Neither CPU is visibly saturated
  on the culprit frame; the two-VBlank margin has been eroded to ~zero and
  build-level differences (code placement, HUD variant) decide which frame
  slips. The same plateaus pass on the hudinline build even at 190 loads.

**4. The W (Main-waits-for-Sub) series alternates ~60 lines / ~1 line with
frame parity in the heavy sections**, matching the 1001/400 CD delivery
pattern (3,2,3,2 sectors) — the Sub finishes visibly later on one parity.
All plateau culprits are 2-sector (rated 2) frames.

**5. Corrected qualification landscape (current player era):** realized 175
is clean with real margin; realized 178 is marginal (0-2 slips depending on
build); realized 190 survived its plateaus on one build and lost only a
palette frame. The gap between 175 and ~190 is not a hard cycle wall but a
near-zero *phase margin* around the flip deadline, plus one real Main-window
ceiling (high-run-count frames) and one palette-frame hazard.

## Phase 1 findings (mechanism + cost coefficients)

**Flip pacing allows phase drift (the ratchet).** `do_flip`
(`boot/movieplay_ip.s`) stamps `pace_flip_tick` with the *actual* flip
stopwatch time; the next frame's arm point is that stamp + 800 ticks
(24.58 ms), and a flip is accepted anywhere inside the target VBlank
except its terminal 4 V-counter lines (`FC..FF` guard). A flip accepted
late inside its blank therefore moves the next deadline base late as
well. A frame that misses the guarded window pays a whole extra VBlank
(the observed cadence break) and thereby resets the phase early.
Palette-switch frames are strictly harder: `wait_fixed_palette_flip`
requires a *fresh* VBlank start after the arm point, then writes 64 CRAM
words before flipping — consistent with the realized-190 break landing
exactly on a palette frame.

**Main transfer cost is run-dominated.** Regressing the HUD `U` series
(pattern-transfer ticks) on the extracted workload gives, consistently
across three builds/streams (residual std ≈ 31 ticks):

```
U [ticks] ≈ 0.61..0.71 per load + 9.2..9.9 per run − 5..6 per short run − 65
```

i.e. ~300 µs per DMA-path run descriptor versus ~20 µs per 32-byte
pattern. First-principles instruction counting of `bf_run_lp` +
`dma_chunk_wr` (register programming ≈ 40 instructions, completion poll,
first-word repair) explains ~100-150 µs; the excess is consistent with
Word-RAM DMA source reads contending with the Sub CPU. Short runs take
the CPU-direct path and are *cheaper* than a DMA setup. Run-count
reduction (pack-side coalescing/ordering) and per-run overhead reduction
are therefore the highest-leverage Main-side levers — not word volume.

**W (Main-waits-for-Sub at the bank swap) is phase-driven, not
workload-driven.** Regression against workload explains little (residual
std 25-30 lines); W is systematically higher on 2-sector slots and
reaches 99 lines (6.3 ms) on the current build.

**Negative result — breaks are NOT predicted by observable load.** A
naive additive timeline model (fixed work + W + U against the ratchet
rules; `model.py --sweep`) reproduces either zero or a periodic flood of
breaks, never the observed 1-2; measured U cannot be reused additively
because it embeds each run's real VBlank alignment. Rolling sums of
(W·line + U·tick) rank the actual break frames 100th-1300th — the
heaviest stretches (frames ~1976, ~2660) do NOT break. The variable that
decides *which* marginal frame slips is invisible to the current HUD:
the flip phase inside the blank, the Sub's READY micro-timing, or CDC
service alignment.

## Phase 2 first measurements (cap-175 control, HUD p75 with V/O/E)

Recording `SonicJamOp_H40_flipdiag175` (full length, S=D=R=0, J=00)
validated the new fields and immediately restructured the model:

- **Flips land at the VBlank start.** V=`E0` on 2,693/2,714 frames: the
  flip-phase-ratchet hypothesis is dead for normal frames.  The only
  mid-blank flips (V=`E1..E7`) are the palette frames (CRAM before flip)
  and the heaviest section (frames ~1960-1995), which the slack model
  below explains exactly.
- **O behaves**: median = 62 ticks = the nominal 1086-tick N2 interval
  minus the 1024 base; a slipped frame saturates at 255.
- **The pre-transfer phase is far larger than the planning envelope.**
  E (Pass2 entry since previous flip) has median 104 (12.8 ms) and p99
  140 (17.2 ms) — parse + bitmap walk + name-table blit plus the swap
  wait cost 12-14 ms even on ordinary frames, versus the ~8.5 ms
  STREAMING.md planning envelope.  The VDP FIFO throttling of the
  1,120-word name-table blit during active display is the likely gap.
- **Two measured cliffs.** (1) Entry cliff: E*4 must stay below the
  field-1 blank end (~622 ticks, E≈155); observed max 147. (2) Transfer
  cliff: reconstructing transfer end as `max(E*4, 543) + U` and slack
  against the flip blank at 1086 ticks puts the heavy 1960-1995 frames at
  -17..+60 ticks of slack — precisely the frames whose flips appear
  mid-blank (V=`E1..E5`), surviving on the do_flip mid-blank acceptance.
- **An unexplained ~8.5 ms delay causes the actual break.** The one
  cadence slip (frame 471 held 3 fields; the late flip is frame 472's,
  O=255 in row 473) has slack +277 ticks and modest E/U/W — the measured
  Main path cannot account for the lost field.  Every observable says the
  flip should have been on time.  The deciding term is still outside
  V/O/E/U/W: candidates are a Sub-side stall inside the swap handshake
  after `W` was sampled, VDP FIFO backpressure on the HUD publish, or an
  emulator-level artifact.  Break location also moved p74→p75 (1107 →
  471), confirming build-level phase sensitivity of marginal frames.

Next: the realized-180 diagnostic stream (CBRSIM_COLD_CAP_DIAG=180,
profile `configs/sonic-jam-op-h40-cold180.toml`) should produce several
breaks in one recording, enough to pattern-match the unexplained term.

## Phase 2 measurement design (as built)

The missing per-frame observables, each cheap (a stopwatch/HV read plus
a stored word):

1. **Flip phase** — V-counter (or stopwatch minus blank anchor) at the
   accepted flip: shows the ratchet drift directly and how close each
   flip runs to the FC..FF guard.
2. **Arm overshoot** — how far past `pace_flip_tick + 800` the flip
   actually happened.
3. **Sub READY margin** — time between the Sub posting STAT_READY and
   Main's CMD_SWAP arrival (or the wait on the other side, complementing
   `W`).

Then record the realized-178 stream (breaks expected on current builds)
plus a realized-175 control with the instrumented DEBUG build and read
the series around the breaks. The static model resumes calibration with
those phases pinned.
