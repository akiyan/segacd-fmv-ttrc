# On-hardware DEBUG HUD

This document is the complete reference for the values-only playback HUD drawn
by `boot/movieplay_ip.s` in `DEBUG=1` builds. It covers the runtime movie HUD,
not the minimal four-digit boot preload counter and not the offline analysis
overlay documented in [`ANALYSIS.md`](ANALYSIS.md). The boot counter reuses the
same 16 hexadecimal glyphs; their VRAM is fixed at `0xD000` (tiles 1664..1679)
in the unused `0xD000`-`0xDFFF` gap between the two name tables, identical in
DEBUG and release builds. Because the font no longer sits above the resident
pool, the pool can grow to a full 1535 slots and DEBUG and release share exactly
the same pool shape.

The HUD answers three different questions at once:

1. Which movie frame and palette segment are actually visible?
2. Is the Sub CPU keeping the CD stream and audio ready?
3. Is the Main CPU finishing pattern transfer before the display deadline?

It is deliberately diagnostic. A nonzero value is not automatically a codec
failure, and several fields show only the low byte of a larger counter.

## Enabling the HUD

Build the profile with DEBUG enabled:

```sh
make disc CONFIG=configs/PROFILE.toml DEBUG=1
```

`tools/record_movie.sh` uses a DEBUG disc by default. Release builds omit the
HUD. They retain the older slip-triggered CRAM0 red indicator, while DEBUG
builds keep the HUD colours stable and expose slips through `S` instead.

## Physical layout

The hardware draws hexadecimal values only. There are no labels, spaces, or
separators in the actual image. Spaces below show the field boundaries:

```text
H32/H40: FFFF PP SS DD RR LL CC WW MM AA UUUU NN JJ
```

The fixed interpretation order is:

```text
F / P / S / D / R / L / C / W / M / A / U / N / J
```

H32 and H40 use the same layout. Every digit occupies one 8x8 cell.
H40 DEBUG builds additionally append three flip-phase fields, extending the
order to `... / J / V / O / E` (36 cells). H32 keeps the 30-cell layout —
its 32-cell row has no room for the extension.

| Field | Cell columns | Native pixel range | Digits |
|---|---:|---:|---:|
| `F` | 0-3 | x=0-31 | 4 |
| `P` | 4-5 | x=32-47 | 2 |
| `S` | 6-7 | x=48-63 | 2 |
| `D` | 8-9 | x=64-79 | 2 |
| `R` | 10-11 | x=80-95 | 2 |
| `L` | 12-13 | x=96-111 | 2 |
| `C` | 14-15 | x=112-127 | 2 |
| `W` | 16-17 | x=128-143 | 2 |
| `M` | 18-19 | x=144-159 | 2 |
| `A` | 20-21 | x=160-175 | 2 |
| `U` | 22-25 | x=176-207 | 4 |
| `N` | 26-27 | x=208-223 | 2 |
| `J` | 28-29 | x=224-239 | 2 |
| `V` (H40 only) | 30-31 | x=240-255 | 2 |
| `O` (H40 only) | 32-33 | x=256-271 | 2 |
| `E` (H40 only) | 34-35 | x=272-287 | 2 |

The common part covers 30 cells or 240 pixels. H32's rightmost 2 cells
remain movie-visible; H40 covers 36 cells, leaving its rightmost 4 cells
movie-visible.

The HUD always occupies row 0 of the native 256x224 or 320x224 raster. It can
cover active picture content; it is not repositioned around letterboxing.

For any recording that can proceed to compilation or upload, the complete first
movie loop is a mandatory gate with three results: `PASS`, `WARNING`, and
`FAIL`. `WARNING` remains upload-capable. `S/D/R` must remain zero. `C/M` and
`J` thresholds follow the profile's player cadence. Fixed-N2 warns when
`C>00` and fails when `M>01`. Delivery-paced content may use all but the
already-armed control sector on the current Sub path and all display fields in
one content frame: 15 fps warns when `C>04` and fails when `M>04`; 24 fps warns
when `C>03` and fails when `M>03`. The largest passing `J` is
normal-ceiling-to-physical-end minus one
KiB: `2B` at 15fps, `1C` at 24fps, and `17` at 30fps. Values above the normal
jitter interval (`28`, `19`, or `14` respectively) show that
sector-granular back-pressure entered the separate 4 KiB physical guard.
Report the value, but a `J` within the cadence-specific passing limit does not
by itself require another confirmation or fail the recording. Report all gate
maxima on PASS or WARNING, and report every C warning frame. When the enclosing
task already authorizes publication,
continue without requesting another approval merely because the gate ran.

## At-a-glance field reference

| Field | Owner | Scope | Meaning | Healthy interpretation |
|---|---|---|---|---|
| `F` | Main | current state | Visible movie frame number | Advances according to the movie cadence |
| `P` | Main | current state | Zero-based CRAM palette-segment number | Changes only at expected palette boundaries |
| `S` | Sub | cumulative | CD sector-slip/re-seek recovery count | `00` throughout a clean run |
| `D` | Sub | cumulative | Control-stream frame-sequence mismatch count | `00` throughout a clean run |
| `R` | Sub | cumulative | RF5C164 write-pointer re-sync count | `00` is ideal |
| `L` | Sub | current state | Audio write lead, in units of 256 decoded bytes | Stable and comfortably inside the configured lead range |
| `C` | Sub | per frame | Blocking CD sector pumps before control execution | `00` means control was already ready |
| `W` | Main | per frame | Main wait for Sub handoff, in approximate scanlines | Small and stable |
| `M` | Main | per timed frame | VBlank starts waited by the Main pattern path; frame 0 reports `00` because it is an untimed boot construction | `00` or `01`; `02+` proves an extra spill |
| `A` | Sub | per frame | ADPCM decode phase time | Stable band for the same profile |
| `U` | Main | per frame | Main pattern-transfer elapsed time | Below the frame's available transfer window |
| `N` | Main | per frame | Source-aware cold-run descriptor count | Content-dependent; correlate with `U` |
| `J` | Sub | cumulative peak | Maximum streamed PrgBuf occupancy above the fps-derived normal ceiling | `00` means the jitter headroom was never used |
| `V` | Main | previous frame | V-counter at the last accepted display flip (H40 only) | `E0` = flip at the VBlank start; higher blank lines mean the flip ran late inside its blank |
| `O` | Main | previous frame | That flip's interval excess over 1024 stopwatch ticks (H40 only) | About `3E` (62 = nominal 1086-tick N2 interval); `FF` marks a slipped 3-field frame |
| `E` | Main | per frame | Pass2 entry delay since the previous flip, in 4-tick units (H40 only) | Below one field (`88` = 544 ticks) with margin; approaching the field-1 blank end means the transfer is about to miss its VBlank |

`S`, `D`, and `R` are cumulative counters. They should be read as transitions:
once incremented, they remain nonzero until playback restarts, and the displayed
low byte wraps from `FF` to `00`. `J` is also cumulative but retains the
largest observed excess rather than counting events. `C`, `W`, `M`, `A`, `U`,
and `N` describe one frame. `F`, `P`, and `L` describe current player state.
`V` and `O` are sampled at `do_flip` *after* the flip register write, so the
row that carries them was built one frame later: frame `F`'s row shows the
flip that published frame `F - 1`. Shift by one frame when correlating them
with per-frame workload. `E` is sampled during frame `F`'s own build, before
its transfer VBlank wait, and needs no shift.

## Field details

### `F`: displayed frame

`F` is the full 16-bit movie frame number. Frame 0 is `0000`. The Main CPU
formats the number into the inactive table and selects that table with the same
Plane A flip that publishes the picture, so the value and image identify the
same frame.

The current stream format holds fewer than 65536 frames, so `F` does not wrap
inside one valid playback loop. It returns to `0000` when loop playback starts
again after the end hold.

### `P`: active palette segment

`P` is the low byte of the zero-based palette-segment number currently used by
CRAM. Segment 0 displays as `00`. The stream's palette reference is stored as
segment plus one, but the Main CPU subtracts one before updating this HUD state.

`P` reports the active state, not merely a switch command. It therefore remains
constant between CRAM changes. The current table capacity is 64 segments, so a
valid stream never needs the field's byte wrap.

### `S`: CD sector slips and re-seek recovery

`S` is the low byte of the cumulative `slip_count`. The Sub CPU increments it
when continuous CD delivery loses or skips a sector and the recovery path must
re-establish the read position. A new increment marks a real streaming
incident and is a likely visual or timing glitch boundary.

`S=00` is required for a clean qualified run. Because only the low byte is
shown, compare adjacent frames rather than treating a later `00` as proof that
no earlier slips occurred.

### `D`: control-stream desynchronization

Every control block carries a frame sequence. `D` increments when that sequence
does not match the frame the Sub CPU expected. The player rejects the mismatched
control and holds the previous image instead of walking corrupt data.

`D` is therefore more severe than ordinary encoder Miss: it means the runtime
stream position is wrong. A clean run keeps `D=00`.

### `R`: audio pointer re-syncs

The RF5C164 writer normally remains ahead of playback. `R` increments when the
measured lead leaves the configured `[SYNC_MIN, SYNC_MAX]` range and the writer
jumps to `play + SYNC_LEAD`. The jump restores safety but can be audible.

`R=00` is ideal. Diagnose an increment together with the preceding `L` trend;
the transition matters more than the persistent nonzero value afterwards.

### `L`: audio lead

`L` is the high byte of the current ring distance from the RF5C164 play pointer
to the write pointer. One displayed unit is 256 decoded sample bytes:

```text
lead_bytes is approximately L * 256 through L * 256 + 255
```

With the current constants, normal re-sync placement uses `SYNC_LEAD=0x3000`,
which appears near `L=30`. `SYNC_MAX=0x6800`, which appears near `L=68`.
Approaching `00` means the reserve is draining; approaching or exceeding the
upper boundary means the writer has run too far ahead. Convert bytes to time
using the profile's effective playback sample rate.

### `C`: blocking CD work on the Sub critical path

`C` is the low byte of two per-frame counts added together:

- pumps needed to finish this frame's control sectors;
- pumps needed while an older BODY payload or padding slot was still draining.

Each pump drains one physical sector. `C=00` means the needed control was
already armed when `process_frame` reached it. A small nonzero value is not a
sector slip; it means delivery work landed directly on the current frame's
critical path. The profile-derived threshold is zero for fixed-N2 and follows
the slot allowance for delivery-paced 15/24 fps. Above-threshold `C` produces
`WARNING`, not `FAIL`: it identifies Sub/CD deadline pressure for review while
remaining upload-capable. Persistent C, especially with rising `W`, is stronger
diagnostic evidence than an isolated peak.

### `W`: Main wait for the Sub CPU

At `CMD_SWAP`, the Main CPU samples the V counter, waits for the Sub CPU's
`STAT_READY` or `STAT_END`, then samples it again. `W` is the masked eight-bit
difference, expressed as approximate scanlines.

This includes whatever prevents the Sub from completing the handoff, such as
control delivery, expansion, ADPCM work, or a late command response. It is not
a cycle-accurate timer and wraps at 256. Use it for relative comparisons and
spikes within the same mode, not as an absolute duration measurement.

### `M`: Main pattern-path VBlank waits

`M` counts VBlank starts consumed by the Main-side pattern transfer path for
the current frame. Display cadence waiting is deliberately excluded. This
makes `M` a deadline diagnostic rather than a restatement of 15/24/30 fps
pacing.

For fixed-N2 playback, `M=00` or `01` is the normal region and `M>=02` proves
that pattern work spilled into an additional VBlank. Delivery-paced 15/24 fps
has more display fields per content frame, so its automatic limit is
`ceil(60000/1001/fps)`: four at 15 fps and three at 24 fps. Correlate `M` with
`U` and `N` to distinguish total transfer volume from run fragmentation.

### `A`: Sub ADPCM decode time

The Sub CPU measures the checkpointed IMA decode phase with the Mega-CD
stopwatch. The player shifts the raw value right by two before displaying its
low byte:

```text
one A unit = 4 * 30.72 us = approximately 0.12288 ms
```

For example, `A=40` means about 7.86 ms. At low frame rates, the longer ADPCM
decoder periodically services the CDC, so `A` can also
include that intentionally interleaved pump work. It does not measure the
subsequent RF5C164 wave-RAM write phase.

### `U`: Main pattern-transfer time

`U` displays four hexadecimal digits from the Main CPU's Mega-CD stopwatch.
One tick is 30.72 us. Measurement begins at the first cold run and ends after
the final DMA repair or short-run CPU write. It includes waits between pieces
when a long run must cross a VBlank word-budget boundary.

The hardware counter is 12-bit, and the player masks the difference to
`0x0FFF`. It therefore wraps after 4096 ticks, about 125.83 ms. A frame with no
cold runs reports `0000`.

### `N`: packed cold-run count

`N` is the low byte of the source-aware cold-run descriptor count constructed
for the frame. A run groups consecutive VRAM slots from the same physical
pattern source. Prg, the parity-selected WordBuf, and DicBuf boundaries split
runs even when the destination slots are consecutive.

`N` is not the cold-tile count and is not the number of physical VDP DMA
commands. One- and two-tile runs use CPU writes. Longer runs use DMA and can be
split again at VBlank boundaries. `N` measures fragmentation before those
hardware transfer choices.

### `J`: streamed PrgBuf jitter-reserve high-water mark

`J` is the maximum simultaneous streamed PrgBuf occupancy above the
fps-derived normal ceiling observed since BODY streaming began. That ceiling
is 384 KiB at 15fps, 399 KiB at 24fps, and 404 KiB at 30fps. It is rounded
upward to KiB and displayed in hexadecimal. `J=00` proves that occupancy never
crossed the ceiling; `J=01` means a nonzero excess of at most 1 KiB, and
`J=0A` means a maximum excess of at most 10 KiB.

The Sub CPU samples occupancy immediately after each BODY payload sector is
appended. Only an append can raise the high-water mark, so polling and pattern
consumption need no extra sampling. The separate frame-0 block temporarily
stored at `F0PAT_TMP` does not pass through this path and is deliberately
excluded. The field measures simultaneous occupancy, not whether a circular
read or write pointer happened to enter the physical address range above that
stream's normal boundary.

### `V` / `O` / `E`: flip phase and Pass2 entry phase (H40 DEBUG builds)

`V` is the raw V-counter high byte read immediately after the accepted
display flip's register write. `E0` (line 224) is a flip taken exactly at
the VBlank start — the dominant healthy value; higher blank lines mean the
flip ran late inside its blank, and the guarded terminal lines (`FC..FF`)
are never accepted. `O` is the flip-to-flip stopwatch interval minus 1024
ticks, clamped to `0..FF`: the nominal fixed-N2 interval of ~1086 ticks
reads as about `3E` (62), late-in-blank flips read higher, and a slipped
3-field frame saturates at `FF`. Both describe the flip that published the
*previous* frame (see above).

`E` is sampled at the Pass2 entry (`bf_dma`), before its VBlank wait: the
stopwatch distance from the previous flip in 4-tick units. It measures the
complete pre-transfer Main phase — CMD_SWAP wait (`W`), control parse, the
bitmap/list shadow walk, and the name-table blit — against the hard
deadline this investigation identified: Pass2 must enter before field 1's
VBlank (one field = 543 ticks = `E` about `88`) or the transfer consumes
the flip's own VBlank and the frame slips to three fields. `U` alone
cannot show this because its stopwatch starts only inside the transfer
VBlank.

## Reading combinations

| Observation | Likely interpretation |
|---|---|
| `S`, `D`, and `R` remain `00` | No detected CD loss, control desync, or audio pointer jump |
| `S` increments, then `D` remains `00` | Sector recovery occurred without losing control alignment |
| `D` increments | A control block was rejected and the previous image was held |
| `L` trends toward a boundary, then `R` increments | Audio reserve left its safe range and the write pointer jumped |
| `C` and `W` rise together | CD/control work is delaying the Sub-to-Main handoff |
| `A` rises with `W`, while `C` stays low | ADPCM decode or its internal low-rate CDC service is the stronger Sub-side cost |
| `N` rises with `U` | Run fragmentation is increasing Main fixed transfer overhead |
| `U` rises while `N` stays modest | Larger pattern volume or VBlank splitting dominates rather than descriptor count |
| `M` reaches `02` or more | Main pattern work crossed an extra VBlank deadline |
| `P` changes with stable `S/D` | Normal scheduled CRAM segment switch |
| `J` changes from `00` | Streaming occupancy used part of the physical jitter reserve |
| `J` rises again later | Timed playback exceeded the previous startup/runtime high-water mark |

These are correlations, not standalone proofs. Use native lossless capture and
the packed stream when investigating a regression.

## How the HUD is rendered

The HUD does not use the Window plane. For each frame the Main CPU:

1. builds the complete next movie name table in the inactive Plane A table;
2. formats the HUD into a 60-byte Main-RAM row (72 bytes with `V`/`O`/`E`);
3. overwrites only the first 30 name-table cells (36 on H40 DEBUG builds);
4. selects that completed table with the same register-2 flip as the movie.

The inactive tables are at VRAM `0xC000` and `0xE000`. Publishing the HUD uses
15 longword writes (18 with `V`/`O`/`E`) and no DMA. The unoccupied cells
retain their movie entries, which avoids exposing an unrelated Plane B frame.

The final flip has a terminal-VBlank guard: V-counter lines `FC` through `FF`
are rejected so the table is not selected at the end-of-blank race.

## Hex font and CRAM stability

The HUD font contains exactly 16 patterns, one for each hexadecimal digit.
Each 8x8 glyph has:

- a top-row four-bit barcode, with two pixels per bit, most-significant bit
  first;
- a compact 6x7 human-readable hexadecimal glyph below it.

The 16 font patterns are uploaded once at startup to VRAM `0xD000` (tiles
1664..1679); the name-table cells reference those fixed tile indices directly
(11-bit indices reach the whole 2048-tile space). The player expands source
colour 0 to P0/index1 and source colour 1 to P0/index15 when uploading the
font. The encoder and packer canonicalize these
as the globally darkest and brightest usable colours across every palette
segment. Consequently CRAM switches do not recolour or blink the HUD, and no
font scan, recolour, DMA, or extra VBlank wait is needed per frame.

## OCR and recording analysis

Read a native screenshot with:

```sh
tools/python.sh tools/read_frameno.py frame.png
```

The result includes every field and a confidence value, for example:

```text
frame.png -> F=012A(0.99) P=03(0.99) S=00(0.99) ...
```

`read_frameno.py` decodes the barcode and checks the lower glyph with normalized
correlation. H32 and H40 share the same field layout; native width is retained
only as mode metadata. If an H40 image has already been cropped narrower, call
`read_hud` with `HUD_H40_LAYOUT` explicitly.

For a complete recording, `harness/startup_resync/analyze.py` groups repeated
60 Hz capture frames by `F`, retains per-field confidence, and reports counter
transitions. HUD timing is diagnostic only: do not use OCR to trim publication
recordings or place YouTube chapters.

Write the complete per-frame series as the canonical project TSV:

```sh
tools/python.sh harness/startup_resync/analyze.py \
  videos/STEM_emu_lossless.mkv configs/PROFILE.toml \
  --tsv videos/STEM_emu_hud.tsv \
  --gate-json videos/STEM_emu_hud_gate.json \
  --expected-frames FRAME_COUNT
```

The log is UTF-8 with a header row, tab separators, LF line endings, and a
`.tsv` extension. Project-owned HUD logs are never comma-delimited.

The reproducible glyph/layout proof is:

```sh
tools/python.sh harness/hud_ocr/verify.py
```

## Maintenance contract

Any field, width, or ordering change must update these together:

- `boot/movieplay_ip.s` `prepare_dbg` and `publish_dbg`;
- `tools/read_frameno.py` layout and field decoding;
- `harness/hud_ocr/verify.py` layout/OCR proof;
- this document and the short README summary;
- field-specific verification such as `harness/pipeline_speedup/verify_run_hud.py`.

Keep the HUD values-only unless a new layout is deliberately qualified. Adding
labels consumes movie cells and Main-side publication work; it is not a free
presentation change.
