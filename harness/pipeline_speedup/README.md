# Pipeline speedup (issue #15) — where the Sub actually spends time

Goal: let a dense spec (Sonic H32, 256x208, 832 cells) play clean 30fps. Today it
plays structurally correct (D=0, rate-matched disc) but the effective rate settles
at ~16fps with CD sector slips.

## Diagnostic: it is NOT the cold cap / cold-pop

The issue was framed around raising the per-VBlank cold draw limit. A direct test
disproves that framing:

| build | per-frame cold cap | realized cold | effective fps |
|---|---|---|---|
| Sonic 30fps cap 175 | 175 | ~200 | ~16.0 |
| Sonic 30fps cap 88  | 88  | ~121 | ~18.5 |

Halving the cold barely moved the rate (16 → 18.5). The disc is rate-matched to CD 1x
in **both** (6777 sectors), so the pump/drain load is identical; only the cold-pop
(32-byte pattern copies) changed — and it is not the bottleneck.

## What the bottleneck actually is: per-cell decode work

Compare the sustained **cell-update rate** (cells × fps):

- ed 15fps, 1120 cells: 16,800 cell-updates/s → plays clean (S=0).
- Sonic 30fps, 832 cells: wants 24,960 cell-updates/s → the Sub can't, settles ~16fps
  (≈13,300/s) with slips.

So the Sub saturates on the **per-cell decode loop** in `expand_frame` (`ef_byte`/
`ef_bit`): the bitmap walk, per-updated-cell entry read, the two `O_UPDS` writes, the
cold/reuse test, and the run-table (slot-run) building — plus the fixed costs that do
not scale with cold: the CD drain (`drain1` + `stage_copy`, 75 sectors/s) and the PCM
audio write (`write_wave_chunk`, ~13.3 kB/s, a per-byte wave-RAM loop). None of these
shrink when you lower the cold cap.

## Implication for optimization

To reach 30fps at 832 cells the Sub needs ~1.5x on the per-cell decode path (and the
fixed pump/audio costs trimmed), i.e. broad work, not one lever:

1. `expand_frame` inner loop (`ef_bit`): remove sign-extension (`CLR.L`+`MOVE.B`),
   kill redundant address math, tighten the run-table build. Per updated cell.
2. `write_wave_chunk`: the wave-RAM address is recomputed (`lea`+`add`+`adda`) every
   byte; keep a running pointer (+2/byte, handle the bank window) instead. Fixed cost.
3. `stage_copy` / `drain1`: the 2 KB Word-RAM->PRG CPU copy per sector, 75x/s.
4. Cut per-frame fixed overhead (fetch_control copy, expand setup, handshake) — these
   double at 30fps vs 15fps.

Individually each is a few percent, so they must compound; expect this to be several
increments, measured against the Sonic-30fps slip/fps, not a single change. A lighter
grid (fewer cells) reaches clean 30fps now without any of this (cell-rate is the knob).

## Results (first optimization pass, Sonic H32 832-cell 30fps)

Measured effective fps (from the debug F counter, 60s window), D=0 throughout, full
movie plays correct:

| build | change | fps | slips (mid-movie) |
|---|---|---|---|
| baseline (rate-matched) | — | ~16.0 | ~168 |
| opt1 | pump_poll 8→16B (expand) + wwc 0x40→0x80; wave-write running pointer | ~21.2 | ~107 |
| opt2 | pump 16→32B, wwc 0x80→0x100, drop fetch_control's redundant movem | ~22.9 | ~81 |
| opt3 | pump 32→64B | ~24.0 | ~68 |

**+50% (16→24fps)** from freeing Sub time. The gain is **nonlinear**: the disc delivers
at 30fps but the Sub consumes slower, so the surplus overflows the CDC → sector slips →
re-seeks (expensive). Freeing Sub time cuts slips, and each avoided re-seek is worth far
more than its cycle cost — hence a few percent of Sub work buys a big fps jump.

Consequence: **slips cannot reach 0 until the Sub actually sustains 30fps** (matches the
disc). The pump/fixed-cost lever has largely plateaued (opt1 +5, opt2 +2, opt3 +1). The
remaining ~6fps to 30 requires speeding the **raw per-cell decode** (`ef_bit` + cold
handling), which is the harder, higher-risk work (must preserve D=0). At 24fps the value
already holds: if the cell count is later reduced, fewer cells need cutting.
