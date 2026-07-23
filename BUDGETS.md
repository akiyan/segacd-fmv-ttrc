# SEGA-CD FMV Budgets

This note collects the first-order tile, DMA, and CD raw-read budgets used when
choosing encoder targets. Numbers are estimates for NTSC 60 Hz playback.

## Assumptions

- Tile size: 8x8 pixels.
- Pattern payload: 32 bytes per 4bpp tile.
- Raw tile update from CD: 34 bytes per tile, counted as 32 bytes pattern plus
  2 bytes name-table entry.
- CD rate: 150 KiB/s = 153,600 bytes/s.
- ADPCM control audio uses about 11,150 bytes/s at the supported cadences.
  Exact per-frame sizes are documented in `CONFIG.md` and `ADPCM.md`.
- Raw video CD budget after that audio allowance: about 142,450 bytes/s.
- The theory table uses `tools/layout_preview.py` timing constants converted to
  pattern tiles.
- Tile counts below use pattern bytes only. Name-table DMA still needs to be
  budgeted separately in a real frame.
- H40's exact full-width 16:9 height is 180 pixels, which is 22.5 tile rows.
  The table uses the tile-aligned fit that stays under that height: 320x176.

## Screen Modes

| Mode | Visible resolution | Tile grid | Total tiles | Tile-aligned 16:9 area | 16:9 tiles |
|---|---:|---:|---:|---:|---:|
| H40 | 320x224 | 40x28 | 1,120 | 320x176 (40x22) | 880 |
| H32 | 256x224 | 32x28 | 896 | 256x144 (32x18) | 576 |
| mode4 | 256x192 | 32x24 | 768 | 256x144 (32x18) | 576 |

## Theory DMA Per VBlank

| Mode | Active lines | Blanking lines | Pattern tiles/VBlank |
|---|---:|---:|---:|
| H40 | 224 | 38 | 243 |
| H32 | 224 | 38 | 198 |
| mode4 | 192 | 70 | 365 |

The mode4 row is only a theory estimate for a 192-line SMS-style display. True
SMS Mode 4 changes the meaning of VDP registers; in particular, the bit used as
DMA enable in Mode 5 is a height-mode bit in SMS Mode 4. The practical measured
path below uses SMS Mode 4 for display, then switches to Mode 5 only during
VBlank to issue the DMA.

## DMA Update Budget Per Video Frame

This is the average DMA capacity available per encoded video frame, expressed
as pattern tiles and using the GPGX `dmabench` measured values below. At 24 fps,
the average is 2.5 VBlanks per video frame, so the real scheduler would
alternate shorter and longer gaps.

| Mode | 15 fps tiles/frame | 24 fps tiles/frame | 30 fps tiles/frame |
|---|---:|---:|---:|
| H40 | 916 | 572 | 458 |
| H32 | 745 | 465 | 372 |
| mode4 | 1,405 | 878 | 702 |

## CD Raw Read Budget Per Video Frame

The raw-read budget is independent of screen mode. This table is the CD budget
left after the ADPCM allowance above, expressed as raw tile updates; it is not a
replacement for the exact per-profile scheduler.

| Frame rate | Raw tiles/frame |
|---|---:|
| 15 fps | 279 |
| 24 fps | 174 |
| 30 fps | 139 |

## Empirical measurement — `dmabench`

Reusable measurement build. `make dmabench DMABENCH_MODE=0|1|2` (0=H32, 1=H40,
2=mode4) builds `out/DMABENCH.iso`. It binary-searches the largest
`Main-RAM → VRAM` DMA that finishes inside one VBlank and prints, top-left:

- `W xxxx` = max words per VBlank (hex)
- `F xxxx` = derived tiles/frame ≈ `(W/16) * 3`

Source: `boot/dmabench_ip.s` (+ `dmabench_boot.s`, stub SP = `cdcbench_sp`).
**Run it on ares / real hardware for authoritative numbers** — Genesis Plus GX
is lenient and over-reports.

### Measured (Genesis Plus GX)

| Mode  | Pattern tiles/VBlank | note |
|-------|---------------------:|------|
| H32   | 186                  | `W 0BA6`; `out/DMABENCH_mode0.cue`, screenshot `tmp/dmabench_h32_clean_sheet.jpg` |
| H40   | 229                  | `W 0E50`; `out/DMABENCH_mode1.cue`, screenshot `tmp/dmabench_h40_clean_sheet.jpg` |
| mode4 | 351                  | `W 15F4`; `out/DMABENCH_mode2.cue`, screenshot `tmp/dmabench_mode4_clean_sheet.jpg`; SMS Mode 4 display, VBlank-only Mode 5 DMA, with a white proof block showing the DMA destination tile was written |
| *ares* | TBD                 | run the ISO to fill in |

The earlier GPGX result `0x0F98` for every mode was invalid. The old harness
used `reg1 = 0x8144` for mode4: that left Mode 5 selected, did not enable Mode 5
DMA, and was followed by a BIOS display-enable call that could restore register
1 anyway. It was measuring a Mode 5-like setup, not true 192-line SMS Mode 4.

A direct "stay in SMS Mode 4 and issue Main-RAM to VRAM DMA" test did not give a
credible budget in GPGX: the reported value was far above the 192-line theory
and had to be treated as a no-op/status artifact. The usable path is to keep
SMS Mode 4 for active display, switch to Mode 5 at VBlank start, issue the DMA,
then switch back to SMS Mode 4 before active display resumes.

### The real limit is the pipeline, not raw DMA

The pure-DMA ceiling is **not** the binding constraint. Actual playback shows
the audio "巻き戻し" (RF5C164 underrun → resync) at the 562-tile/frame section
(frame 108–125, overlay `F0076`), even though 562 tiles = 8992 words = only
~2.25 VBlanks of DMA — well under the 4-VBlank/frame budget. So the bottleneck
is the whole per-frame pipeline:

- Sub-CPU `expand_frame`: 562 cold pops × 16 words (PRG→Word-RAM) + interleaved
  `pump_poll` CD drain.
- Main-CPU: Word-RAM→Main-RAM stage copy, shadow blit, VBlank-split tile DMA
  (a full-frame wait each time the per-VBlank word budget `md_vbudget` is
  exhausted), flip.
- The two CPUs serialize at the swap handshake.

Observed (GPGX, older player — `pump_poll` ran every bitmap byte):

| cold tiles/frame | playback       |
|------------------|----------------|
| ≤ ~350           | OK             |
| 562 (f108–125)   | audio 巻き戻し |

The pump-optimized player (p5, `pump_poll` every 8 bytes) moved this ceiling
up: on the full-screen H40 (1120-cell) stream, realized cold up to ~429/frame
plays with zero CD slips, and the practical limit became audio-lead dips, not
the Sub-CPU pipeline.

### Encoder cap (current)

The shipped cold-tile ceiling is selected from the full-length qualification
table in `tools/av_config.py`. Selection uses display mode, nominal fps, and
an exactly matching measured active-tile count. An unmeasured tuple is rejected
instead of receiving a scaled/default or larger-area value. The
pack asserts each streaming frame's realized new-tile loads stay within the
same selected cap and never re-caps the stream. Capped cells show as Miss in
the analysis overlay's category map.

The current full-raster H40/30fps tuple is 1,120 active tiles at cold cap 185.
Its e83 physical-slot allocation keeps every frame at 85% or more of that cap
to 30 or fewer source-aware runs. The full Sonic qualification reconstructs
all frames exactly with no Prg underrun; two exact same-Replay recordings keep
all 2,713 timed intervals at two VBlanks with `S/D/R/C=0`, `M=1`, and
`J=12 KiB`. Total runs across the whole movie are informational, not a limit:
extra fragmentation on light frames is acceptable when heavy-frame deadline
cost falls.

`boot/movieplay_ip.s` sets a per-mode VBlank word budget (`md_vbudget`):
`VB_WORDS_H32` = 2800 and `VB_WORDS_H40` = 3400. Both are below the GPGX
ceilings (H32 2982 words/VBlank, H40 3664 words/VBlank). Re-check against the
ares `dmabench` value before raising them.

For future mode4 player work, use the measured path above: true SMS Mode 4 for
display, with VBlank-only Mode 5 DMA. Re-prove on ares or hardware before
raising player limits.
