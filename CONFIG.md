# CONFIG.md — Tunable settings, throttles and buffers

Reference for the numeric knobs of the Tile Texture Reuse Codec pipeline
(`tools/sim.py` -> `tools/pack_stream.py` -> `boot/movieplay_*.s`). Pure hardware
register addresses and memory-map constants are intentionally omitted; this lists
the values you actually *tune*.

`CONFIG.md` describes the shared model, defaults, and profile schema; it does
not hold settings for a particular movie. Per-source values belong in a versioned
TOML file under [`configs/`](configs/). This keeps documentation stable while
making every encode reproducible.

**Single source of truth.** The streaming-buffer geometry lives in
[`tools/av_config.py`](tools/av_config.py) and is *derived*, so the sim, the
packer and the player cannot drift apart. The player's `.equ RING_SIZE` is
asserted equal to `av_config.RING_SIZE_KB` at build time
(`tools/check_player_ring.py`, run by the Makefile). Do not redefine a derived
value anywhere else.

Where a value lives: **sp** = `boot/movieplay_sp.s` (Sub CPU), **ip** =
`boot/movieplay_ip.s` (Main CPU), **cfg** = `tools/av_config.py`, **sim** =
`tools/sim.py`, **pack** = `tools/pack_stream.py`.

---

## A. Pattern supplies and offline quality budget

The player exposes four physical pattern supplies: streamed `PrgBuf`,
boot-preloaded `WordBuf0`, `WordBuf1`, and `MainBuf`. The short analysis labels
are Prg, Wr0, Wr1, and Main. The old Tank name is retired.

The encoder also keeps an offline whole-movie quality budget. It decides when
the encode may spend bytes, but it is not another player buffer and is not
shown as a supply meter. Its ceiling matches usable `PrgBuf` capacity so the
quality plan cannot assume more time-shifting freedom than the physical stream
can schedule.

| Name | Value | Where | Meaning |
|---|---|---|---|
| `RING_SIZE` / `RING_SIZE_KB` | 428 KB (0x6B000) | sp / cfg | Internal circular allocation backing `PrgBuf`, from 0x0C000 up to `APPLY_BASE`. The `RING_*` spelling describes the implementation, not a fifth public object. |
| `RING_JITTER_MARGIN_KB` | 40 KB | cfg | Headroom for real CD-delivery jitter, subtracted from the physical PRG ring. |
| `PRG_BUF_CAP_KB` | 388 KB (derived) | cfg -> sim / pack | Public usable `PrgBuf` schedule/prefetch ceiling = `RING_SIZE_KB - RING_JITTER_MARGIN_KB`. `RING_CAP_KB` remains an internal compatibility alias. |
| `QUALITY_BUDGET_KB` | 388 KB (derived) | cfg -> sim | Capacity of offline quality accounting. It matches the usable Prg ceiling but has an independent trace and no physical meter. Configured runs overwrite inherited `CBRSIM_QUALITY_BUDGET_KB`. |
| `WordBuf0` / `WordBuf1` | 880 patterns each (27.5 KB each) | sp / ip / sim / pack | Different boot-preloaded sequences in the two physical Word-RAM banks at offset `+0x15200..+0x1C000`. Wr0 serves even timed frames and Wr1 odd timed frames; they are not duplicated copies. |
| `MainBuf` | 208 patterns (6.5 KB) | ip / sim / pack | Boot-staged at Word-RAM `+0xD000`, then copied once to Main RAM `0xFF6600..0xFF8000`. Either frame parity may consume it. |
| `BACKPRESSURE_KB` | 424 KB (`RING_SIZE-4`) | cfg | Where `pump_poll` stops draining the CDC to avoid overrunning the PRG ring. The Prg schedule ceiling must stay below it. |
| routing table | 16 KB per 1M Word-RAM bank, 16384 frames (v7+) | sp / pack | One byte per frame: bits 0-2 are control sectors, bits 3-5 are total control-plus-payload sectors, and bits 6-7 must be zero. `routing_sec` is exactly `ceil(frames / 2048)`. v10 retains the v7 one-byte layout. The table is copied identically into both banks at boot, so the Sub can read it regardless of delivery/display frame parity. v6 used two bytes per frame and was limited to 8192 frames. |
| `APPLY_SIZE` | 34 KB (0x8800) | sp | Control-block apply ring (the per-frame update/cram/audio blocks). |
| Prg prebuffer | up to `PRG_BUF_CAP_KB` | sim / pack | Final region of `HEADER.DAT`; a boot-time Prg payload burst before frame 1. It is capped by both usable Prg capacity and the clip's future Prg load total. |
| frame-0 boot staging | 36 KB max in the 40 KB jitter tail | sp | Frame 0 is temporarily stored at the usable-Prg end and expanded before BODY streaming reuses those PRG-RAM bytes. |

DEBUG uses the Window name table for one HUD row. Only its 22 H32 or 28 H40
value cells are opaque; unused width uses transparent tile 0 so the movie stays
visible at the right. The player still updates every video row behind the
Window, so diagnostic playback has the same video-name-table work as a release
build. The encoder only reorders existing CRAM colours to keep palette 0 index
1 globally darkest (HUD background) and index 15 globally brightest (HUD text);
it does not alter either colour value.

## A2. CRAM pre-load (PALTAB) — palette table, off the stream

All segment palettes are shipped once in a **PALTAB** region right after the
first sector of `HEADER.DAT` (see [`MOVIE.md`](MOVIE.md)) and copied at boot
into a Main-RAM table. The per-frame stream then carries only a 1-byte segment
reference (`pal = seg + 1`, 0 = no switch) instead of a 128-byte in-stream CRAM
payload. So palettes no
longer depend on stream-delivery timing (a CD slip or re-seek can't corrupt a
segment's colours), and the palette-switch frame's byte budget is freed.

The encoder also gives every segment a canonical DEBUG pair before frame
quantisation. It only reorders the 60 existing usable colours: the globally
darkest goes to P0/index1 and the globally brightest goes to P0/index15. No
colour value is added or changed, and transparent index 0 remains zero in all
four rows. Frames are then quantised against this final palette grouping.

| Name | Value | Where | Meaning |
|---|---|---|---|
| `PALTAB_MAX_SEG` | 64 | cfg | Palette-table capacity (segments). Main-RAM table = `PALTAB_MAX_SEG * 128 B` (8 KB at `PALTAB_RAM` 0xFFB000). Build-asserted equal to the player's `.equ PALTAB_MAX_SEG`. |
| `PALTAB_OFF` | 0xB000 | sp / ip | Word-RAM staging offset for the table at boot (must agree between the two CPUs; build-checked). Staging room caps the hard limit at 160 segments; the 1-byte `pal` ref caps it at 255. |
| PALTAB sectors | `ceil(n_seg * 128 / 2048)` | pack | Region size; 16 segments per sector (op/ed both fit in 1). |
| P0/index1 | global minimum `R + G + B` among 60 usable colours | sim -> pack / ip | Fixed opaque HUD background colour. The packer rejects non-canonical decision logs. |
| P0/index15 | global maximum `R + G + B` among 60 usable colours | sim -> pack / ip | Fixed font colour. Whole-row and within-row swaps are mirrored in tile attributes and indices; pack rejects non-canonical decision logs. |

## B. Cold cap (quality vs. sector slip) — the main quality lever

"Cold" = 32-byte tile patterns newly written to VRAM this frame, whether their
physical source is Prg, Wr0, Wr1, or Main (as opposed to reusing a resident
tile). More cold gives the encoder more exact updates, but the player still has
a measured per-frame processing ceiling.

The cap is selected from `av_config.COLD_CAP_QUALIFICATIONS` by display mode,
nominal fps, and active picture-tile count, and shared by profile validation,
sim, pack, and analysis. All three conditions must exactly match a measured
tuple. A result measured for a larger or smaller active picture is not reused,
even at the same mode and fps. If no exact measurement exists, profile loading
stops with `cold-cap measurement required`; there is no scaled/default fallback
and no per-source environment override.

| Mode | fps | Measured active tiles | Qualified cap |
|---|---:|---:|---:|
| H32 | 24 | 896 | 219 |
| H32 | 30 | 896 | 175 |
| H40 | 15 | 720 | 400 |
| H40 | 15 | 1,040 | 400 |
| H40 | 24 | 1,120 | 200 |
| H40 | 30 | 1,120 | 178 |

For example, H40/15 at 720 or 1,040 active tiles uses its respective measured
cap of 400. H40/15 at 900 or 1,120 tiles has no exact measurement and is
rejected until that tuple is measured. The sim and pack use ONE tile allocator
(`tools/tile_alloc.py`), so the pack's **realized cold == the selected cap
exactly** (the old +overhead from LRU-vs-contig re-loads is gone).

| Name | Value | Where | Meaning |
|---|---|---|---|
| cap `cold_cap_for_fps` | selected from the measured table above | cfg (auto) | **Per-frame cold cap** selected only by an exact mode/fps/active-tile match. A missing tuple is an error. |
| realized cold | at most the mode/fps/active-tile cap | pack (measured) | Uses the shared two-pass allocator. The pack asserts `realized <= cap` as a guard. `COLD_CAP_REALIZED` / `CBRSIM_COLD_CAP_REALIZED` are removed. |

The H40/15 fps/720-active-tile value of 400 is full-length-qualified with the
2,293-frame Machi OP stream. Its 320x130 picture touches 40x18 tile cells after
being placed at y=47 in the 320x224 raster; the remaining rows are confirmed
black across every master frame. The packed stream had no ring underrun and
decoded exactly; the DEBUG recording kept `S=0`, `D=0`, and `R=0`, with at most
two Main-CPU VBlank waits. This result applies only to exactly 720 active tiles;
the separate 1,040-tile measurement below applies only to exactly 1,040.

The H40/30 fps/1,120-active-tile value of 178 is full-length-qualified with the
2,714-frame Sonic Jam OP stream. The pack had `under=0`, exact reconstruction,
and a 17-pattern minimum ready payload. The DEBUG recording kept `S=0`, `D=0`,
`R=0`, and `C=0`; every one of the 2,713 timed frame intervals was exactly two
VBlanks. Cold 179 inserted one extra scanout between frames 30 and 31, while
200 depleted delivery margin and held `S=2` from frame 2,126 onward. This value
is specific to H40, 30 fps, and the full 1,120-tile raster.

The H40/15 fps/1,040-active-tile value of 400 is full-length-qualified with the
3,998-frame Machi ED stream. Its 320x204 picture touches 40x26 tile cells after
being placed at y=10 in the 320x224 raster. The pack had `under=0`, a one-pattern
minimum ready payload, and exact reconstruction. Across the 3,997 timed DEBUG
HUD groups, `S`, `D`, and `R` stayed zero, Main-CPU VBlank waits were at most
two, cold-run count was at most 221, and the longest pattern-update interval was
1,648 ticks (50.63 ms). The lossless recording passed packet, frame, audio, and
extracted-frame checks. Full 1,120-active-tile H40/15 has no exact
qualification and is rejected until it is measured.

## C. Audio sync throttles

RF5C164 playback is a fixed rate, so playback must trail the write pointer by a lead. If the
lead drifts out of `[SYNC_MIN, SYNC_MAX]`, the writer jumps (a re-sync = an
audible click). See the `R`/`L` HUD readouts below.

Both `pcm13` and `adpcm22` are supported profile choices. `adpcm22` is the
default for new profiles and for direct sim runs that do not set
`CBRSIM_AUDIO`. ADPCM22 implementation is complete and H40 Sonic is full-length
emulator- and listening-qualified. Machi OP's H40/15 raster with 720 active
tiles, Machi ED's H40/15 raster with 1,040 active tiles, and the v10 four-supply
Bad Apple H40/30 raster with 1,120 active tiles completed full recording, HUD,
stream, and replay-equivalence checks. Physical hardware and the other
cadence/mode combinations remain broader compatibility checks.

Low-rate ADPCM chunks need one extra streaming safeguard. An N4 decode is about
16 ms, longer than the 13.3 ms interval between CD sectors, so the Sub CPU polls
the CDC during the decode at intervals of at most 512 packed bytes. The
profile-specialized 24/30 fps decoder omits that counter and call entirely.

| Name | Value | Where | Meaning |
|---|---|---|---|
| `audio.kind` | `pcm13` or `adpcm22` | TOML -> sim / pack / player | `pcm13` stores RF5C164 bytes directly. `adpcm22` extracts 22.05 kHz signed 16-bit mono, then stores checkpointed continuous IMA codes in live controls. |
| sim playback WAV | `stats.npz:audio_playback_file` | sim / analysis | ADPCM22's waveform and mux use the shared packer-reference encode/decode result after RF5C164 8-bit conversion. The separate signed-16 source WAV remains the packer input. |
| decoded `AUDIO_BYTES` | PCM13: 888 / 555 / 444 at 15 / 24 / 30 fps; ADPCM22: normally 1472 / 920 / 736 samples | sp / pack | Fixed decoded RF5C164 samples per frame, rounded to the effective playback cadence; ADPCM counts are even. The packer evenly retimes the source WAV to this fixed total. |
| control audio bytes | PCM13: `AUDIO_BYTES`; ADPCM22: `4 + AUDIO_BYTES/2` | pack / sp | ADPCM's four bytes are a signed predictor, step index, and reserved zero. H40/N2 is 372 control bytes for 736 decoded samples. |
| `audio_fd` | header offset 58 | pack / sp | RF5C164 frequency delta derived from decoded samples per frame times the actual playback cadence. H40/N2 ADPCM uses `0x056C`; deriving it avoids wave-RAM lead drift and repeated re-syncs. |
| ADPCM full table | 8,800 B at Word-RAM `+0x12800`, copied to both physical banks | pack / sp | Five sectors after PALTAB contain next-index, signed-delta, and RF5C164-output tables. Boot duplicates them once; timed decode never copies tables across a bank handoff. |
| ADPCM PCM buffer | 1,536 B reserved at Word-RAM `+0x14C00`, per physical bank | sp | Holds one reconstructed chunk before the existing batched wave-RAM writer. |
| `SYNC_LEAD` | 0x3000 (12288 B, ~0.92 s) | sp | Write-ahead lead in wave RAM. PCM starts at this address; the ring's initial silence is not played, so the first source sample aligns with the first visible movie frame. |
| `pack.startup_audio_frames` | requested 30 | TOML -> decision log -> pack/sp | Persistent decoded-PCM prefetch. It is clamped by wave-RAM capacity and decoded chunk size; H40/N2 ADPCM queues 19 chunks. The next source chunk goes in frame 0's live control. Playback still begins with chunk 0 at frame 0. |
| `SYNC_MIN` | 0 (0 B) | sp | Lower lead bound. The persistent prefetch should keep the writer far above it; reaching zero indicates a real supply or clock problem. |
| `SYNC_MAX` | 0x6800 (26624 B, ~2.0 s) | sp | Upper lead bound. Above it -> re-sync. |
| `WAVE_RING_END` | 0x8000 (32 KB) | sp | RF5C164 wave-RAM ring size. |

## D. CD pump throttles (keeping the Sub from dropping sectors)

Startup is deliberately two-phase: read `HEADER.DAT` through PREBUFFER, fully
expand frame 0 after that request ends, then start one continuous `BODY.DAT`
read at frame 1. The steady read delivers 75 sectors/s, so the Sub must drain it
continuously.
`pump_poll` grabs one ready sector if the receivers have room.

| Name | Value | Where | Meaning |
|---|---|---|---|
| pump_poll frequency | every 64 entries at <=20 fps; one end poll for a non-empty 24-30 fps descriptor frame | sp `expand_frame` | Runtime-selected cadence. A high-fps block with at most 1024 updates consumes packed cold-run descriptors directly and preserves the old end-of-frame poll. Larger H40 blocks and <=20fps streams retain the entry walker. Frame 0 has no active `BODY.DAT` read. |
| ring-full skip | occ >= 424 KB (`RING_SIZE-0x1000`) | sp `pump_poll` | Skip draining if the ring is this full (back-pressure). |
| apply-full skip | occ >= 30 KB (`APPLY_SIZE-0x1000`) | sp `pump_poll` | Skip draining if the apply ring is this full. |
| `FRAME_SECTORS` | max 5 | pack -> sp (`cur_fsec`) | Routing-byte maximum. With `FEATURE_FIXED_N2`, 400 frames receive exactly 1001 sectors: 199 two-sector and 201 three-sector allowances. Feature-clear 24fps and 15fps retain the delivery-paced 75/fps schedule (3.125 and 5 sectors/frame). In v6+ each `BODY.DAT` slot is control / future payload / pad; v7+ packs the control and total counts into one routing byte. |
| `HEADER_SECTORS` | 1 | sp / pack | The fixed metadata sector at the start of `HEADER.DAT`; PALTAB, optional ADPCM tables, the v10 WordBuf0 / WordBuf1 / MainBuf boot-pattern regions, startup audio, frame 0, routing, and PREBUFFER follow it in the same file. |
| `FEATURE_COLD_RUNS` | header bit 0 at offset 62 | pack / sp | Appends `(slot_start,count)` cold-run descriptors after each aligned audio chunk. At 24fps or above, the Sub copies eligible blocks by these runs instead of scanning every update entry again. Old streams use the entry fallback; old players ignore the suffix via `total_len`. |
| `FEATURE_FIXED_N2` | header bit 1 at offset 62 (v8) | pack / sp / ip | Authoritative fixed-cadence contract. Main forces one flip every two VBlanks and Sub selects the matching 1001/400 sector accumulator. The packer sets it only when `uses_fixed_n2_cadence(fps)` is true; 24fps leaves it clear despite its N=2 hint. |
| `FEATURE_ADPCM22` | header bit 2 at offset 62 (v9) | pack / sp | Live controls use checkpointed IMA ADPCM, the full-table boot region is present, and `audio_bytes` means decoded samples. |
| Word-RAM swap completion | DMNA bit 1 | sp `swap_settle` | Poll the hardware's 1M bank-switch busy flag. The former fixed `0x400` loop burned about 0.82 ms after every frame even when the switch was already complete. |

## E. VDP DMA budget (Main CPU)

| Name | Value | Where | Meaning |
|---|---|---|---|
| `VB_WORDS_H40` | 3400 words/VBlank | ip | H40 per-VBlank DMA word budget (conservative vs. ~3895 theoretical). |
| `VB_WORDS_H32` | 2800 words/VBlank | ip | H32 per-VBlank DMA word budget. |
| fixed N2 cadence | `FEATURE_FIXED_N2` (v8) | pack / sp / ip | Main flips every exactly two VBlanks. The paired Sub schedule is 1001/400 sectors/frame, so CD delivery does not run ahead of the fixed display clock. This feature bit is authoritative; `vsync_n` alone never enables the path. Current 24fps and 15fps streams leave it clear and remain delivery-paced. |
| `MAIN_CODEGEN_BASE..LIMIT` | 17.5 KB (`0xFF2000..0xFF65FF`) | ip | Reserved for Main-CPU code generated once after header setup. The H40 maximum currently ends at `0xFF6580`; `MainBuf` begins at `0xFF6600`, leaving a 128-byte guard. |
| `RUN_TABLE` | 1536 records by address range; current cold cap is much lower | ip | `(dst, len, src)` table of contiguous cold-slot runs. Each record is counted by H40 HUD `N`; a one- or two-tile record uses CPU writes, while a longer record can become one or more DMA commands at VBlank boundaries. |

## F. CBR / transfer rate

| Name | Value | Where | Meaning |
|---|---|---|---|
| `CD_RATE` | 153600 B/s | sim | CD 1x — the absolute delivery ceiling. |
| `TARGET_RATE` (`encoder.rate_kib`) | 144 KiB/s | TOML -> sim | The CBR target rate. |
| `FRAME_BYTES` | `TARGET_RATE / FPS` (~10 KB) | sim | Fixed per-frame CBR byte budget. |
| `SECTOR` / `PAT` / `PAT_PER_SEC` | 2048 / 32 / 64 | pack | Sector = 2 KB, one tile pattern = 32 B, so 64 tiles per sector. |

## G. Encoder quality knobs

Per-cell the sim picks: Raw (accurate load charged to the current-frame
allowance), Same, Near/Coa/Flbk (reuse a resident tile), Buf (accurate load
funded by saved whole-movie allowance or a boot-preload credit), or Miss. Raw
and Buf are quality-funding classes; Prg/Wr0/Wr1/Main independently records the
physical source. These thresholds steer the choice.
Frequently changed profile values use their TOML names below. The remaining
`CBRSIM_*` variables are advanced shared experiments; do not put per-movie
values in this document.

| Name | Default | Meaning |
|---|---|---|
| `encoder.vram_tiles` | 1400 | Resident tile pool size (LRU). |
| `CBRSIM_COA_DETAIL` / `_MEAN` / `_MAX` / `_K` | 0.7 / 4 / 8 / 24 | Coa = reuse a resident tile whose low-frequency look matches a flat cold tile (detail below DETAIL; 2x2 mean color diff within MEAN/MAX; check K newest candidates). |
| `CBRSIM_NEAR_YM` / `_YP` / `_C` | 10 / 28 / 24 | Near = reuse an almost-identical resident tile (mean/max luma diff, mean chroma diff). |
| `CBRSIM_FLBK_IMPROVE_ONLY` / `_MIN_IMPROVE` | 1 / 0 | Flbk = fill a Miss with a resident tile only if it improves the picture. |
| `CBRSIM_TFLBK_YM` / `_YP` / `_C` | 120 / 252 / 200 | Flbk match thresholds (loose — a coarse fill beats a hole). |
| `AGING_ALPHA` / `WAIT_CAP` | 0.6 / 10 | Priority boost per waited frame, saturating at WAIT_CAP frames. |
| `encoder.dither` / `encoder.segment_palettes` | on / on | Dithering / per-segment palette swaps. |
| `palette.algorithm` | `stl4` | Palette-line selector. `stl4` is the legacy segmented four-line Tile-Lloyd learner; `mosaic-gm` starts at one shared-core line and grows/merges only when validation improves. A selected one-line candidate receives a complete flattened-RGB333 histogram refinement and all-frame error proof before segment palettes are considered. |
| `palette.map_weight` | 1.0 | MOSAIC-GM penalty for mapping the same RGB333 source colour differently on different palette lines. |
| `palette.seam_weight` / `palette.seam_iterations` | 8.0 / 2 | MOSAIC-GM spatial assignment cost for a quantization discontinuity introduced at an 8x8 boundary, and deterministic checkerboard passes. Real source edges are excluded from the cost. |
| `CBRSIM_PAL_GROW_REL` / `_ABS` / `_MIN_USAGE` | 0.005 / 0.002 / 0.002 | Minimum relative gain, gain per pixel, and tile-use fraction required to add another MOSAIC-GM line. |
| `CBRSIM_PAL_CORE_SIZES` | `4,6,8,10,12,14` | Shared-colour counts tried when a specialist line grows. The remaining slots are line-specific. |
| `palette.sample_counts` / `palette.validate_frames` | `[120,240,480]` / 120 | Whole-movie learning candidates and the separate validation sample used to select among them. |
| `palette.segment_train_frames` / `palette.segment_validate_frames` | 240 / 60 | Maximum learning/validation frames per dark or uniform CRAM-segment candidate. |
| `palette.segment_gain_relative` / `palette.segment_gain_per_pixel` | 0.005 / 0.002 | Improvement required before a local segment palette replaces the selected global palette. Adjacent identical choices are merged. |

After quantization, the encoder dry-runs the exact target through the shared
VRAM allocator and predicts each frame's name-table and cold-pattern demand.
It first water-fills the finite WordBuf0/WordBuf1/MainBuf boot credits across
predicted bursts, then a backwards pass derives the minimum offline quality
reserve needed after every frame. This is the only quality-budget allocation
path.

Optional Raw/Buf upgrades protect against the complete exact-demand trace.
Normal exact updates use a narrower Miss-risk trace: source changes that fit
the existing Coa visual bound are excluded because they can degrade gracefully
to resident reuse, while changes beyond Coa reserve quality allowance against future Flbk
and Miss bursts. The risk trace is independent from optional quality spending.
Both curves end at zero by definition, so the useful tail naturally releases
the quality budget without a separate end-of-movie rule. The physical PrgBuf
sector schedule remains a separate exact proof in `stream_schedule.py`. See
[`BUEFFERING.md`](BUEFFERING.md) for the complete planning flow and validation.

Schema-4 `buffer_remaining.npz` records `prg_remaining`, `wr0_remaining`,
`wr1_remaining`, and `main_remaining` plus the matching capacities and
per-frame loads. `quality_budget_remaining` is diagnostic only and is not one
of the four analysis meters.

`buffer_remaining.npz` also stores the physical BODY delivery-slot trace:
`body_useful_payload_bytes`, `body_useful_control_bytes`, `body_pad_bytes`, and
`body_physical_bytes`. The four values are pack-verified for every slot, with
useful payload + useful control + pad equal to physical bytes. Analysis Band
divides the first two by each slot's physical CD read time, so it ranges from
0 to the CD-1x limit of 150 KiB/s. `report.txt:body_useful_bps` divides the
whole-series useful total by the whole-series physical read time;
`codec_work_bps` is the separate encoder quality-allocation diagnostic.

## H. Per-source TOML profiles

Use one `schema_version = 1` TOML file per source/mode combination. Examples are
[`configs/bad-apple-h32.toml`](configs/bad-apple-h32.toml) and
[`configs/bad-apple-h40.toml`](configs/bad-apple-h40.toml). The profile is the
human-edited input; `CBRSIM_*` is only the encoder's internal compatibility
layer.

```sh
python tools/sim.py --config configs/bad-apple-h32.toml
python tools/render_analysis.py --config configs/bad-apple-h32.toml
python tools/pack_stream.py --config configs/bad-apple-h32.toml --verify
make disc CONFIG=configs/bad-apple-h32.toml DEBUG=1
```

`MAIN_CODEGEN=1` is the default Main-CPU bitmap handler generator. It
emits code once after header setup and falls back to the reference bit loop if
its runtime size/range checks fail. Set `MAIN_CODEGEN=0` only for a reference
bit-loop A/B build.

`DMA_RUN_FASTPATH=1` is the default Main pattern-transfer path. One- and
two-tile cold runs use direct CPU writes from Word RAM, while longer runs retain
Word-RAM DMA with the required first-word repair and reuse its destination
command. `DMA_RUN_FASTPATH=0` is an all-DMA diagnostic fallback for A/B builds;
it does not change the packed stream or encoded image.

`PLAYER_SPECIALIZE=1` is the default disc-specific player build. The packer
writes `player_constants.inc` beside `HEADER.DAT`; both player objects depend on
that generated file, and the Sub CPU verifies the matching fixed-header
signature before using any immediate. Set `PLAYER_SPECIALIZE=0` only for the
generic runtime-header A/B player. For the Sonic H32 profile the specialized Sub
text is 3,754 bytes in Release and 3,804 bytes in DEBUG, versus 4,034 and 4,084
bytes for the generic path. Both remain below the 4,096-byte boot-SP limit; any
future change must check the DEBUG size as well as Release.

The same generated constants specialize the Main object. For Sonic H32 the
complete IP binary is 5,024 bytes in Release and 5,216 bytes in DEBUG, versus
5,360 and 5,584 bytes for the generic build. The existing runtime bitmap-handler
and name-table code generation remains enabled; specialization
removes the remaining per-frame RAM reads and the zero `col0` additions around
that generated fast path.

`sim.py` resolves the profile once and stores the exact geometry, timing, audio,
stream, hardware, palette, and pack settings plus the TOML SHA-256 in
`decisions.pkl`. `pack_stream.py` then uses that frozen configuration only. It
does not import `sim.py` and does not read per-source `CBRSIM_*` values. When
`--config` is supplied to the packer, its hash must match the one recorded by
the sim; editing a TOML after simulation requires a new sim run.

The TOML filename is also the build-artifact identity. For example,
`configs/bad-apple-h32.toml` writes the packed stream under
`out/bad-apple-h32/`, keeps assembler objects, binaries, disc staging, and the
default headless-emulator scratch area under `tmp/bad-apple-h32/`, then builds
`out/bad-apple-h32.iso` and `out/bad-apple-h32.cue`. The CUE references that
same ISO basename. This is derived rather than configurable, so two profiles
cannot silently overwrite one shared image or one shared temporary directory.
`HEADER.DAT` and `BODY.DAT` keep their fixed names inside the artifact directory
and on the disc because those are TTRC format names read by the player.

| TOML table | Keys | Meaning |
|---|---|---|
| `[source]` | `path`, `fps`, `duration`, optional `sar` | Input identity and native timing. `sar` repairs missing/wrong source metadata; it does not crop. |
| `[source.preprocess.endpoint_snap]` | `black_max`, `white_min` | Optional RGB888 source preprocessing before denoise, geometry conversion, and encoding. Each RGB channel at or below `black_max` becomes 0; each channel at or above `white_min` becomes 255; middle values remain unchanged. Omitting the table disables it. |
| `[video]` | `mode`, `width`, `height`, `fit`, optional `active_tiles`, `resize_filter`, `master_denoise`, `master_filter`, `raw_filter` | Sega output raster and HAR-aware conversion. `active_tiles` counts tiles that are ever non-black after conversion, including partially covered boundary tiles. Omit it for the conservative full-grid count; when it reduces that count, sim scans every master frame and rejects a mismatch. `fit="pad"` preserves every source pixel and adds bars when the displayed aspects differ. `fit="crop"` is an explicit object-fit-cover conversion: it fills the complete output raster while preserving displayed aspect, so it may discard active pixels at the outer source edges. `resize_filter` defaults to `lanczos`; `master_denoise` defaults to `true` and controls the master-only upscale, denoise, and blur pass. H32 uses PAR 8:7 and H40 uses 32:35. |
| `[audio]` | `kind` | Write `adpcm22` for the default 22.05 kHz Sub-CPU IMA path. Use `pcm13` for the physical-console-qualified 13.3 kHz fallback. The strict profile keeps this choice explicit. |
| `[output]` | `directory`, `reuse`, `emit_decisions` | Sim work directory, decoded-input reuse, and decision-log emission. Normal hardware work sets `emit_decisions=true`. |
| `[encoder]` | `gpu`, `rate_kib`, `vram_tiles`, `dither`, `segment_palettes`, `near`, `coa` | Common codec controls. GPU is the default; CPU fallback remains automatic. |
| `[palette]` | `algorithm`, sampling/validation keys, MOSAIC-GM seam keys | Palette-selection algorithm and its training controls. |
| `[pack]` | `debug`, `fill`, `startup_audio_frames` | Disc-generation choices frozen with the encode. `debug=true` is the normal recording build. `fill=true` replaces CD-1x padding with useful future payload where proven safe. Output paths are derived from the TOML filename. |

Schema v1 still accepts the old `pack.output` key so existing authenticated
profiles and decision logs remain usable, but configured pack runs ignore its
value. New path selection always uses the TOML filename.

The profile loader is strict: misspelled sections/keys, unsupported display
modes, non-tile-aligned dimensions, and unsafe TOML filename characters fail
immediately. Profile values replace
inherited per-source environment values unconditionally. Shared hardware
limits such as PrgBuf size, quality-budget capacity, preload capacities, and the measured cold-cap table
stay in `tools/av_config.py`; they are deliberately not per-source TOML fields.
`video.active_tiles` supplies source geometry to that shared selector, not a
per-source cap override. Profile loading fails before encoding when no measured
tuple exactly matches the requested mode, fps, and active-tile count.

## Diagnostic HUD readouts (DEBUG=1 builds)

Not settings, but the live readouts of the throttles above — a single top row
on the VDP Window plane (`prepare_dbg` / `publish_dbg` in ip, read back by
`tools/read_frameno.py: read_hud`). Keeping the HUD on Window separates it from
the two alternating video name tables, so a video-plane flip cannot make the
text disappear for one frame.

The player draws values only, with no category letters or separators. H32 uses
22 cells: `xxxx xx xx xx xx xx xx xx xx xx`. H40 keeps that prefix and appends
`xxxx xx`, for 28 cells total. The fixed interpretation order is
`F/P/S/D/R/L/C/W/M/A`, followed by H40-only `U/N`. `F` and `U` are four
hexadecimal digits; `L` shows the high byte of the lead, and the other fields
show their low byte. Two-digit fields wrap naturally from `FF` to `00`.

The shared font asset uses source index 0 for its background and source index 1
for set pixels. The movie player expands them once to P0/index1 and P0/index15
while uploading the font to VRAM. The result is an opaque darkest-colour HUD
background with brightest-colour text in every palette segment, with no
per-frame font scan, recolour, DMA, or additional VBlank wait.

The occupied value cells cover the video visually, but a DEBUG build still
updates the hidden video name-table row and all other rows exactly as a release
build. The complete Window row is initialized to transparent tile 0; only the
22 H32 or 28 H40 value cells are overwritten with opaque font tiles each
frame. Thus H40's unused right-hand 12 cells show the movie instead of black.
The values are formatted into a Main-RAM row before the display deadline, then
published with 11 H32 or 14 H40 longword writes to the inactive one of two
Window name tables at VRAM `0xD000` and `0xF000`. The final control-port
longword switches the picture name table and Window name table together. A
terminal-VBlank guard rejects V-counter lines `0xFC..0xFF` and waits for a fresh
blank before that paired write, closing the end-of-blank race without adding a
third scanout. This adds no DMA and does not branch on whether the video starts
at row 0. In DEBUG builds,
the old slip-triggered CRAM0 red border is disabled; slips remain
visible in `Sxx`, while the HUD colours stay stable. Release builds retain the
red indicator because they do not have the HUD.

| Position key | Digits | Meaning |
|---|---|---|
| `F` | 4 | 16-bit frame number. |
| `P` | 2 | Low byte of the palette segment. |
| `S` | 2 | Low byte of the CD sector-slip count (re-seek recoveries). 0 = clean video. |
| `D` | 2 | Low byte of the stream-desync count. 0 = clean. |
| `R` | 2 | Low byte of the audio re-sync count (lead left `[SYNC_MIN, SYNC_MAX]`). 0 is ideal; each increment is a write-pointer jump. |
| `L` | 2 | High byte of the current audio lead (write - play), in 256-byte units. Approaching `00` means the startup reserve is draining. |
| `C` | 2 | Blocking CD pumps needed before the current control could run, including an older BODY slot. Zero means delivery was already armed. |
| `W` | 2 | Approximate Main-CPU wait for Sub completion at `CMD_SWAP`, in V-counter scanlines. It wraps at 256, so use it as a short-wait diagnostic rather than an absolute stopwatch. |
| `M` | 2 | VBlank starts waited by the Main pattern path this frame. Values of 2 or more prove an extra VBlank spill. |
| `A` | 2 | Sub ADPCM decode phase time. One displayed unit is four 30.72 us stopwatch ticks (about 0.1229 ms); PCM builds display zero. H40 Sonic ADPCM measured `3E..42`, about 7.62..8.11 ms. At low frame rates this phase includes any opportunistic CDC pump performed inside the longer decode. |
| `U` | 4 (H40) | Main pattern-transfer time in Mega-CD stopwatch ticks, measured from the first run through the final DMA repair or CPU-direct write. One tick is 30.72 us; the 12-bit counter wraps after 4096 ticks (about 125.83 ms). |
| `N` | 2 (H40) | Low byte of the source-aware packed cold-run descriptor count for this frame. This is the fragmentation count before a long run is split by the VBlank word budget and wraps at 256. |
