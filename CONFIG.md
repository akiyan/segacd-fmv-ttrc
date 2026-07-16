# CONFIG.md — Tunable settings, throttles and buffers

Reference for the numeric knobs of the Tile Texture Reuse Codec pipeline
(`tools/sim.py` -> `tools/pack_stream.py` -> `boot/movieplay_*.s`). Pure hardware
register addresses and memory-map constants are intentionally omitted; this lists
the values you actually *tune*.

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
| `RING_SIZE` / `RING_SIZE_KB` | 412 KB (0x67000) | sp / cfg | Physical PRG ring. The final 8 KB was reassigned to the long-form routing table. |
| `RING_JITTER_MARGIN_KB` | 40 KB | cfg | Headroom for real CD-delivery jitter, subtracted from the physical ring. |
| `RING_CAP_KB` | 372 KB (derived) | cfg -> pack | Pack schedule / prefetch cap = `RING_SIZE_KB - RING_JITTER_MARGIN_KB`. |
| `TANK_KB` | 372 KB (derived) | cfg -> sim | Sim VBV tank = usable ring. How much bandwidth a heavy frame may borrow. (Was wrongly 440 = larger than the ring.) |
| `BACKPRESSURE_KB` | 408 KB (`RING_SIZE-4`) | cfg | Where `pump_poll` stops draining the CDC to avoid overrunning the ring. `RING_CAP` must stay below it. |
| routing table | 16 KB, 8192 frames | sp / pack | Two bytes per frame. The packer rejects longer streams before they can overwrite the apply ring. |
| `APPLY_SIZE` | 34 KB (0x8800) | sp | Control-block apply ring (the per-frame update/cram/audio blocks). |
| prebuffer | fills ring to `RING_CAP` | pack | Final region of `HEADER.DAT`; a boot-time burst that fills the ring before frame 1 so bursts are pre-buffered. |

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

The cap is **auto-derived** from `av_config.cold_cap_for_fps` (15fps→350, 30fps→175)
and shared by the sim and the pack — no manual per-source env. The sim and pack use
ONE tile allocator (`tools/tile_alloc.py`), so the pack's **realized cold == the cap
exactly** (the old +overhead from LRU-vs-contig re-loads is gone).

| Name | Value | Where | Meaning |
|---|---|---|---|
| cap `cold_cap_for_fps` | 15fps→350, 30fps→175 | cfg (auto) | **Per-frame cold cap** = `COLD_CAP_15FPS(350)·15/fps`. Applied by the sim; the pack ships exactly this. All sources (op/ed/sonic) use it — none uncapped. |
| `CBRSIM_MAX_COLD` | (unset = auto) | sim (env) | Optional override of the auto cap for special cases only; normally leave unset. |
| realized cold | == cap (e.g. op/ed 350, sonic 175) | pack (measured) | Equals the cap by construction (shared two-pass allocator). The pack asserts `realized <= cap` as a guard. `COLD_CAP_REALIZED` / `CBRSIM_COLD_CAP_REALIZED` are removed. |

## C. Audio sync throttles

PCM is a fixed rate, so playback must trail the write pointer by a lead. If the
lead drifts out of `[SYNC_MIN, SYNC_MAX]`, the writer jumps (a re-sync = an
audible click). See the `R`/`L` HUD readouts below.

| Name | Value | Where | Meaning |
|---|---|---|---|
| `AUDIO_BYTES` / `AUDIO` | 888 B at N4 (~15 fps); 444 B at N2 (~30 fps) | sp / pack | Fixed PCM bytes per frame, rounded up against the actual NTSC VBlank cadence. FD=0x0345 consumes about 13,303.76 samples/s while either chunk size supplies about 13,306.69 samples/s, so the reserve grows by only about 2.94 B/s. The packer evenly retimes the source WAV to this fixed-chunk length instead of padding only the tail. |
| `SYNC_LEAD` | 0x3000 (12288 B, ~0.92 s) | sp | Write-ahead lead in wave RAM. PCM starts at this address; the ring's initial silence is not played, so the first source sample aligns with the first visible movie frame. |
| `CBRSIM_STARTUP_AUDIO_FRAMES` | 30 | pack/sp | Persistent audio prefetch. `HEADER.DAT` queues source chunks 0-29; control frame 0 carries chunk 30, frame 1 carries 31, and so on. Playback still begins with chunk 0 at frame 0, while the writer remains about 30 frames ahead instead of consuming the reserve by skipping duplicate chunks. |
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
| pump_poll frequency | every 64 entries at <=20 fps; one end poll for a non-empty 30 fps descriptor frame | sp `expand_frame` | Runtime-selected cadence. A 30fps block with at most 1024 updates consumes packed cold-run descriptors directly and preserves the old end-of-frame poll. Larger H40 blocks and <=20fps streams retain the entry walker. Frame 0 has no active `BODY.DAT` read. |
| ring-full skip | occ >= 416 KB (`RING_SIZE-0x1000`) | sp `pump_poll` | Skip draining if the ring is this full (back-pressure). |
| apply-full skip | occ >= 30 KB (`APPLY_SIZE-0x1000`) | sp `pump_poll` | Skip draining if the apply ring is this full. |
| `FRAME_SECTORS` | max 5 | pack -> sp (`cur_fsec`) | Routing-byte maximum. v4+ uses a rate-matched variable total averaging 75/fps sectors per frame (5 at 15 fps; alternating 2/3 at 30 fps). In v6 each `BODY.DAT` slot is control / future payload / pad. |
| `HEADER_SECTORS` | 1 | sp / pack | The fixed metadata sector at the start of `HEADER.DAT`; PALTAB, startup audio, frame 0, routing, and PREBUFFER follow it in the same file. |
| `FEATURE_COLD_RUNS` | header bit 0 at offset 62 | pack / sp | Appends `(slot_start,count)` cold-run descriptors after each aligned audio chunk. The 30fps Sub copies patterns by these runs instead of scanning every update entry again. Old streams use the entry fallback; old players ignore the suffix via `total_len`. |
| Word-RAM swap completion | DMNA bit 1 | sp `swap_settle` | Poll the hardware's 1M bank-switch busy flag. The former fixed `0x400` loop burned about 0.82 ms after every frame even when the switch was already complete. |

## E. VDP DMA budget (Main CPU)

| Name | Value | Where | Meaning |
|---|---|---|---|
| `VB_WORDS_H40` | 3400 words/VBlank | ip | H40 per-VBlank DMA word budget (conservative vs. ~3895 theoretical). |
| `VB_WORDS_H32` | 2800 words/VBlank | ip | H32 per-VBlank DMA word budget. |
| `DMA_STAGE` | 24 KB (~768 tiles) | ip | Tile DMA staging area. |
| `RUN_TABLE` | up to ~128 runs | ip | `(dst, len, src)` table of contiguous-slot runs, one big DMA per run. |

## F. CBR / transfer rate

| Name | Value | Where | Meaning |
|---|---|---|---|
| `CD_RATE` | 153600 B/s | sim | CD 1x — the absolute delivery ceiling. |
| `TARGET_RATE` (`CBRSIM_RATE_KIB`) | 144 KiB/s | sim (env) | The CBR target rate. |
| `FRAME_BYTES` | `TARGET_RATE / FPS` (~10 KB) | sim | Fixed per-frame CBR byte budget. |
| `SECTOR` / `PAT` / `PAT_PER_SEC` | 2048 / 32 / 64 | pack | Sector = 2 KB, one tile pattern = 32 B, so 64 tiles per sector. |

## G. Encoder quality knobs (sim decisions; the `CBRSIM_*` ones are env-overridable)

Per-cell the sim picks: Raw (fresh CD load), Same, Near/Coa/Flbk (reuse a
resident tile), Buf (prefetched), or Miss. These thresholds steer that choice.

| Name | Default | Meaning |
|---|---|---|
| `CBRSIM_VRAM_TILES` | 1400 | Resident tile pool size (LRU). |
| `CBRSIM_COA_DETAIL` / `_MEAN` / `_MAX` / `_K` | 0.7 / 4 / 8 / 24 | Coa = reuse a resident tile whose low-frequency look matches a flat cold tile (detail below DETAIL; 2x2 mean color diff within MEAN/MAX; check K newest candidates). |
| `CBRSIM_NEAR_YM` / `_YP` / `_C` | 10 / 28 / 24 | Near = reuse an almost-identical resident tile (mean/max luma diff, mean chroma diff). |
| `CBRSIM_FLBK_IMPROVE_ONLY` / `_MIN_IMPROVE` | 1 / 0 | Flbk = fill a Miss with a resident tile only if it improves the picture. |
| `CBRSIM_TFLBK_YM` / `_YP` / `_C` | 120 / 252 / 200 | Flbk match thresholds (loose — a coarse fill beats a hole). |
| `AGING_ALPHA` / `WAIT_CAP` | 0.6 / 10 | Priority boost per waited frame, saturating at WAIT_CAP frames. |
| `UPGRADE_NEAR_RESERVE` | 0.7 | Apply Near only when 70%+ of the tile budget is still free. |
| `CBRSIM_DITHER` / `CBRSIM_SEGPAL` | on / on | Dithering / per-segment palette swaps. |
| `CBRSIM_PAL_ALGO` | `stl4` | Palette-line selector. `stl4` is the legacy segmented four-line Tile-Lloyd learner; `mosaic-gm` starts at one shared-core line and grows/merges only when validation improves. A selected one-line candidate receives a complete flattened-RGB333 histogram refinement and all-frame error proof before segment palettes are considered. |
| `CBRSIM_PAL_MAP_WEIGHT` | 1.0 | MOSAIC-GM penalty for mapping the same RGB333 source colour differently on different palette lines. |
| `CBRSIM_PAL_GROW_REL` / `_ABS` / `_MIN_USAGE` | 0.005 / 0.002 / 0.002 | Minimum relative gain, gain per pixel, and tile-use fraction required to add another MOSAIC-GM line. |
| `CBRSIM_PAL_CORE_SIZES` | `4,6,8,10,12,14` | Shared-colour counts tried when a specialist line grows. The remaining slots are line-specific. |
| `CBRSIM_PAL_SAMPLE_COUNTS` / `_VALIDATE_FRAMES` | `120,240,480` / 120 | Whole-movie learning candidates and the separate validation sample used to select among them. |
| `CBRSIM_PAL_SEG_TRAIN_FRAMES` / `_SEG_VALIDATE_FRAMES` | 240 / 60 | Maximum learning/validation frames per dark or uniform CRAM-segment candidate. |
| `CBRSIM_PAL_SEG_GAIN_REL` / `_ABS` | 0.005 / 0.002 | Improvement required before a local segment palette replaces the selected global palette. Adjacent identical choices are merged. |

## H. Per-source env vars (`CBRSIM_*`)

Set per encode; they select the output and the codec behavior for that source.

| Env | Meaning |
|---|---|
| `CBRSIM_W`, `CBRSIM_H` | Output resolution in pixels. |
| `CBRSIM_MODE` | Display mode: `H32` / `H40` / `mode4`. |
| `CBRSIM_MASTER_VF` / `CBRSIM_RAW_VF` | Optional ffmpeg overrides. If unset, `tools/video_geometry.py` probes the source and applies a full-frame, minimal-pad conversion using the mode HAR. H32 uses 8:7; H40 uses 32:35. |
| `CBRSIM_GEOMETRY_FIT` | `pad` (default, preserves all source pixels) or `crop` (explicitly discard centered outer margins). |
| `CBRSIM_SOURCE_SAR` | Optional input SAR override such as `25:27` when a 576x400 file is authored as 4:3 but has no SAR metadata. |
| `CBRSIM_FPS` | Frame rate (= the source's native rate). |
| `CBRSIM_SRC` | Source video path. |
| `CBRSIM_DURATION` | Encode length in seconds. |
| `CBRSIM_MAX_COLD` | Per-frame cold cap (section B). |
| `CBRSIM_RING_CAP_KB` / `CBRSIM_TANK_KB` | Override the ring cap / tank (normally derived from av_config). |
| `CBRSIM_RATE_KIB` | CBR target rate (section F). |
| `CBRSIM_PACK_FILL` | Packer payload scheduling. Default `1` replaces CD-1x rate padding with useful future payload while space is available, but sends more only when a future deadline requires it. `0` selects the backwards-minimum diagnostic schedule. |
| `CBRSIM_REUSE` | Reuse decoded frames. |
| `CBRSIM_GPU` | GPU quantization is on by default (`1`). Set `0`, `off`, `false`, or `no` only to force CPU execution. If CuPy/CUDA cannot be initialized, the encoder reports the reason and falls back to CPU. |
| `CBRSIM_PAL_ALGO` | `stl4` preserves the current encoder; `mosaic-gm` enables automatic shared-core Grow/Merge selection while it is being tuned. |
| `CBRSIM_EMIT_DEC`, `CBRSIM_OUT` | Save the decision log / output dir. `CBRSIM_EMIT_DEC=1` writes `CBRSIM_OUT/decisions.pkl`; an explicit path is also accepted. |

## Diagnostic HUD readouts (DEBUG=1 builds)

Not settings, but the live readouts of the throttles above — a single top row
on the VDP Window plane (`render_dbg` in ip, read back by
`tools/read_frameno.py: read_hud`). Keeping the HUD on Window separates it from
the two alternating video name tables, so a video-plane flip cannot make the
text disappear for one frame.

Both H32 and H40 use the same contiguous 32-cell layout, with no field gaps:
`FxxxxPxxSxxDxxRxxLxxCxxWxxMxxAxx`. `F` is four hexadecimal digits; `L`
shows the high byte of the lead, and `P/S/D/R/C/W/M/A` show their low byte.
Every compact field is two digits and wraps naturally from `FF` to `00`.

The shared font asset uses source index 0 for its background and source index 1
for set pixels. The movie player expands them once to P0/index1 and P0/index15
while uploading the font to VRAM. The result is an opaque darkest-colour HUD
background with brightest-colour text in every palette segment, with no
per-frame font scan, recolour, DMA, or additional VBlank wait.

The top Window row covers the video visually, but a DEBUG build still updates
the hidden video name-table row and all other rows exactly as a release build.
All Window cells are initialized with the opaque darkest-colour blank glyph;
the 32 live HUD cells are then overwritten once per frame. This adds no DMA and
does not branch on whether the video starts at row 0. In DEBUG builds, the old
slip-triggered CRAM0 red border is disabled; slips remain visible in `Sxx`, while
the HUD colours stay stable. Release builds retain the red indicator because
they do not have the HUD.

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
| `A` | `Axx` | Legacy startup-audio duplicate chunks still being skipped. Current persistent-prefetch streams start and remain at zero. |
