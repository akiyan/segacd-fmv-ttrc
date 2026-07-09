# Comparison overlay reference (Real vs Encoder-ideal)

The comparison video plays the **Real output** (an emulator/hardware recording of
the built disc) next to the **Encoder ideal output** (the offline encoder's clean
decoded frames) side by side, locked to the same movie frame, over the same
shared footer used by the analysis overlay (see `ANALYSIS.md`).

The canonical dummy-data layout is `tools/comparison_preview.py`
(`python3 tools/comparison_preview.py` → `tmp/comparison_preview.png`). The
real-data renderer is `tools/render_comparison.py`. Both reuse
`tools/layout_preview.py` for the shared footer (`draw_footer`) and fonts.

## Layout map

```
+----------------------------------------------------------------------+
| SEGA-CD Tile Texture Reuse Codec: Real vs Ideal  <specs>     <sync Frame:NNNNN>
+---------------------------------+   +--------------------------------+
| Real output <emu name/ver>      |   | Encoder ideal output           |
| +-----------------------------+ |   | +----------------------------+ |
| |  emulator recording (4:3),  | |   | |  sim decoded output (4:3), | |
| |  on-screen debug HUD baked  | |   | |  clean, no overlay         | |
| |  in (top black bar)         | |   | |                            | |
| | audio 1 . Emulator (default)| |   | | audio 2 . Encoder ideal    | |
| +-----------------------------+ |   | +----------------------------+ |
+---------------------------------+   +--------------------------------+
+----------------------------------------------------------------------+
| SHARED FOOTER (identical to the analysis overlay, see ANALYSIS.md)    |
|  status bar: Req / Band / Tank / Buff / DMA + palette + 3 timelines   |
|  category totals (whole clip)                                         |
+----------------------------------------------------------------------+
```

Regions live in `tools/comparison_preview.py`: `CMP_L` / `CMP_R` are the two
4:3 video panels (each `PANEL_W`x`PANEL_H`, side by side); `TITLE_BASE` is the
top title/spec baseline; the footer uses the same `STATUS_XY` / `PAL_XY` as the
analysis overlay.

## Top title

- Big title `SEGA-CD Tile Texture Reuse Codec: Real vs Ideal`, plus
  a small spec line `mode / WxH (cols x rows) / audio / fps` (from the sim
  output, same fields as the analysis heading).
- Right edge: `sync Frame:NNNNN` — the movie frame both panels are showing.

## Panels

- **Real output** (left): a recording of the emulator/hardware playing the built
  disc, shown 4:3. The player's on-screen debug HUD (`FXXXX RXXXX ...`, the top
  black bar) is baked into the recording; the leading `F` field is the movie
  frame number in hex. Small `<emu name/ver>` next to the label.
- **Encoder ideal output** (right): the offline encoder's decoded frames
  (`CBRSIM_OUT/preview`, no overlay), placed on the same Sega CD screen so both
  panels frame the picture identically.
- Each panel carries a small audio badge in its lower-left: track 1 = Emulator
  (default, bright) on the left, track 2 = Encoder ideal (dim) on the right.

## Frame sync (the key idea)

The emulator plays the movie at its own steady rate (native fps), which is not
phase-locked to the sampling grid, so wall-clock/sample-index alignment drifts.
Instead the two panels are locked by the **movie frame number** the player prints
in its debug HUD:

- `tools/read_frameno.py` reads the leading `F<hex>` field from the emulator
  frame by template-matching the 8x8 debug font (`gen_debugfont.py` glyphs) with
  normalized cross-correlation.
- `tools/find_comparison_sync.py` finds the real recording frame where the HUD
  shows `F0000`. The renderer uses that as the exact anchor
  (`CMP_F0_REAL_FRAME` can override it) and maps `ideal_frame = k - F0000_k`,
  with confident OCR reads used to follow sampled frames that are held or skipped
  by the 60fps-to-15fps extraction phase.
- The footer for output frame `k` is drawn from sim frame `F`, so the picture,
  the category map data, and the meters are all the same movie frame.

## Shared footer

Identical to the analysis overlay's bottom strip, drawn by
`layout_preview.draw_footer` (status bar `draw_status_real` + category totals
`draw_cattotals`) with the same per-frame sim data. See `ANALYSIS.md` for every
meter, timeline, and the category totals bar.

## Audio

Two tracks are muxed: track 1 = the emulator's recorded audio (default), track 2
= the encoder/sim audio. Both are the same movie audio; track 1 carries any
hardware-playback characteristics. (When the emulator recording's timeline is not
cleanly usable, the sim audio is used as a temporary stand-in — noted per run.)

## Crispness (dot-by-dot)

The dithered output must stay dot-by-dot. Record the emulator with a **lossless**
video preset (`record_movie.sh --preset ffv1-flac` → FFV1), and extract the Real
panel frames from that lossless recording. A lossy (h264) recording blurs the
dither, and pairing it against the lossless sim frames makes the Real panel look
worse than the ideal. Panels are upscaled with nearest-neighbor.

## Pipeline and tools

```
sim (tools/sim.py) --> tools/pack_stream.py --> disc --> emulator recording
    |                                                        (lossless FFV1)
    |--> CBRSIM_OUT/preview (clean ideal frames) ------------------+
                                                                   v
   tools/render_comparison.py  (left=recording, right=preview[F], shared footer)
    reuses render_analysis (footer data) + comparison_preview (layout)
```

- `tools/comparison_preview.py` — dummy-data layout preview (the layout source).
- `tools/render_comparison.py` — real-data full render + 2-track mux.
- `tools/read_frameno.py` — reads the movie frame number from the emulator HUD.
- `tools/find_comparison_sync.py` — reports the `F0000` real-frame anchor for a
  recording or extracted PNG directory.
- `tools/export_sim_video.py` — writes a straight sim video (video+audio, no
  overlay); a standalone clean "Encoder ideal output" clip.

## Output paths

Follows AGENTS.md "Output Paths": `videos/<stem>_comparison.mp4` for the
side-by-side, `videos/<stem>_sim.mp4` for the straight sim clip,
`videos/<stem>_emu.mp4` for the emulator recording.

## Full pipeline runbook (sim → disc → record → comparison → upload)

Set the **encode identity** env once. Every tool derives the same working dir
(`videos/<stem>/tmp`) and artifact names (`videos/<stem>_*.mp4`) from it via
`tools/cbr_paths.py` — so do NOT set `CBRSIM_OUT` (let it auto-derive), and use
the same env for every step. `stem = <src>_<mode>_<WxH>_<audio-tag>`.

Example: **machi_op, H40, PCM 13.3 kHz** (adjust `CBRSIM_W/H`, duration, seconds
per source; each tool's docstring lists its full env):

```sh
# 0) encode identity (H40 → 320 wide / 40 cols; PCM → audio-verified path)
export CBRSIM_SRC=assets/machi_op.mp4 CBRSIM_FPS=15
export CBRSIM_MODE=H40 CBRSIM_W=320 CBRSIM_H=176        # H per /sim aspect+DMA rules
export CBRSIM_AUDIO=pcm13
# stem = machi_op_H40_320x176_pcm ; sim dir = videos/machi_op_H40_320x176_pcm/tmp

# 1) encode — also emit the decision log the packer replays
CBRSIM_EMIT_DEC=1 python3 tools/sim.py

# 2) analysis overlay video (optional upload)   -> videos/<stem>_analysis.mp4
python3 tools/render_analysis.py

# 3) straight ideal clip (optional)             -> videos/<stem>_sim.mp4
python3 tools/export_sim_video.py

# 4) pack the on-disc stream (mode byte from CBRSIM_MODE) -> out/movieplay/MOVIE.DAT
python3 tools/pack_stream.py --output out/movieplay/MOVIE.DAT

# 5) build the disc                             -> out/MOVIEPLAY.cue
make disc

# 6) record the emulator LOSSLESS (crisp dither)  -> tmp/<tag>.mkv
tools/record_movie.sh --disc out/MOVIEPLAY.cue --no-build --seconds 175 \
  --preset ffv1-flac --tag comp_emu --out videos/machi_op_H40_320x176_pcm_emu.mp4

# 7) (optional) report the F0000 sync anchor
python3 tools/find_comparison_sync.py tmp/comp_emu.mkv

# 8) render the comparison (left = lossless mkv, right = sim preview[F])
CMP_REAL=tmp/comp_emu.mkv CMP_OUT=videos/machi_op_H40_320x176_pcm_comparison.mp4 \
  python3 tools/render_comparison.py

# 9) upload analysis and/or comparison (unlisted, category 20) — see AGENTS.md
```

Shared-machine rule: steps 1-3, 8 (sim/render, CPU-heavy) and step 6 (emulator,
timing-sensitive) MUST NOT overlap — run one at a time, and never kill another
session's runs (see AGENTS.md "Shared-Machine Exclusion").

## Known limitations / TODO

- **Start marker (planned):** release comparison should not depend on the debug
  HUD. The player should show an easy-to-detect marker on the last frame before
  movie playback starts, and `render_comparison.py` should sync from that marker.
  Once that exists, the debug HUD can remain off by default and streams can be
  packed without debug blocks by default.
- **Panel geometry (deferred):** the emulator's display mode / overscan geometry
  differs from the sim, so placing the 256x144 ideal content onto the Sega CD
  screen to match the recording exactly is not yet a clean spec-only calculation.
  The default is the geometric center (`(SCREEN_H - CONTENT_H) / 2`); the measured
  emulator placement is a few pixels higher (`CMP_PADY` overrides). To be
  finalized by folding the real display geometry (player `plane_row`, HUD rows,
  overscan crop) into the calculation.
- **Audio alignment:** aligning the emulator's real audio to the frame-synced
  video depends on a clean recording timeline; use the sim audio as a stand-in
  when the recording overruns/loops.
