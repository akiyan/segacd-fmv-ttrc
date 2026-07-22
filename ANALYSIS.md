# Analysis Overlay Reference

This document defines, exactly and completely, every element drawn in the
1920x1080 analysis frame produced by `tools/render_analysis.py` for the
**Tile Texture Reuse Codec**. The layout "source of truth" is
`tools/layout_preview.py` (dummy data); `render_analysis` runs the same drawing
functions on real encoder output.

Every render also writes a machine-readable, one-row-per-frame TSV beside the
video: `videos/<stem>_analysis.tsv`. It is generated from the same
`frame_data()` values used by the overlay, before PNG rendering begins, so
numeric comparisons do not require OCR. A frame-range render still refreshes
the complete TSV. Set `ANALYSIS_TSV` only when a different output path is
required.

Keep this file in sync whenever the layout changes (the `/analysis` skill
automates: update layout -> update this file -> notify).

## Analysis TSV sidecar

The TSV starts with one header row and then contains every encoded frame in
ascending order. Integer display fields are written exactly as the overlay
uses them. In particular, frame 0 keeps its `legend_raw` and `legend_same`
classification, while the untimed `status_cold`, `status_pre`,
`status_band_kib_s`, `status_dma`, and `status_run` fields are zero. The
corresponding encoder values remain available in the `stat_*` columns.

| Columns | Definition |
|---|---|
| `schema_version` | TSV schema version, currently `1`. |
| `frame`, `frame_hex`, `time_seconds`, `palette_segment` | Decimal frame, HUD-style hexadecimal frame, exact playback time, and CRAM palette-segment index. |
| `cells`, `active_tiles`, `budget_tiles`, `cold_cap_tiles`, `prefetch_cap_tiles` | Raster and configured per-frame limits repeated on every row for self-contained filtering. |
| `legend_raw`, `legend_same`, `legend_dic`, `legend_prg`, `legend_wr`, `legend_wr0`, `legend_wr1`, `legend_near`, `legend_coa`, `legend_flbk`, `legend_miss` | Per-frame category counts. `legend_wr` is the displayed Wr0+Wr1 total; the two source banks are also kept separately. |
| `status_req`, `status_miss`, `status_cold`, `status_pre`, `status_band_kib_s`, `status_prg`, `status_wr0`, `status_wr1`, `status_dma`, `status_run` | Numeric values printed in the bottom status bar, including the frame-0 untimed display rule. |
| `body_payload_bytes`, `body_control_bytes`, `body_pad_bytes`, `body_physical_bytes`, `body_useful_bytes`, `body_band_bps` | Exact physical BODY delivery-slot accounting behind the Band display. Slot 0 is zero because frame 0 comes from `HEADER.DAT`. |
| `quality_budget_remaining_bytes` | Encoder-only whole-movie quality allowance remaining after the frame. This is diagnostic state, not a physical meter. |
| `stat_frame` through the remaining `stat_*` columns | Every column from `stats.npz`, preserved with a `stat_` prefix and in its original order. These raw columns may grow when the simulator gains a new statistic. |

The default path follows `ANALYSIS_OUT`: changing
`videos/example_analysis.mp4` produces `videos/example_analysis.tsv` unless
`ANALYSIS_TSV` is explicitly set.

## Layout map

```
+----------------------------------------------+   +-----------------------------+
| SEGA-CD sim output  <meta>      <PL/Time/Fr> |   | Source  <res/fps/audio>     |
| +------------------------------------------+ |   | +-------------------------+ |
| |                                          | |   | | source frame (4:3)      | |
| |   SEGA-CD OUTPUT (centered on the real   | |   | +-------------------------+ |
| |   screen; letterboxed to the panel)      | |   | LEGEND (2 rows, 10 classes) |
| |                                          | |   | +-------------------------+ |
| |                                          | |   | | CATEGORY MAP (4:3)      | |
| |                                          | |   | | (tile content + border; | |
| |                                          | |   | |  Miss = red-filled hole)| |
| +------------------------------------------+ |   | +-------------------------+ |
+----------------------------------------------+   | CATEGORY TOTALS (whole clip)|
+----------------------------------------------+   | +-------------------------+ |
| STATUS BAR                                   |   | | AUDIO WAVEFORM          | |
|  [Req][Cold][Pre][Band][Prg][Wr0][Wr1]...      | | | (+/-2s, now = centre)   | |
|  Prev/Current/Next palette strip             |   | +-------------------------+ |
|  3 timelines (Req / three supplies / BODY Band)| +-----------------------------+
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
- **Legend** (between Source and the category map): five entries on the first
  row and four on the second, ordered `Raw Same Dic Prg Wr` then
  `Near Coa Flbk Miss`. The ten mutually exclusive data classes remain intact,
  but the displayed `Wr` count combines `Wr0 + Wr1`.
  Numeric fields are text directly on the legend background; there is no level
  fill behind the digits. All zero-padded digits use the normal text colour.
  Swatch styles mirror the map except that borderless `Same` uses the original
  light/dark checker swatch in the legend. `Raw` = black/white dashed frame,
  `Miss` = red fill, `Near/Flbk` = thin frame, and `Coa` = a thick frame in the
  same blue as Near. `Dic/Prg/Wr` use thin borders alternating between their
  colour and transparent gaps.
- **Category map** (middle): the tile grid. Each 8x8 tile shows its
  **reconstructed content**; the category (see Tile Categories) is indicated by
  the border: `Raw` = thin black/white dashed frame, `Same` = no border,
  `Near/Flbk` = thin 1px border, `Coa` = thick 3px Near-blue border, and
  `Dic/Prg/Wr0/Wr1` = thin colour-and-transparent dashed border. Wr0 and Wr1
  share the Wr1 cyan display colour. A `Miss` tile is
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

### The ten classes

| Class | Colour | Bytes | Meaning |
|-------|--------|-------|---------|
| **Raw**  | black/white dashed border | 34 | An exact pattern delivered for this frame, loaded into VRAM before display, and used immediately. Timed frames are bounded by the per-frame cold cap; frame 0 is boot-loaded from `HEADER.DAT` and is exempt. |
| **Same** | light/dark checker in legend; no map border | 0 or 2 (name only) | The target tile's exact pattern is **already resident** in VRAM. This includes a pattern prefetched in an earlier frame and first displayed now. No pattern transfer occurs this frame. |
| **Near** | blue | 2 (name) | No exact match, but a resident pattern passes the **Near** thresholds; the cell points to it. Near-perfect reuse. Also covers "keep the current display" when the currently shown tile is already accurate and still within Near of the new target. |
| **Coa**  | blue thick border (same colour as Near) | 2 (name) | Best resident passes **Coa** (a bit rougher than Near). Used for flat/low-detail tiles where a close-enough resident exists. Its meter/timeline fill remains green so Near and Coa totals remain distinguishable there. |
| **Flbk** | red thin border | 2 (name) | **Fallback** (merged Mid+Far). Used when an exact load is unavailable. It remains distinct from the solid-red Miss because it did improve the displayed tile. |
| **Miss** | red (filled) | 0 | The tile was **not updated**; it still shows whatever was there before. A red-filled hole in the category map. |
| **Prg** | violet/transparent thin dashed border | 34 | An exact cold load funded from saved whole-movie allowance and physically supplied by streamed PrgBuf. |
| **Wr0** | cyan/transparent thin dashed border | 2 (name) | An exact cold load using a boot-preloaded WordBuf0 pattern. Its legend count is combined into `Wr`. |
| **Wr1** | cyan/transparent thin dashed border | 2 (name) | An exact cold load using a boot-preloaded WordBuf1 pattern. Its legend count is combined into `Wr`. |
| **Dic** | amber/transparent thin dashed border | 2 (name) | An exact cold load using an entry from persistent DicBuf. |

### Selection order (per changed tile, `commit_unified`)

Frame 0 is a deliberate exception to this list. It has no timed BODY or cold
budget, so every cell is installed as its exact target. The first cell using an
exact pattern is `Raw`; further cells using the same pattern are `Same`.
`Near`, `Coa`, `Flbk`, `Prg`, `Wr0`, `Wr1`, `Dic`, and `Miss` must all be zero
in frame 0's displayed category totals. After those exact display patterns are
placed, unused startup pattern capacity may install future exact patterns into
otherwise-free VRAM slots; these have no displayed-cell category in frame 0.

1. If the **currently displayed** tile is already accurate (its class last frame
   was exact) and is within `Near` of the new target -> keep it, 0 bytes -> `Near`.
2. Else if the exact target pattern is resident -> `Same` (2 B).
3. Else find the best resident. If it passes `Near`(tier 0) or `Coa`(tier 1) and
   the budget allows the 2 B name -> `Near` / `Coa`.
4. Else load the exact pattern (34 B), unless the per-frame **cold cap**
   (`cold_cap_for_fps`, `av_config.py`) is already reached: charge the current
   current-frame allowance -> `Raw`, saved whole-movie allowance ->
   `Prg`, a boot-preload credit -> `Wr0/Wr1`, or a persistent dictionary hit -> `Dic`.
5. Else (quality budget/cold-cap exhausted) if the best resident improves on the
   current display (default improve mode; see Flbk above) -> `Flbk`
   (2 B fallback).
6. Else -> `Miss`.

Notes: `Same/Near/Coa/Flbk` use a resident 32-byte pattern and require at most
a 2-byte name-table entry. A `Raw` or `Prg` load costs 34 bytes in the
encoder model. A Wr0/Wr1 boot-preloaded load or DicBuf hit already owns its pattern
bytes and therefore costs only the 2-byte name entry during playback. A persistent
approximation (a tile stuck in Near/Coa/Flbk for >= 0.3s) is escalated to
Miss-priority so it gets an accurate reload when budget allows.

## Status bar (bottom-left)

Left to right: **Req**, **Cold**, **Pre**, **Band**, **Prg**,
**Wr0**, **Wr1**, **DMA**, and **Run** meters (each bar is as wide as
its own label). Below the meters is the palette strip; to the right are three
stacked timelines. The old Tank and Buf meters are removed.

### Req meter
All categories stacked into one bar (full width = total tile count `C`), with a
yellow vertical **budget line** marking the per-frame update budget. The compact
label is `Req:NNN Miss:NNN`.

### Cold meter
`Cold:NNN` = this frame's **timed new tile loads** (`Raw + Prg + Wr0 + Wr1 + Dic + future raw
prefetch`, i.e. every 32-byte pattern newly written to VRAM from any physical
supply). The bar stacks the corresponding category/source colours and blue
prefetch;
full-scale = `cold_cap_for_fps`
(`av_config.py`, selected only when mode/fps/active tiles exactly match a
measured tuple; an unmeasured tuple is rejected before encoding).
Frame 0 is outside this timing calculation and is displayed as `Cold:000`.
Its Raw/Same category counts remain visible in the legend.
This visualises the value the hardware slip investigations were fought over.

### Pre meter
`Pre:NNN` is the number of future exact patterns written by a timed frame
without being displayed yet. Frame 0's boot preload is deliberately outside
this meter and appears as `Pre:000`; its capacity and realized count remain in
the decision log and report. The meter scale is the runtime per-frame request
cap. If a prefetched pattern is used later, the displayed cell is `Same`, not
`Raw`.

### Band meter (useful BODY delivery) - KiB/sec
`Band` is the non-pad data physically read from `BODY.DAT` in this delivery
slot, divided by that slot's actual CD read time:

`Band = useful BODY bytes / physical BODY bytes * 150 KiB/sec`.

The physical bytes are the slot's whole sectors, including pad. At CD 1x each
sector takes 1/75 second, so a completely useful slot reads `Band:150`, a
half-pad slot reads `Band:075`, and a valid slot never exceeds 150 KiB/sec.

The bar is split into **Raw light grey** for the continuous 32-byte cold-pattern
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
quantized movie through the shared VRAM allocator. When a continuous Main-risk
burst exceeds the complete quality-budget capacity, its unavoidable shortfall
is distributed proportionally from the burst start through its peak instead of
being concentrated in the first frame. A backwards pass over that feasible
risk demand builds the reserve that protects normal updates from future
Flbk/Miss bursts. Optional exact-load upgrades use a separate strict reserve
from complete exact-update demand; their deliberately infeasible all-exact
shortage is not allowed to consume protection for live Main work. Both curves
finish at zero. The Main-risk original demand, balanced planned demand,
unavoidable shortfall, and reserve are saved as separate byte traces in
`buffer_remaining.npz`; none is a physical supply meter.
[`BUEFFERING.md`](BUEFFERING.md) describes how both curves are constructed and
applied.

### Four pattern-supply meters

Each meter is an independent remaining count in 32-byte patterns:

| Meter | Physical object | Behaviour |
|---|---|---|
| `Prg:NNNNN` | usable PRG-RAM `PrgBuf` | End-of-frame occupancy from the exact sector scheduler. It can rise through BODY prefetch and fall through Prg consumption. |
| `Wr0:NNN` | `WordBuf0` in physical Word-RAM bank 0 | Actual boot-loaded total minus patterns consumed by eligible even frames. It only falls. |
| `Wr1:NNN` | `WordBuf1` in physical Word-RAM bank 1 | Actual boot-loaded total minus patterns consumed by eligible odd frames. It only falls. |

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
to VRAM by the timed frame. The numeric field uses the digits required by the
current raster. Frame 0's boot construction is outside this calculation and is
shown as zero. Its full-scale starts from the
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
Frame 0 is outside the timed run calculation and is shown as `Run:0000`, even
though its internal boot-transfer trace remains available for packer checks.
Cold payload order follows the movie-wide physical slot permutation and is
independent of cell/name-update order. The optimizer targets the worst
source-aware run count among frames at 85% or more of the measured cold cap;
Prg/Wr/Dic boundaries are part of that count. Total runs over the whole movie
are not constrained, so a light frame can gain runs when a heavy frame loses
more expensive fragmentation. The two-pass encoder freezes logical decisions
before deriving the delivered map and rechecks display identity plus the
whole-movie quality budget afterwards.

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
1. **Req heatmap** - `Raw / Prg / Wr0 / Wr1 / Dic / Coa / Flbk / Miss`
   stacked per frame. `Same` and `Near` are omitted so the interesting load
   remains visible.
2. **Pattern supply** - `Prg / Wr0 / Wr1` remaining counts stacked per
   frame. All three use one scale: the sum of their capacities. Wr0 and Wr1
   both use the Wr1 cyan display colour; their distinct stack positions and
   separate numeric meters retain bank identity. The persistent DicBuf has no
   remaining count and is therefore omitted.
3. **BODY Band** - useful payload (Raw light grey) plus useful control (dim
   blue-grey) as a fraction of the physical bytes in each delivery slot. Pad
   remains blank and a horizontal line at the top marks CD 1x (150 KiB/sec).

## Colours (RGB)

Raw `(205,205,205)`, Same `(150,150,158)` grey, Near `(95,115,215)` blue,
Coa `(45,240,70)` green, Flbk and Miss `(220,70,70)` red,
DMA `(70,190,90)` green,
DMA-run `(215,165,65)` amber, Band-control `(95,110,122)` blue-grey.
Physical supply colours: Prg `(165,105,225)`, Wr0 and Wr1 both
`(65,205,195)`, Dic `(235,175,70)`. Dic, Prg, and Wr use these colours only
on alternating segments of their thin category borders.
