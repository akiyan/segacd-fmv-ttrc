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

## A. Streaming buffer (ring / tank)

The ring is the PRG-RAM area that holds prefetched tile payload. The sim's "tank"
models the same usable buffer so a schedule it calls feasible actually is.

| Name | Value | Where | Meaning |
|---|---|---|---|
| `RING_SIZE` / `RING_SIZE_KB` | 428 KB (0x6B000) | sp / cfg | Physical PRG ring. It fills the complete safe range from 0x0C000 up to `APPLY_BASE` after routing moved to Word RAM. |
| `RING_JITTER_MARGIN_KB` | 40 KB | cfg | Headroom for real CD-delivery jitter, subtracted from the physical ring. |
| `RING_CAP_KB` | 388 KB (derived) | cfg -> pack | Pack schedule / prefetch cap = `RING_SIZE_KB - RING_JITTER_MARGIN_KB`. |
| `TANK_KB` | 388 KB (derived) | cfg -> sim | Sim VBV tank = usable ring. How much bandwidth a heavy frame may borrow. (Was wrongly 440 = larger than the ring.) |
| `BACKPRESSURE_KB` | 424 KB (`RING_SIZE-4`) | cfg | Where `pump_poll` stops draining the CDC to avoid overrunning the ring. `RING_CAP` must stay below it. |
| routing table | 16 KB per 1M Word-RAM bank, 16384 frames (v7+) | sp / pack | One byte per frame: bits 0-2 are control sectors, bits 3-5 are total control-plus-payload sectors, and bits 6-7 must be zero. `routing_sec` is exactly `ceil(frames / 2048)`. v8 retains the v7 one-byte layout. The table is copied identically into both banks at boot, so the Sub can read it regardless of delivery/display frame parity. v6 used two bytes per frame and was limited to 8192 frames. |
| `APPLY_SIZE` | 34 KB (0x8800) | sp | Control-block apply ring (the per-frame update/cram/audio blocks). |
| prebuffer | fills ring to `RING_CAP` | pack | Final region of `HEADER.DAT`; a boot-time burst that fills the ring before frame 1 so bursts are pre-buffered. |
| frame-0 boot staging | 36 KB max in the 40 KB jitter tail | sp | Frame 0 is temporarily stored at `RING_CAP` and expanded before BODY streaming reuses those PRG-RAM bytes. |

DEBUG uses the Window name table for one opaque HUD row. It still updates every
video row behind that Window, so diagnostic playback has the same video-name-table
work as a release build. The encoder only reorders existing CRAM colours to keep
palette 0 index 1 globally darkest (HUD background) and index 15 globally
brightest (HUD text); it does not alter either colour value.

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

"Cold" = fresh 32-byte tile patterns loaded from CD this frame (vs. reuse of a
resident tile). More cold = sharper picture, but a heavy frame that loads too
many can overrun the CDC (a sector slip).

The cap is **auto-derived** from `av_config.cold_cap_for_fps` by display mode and
fps, and shared by the sim and the pack — no manual per-source env. The common
limits are 15fps→350 / 24fps→219 / 30fps→175. Full-raster H40 at exactly 24fps
is the measured exception at 200: Lunar measured S=2 at 219 and S=0 at 200.
That exception is not extrapolated to 15fps or 30fps because 24fps alternates
between two and three VBLANKs per frame, while 30fps gets a steady two. The sim
and pack use
ONE tile allocator (`tools/tile_alloc.py`), so the pack's **realized cold == the cap
exactly** (the old +overhead from LRU-vs-contig re-loads is gone).

| Name | Value | Where | Meaning |
|---|---|---|---|
| cap `cold_cap_for_fps` | 350/219/175 at 15/24/30fps; H40 24fps only: 200 | cfg (auto) | **Per-frame cold cap** = the 15fps reference scaled by `15/fps`, plus the measured H40/24fps exception. Applied by the sim; the pack ships exactly this. |
| `CBRSIM_MAX_COLD` | (unset = auto) | sim (env) | Optional override of the auto cap for special cases only; normally leave unset. |
| realized cold | at most the mode/fps cap | pack (measured) | Uses the shared two-pass allocator. The pack asserts `realized <= cap` as a guard. `COLD_CAP_REALIZED` / `CBRSIM_COLD_CAP_REALIZED` are removed. |

## C. Audio sync throttles

PCM is a fixed rate, so playback must trail the write pointer by a lead. If the
lead drifts out of `[SYNC_MIN, SYNC_MAX]`, the writer jumps (a re-sync = an
audible click). See the `R`/`L` HUD readouts below.

| Name | Value | Where | Meaning |
|---|---|---|---|
| `AUDIO_BYTES` / `AUDIO` | 888 B at ~15 fps; 555 B at delivery-paced 24 fps; 444 B at ~30 fps | sp / pack | Fixed PCM bytes per frame, rounded up against the effective playback cadence. Integer-VBlank rates use the exact NTSC cadence (14.985/29.97); rates such as 24 fps stay CD-delivery-paced and are not rounded to 29.97. FD=0x0345 consumes about 13,303.76 samples/s. The packer evenly retimes the source WAV to the fixed-chunk total instead of padding only the tail. |
| `SYNC_LEAD` | 0x3000 (12288 B, ~0.92 s) | sp | Write-ahead lead in wave RAM. PCM starts at this address; the ring's initial silence is not played, so the first source sample aligns with the first visible movie frame. |
| `pack.startup_audio_frames` | 30 | TOML -> decision log -> pack/sp | Persistent audio prefetch. `HEADER.DAT` queues source chunks 0-29; control frame 0 carries chunk 30, frame 1 carries 31, and so on. Playback still begins with chunk 0 at frame 0, while the writer remains about 30 frames ahead instead of consuming the reserve by skipping duplicate chunks. |
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
| `FRAME_SECTORS` | max 5 | pack -> sp (`cur_fsec`) | Routing-byte maximum. With v8 `FEATURE_FIXED_N2`, 400 frames receive exactly 1001 sectors: 199 two-sector and 201 three-sector allowances. Feature-clear 24fps and 15fps retain the delivery-paced 75/fps schedule (3.125 and 5 sectors/frame). In v6+ each `BODY.DAT` slot is control / future payload / pad; v7+ packs the control and total counts into one routing byte. |
| `HEADER_SECTORS` | 1 | sp / pack | The fixed metadata sector at the start of `HEADER.DAT`; PALTAB, startup audio, frame 0, routing, and PREBUFFER follow it in the same file. |
| `FEATURE_COLD_RUNS` | header bit 0 at offset 62 | pack / sp | Appends `(slot_start,count)` cold-run descriptors after each aligned audio chunk. At 24fps or above, the Sub copies eligible blocks by these runs instead of scanning every update entry again. Old streams use the entry fallback; old players ignore the suffix via `total_len`. |
| `FEATURE_FIXED_N2` | header bit 1 at offset 62 (v8) | pack / sp / ip | Authoritative fixed-cadence contract. Main forces one flip every two VBlanks and Sub selects the matching 1001/400 sector accumulator. The packer sets it only when `uses_fixed_n2_cadence(fps)` is true; 24fps leaves it clear despite its N=2 hint. |
| Word-RAM swap completion | DMNA bit 1 | sp `swap_settle` | Poll the hardware's 1M bank-switch busy flag. The former fixed `0x400` loop burned about 0.82 ms after every frame even when the switch was already complete. |

## E. VDP DMA budget (Main CPU)

| Name | Value | Where | Meaning |
|---|---|---|---|
| `VB_WORDS_H40` | 3400 words/VBlank | ip | H40 per-VBlank DMA word budget (conservative vs. ~3895 theoretical). |
| `VB_WORDS_H32` | 2800 words/VBlank | ip | H32 per-VBlank DMA word budget. |
| fixed N2 cadence | `FEATURE_FIXED_N2` (v8) | pack / sp / ip | Main flips every exactly two VBlanks. The paired Sub schedule is 1001/400 sectors/frame, so CD delivery does not run ahead of the fixed display clock. This feature bit is authoritative; `vsync_n` alone never enables the path. Current 24fps and 15fps streams leave it clear and remain delivery-paced. |
| `MAIN_CODEGEN_BASE..LIMIT` | 24 KB (`0xFF2000..0xFF7FFF`) | ip | Reserved for Main-CPU code generated once after header setup. The former tile staging use is obsolete because pattern DMA reads Word RAM directly. |
| `RUN_TABLE` | 1536 records by address range; current cold cap is much lower | ip | `(dst, len, src)` table of contiguous cold-slot runs. Each record is counted by H40 HUD `N`; a one- or two-tile record uses CPU writes, while a longer record can become one or more DMA commands at VBlank boundaries. |

## F. CBR / transfer rate

| Name | Value | Where | Meaning |
|---|---|---|---|
| `CD_RATE` | 153600 B/s | sim | CD 1x — the absolute delivery ceiling. |
| `TARGET_RATE` (`encoder.rate_kib`) | 144 KiB/s | TOML -> sim | The CBR target rate. |
| `FRAME_BYTES` | `TARGET_RATE / FPS` (~10 KB) | sim | Fixed per-frame CBR byte budget. |
| `SECTOR` / `PAT` / `PAT_PER_SEC` | 2048 / 32 / 64 | pack | Sector = 2 KB, one tile pattern = 32 B, so 64 tiles per sector. |

## G. Encoder quality knobs

Per-cell the sim picks: Raw (fresh CD load), Same, Near/Coa/Flbk (reuse a
resident tile), Buf (prefetched), or Miss. These thresholds steer that choice.
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
| `UPGRADE_NEAR_RESERVE` | 0.7 | Apply Near only when 70%+ of the tile budget is still free. |
| `encoder.dither` / `encoder.segment_palettes` | on / on | Dithering / per-segment palette swaps. |
| `palette.algorithm` | `stl4` | Palette-line selector. `stl4` is the legacy segmented four-line Tile-Lloyd learner; `mosaic-gm` starts at one shared-core line and grows/merges only when validation improves. A selected one-line candidate receives a complete flattened-RGB333 histogram refinement and all-frame error proof before segment palettes are considered. |
| `palette.map_weight` | 1.0 | MOSAIC-GM penalty for mapping the same RGB333 source colour differently on different palette lines. |
| `palette.seam_weight` / `palette.seam_iterations` | 8.0 / 2 | MOSAIC-GM spatial assignment cost for a quantization discontinuity introduced at an 8x8 boundary, and deterministic checkerboard passes. Real source edges are excluded from the cost. |
| `CBRSIM_PAL_GROW_REL` / `_ABS` / `_MIN_USAGE` | 0.005 / 0.002 / 0.002 | Minimum relative gain, gain per pixel, and tile-use fraction required to add another MOSAIC-GM line. |
| `CBRSIM_PAL_CORE_SIZES` | `4,6,8,10,12,14` | Shared-colour counts tried when a specialist line grows. The remaining slots are line-specific. |
| `palette.sample_counts` / `palette.validate_frames` | `[120,240,480]` / 120 | Whole-movie learning candidates and the separate validation sample used to select among them. |
| `palette.segment_train_frames` / `palette.segment_validate_frames` | 240 / 60 | Maximum learning/validation frames per dark or uniform CRAM-segment candidate. |
| `palette.segment_gain_relative` / `palette.segment_gain_per_pixel` | 0.005 / 0.002 | Improvement required before a local segment palette replaces the selected global palette. Adjacent identical choices are merged. |

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

`MAIN_CODEGEN=1` is the default issue #27 Main-CPU bitmap handler generator. It
emits code once after header setup and falls back to the reference bit loop if
its runtime size/range checks fail. Set `MAIN_CODEGEN=0` only for a reference
bit-loop A/B build.

`DMA_RUN_FASTPATH=1` is the default Main pattern-transfer path. One- and
two-tile cold runs use direct CPU writes from Word RAM, while longer runs retain
Word-RAM DMA with the required first-word repair and reuse its destination
command. `DMA_RUN_FASTPATH=0` is an all-DMA diagnostic fallback for A/B builds;
it does not change the packed stream or encoded image.

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
| `[video]` | `mode`, `width`, `height`, `fit`, optional `resize_filter`, `master_denoise`, `master_filter`, `raw_filter` | Sega output raster and HAR-aware conversion. `fit="pad"` preserves every source pixel; use `crop` only for confirmed black margins. `resize_filter` defaults to `lanczos`; `master_denoise` defaults to `true` and controls the master-only upscale, denoise, and blur pass. H32 uses PAR 8:7 and H40 uses 32:35. |
| `[audio]` | `kind` | `pcm13` is the shipping RF5C164 path. |
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
inherited per-source environment values unconditionally. Shared hardware limits
such as ring size, tank size, and the automatic cold cap stay in
`tools/av_config.py`; they are deliberately not per-source TOML fields.

## Diagnostic HUD readouts (DEBUG=1 builds)

Not settings, but the live readouts of the throttles above — a single top row
on the VDP Window plane (`render_dbg` in ip, read back by
`tools/read_frameno.py: read_hud`). Keeping the HUD on Window separates it from
the two alternating video name tables, so a video-plane flip cannot make the
text disappear for one frame.

H32 uses the contiguous 32-cell layout
`FxxxxPxxSxxDxxRxxLxxCxxWxxMxxAxx`. H40 keeps that exact prefix and uses its
eight additional visible cells for `UxxxxNxx`. `F` and `U` are four
hexadecimal digits; `L` shows the high byte of the lead, and the other compact
fields show their low byte. Two-digit fields wrap naturally from `FF` to `00`.

The shared font asset uses source index 0 for its background and source index 1
for set pixels. The movie player expands them once to P0/index1 and P0/index15
while uploading the font to VRAM. The result is an opaque darkest-colour HUD
background with brightest-colour text in every palette segment, with no
per-frame font scan, recolour, DMA, or additional VBlank wait.

The top Window row covers the video visually, but a DEBUG build still updates
the hidden video name-table row and all other rows exactly as a release build.
All Window cells are initialized with the opaque darkest-colour blank glyph;
32 H32 or 40 H40 live HUD cells are then overwritten once per frame. This adds
no DMA and does not branch on whether the video starts at row 0. In DEBUG
builds, the old slip-triggered CRAM0 red border is disabled; slips remain
visible in `Sxx`, while the HUD colours stay stable. Release builds retain the
red indicator because they do not have the HUD.

| Marker | Display | Meaning |
|---|---|---|
| `F` | `Fxxxx` | 16-bit frame number. |
| `P` | `Pxx` | Low byte of the palette segment. |
| `S` | `Sxx` | Low byte of the CD sector-slip count (re-seek recoveries). 0 = clean video. |
| `D` | `Dxx` | Low byte of the stream-desync count. 0 = clean. |
| `R` | `Rxx` | Low byte of the audio re-sync count (lead left `[SYNC_MIN, SYNC_MAX]`). 0 is ideal; each increment is a write-pointer jump. |
| `L` | `Lxx` | High byte of the current audio lead (write - play), in 256-byte units. Approaching `00` means the startup reserve is draining. |
| `C` | `Cxx` | Blocking CD pumps needed before the current control could run, including an older BODY slot. Zero means delivery was already armed. |
| `W` | `Wxx` | Approximate Main-CPU wait for Sub completion at `CMD_SWAP`, in V-counter scanlines. It wraps at 256, so use it as a short-wait diagnostic rather than an absolute stopwatch. |
| `M` | `Mxx` | VBlank starts waited by the Main pattern path this frame. Values of 2 or more prove an extra VBlank spill. |
| `A` | `A00` | Always cleared by the v8 player. Header offset 58 is the obsolete startup-audio duplicate-skip count: the packer writes zero and the player ignores it. |
| `U` | `Uxxxx` (H40) | Main pattern-transfer time in Mega-CD stopwatch ticks, measured from the first run through the final DMA repair or CPU-direct write. One tick is 30.72 us; the 12-bit counter wraps after 4096 ticks (about 125.83 ms). |
| `N` | `Nxx` (H40) | Low byte of the packed cold-run descriptor count for this frame. This is the fragmentation count before a long run is split by the VBlank word budget and wraps at 256. |
