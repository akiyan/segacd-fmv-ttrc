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
|  [Req] [Cold] [Band] [Tank] [Buff] [DMA] [Run]|  | | (+/-2s, now = centre)   | |
|  Prev/Current/Next palette strip             |   | +-------------------------+ |
|  3 stacked timelines (Req / Tank / transfer) |   +-----------------------------+
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
  - `avg N KiB/sec` = average of the effective Band over the whole clip
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
  The numeric field has a subtle seven-character-wide horizontal level fill.
  The largest used-cell count in the frame reaches the full width and the other
  categories are proportional to it; the tint is derived from the category
  colour. `unique/used` entries use the `used` value for the fill. All
  zero-padded digits use the normal text colour.
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
| **Raw**  | light grey | 34 (32 pattern + 2 name) | An accurate full-cost load charged to this frame's virtual CBR budget. Physical payload delivery may have happened earlier through the RING. |
| **Same** | checker grey | 2 (name only) | The target tile's exact pattern is **already resident** in VRAM; the cell just points to it (lossless dedup). No pattern transfer. |
| **Near** | blue | 2 (name) | No exact match, but a resident pattern passes the **Near** thresholds; the cell points to it. Near-perfect reuse. Also covers "keep the current display" when the currently shown tile is already accurate and still within Near of the new target. |
| **Coa**  | green | 2 (name) | Best resident passes **Coa** (a bit rougher than Near). Used for flat/low-detail tiles where a close-enough resident exists. |
| **Flbk** | orange (thick border) | 2 (name) | **Fallback** (merged Mid+Far). Only used when no Raw/Buf load is possible (budget/tank exhausted or the per-frame cold cap reached). Default is **improve mode**: the best resident is taken if it gets closer to the target than the current display (`CBRSIM_FLBK_IMPROVE_ONLY=0` reverts to the absolute wide **Flbk** threshold). Visibly approximate, but "better than a Miss". This is the last resort before Miss. |
| **Buf**  | violet (thick border) | 34 (32 pattern + 2 name) | An accurate full-cost load charged to banked virtual VBV budget. Same accuracy and physical payload path as Raw; only the encoder's funding class differs. |
| **Miss** | red (filled) | 0 | The tile was **not updated**; it still shows whatever was there before. A red-filled hole in the category map. |

### Selection order (per changed tile, `commit_unified`)

1. If the **currently displayed** tile is already accurate (its class last frame
   was exact) and is within `Near` of the new target -> keep it, 0 bytes -> `Near`.
2. Else if the exact target pattern is resident -> `Same` (2 B).
3. Else find the best resident. If it passes `Near`(tier 0) or `Coa`(tier 1) and
   the budget allows the 2 B name -> `Near` / `Coa`.
4. Else load the exact pattern (34 B), unless the per-frame **cold cap**
   (`cold_cap_for_fps`, `av_config.py`) is already reached: charge the current
   virtual CBR budget -> `Raw`, or banked virtual VBV budget -> `Buf`.
5. Else (budget/VBV/cold-cap exhausted) if the best resident improves on the
   current display (default improve mode; see Flbk above) -> `Flbk`
   (2 B fallback).
6. Else -> `Miss`.

Notes: `Same/Near/Coa/Flbk` cost only a 2-byte name-table entry (they reuse
a resident 32-byte pattern). `Raw/Buf` cost a full 34 bytes in the encoder
model. `Raw` spends current virtual CBR budget; `Buf` spends banked virtual VBV
budget. Both are later delivered through the same physical payload RING. A persistent
approximation (a tile stuck in Near/Coa/Flbk for >= 0.3s) is escalated to
Miss-priority so it gets an accurate reload when budget allows.

## Status bar (bottom-left)

Left to right: one wide **Req** meter, then **Cold**, **Band**, **Tank**,
**Buff**, **DMA**, **Run** meters (each bar is as wide as its own label). Below the
meters is the palette strip; to the right are three stacked timelines.

### Req meter
All categories stacked into one bar (full width = total tile count `C`), with a
yellow vertical **budget line** marking the per-frame update budget. Labels:
`Req:NNN` (changed tiles requested this frame), `Raw:NNN` (current-budget loads),
`Comp:NNN` (tiles satisfied by resident reuse = `Same+Near+Coa+Flbk`).

### Cold meter
`Cold:NNN` = this frame's **new tile loads** (`Raw + Buf`, i.e. every 32-byte
pattern that had to be consumed from the payload RING). The bar stacks a
Raw-coloured and a Buf-coloured segment; full-scale = `cold_cap_for_fps`
(`av_config.py`, the mode/fps/active-tile-specific drop-safe per-frame cold
ceiling; an explicit positive `CBRSIM_MAX_COLD` is reserved for special experiments).
This visualises the value the hardware slip investigations were fought over.

### Band meter (effective CD usage) - KiB/sec
`Band` is the encoder's **virtual CBR-budget use**: how much of the fixed
`FRAME_BYTES` allowance it put to use this frame. It is a quality-allocation
model, not a reading of physical BODY sectors or payload-RING occupancy. The
bar is split, in order, into:

- **Raw colour** = video written this frame (patterns + name table + CRAM).
- **Buf/violet colour** = bytes banked into the encoder's virtual VBV budget
  this frame (saving for future hard frames is also "effective" use).
- **dim blue-grey colour** = audio + every other fixed header (name-table base,
  flag maps, CRAM, and anything else in the stream).

`Band = FRAME_BYTES - padding`, where *padding* is the unused part of this
virtual budget. So Band sits at the CBR ceiling (~144 KiB/sec) almost always,
dipping only when the VBV budget is full and there is nothing useful to send.
The value comes from the encoder's own
`cd_used` log, so it includes bytes the renderer does not otherwise model.
`avg N KiB/sec` in the top meta is the mean of this Band. The bar's full-scale
(like the effective-transfer timeline's) is the CD 1x per-frame byte count
(`153600 / fps`).

Before making these per-frame choices, the encoder dry-runs the complete
quantized movie through the shared VRAM allocator. A backwards pass builds two
virtual reserve curves: complete exact-update demand limits optional Raw/Buf
upgrades, while changes beyond the Coa bound form the narrower reserve that
protects normal updates from future Flbk/Miss bursts. Both curves finish at
zero. They are saved as `upgrade_reserve_bytes` and
`main_risk_reserve_bytes` in `buffer_remaining.npz`; neither is the physical
Tank meter below.

### Tank meter
`Tank:NNNNN` = actual end-of-frame PRG-RAM **payload RING occupancy**, in
32-byte pattern slots. The sim runs the same sector scheduler as the packer,
using the exact per-frame cold counts and control-block lengths. This includes
the HEADER prebuffer, whole-sector padding, per-frame payload delivery, and
pattern consumption. The packer recomputes the trace from its built control
blocks and rejects any mismatch.

This is intentionally separate from the encoder's virtual VBV budget. Near the
end of a movie, Tank can only retain already delivered payload (usually no more
than final-sector padding); it cannot rise just because unused virtual budget
remains.

### Buff meter (payload-RING change indicator)
A centre-anchored gauge for this frame's physical payload-RING change. A faint
centre line; fill grows **left in red** when the RING drained, **right in blue** when it
filled. Label `Buff:-NNN / +NNN / +/-000`. Full-scale = `C - per-frame Raw
budget` (the largest plausible one-frame swing).

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
2. **Tank level** - actual physical payload-RING occupancy (violet).
3. **Effective transfer** - Raw (video) plus Buf (banked) per frame.

## Colours (RGB)

Raw `(205,205,205)`, Same `(150,150,158)` grey, Near `(95,115,215)` blue,
Coa `(45,240,70)` green, Flbk `(240,150,50)` orange,
Buf `(175,120,235)` violet, Miss `(220,70,70)` red, DMA `(70,190,90)` green,
DMA-run `(215,165,65)` amber, Band-overhead `(95,110,122)`.
