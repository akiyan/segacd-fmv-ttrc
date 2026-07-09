---
name: sim
description: Project skill for encoding an arbitrary video source with the delta stream codec in tools/sim.py and producing the standard full-length analysis video. Applies the four rules automatically: keep source fps, use resolution up to the DMA limit, crop black bars, and allow starvation. Then run simulation, compose the analysis video, and optionally upload. Use for requests like "make an analysis video for this mp4", "make one for Sakura", or "/sim <src.mp4>". Implementation: tools/sim.py for codec simulation, tools/layout_preview.py for the canonical analysis layout and dummy preview, and tools/render_analysis.py for full MP4 rendering with real data.
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
2. **Use resolution up to the DMA limit**: target about 384-396 total grid
   tiles. Reason: the H32 2-VBlank VRAM write budget is about 13 KB.
   A full refresh costs about 34 bytes per tile
   (32 pattern bytes + 2 name-table bytes) plus 128 bytes of CRAM:
   `34*C + 128 <= 13000`, so `C <= about 390`.
   Match aspect to the source display aspect after crop.
3. **Crop black bars**: if `cropdetect` finds black bars, crop them. If not,
   do not crop.
4. **Allow starvation**: it is OK if CD supply is not enough and starvation
   appears. Prefer the DMA-limit resolution. Do not force starvation to 0%.

Other fixed defaults:

- All features on: `DITHER`, `SEGPAL`, `NEAR`, `VBV`, `COA`.
- `TANK_KB=440`.
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

# Black-bar detection. Use a mid section to avoid fade-to-black frames.
ffmpeg -hide_banner -ss 15 -t 30 -i SRC -vf cropdetect=24:2:0 -f null - 2>&1 \
  | grep -oE 'crop=[0-9:]+' | sort | uniq -c | sort -rn | head
```

Rules:

- Always inspect at least one content frame. A filename can lie about what is
  inside.
- `fps = round(r_frame_rate)`.
- Use crop only when cropdetect returns something other than `W:H:0:0`.

### 2. Choose Resolution / Tile Grid

- Let `A` be the display aspect of the cropped content: crop width / height for
  square pixels, or DAR if the source has one.
- H32 pixel aspect ratio makes stored pixels display about 1.167x wider, so the
  tile-grid aspect is `r = A / 1.167`.
- Aim for about `T = 390` total tiles:

```text
Hc = round(sqrt(T / r))
Wc = round(T / Hc)
```

- Keep `Wc * Hc <= about 400`.
- The stored resolution is `Wc*8` by `Hc*8`.

Examples:

- 16:9 source: `A=1.778`, `r=1.52`, `24x16=384`, so `192x128`.
- Sonic Jam-like source: `A=1.44`, `r=1.23`, `22x18=396`, so `176x144`.
- 4:3 source: `A=1.333`, `r=1.14`, around `20x18=360`.

### 3. Run Simulation

This can take about 10-13 minutes for 2700-3100 frames.

```sh
export CBRSIM_SRC=SRC
export CBRSIM_W=<W> CBRSIM_H=<H> CBRSIM_FPS=<fps> CBRSIM_DURATION=<sec>

# If there is crop, put it at the start of MASTER_VF / RAW_VF.
# The final scale in the dedither chain must match W:H exactly.
export CBRSIM_MASTER_VF="[crop=...,]scale=<~2x>:flags=lanczos,hqdn3d=6:6:8:8,gblur=sigma=1.6,scale=<W>:<H>:flags=lanczos"
export CBRSIM_RAW_VF="[crop=...,]scale=<src panel>"

# Output convention (AGENTS.md "Output Paths"): one stem per encode,
#   <stem> = <input-basename>_<mode>_<resolution>_<audio>
#            e.g. OP1_ps2_H32_256x144_adpcm22  (resolution = output WxH px)
# All artifacts go under videos/ (git-ignored), never tmp/.
export CBRSIM_OUT=videos/<stem> CBRSIM_TANK_KB=440
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
