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
| `RING_SIZE` / `RING_SIZE_KB` | 420 KB (0x69000) | sp / cfg | Physical PRG ring. The one source; everything else derives from it. |
| `RING_JITTER_MARGIN_KB` | 40 KB | cfg | Headroom for real CD-delivery jitter, subtracted from the physical ring. |
| `RING_CAP_KB` | 380 KB (derived) | cfg -> pack | Pack schedule / prefetch cap = `RING_SIZE_KB - RING_JITTER_MARGIN_KB`. |
| `TANK_KB` | 380 KB (derived) | cfg -> sim | Sim VBV tank = usable ring. How much bandwidth a heavy frame may borrow. (Was wrongly 440 = larger than the ring.) |
| `BACKPRESSURE_KB` | 416 KB (`RING_SIZE-4`) | cfg | Where `pump_poll` stops draining the CDC to avoid overrunning the ring. `RING_CAP` must stay below it. |
| `APPLY_SIZE` | 34 KB (0x8800) | sp | Control-block apply ring (the per-frame update/cram/audio blocks). |
| prebuffer | fills ring to `RING_CAP` | pack | Boot-time burst that fills the ring before frame 1 so bursts are pre-buffered. |

## B. Cold cap (quality vs. sector slip) — the main quality lever

"Cold" = fresh 32-byte tile patterns loaded from CD this frame (vs. reuse of a
resident tile). More cold = sharper picture, but a heavy frame that loads too
many can overrun the CDC (a sector slip).

| Name | Value | Where | Meaning |
|---|---|---|---|
| `CBRSIM_MAX_COLD` | ed = 350, op = 0 (uncapped) | sim (env) | **Encoder-side per-frame cold cap** — the real lever. Capped cells become smart reuse. 0 = uncapped. |
| `COLD_CAP_REALIZED` | 380 | cfg | Pack-time assert: if realized cold exceeds it, the build FAILS (never ships a slipping/glitching disc). Raised 200 -> 380 with the p5 player. |
| realized cold | 362 (ed, sim cap 350) | pack (measured) | Actual loads: contiguous slot allocation reloads a few tiles the sim kept resident, so it runs a bit above the sim cap. |

Chain: `CBRSIM_MAX_COLD` (sim 350) -> realized cold (~362) must be `<= COLD_CAP_REALIZED` (380).
Per-source: lighter sources raise the ceiling via `CBRSIM_COLD_CAP_REALIZED` (machi_op ships uncapped).

## C. Audio sync throttles

PCM is a fixed rate, so playback must trail the write pointer by a lead. If the
lead drifts out of `[SYNC_MIN, SYNC_MAX]`, the writer jumps (a re-sync = an
audible click). See the `R`/`L` HUD readouts below.

| Name | Value | Where | Meaning |
|---|---|---|---|
| `AUDIO_BYTES` / `AUDIO` | 887 B/frame | sp / pack | 13.3 kHz / 15 fps. PCM bytes written to wave RAM per frame. |
| `SYNC_LEAD` | 0x1800 (6144 B, ~0.46 s) | sp | Write-ahead lead = the A/V lag and the startup silence. Re-sync resets the write here. |
| `SYNC_MIN` | 0x400 (1024 B, ~1.15 frames) | sp | Lower lead bound. Below it -> re-sync. Lowered 0xC00 -> 0x400 (p4) so a brief heavy-scene dip no longer clicks. |
| `SYNC_MAX` | 0x6800 (26624 B, ~2.0 s) | sp | Upper lead bound. Above it -> re-sync. |
| `WAVE_RING_END` | 0x8000 (32 KB) | sp | RF5C164 wave-RAM ring size. |

## D. CD pump throttles (keeping the Sub from dropping sectors)

The CD never stops (75 sectors/s), so the Sub must drain the CDC continuously.
`pump_poll` grabs one ready sector if the receivers have room.

| Name | Value | Where | Meaning |
|---|---|---|---|
| pump_poll frequency | **every 8 bitmap bytes** (`d7 & 7`) | sp `ef_byte` | **The p5 fix.** How often to poll during tile expansion. Old = every byte (~140x/frame, mostly empty polls). CD delivers 1 sector per ~166k cycles, so 8x is ample and frees the Sub -> higher cold ceiling. |
| ring-full skip | occ >= 416 KB (`RING_SIZE-0x1000`) | sp `pump_poll` | Skip draining if the ring is this full (back-pressure). |
| apply-full skip | occ >= 30 KB (`APPLY_SIZE-0x1000`) | sp `pump_poll` | Skip draining if the apply ring is this full. |
| `FRAME_SECTORS` | 5 | pack -> sp (`h_fsec`) | Sectors the CD delivers per frame (= CD 1x). Defined in pack; the player reads it from the MOVIE.DAT header at boot. Routing splits each into payload / control / pad. |
| `HEADER_SECTORS` | 1 | sp / pack | The 1-second TTRC header block at the start of MOVIE.DAT. |

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

## H. Per-source env vars (`CBRSIM_*`)

Set per encode; they select the output and the codec behavior for that source.

| Env | Meaning |
|---|---|
| `CBRSIM_W`, `CBRSIM_H` | Output resolution in pixels. |
| `CBRSIM_MODE` | Display mode: `H32` / `H40` / `mode4`. |
| `CBRSIM_FPS` | Frame rate (= the source's native rate). |
| `CBRSIM_SRC` | Source video path. |
| `CBRSIM_DURATION` | Encode length in seconds. |
| `CBRSIM_MAX_COLD` | Per-frame cold cap (section B). |
| `CBRSIM_COLD_CAP_REALIZED` | Override the drop-safe ceiling per source (raises it for lighter sources; machi_op ships uncapped). |
| `CBRSIM_RING_CAP_KB` / `CBRSIM_TANK_KB` | Override the ring cap / tank (normally derived from av_config). |
| `CBRSIM_RATE_KIB` | CBR target rate (section F). |
| `CBRSIM_REUSE`, `CBRSIM_GPU`, `CBRSIM_EMIT_DEC`, `CBRSIM_OUT` | Reuse decoded frames / use the GPU / save decisions.pkl / output dir. |

## Diagnostic HUD readouts (DEBUG=1 builds)

Not settings, but the live readouts of the throttles above — a single row in the
top-left corner (`render_dbg` in ip, positions `HUD_ROW`/`HUD_PITCH`/`HUD_COL_*`,
read back by `tools/read_frameno.py: read_hud`). Handy when tuning.

| Marker | Meaning |
|---|---|
| `F` | Frame number. |
| `P` | Palette segment. |
| `S` | CD sector slips (re-seek recoveries). 0 = clean video. |
| `D` | Stream desync count. 0 = clean. |
| `R` | Audio re-sync count (lead left `[SYNC_MIN, SYNC_MAX]`). 1 = baseline (a startup sync). |
| `L` | Current audio lead (write - play), in bytes. Approaching `SYNC_MIN` = the buffer is draining. |
