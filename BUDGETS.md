# SEGA-CD FMV Budgets

This note collects the first-order tile, DMA, and CD raw-read budgets used when
choosing encoder targets. Numbers are estimates for NTSC 60 Hz playback.

## Assumptions

- Tile size: 8x8 pixels.
- Pattern payload: 32 bytes per 4bpp tile.
- Raw tile update from CD: 34 bytes per tile, counted as 32 bytes pattern plus
  2 bytes name-table entry.
- CD rate: 150 KiB/s = 153,600 bytes/s.
- Audio: 22.05 kHz mono ADPCM at 4 bits/sample = 11,025 bytes/s.
- Raw video CD budget after audio: 142,575 bytes/s.
- DMA bytes per VBlank in the first table are theory estimates from
  `tools/layout_preview.py`: `bpl * (262 - active_lines)`. The measured table
  below is the one to use when choosing safe player limits.
- DMA tile counts below use pattern bytes only: `floor(bytes / 32)`. Name-table
  DMA still needs to be budgeted separately in a real frame.
- H40's exact full-width 16:9 height is 180 pixels, which is 22.5 tile rows.
  The table uses the tile-aligned fit that stays under that height: 320x176.

## Screen Modes

| Mode | Visible resolution | Tile grid | Total tiles | Tile-aligned 16:9 area | 16:9 tiles |
|---|---:|---:|---:|---:|---:|
| H40 | 320x224 | 40x28 | 1,120 | 320x176 (40x22) | 880 |
| H32 | 256x224 | 32x28 | 896 | 256x144 (32x18) | 576 |
| mode4 | 256x192 | 32x24 | 768 | 256x144 (32x18) | 576 |

## DMA Per VBlank

| Mode | Active lines | Blanking lines | Bytes/line | DMA bytes/VBlank | Pattern tiles/VBlank |
|---|---:|---:|---:|---:|---:|
| H40 | 224 | 38 | 205 | 7,790 | 243 |
| H32 | 224 | 38 | 167 | 6,346 | 198 |
| mode4 | 192 | 70 | 167 | 11,690 | 365 |

The mode4 row is only a theory estimate for a 192-line SMS-style display. It is
not a confirmed Main-RAM to VRAM DMA budget. True SMS Mode 4 changes the meaning
of VDP registers; in particular, the bit used as DMA enable in Mode 5 is a
height-mode bit in SMS Mode 4.

## DMA Update Budget Per Video Frame

This is the average DMA capacity available per encoded video frame. At 24 fps,
the average is 2.5 VBlanks per video frame, so the real scheduler would
alternate shorter and longer gaps.

| Mode | 15 fps bytes/frame | 15 fps pattern tiles | 24 fps bytes/frame | 24 fps pattern tiles | 30 fps bytes/frame | 30 fps pattern tiles |
|---|---:|---:|---:|---:|---:|---:|
| H40 | 31,160 | 973 | 19,475 | 608 | 15,580 | 486 |
| H32 | 25,384 | 793 | 15,865 | 495 | 12,692 | 396 |
| mode4 | 46,760 | 1,461 | 29,225 | 913 | 23,380 | 730 |

## CD Raw Read Budget Per Video Frame

The raw-read budget is independent of screen mode. It is the CD budget left
after 22.05 kHz mono ADPCM audio, divided by 34 bytes per raw tile update.

| Frame rate | CD bytes/frame after audio | Raw tiles/frame |
|---|---:|---:|
| 15 fps | 9,505.0 | 279 |
| 24 fps | 5,940.6 | 174 |
| 30 fps | 4,752.5 | 139 |

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

| Mode  | W (words/VBlank) | tiles/VBlank (W/16) | note |
|-------|------------------|---------------------|------|
| H32   | 0x0BA6 = 2982    | 186                 | `out/DMABENCH_mode0.cue`, screenshot `tmp/dmabench_h32_sheet.jpg` |
| H40   | 0x0E50 = 3664    | 229                 | `out/DMABENCH_mode1.cue`, screenshot `tmp/dmabench_h40_seq_sheet.jpg` |
| mode4 | invalid          | invalid             | `out/DMABENCH_mode2.cue` enters true SMS Mode 4, but the Main-RAM to VRAM DMA/result-display path does not produce a readable result in GPGX. Do not reuse the H32/H40 budget for mode4. |
| *ares* | TBD             | TBD                 | run the ISO to fill in |

The earlier GPGX result `0x0F98` for every mode was invalid. The old harness
used `reg1 = 0x8144` for mode4: that left Mode 5 selected, did not enable Mode 5
DMA, and was followed by a BIOS display-enable call that could restore register
1 anyway. It was measuring a Mode 5-like setup, not true 192-line SMS Mode 4.

### The real limit is the pipeline, not raw DMA

The pure-DMA ceiling is **not** the binding constraint. Actual playback shows
the audio "巻き戻し" (RF5C164 underrun → resync) at the 562-tile/frame section
(frame 108–125, overlay `F0076`), even though 562 tiles = 8992 words = only
~2.25 VBlanks of DMA — well under the 4-VBlank/frame budget. So the bottleneck
is the whole per-frame pipeline:

- Sub-CPU `expand_frame`: 562 cold pops × 16 words (PRG→Word-RAM) + interleaved
  `pump_poll` CD drain.
- Main-CPU: Word-RAM→Main-RAM stage copy, shadow blit, VBlank-split tile DMA
  (a full-frame wait each time `VBLANK_DMA_WORDS` is exhausted), flip.
- The two CPUs serialize at the swap handshake.

Observed (GPGX, current build):

| cold tiles/frame | playback       |
|------------------|----------------|
| ≤ ~350           | OK             |
| 562 (f108–125)   | audio 巻き戻し |

### Encoder cap (recommended)

Cap cold tiles/frame in the sim; excess cells stay stale (Miss, carried to a
later frame). Conservative start pending ares testing: **~400 cold tiles/frame**
(below the observed glitch threshold, above the common ~260 load). The debug
overlay `M` (Miss) shows the deferred count in capped sections.

`boot/movieplay_ip.s` `VBLANK_DMA_WORDS` = 2800. This is below the H32 GPGX
ceiling of 2982 words/VBlank and below the H40 GPGX ceiling of 3664
words/VBlank. Re-check against the ares `dmabench` value before raising it.

For future mode4 player work, first decide whether the player is using true SMS
Mode 4 or a Mode 5 layout shaped like 256x192. If it is true SMS Mode 4, prove
the VRAM update path separately; do not assume the Mode 5 DMA budget applies.
