---
name: sim
description: Project skill for encoding an arbitrary video source with the delta stream codec in tools/sim.py and producing the standard full-length analysis video. Applies the four rules automatically: keep source fps, use resolution up to the DMA limit, preserve source pixels while removing only confirmed black bars, and allow starvation. Then run simulation, compose the analysis video, and optionally upload. Use for requests like "make an analysis video for this mp4", "make one for Sakura", or "/sim <src.mp4>". Implementation: tools/sim.py for codec simulation, tools/layout_preview.py for the canonical analysis layout and dummy preview, and tools/render_analysis.py for full MP4 rendering with real data.
---

# /sim: Source Video -> Delta Codec -> Analysis Video

Encode any video with the Sega CD delta stream codec and produce the usual
1920x1080 analysis video.

The analysis frame contains:

- left: Sega CD output
- right column: Source, category map, Miss + MissCarry map
- bottom: status bar and palette state

Argument: source MP4 path, optionally plus display name or upload instruction.

## Four Rules: Automatic Policy

1. **Keep the source fps**: use the native source fps. Do not lower it.
   Examples: 29.97 -> 30, 23.976 -> 24, 15 -> 15.
2. **Use the maximum valid display raster**: use H32 256x224 (32x28,
   896 cells) or H40 320x224 (40x28, 1120 cells) unless a source-specific
   hardware constraint requires less. The DMA budget limits how many changed
   tiles can be delivered in one frame; it does not limit the canvas or total
   cell count. Preserve the source display aspect with the mode's HAR-aware
   fit/pad conversion.
3. **Preserve source pixels**: `tools/video_geometry.py` uses HAR-aware
   full-frame `pad` by default. Use `crop` only when inspection confirms that
   the discarded outer margins are black bars, not picture content.
4. **Allow starvation**: it is OK if CD supply is not enough and starvation
   appears. Keep the maximum valid display raster. Do not force starvation to
   0% by shrinking the canvas.

Other fixed defaults:

- All features on: `DITHER`, `SEGPAL`, `NEAR`, `VBV`, `COA`.
- Tank size comes from `tools/av_config.py`, matching the
  packer's usable ring cap. Do not set `CBRSIM_TANK_KB` in normal runs.
- Rate = 144 KiB/s by default.
- GPU encoding is on by default. CPU is the fallback.
- Start sim/render with the GPU-specific Python when available:

```sh
PY=~/.config/cbrsim-gpu/venv/bin/python
[ -x "$PY" ] || PY=python3
```

If CuPy is missing, it falls back to CPU automatically. Explicitly force CPU
only with `CBRSIM_GPU=0`. CPU and GPU outputs should match bit for bit. See
`[[gpu-quant-acceleration]]` for details.

## Procedure

### 1. Inspect the Source

```sh
ffprobe -v error -select_streams v:0 -show_entries \
  stream=width,height,r_frame_rate,display_aspect_ratio,sample_aspect_ratio \
  -of default=nw=1 SRC
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 SRC

# Black-bar detection. Sample at least three separated content sections; avoid
# fades and title cards. Replace the timestamps with positions inside SRC.
for t in 15 60 120; do
  ffmpeg -hide_banner -ss "$t" -t 10 -i SRC \
    -vf cropdetect=24:2:0 -an -f null - 2>&1 \
    | rg -o 'crop=[0-9:]+' | sort | uniq -c | sort -rn | head
done
```

Rules:

- Always inspect at least one content frame. A filename can lie about what is
  inside.
- `fps = round(r_frame_rate)`.
- Treat a margin as a black bar only when the same crop rectangle dominates at
  three or more separated content sections and a visual check confirms that
  the margin is fixed, edge-to-edge black rather than picture content. A dark
  or black-and-white scene is not evidence of a black bar.
- If the samples disagree, include fades, or are otherwise ambiguous, do not
  crop. Preserve the complete source instead.
- Crop only the confirmed fixed black margins. Never crop active picture just
  to fill H32/H40; fit/pad the remaining active picture with the mode HAR.
- Read the exact full duration from `ffprobe` and always export it as
  `CBRSIM_DURATION`. Do not rely on `sim.py`'s diagnostic default or unset the
  variable for a full-length encode.

### 2. Choose Resolution / Tile Grid

- Use the full valid raster for the selected display mode: H32 is 256x224
  (32x28, 896 cells), and H40 is 320x224 (40x28, 1120 cells).
- Let `A` be the displayed aspect of the source after applying its SAR. If the
  file has no reliable SAR, set `CBRSIM_SOURCE_SAR` explicitly.
- Fit the complete source into that raster using H32 HAR 8:7 or H40 HAR 32:35.
  This normally leaves no border or only a small border. Do not reduce the
  grid merely because fewer tiles can change in one frame; the encoder's
  priority and starvation behavior handle the update budget.
- Sonic Jam-like sources must use the declared display aspect; do not infer
  4:3 from a 576x400 coded raster without an SAR override.

### 3. Run Simulation

This can take about 10-13 minutes for 2700-3100 frames.

```sh
export CBRSIM_SRC=SRC
export CBRSIM_W=<W> CBRSIM_H=<H> CBRSIM_FPS=<fps> CBRSIM_DURATION=<sec>

# Display mode & audio format:
#   CBRSIM_MODE = H32 (256 wide, default) / H40 (320 wide → set CBRSIM_W=320, 40 cols)
#                 / mode4 (256x192 — RESERVED: packer tile format not ready, do not ship)
#   CBRSIM_AUDIO = pcm13 (13.3kHz mono 8bit PCM, RF5C164 — the shipping audio path, and
#                  the code default). ADPCM (adpcm22) was investigated but shelved; see ADPCM.md.
# CBRSIM_MODE flows into the analysis overlay AND the MOVIE.DAT header mode byte (via pack).
export CBRSIM_MODE=H32 CBRSIM_AUDIO=pcm13

# The geometry helper is automatic unless explicit filters are required.
export CBRSIM_GEOMETRY_FIT=pad       # crop only confirmed black margins
export CBRSIM_SOURCE_SAR=25:27       # example: 576x400 authored as 4:3
# Optional explicit overrides remain available for unusual sources.
# export CBRSIM_MASTER_VF="..."
# export CBRSIM_RAW_VF="..."

# Output convention (AGENTS.md "Output Paths"): one stem per encode,
#   <stem> = <input-basename>_<mode>_<resolution>_<audio>
#            e.g. OP1_ps2_H32_256x224_pcm  (resolution = output WxH px)
# All artifacts go under videos/ (git-ignored), never tmp/.
export CBRSIM_OUT=videos/<stem>
export CBRSIM_DITHER=1 CBRSIM_SEGPAL=1 CBRSIM_NEAR=1 CBRSIM_VBV=1 CBRSIM_COA=1

PY=~/.config/cbrsim-gpu/venv/bin/python
[ -x "$PY" ] || PY=python3
mkdir -p videos/<stem>
nohup "$PY" tools/sim.py >videos/<stem>/sim.log 2>&1 &
```

After completion:

- Check the completion line: `starved_frames=N (X%)`.
- Check `avg_bps`, which should stay within CD 1x budget, about `<=153600`.
- Starvation is allowed, but report it.
- Output appears under `videos/<stem>/`:
  - `preview/`
  - `catmap/`
  - `misscarry/`
  - `stats.npz`
  - `audio_13k3_u8_mono.wav`

### 4. Render the Analysis Video

Use the new layout pipeline: `tools/render_analysis.py` directly.

Do not use the old `make_base`, `render_statusline`, or `compose_*.sh` path for
this analysis video.

The canonical layout source is `tools/layout_preview.py`. Run it alone to
generate a dummy one-second preview at `tmp/layout_preview.png`. If the layout
must change, change it there first. `render_analysis.py` uses the same drawing
function on real data.

```sh
CBRSIM_OUT=videos/<stem> \
CBRSIM_SRCLABEL="Source (<source name>, <platform/year>)" \
CBRSIM_MODE=H32 \
ANALYSIS_OUT=videos/<stem>_analysis.mp4 \
"$PY" tools/render_analysis.py
```

The full render generates all PNG frames in parallel (`nproc-2`) and then calls
FFmpeg, usually with `h264_nvenc`, `-r 60`, and audio.

Frame-range check only:

```sh
python3 tools/render_analysis.py <A> <B>
```

Important rendering notes:

- W/H, tile count, display aspect, resolution text, fps, and average kbps are
  auto-derived from simulation output: `stats.npz`, `report.txt`, preview, and
  raw images.
- Only the source label is passed with `CBRSIM_SRCLABEL`.
- Layout details are implemented in `layout_preview.py`:
  - right column: Source / Category / Miss+MissCarry
  - Miss in Category is an empty red outline rather than filled content
  - legend: 2 rows, 7 categories
  - zero padding and dark leading zeros
  - scrolling line graph with +/-4 seconds and now centered
  - status uses fixed-width Req / Comp / Buff / DMA
  - three-row timeline: Req2 : Buff1 : DMA1
  - DMA is compared against theoretical `(60/fps)` VBlank budget
  - heading metadata plus small top-right Time / Frame, baseline-aligned
  - palette used-color blocks have no outline
- The main Sega CD output is centered exactly like hardware. Do not scale low
  resolution content to fill the panel. In H32 it is centered inside a 256x224
  screen and then displayed as a 4:3 panel.
- Source and Category panels should fit with letterboxing when needed.
- Extract and inspect a few frames:
  - Does it match `tmp/layout_preview.png` visually?
  - Is a 16:9 source not distorted?
  - Are Miss red-outline holes correct?
  - Is the content correct?
  - Use `ffprobe` to verify resolution, duration, and audio.

### 5. Upload If Requested

```sh
PY=~/.config/youtube/venv/bin/python
[ -x "$PY" ] || PY=python3
"$PY" ~/.claude/skills/youtube/youtube.py upload videos/<stem>_analysis.mp4 \
  --title "<source name> OP SEGA-CD delta codec analysis (WxH/WcxHc/fps/aspect/13.3kHz) YYYYMMDD" \
  --desc "<specs, four-rule choices, starvation rate>" \
  --tags "SEGA-CD,SegaCD,FMV,homebrew,codec" \
  --category 20 --privacy unlisted
```

Analysis-video titles should be descriptive, not version-number titles such as
`vNNN`. Upload as unlisted, category 20. See `[[youtube-upload-convention]]`.

## Cautions

- Shared machine: before starting sim/render, always check for other heavy
  processes and wait. Do not run heavy jobs in parallel without coordination.

```sh
ps -eo pid,etimes,args | grep -E "sim\\.py|render_analysis\\.py" | grep -v grep
```

- If another user's simulation is running, wait until it finishes.
- Never kill another session's process. Kill only jobs you started.
- Other simulations can often be identified through env choices, for example
  `CBRSIM_EMIT_DEC`, `NOPANELS`, or default `OUT=tmp/sim` for player
  decision emit. See `[[shared-machine-sim-coordination]]`.
- The analysis layout is consolidated into:
  - `tools/layout_preview.py`: canonical layout
  - `tools/render_analysis.py`: render real data using that layout
- Change the layout in `layout_preview.py`; `render_analysis.py` imports it.
- The old `make_base`, `render_statusline`, `render_palstate`, and
  `compose_*.sh` path is not needed for this new pipeline.
- Use `tools/sim.py` unchanged as the simulation core.
- If W/H are unchanged, `CBRSIM_REUSE=1` can skip master extraction. Quantizing
  still reruns.
- `render_analysis.py` is heavy for 3000-frame PIL rendering. It uses `nproc-2`
  parallelism. For long videos, run in the background or delegate to a forked
  context to avoid filling the main conversation context.
