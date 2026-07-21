# Analysis Overlay Reference

This document defines, exactly and completely, every element drawn in the
1920x1080 analysis frame produced by `tools/render_analysis.py` for the
**Tile Texture Reuse Codec**. The layout "source of truth" is
`tools/layout_preview.py` (dummy data); `render_analysis` runs the same drawing
functions on real encoder output.

Keep this file in sync whenever the layout changes (the `/analysis` skill
automates: update layout -> update this file -> notify).

## Layout map

```
+----------------------------------------------+   +-----------------------------+
| SEGA-CD sim output  <meta>      <PL/Time/Fr> |   | Source  <res/fps/audio>     |
| +------------------------------------------+ |   | +-------------------------+ |
| |                                          | |   | | source frame (4:3)      | |
| |   SEGA-CD OUTPUT (centered on the real   | |   | +-------------------------+ |
| |   screen; letterboxed to the panel)      | |   | LEGEND (2 rows, 7 classes)  |
| |                                          | |   | +-------------------------+ |
| |                                          | |   | | CATEGORY MAP (4:3)      | |
| |                                          | |   | | (tile content + border; | |
| |                                          | |   | |  Miss = red-filled hole)| |
| +------------------------------------------+ |   | +-------------------------+ |
+----------------------------------------------+   | CATEGORY TOTALS (whole clip)|
+----------------------------------------------+   | +-------------------------+ |
| STATUS BAR                                   |   | | AUDIO WAVEFORM          | |
|  [Req] [Cold] [Band] [Prg][Wr0][Wr1][Main] ...|  | | (+/-2s, now = centre)   | |
|  Prev/Current/Next palette strip             |   | +-------------------------+ |
|  3 timelines (Req / four supplies / BODY Band)|  +-----------------------------+
+----------------------------------------------+
```

Regions (pixel rectangles in `layout_preview.py`): `MAIN_FRAME` left,
`SRC_FRAME` / `CATLEG_XY` / `CAT_FRAME` / `CATTOT_XY` / `WAVE_FRAME` right
column (top to bottom), `STATUS_XY` bottom-left. The former per-metric flow
graph (`GRAPH_FRAME`) and its bottom-right totals slot (`PAL_XY`) are gone.

## Headings

- **SEGA-CD sim output** (top-left): big label + a small meta line:
  `mode / WxH (cols x rows) / audio / fps / avg N KiB/sec`.
  - `mode` = screen mode (H32 / H40 / mode4). `WxH` = encoded tile grid in
    pixels; `cols x rows` = tile grid (each tile 8x8).
  - `avg N KiB/sec` = average of useful BODY delivery Band over the whole clip
    (see Band below).
- **PL/Time/Frame** (top-right, small): `PL:cur/total Time:MM:SS.ss Frame:XXXX`.
  `PL` = current palette-segment index / highest index (zero-padded to the
  total's digit count, min 2). `Time`, `Frame` = playback position; `Frame` is
  **4-digit hex** (`%04X`), matching the on-hardware debug HUD's F number.
- **Source** (top of right column): big label + a small spec line with the
  *source video* `resolution / fps / audio codec+rate+channels`
  (from `ffprobe`; bitrate intentionally omitted).

## Main panel (left)

The reconstructed SEGA-CD output. It is centered on the *real hardware screen*
(e.g. 256x224 for H32) and letterboxed into the panel at the mode's display
aspect (4:3 for H32/H40, ~14:9 for mode4) - it is **not** stretched to fill.
Low-resolution grids therefore appear at their true on-screen size.

## Right column

- **Source** (top): the source frame after crop, scaled into the panel (4:3
  panel, same footprint as the category map).
- **Legend** (between Source and the category map): two rows of four, seven
  classes. Each shows the swatch and the name; the resident-reuse classes
  (`Same/Near/Coa/Flbk`) show `unique/used` counts for the frame (how many
  distinct resident tiles served how many cells), the others a single count.
  Numeric fields are text directly on the legend background; there is no level
  fill behind the digits. All zero-padded digits use the normal text colour.
  Swatch styles mirror the map: `Raw` = black/white checker, `Same` = grey
  checker (both meaning "content fill, no border"), `Miss` = red fill,
  `Near`/`Coa` = thin frame, `Flbk`/`Buf` = thick frame.
- **Category map** (middle): the tile grid. Each 8x8 tile shows its
  **reconstructed content**; the category (see Tile Categories) is indicated by
  the border: `Raw`/`Same` = no border, `Near`/`Coa` = thin 1px border,
  `Flbk`/`Buf` = thick 3px border, in the category colours. A `Miss` tile is
  drawn as a **red-filled hole** (its content is not updated this frame).
- **Category totals** (directly below the category map, `CATTOT_XY`): a thin
  stacked horizontal bar of the whole-clip totals per category, with a compact
  swatch+count legend above it (totals only, no unique counts). Static for the
  whole clip.
- **Audio waveform** (bottom, `WAVE_FRAME`): scrolling envelope of the sim's
  playback-model audio (the WAV muxed into the video). For ADPCM22 this is not
  the clean extracted source: it is the exact continuous checkpointed IMA
  encode/decode result after conversion to the RF5C164's 8-bit sign-magnitude
  samples. The original signed-16 WAV remains separate as the packer input.
  Window is +/-2 seconds with
  **now = centre** (white line), scrolling left; the past (left half) is drawn
  bright green, the future (right half) dim green, around a zero-amplitude
  centre line. Heading (outside the frame): `Audio` + the audio spec.
- The former **per-metric flow graph** panel was removed.

## Tile Categories (READ THIS CAREFULLY)

Every 8x8 tile, every frame, is placed in exactly one class. Classes describe
**how the tile was filled**, from best (accurate, cheap-or-fresh) to worst
(not updated). The encoder searches VRAM-resident patterns and picks the best
match under the current byte budget.

### The F3 similarity metric

When comparing a target tile to a candidate resident pattern (both RGB333,
channels 0..7), three per-pixel differences are computed over the 64 pixels:

- **Ym** = mean of `|luma(target) - luma(candidate)|` (average luminance error).
- **Yp** = max  of that same per-pixel luminance error (worst-pixel error;
  this is what catches thin edges / shape changes).
- **C**  = mean of the chroma distance
  `sqrt((Cb_t - Cb_c)^2 + (Cr_t - Cr_c)^2)` (average colour error).

Luma = `0.299R + 0.587G + 0.114B`; `Cb = -0.169R - 0.331G + 0.5B`;
`Cr = 0.5R - 0.419G - 0.081B`. A candidate is accepted into a tier only if it
passes **all three** thresholds (`Ym<=`, `Yp<=`, `C<=`) for that tier. Tiers are
tested tightest-first, so a tile is labelled by the *tightest* tier it fits.

### Tier thresholds (defaults, env-overridable)

| Tier | Ym | Yp | C  | env |
|------|----|----|----|-----|
| Near | 10 | 28 | 24 | `CBRSIM_NEAR_YM/YP/C` |
| Coa  | 20 | 50 | 40 | `CBRSIM_TCOA_YM/YP/C` |
| Flbk |120 |252 |200 | `CBRSIM_TFLBK_YM/YP/C` |

Smaller thresholds = stricter = better visual match. `Near` is a near-perfect
reuse; `Flbk` is deliberately **wide** ("rough but far better than a hole"): it
is the fallback for what would otherwise be a Miss, so it should almost always
find *some* resident rather than leave a hole.

### The seven classes

| Class | Colour | Bytes | Meaning |
|-------|--------|-------|---------|
| **Raw**  | light grey | 34 from Prg; 2 when boot-preloaded | An accurate cold load funded by this frame's fresh quality allowance. Its physical source is tracked separately as Prg, Wr0/Wr1, or Main. |
| **Same** | checker grey | 2 (name only) | The target tile's exact pattern is **already resident** in VRAM; the cell just points to it (lossless dedup). No pattern transfer. |
| **Near** | blue | 2 (name) | No exact match, but a resident pattern passes the **Near** thresholds; the cell points to it. Near-perfect reuse. Also covers "keep the current display" when the currently shown tile is already accurate and still within Near of the new target. |
| **Coa**  | green | 2 (name) | Best resident passes **Coa** (a bit rougher than Near). Used for flat/low-detail tiles where a close-enough resident exists. |
| **Flbk** | orange (thick border) | 2 (name) | **Fallback** (merged Mid+Far). Only used when no Raw/Buf load is possible (quality budget exhausted or the per-frame cold cap reached). Default is **improve mode**: the best resident is taken if it gets closer to the target than the current display (`CBRSIM_FLBK_IMPROVE_ONLY=0` reverts to the absolute wide **Flbk** threshold). Visibly approximate, but "better than a Miss". This is the last resort before Miss. |
| **Buf**  | violet (thick border) | 34 from Prg; 2 when boot-preloaded | An accurate cold load funded by saved whole-movie quality budget or by a boot-preload credit. `Buf` is a funding category, not a physical buffer; Prg/Wr0/Wr1/Main records the actual byte source. |
| **Miss** | red (filled) | 0 | The tile was **not updated**; it still shows whatever was there before. A red-filled hole in the category map. |

### Selection order (per changed tile, `commit_unified`)

1. If the **currently displayed** tile is already accurate (its class last frame
   was exact) and is within `Near` of the new target -> keep it, 0 bytes -> `Near`.
2. Else if the exact target pattern is resident -> `Same` (2 B).
3. Else find the best resident. If it passes `Near`(tier 0) or `Coa`(tier 1) and
   the budget allows the 2 B name -> `Near` / `Coa`.
4. Else load the exact pattern (34 B), unless the per-frame **cold cap**
   (`cold_cap_for_fps`, `av_config.py`) is already reached: charge the current
   current-frame allowance -> `Raw`, or saved whole-movie allowance / a
   boot-preload credit -> `Buf`.
5. Else (quality budget/cold-cap exhausted) if the best resident improves on the
   current display (default improve mode; see Flbk above) -> `Flbk`
   (2 B fallback).
6. Else -> `Miss`.

Notes: `Same/Near/Coa/Flbk` cost only a 2-byte name-table entry (they reuse
a resident 32-byte pattern). A Prg-sourced `Raw/Buf` load costs 34 bytes in the
encoder model. A Wr0/Wr1/Main boot-preloaded load already owns its 32 pattern
bytes and therefore costs only the 2-byte name entry during playback. `Raw`
and `Buf` describe funding; the independent source assignment describes where
the pattern bytes reside. A persistent
approximation (a tile stuck in Near/Coa/Flbk for >= 0.3s) is escalated to
Miss-priority so it gets an accurate reload when budget allows.

## Status bar (bottom-left)

Left to right: one wide **Req** meter, then **Cold**, **Band**, **Prg**,
**Wr0**, **Wr1**, **Main**, **DMA**, and **Run** meters (each bar is as wide as
its own label). Below the meters is the palette strip; to the right are three
stacked timelines. The old Tank and Buf meters are removed.

### Req meter
All categories stacked into one bar (full width = total tile count `C`), with a
yellow vertical **budget line** marking the per-frame update budget. Labels:
`Req:NNN` (changed tiles requested this frame), `Raw:NNN` (current-budget loads),
`Comp:NNN` (tiles satisfied by resident reuse = `Same+Near+Coa+Flbk`).

### Cold meter
`Cold:NNN` = this frame's **new tile loads** (`Raw + Buf + future raw
prefetch`, i.e. every 32-byte pattern newly written to VRAM from any physical
supply). The bar stacks Raw-, Buf-, and blue prefetch-coloured segments;
full-scale = `cold_cap_for_fps`
(`av_config.py`, selected only when mode/fps/active tiles exactly match a
measured tuple; an unmeasured tuple is rejected before encoding).
This visualises the value the hardware slip investigations were fought over.

### Band meter (useful BODY delivery) - KiB/sec
`Band` is the non-pad data physically read from `BODY.DAT` in this delivery
slot, divided by that slot's actual CD read time:

`Band = useful BODY bytes / physical BODY bytes * 150 KiB/sec`.

The physical bytes are the slot's whole sectors, including pad. At CD 1x each
sector takes 1/75 second, so a completely useful slot reads `Band:150`, a
half-pad slot reads `Band:075`, and a valid slot never exceeds 150 KiB/sec.

The bar is split into **Raw colour** for the continuous 32-byte cold-pattern
payload stream and **dim blue-grey** for the continuous control stream
(control header, name entries, audio, palette reference, DEBUG data, and run
descriptors). Future-frame payload is counted in the slot where it is actually
prefetched, not where the target frame later consumes it.

For TTRC v11, the control contribution includes whichever shadow-update
representation was selected for that frame: the legacy bitmap plus name
entries, or the completed offset/entry list. The analysis does not add a
separate meter for this internal representation; its exact byte cost is already
included in the dim control portion of `Band`.

The metric excludes rate-match pad sectors, the zero-filled tail of the final
control/payload sectors, all of `HEADER.DAT`, frame 0 patterns/control, startup
audio, palettes, routing, and the compatibility `MOVIE.DAT` container. Slot 0
therefore reads `Band:000`. `avg N KiB/sec` in the top meta and
`body_useful_bps` in `report.txt` divide all useful BODY bytes by the complete
physical BODY read time. This is a physical-time-weighted average, not a simple
mean of the displayed slots.
`codec_work_bps` remains a separate quality-allocation diagnostic.

The bar uses the slot's physical bytes as full-scale. Payload and control fill
their useful fractions; all pad remains blank. A thin yellow line at the right
edge marks CD 1x.

Before making these per-frame choices, the encoder dry-runs the complete
quantized movie through the shared VRAM allocator. A backwards pass builds two
offline reserve curves: complete exact-update demand limits optional Raw/Buf
upgrades, while changes beyond the Coa bound form the narrower reserve that
protects normal updates from future Flbk/Miss bursts. Both curves finish at
zero. They are saved as `upgrade_reserve_bytes` and
`main_risk_reserve_bytes` in `buffer_remaining.npz`; neither is a physical
supply meter. [`BUEFFERING.md`](BUEFFERING.md) describes how both curves
are constructed and applied.

### Four pattern-supply meters

Each meter is an independent remaining count in 32-byte patterns:

| Meter | Physical object | Behaviour |
|---|---|---|
| `Prg:NNNNN` | usable PRG-RAM `PrgBuf` | End-of-frame occupancy from the exact sector scheduler. It can rise through BODY prefetch and fall through Prg consumption. |
| `Wr0:NNN` | `WordBuf0` in physical Word-RAM bank 0 | Actual boot-loaded total minus patterns consumed by eligible even frames. It only falls. |
| `Wr1:NNN` | `WordBuf1` in physical Word-RAM bank 1 | Actual boot-loaded total minus patterns consumed by eligible odd frames. It only falls. |
| `Main:NNN` | Main-RAM `MainBuf` | Actual boot-loaded total minus patterns consumed by either parity. It only falls. |

The Prg trace includes the `HEADER.DAT` prebuffer, whole-sector payload tails,
per-slot prefetch, and realized Prg consumption. The packer recomputes it from
the built controls and rejects any mismatch. The three preload traces use the
actual loaded totals, so unused fixed capacity is not presented as available
content.

These meters deliberately do not show the offline whole-movie quality budget.
That diagnostic can remain high when physical supplies are low, and it cannot
provide a pattern byte to the player.

### DMA meter
`DMA:NNN` or `DMA:NNNN` = the number of **32-byte pattern tiles** transferred
to VRAM this frame. The numeric field uses only the digits required by the
current raster (three for full H32's 896 tiles, four for full H40's 1120), so
the former five-digit byte meter is narrower. Its full-scale starts from the
mode/fps theoretical VDP byte ceiling, subtracts the fixed full name-table DMA
(`2 * cells` bytes), divides the remainder by 32 bytes/tile, and clamps it to
the raster's tile count. Green fill; if the transfer exceeds that ceiling it
turns orange with a red overflow tail.

### Pattern transfer run meter
`Run:NNNN` = the number of ascending consecutive cold VRAM-slot runs used for
the pattern tiles, exactly matching the packer's cold-run descriptors, the
Main CPU run-table record count, and H40 DEBUG HUD `N` (before its low-byte
display truncation). Reuse entries do not break a run; a slot discontinuity
does, including a wrap from the end of the slot pool to slot zero.

This is deliberately **not the number of VDP DMA commands**. With the p45
player, a one- or two-tile run is copied directly by the CPU, while a longer
run uses DMA and may be split into more than one DMA command at a VBlank budget
boundary. Both still remain one `Run` record. The bar therefore measures the
fragmentation seen by the player independent of the current transfer fast
path.

The bar's per-frame full-scale is the current `DMA` tile count: the theoretical
worst case is every transferred tile isolated into its own one-tile run. A tiny
bar therefore means long, efficient runs; a full amber bar means the maximally
fragmented case. The numeric field is fixed at four digits, derived from full
H40's theoretical worst case of 1120 pattern tiles and therefore 1120 runs.

### Palette strip
`Prev` / `Current` / `Next` palette sets (each 4 palettes x 15 colours, drawn
as square tiles in 2 rows of 30 = 2 palettes per row; the `Current` set gets a
border). Heading per set: `Prev PL:NNN Frame:NNNNN` etc. At a segment edge
(no previous or no next palette segment) that slot is **blank** (`Prev -`).

### Three stacked timelines (right of the meters, full remaining width)
Ratio 2:1:1 top to bottom, sharing the whole clip on the x-axis with a white
playhead:
1. **Req heatmap** - `Raw / Coa / Flbk / Buf / Miss` stacked per frame (same
   colours; `Same` and `Near` are omitted so the interesting load shows).
2. **Pattern supply** - `Prg / Wr0 / Wr1 / Main` remaining counts stacked per
   frame. All four use one scale: the sum of their fixed capacities. Prg is
   violet, Wr0 blue, Wr1 cyan, and Main amber.
3. **BODY Band** - useful payload (Raw colour) plus useful control (dim
   blue-grey) as a fraction of the physical bytes in each delivery slot. Pad
   remains blank and a horizontal line at the top marks CD 1x (150 KiB/sec).

## Colours (RGB)

Raw `(205,205,205)`, Same `(150,150,158)` grey, Near `(95,115,215)` blue,
Coa `(45,240,70)` green, Flbk `(240,150,50)` orange,
Buf `(175,120,235)` violet, Miss `(220,70,70)` red, DMA `(70,190,90)` green,
DMA-run `(215,165,65)` amber, Band-control `(95,110,122)` blue-grey.
Physical supply colours: Prg `(165,105,225)`, Wr0 `(80,145,235)`,
Wr1 `(65,205,195)`, Main `(235,175,70)`.
