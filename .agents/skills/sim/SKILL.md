---
name: sim
description: >-
  Project skill for encoding an arbitrary video source with the delta stream
  codec in tools/sim.py and producing the standard full-length analysis video.
  Applies the four rules automatically: keep source fps, use resolution up to
  the DMA limit, preserve source pixels while removing only confirmed black
  bars, and allow starvation. Then run simulation, compose the analysis video,
  and optionally upload. Use for requests like "make an analysis video for this
  mp4", "make one for Sakura", or "/sim SOURCE.mp4". Implementation:
  tools/sim.py for codec simulation, tools/layout_preview.py for the canonical
  analysis layout and dummy preview, and tools/render_analysis.py for full MP4
  rendering with real data.
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

- All features on: GPU, the full 1,535-tile VRAM pool, `DITHER`, `SEGPAL`,
  `NEAR`, boot VRAM prefetch, whole-movie quality planning, and the unified
  Prg/Wr0/Wr1/Dic pattern supply at every supported cadence.
- Audio = `adpcm22`. Use `pcm13` only when explicitly requested or when a
  physical-console-qualified fallback is required.
- PrgBuf and offline quality-budget ceilings come from `tools/av_config.py`.
  WordBuf0, WordBuf1, and DicBuf capacities come from
  `tools/pattern_supply.py`; none are normal per-source overrides.
- BODY supply follows SEGA-CD 1x's exact integer sector cadence. Fixed control
  data is reserved first; update entries, run descriptors, and Prg pattern
  payload share the remainder. Because run fragmentation is known only after
  allocation, the decision pass protects one worst-case four-byte descriptor
  as each cold tile is selected. It refunds the difference immediately after
  the exact run count is known. It is not a per-source bitrate setting or a
  permanent rate reduction.
- GPU encoding is on by default. CPU is the fallback.
- Start sim/render with the locked GPU environment. Do not fall back to a
  system Python or an older venv:

```sh
tools/python.sh --gpu -c 'import sys; print(sys.executable)'
```

The encoder can fall back when CuPy is missing, but do not use that silent path
for normal work. A deliberate CPU run uses
`CBRSIM_GPU=0 tools/python.sh tools/sim.py ...`. CPU and GPU outputs should
match bit for bit. See `[[gpu-quant-acceleration]]` for details.

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
- Read the exact full duration from `ffprobe` and put it in
  `source.duration` in the TOML profile. Do not rely on `sim.py`'s diagnostic
  default for a full-length encode.

### 2. Choose Resolution / Tile Grid

- Use the full valid raster for the selected display mode: H32 is 256x224
  (32x28, 896 cells), and H40 is 320x224 (40x28, 1120 cells).
- Let `A` be the displayed aspect of the source after applying its SAR. If the
  file has no reliable SAR, set `source.sar` explicitly in the TOML profile.
- Fit the complete source into that raster using H32 HAR 8:7 or H40 HAR 32:35.
  This normally leaves no border or only a small border. Do not reduce the
  grid merely because fewer tiles can change in one frame; the encoder's
  priority and starvation behavior handle the update budget.
- Sonic Jam-like sources must use the declared display aspect; do not infer
  4:3 from a 576x400 coded raster without an SAR override.

### 3. Run Simulation

This can take about 10-13 minutes for 2700-3100 frames.

Create one strict `schema_version = 3` profile under `configs/` for each
source/mode combination. Use the schema in `CONFIG.md`; the checked-in Bad
Apple H32/H40 profiles are complete examples. The profile must name the source,
native fps, exact duration, full mode raster, HAR-aware `fit`, the selected
output directory, optional timed `raw_prefetch`, optional qualified `cold_cap`,
and palette algorithm. Do not add fixed GPU, VRAM, dither, segmented-palette,
Near, boot-prefetch, forward-fill, or startup-audio keys.

Before every `/sim`, perform these steps in this exact order:

1. **Read the complete TOML profile, then explain the relevant settings to the
   user in commentary before starting.** Do not paste the TOML verbatim. In
   plain language, show the values that are commonly tuned for codec/visual
   comparisons and clearly call out every value changed for this run. Omit
   stable source identity/timing fields such as `path`, `fps`, `duration`, and
   `sar` unless they changed from the preceding comparison. Include unchanged
   settings only when they materially affect how the current result should be
   interpreted. This explanation is the user's preflight record of the run.
2. Validate the profile with `tools/encode_config.py` and identify its exact
   `[output].directory`.
3. Check the shared-machine exclusion rule below.
4. Do not manually clean the simulation output directory. Let `sim.py` inspect
   its authenticated completed-artifact cache. It reuses only when source
   bytes, effective encoder/TOML settings, and the encoder `e` version match;
   profile filenames, TOML formatting, output paths, and individual code-file
   hashes are not part of the encode identity. Output-affecting changes must
   bump `tools/av_version.txt`. An interrupted artifact has no completion
   marker and is reset automatically.
5. Run the sim and report whether it printed `sim artifact cache: hit` or
   started a clean seed pass. Use `CBRSIM_FORCE_REENCODE=1` only when the user
   explicitly asks for a fresh encode or cache validation itself is the test.

The profile should normally keep `output.reuse = false`. That legacy setting
controls decoded-input reuse inside an in-progress encode; automatic reuse of
a fully completed, authenticated sim artifact is independent of it.

```sh
tools/python.sh --gpu tools/sim.py configs/<source>-<mode>.toml
```

TOML values replace inherited per-source `CBRSIM_*` values. `sim.py` freezes
the resolved settings and profile SHA-256 in `decisions.pkl`; do not hand-copy
the geometry or fps to the packer later. Existing `CBRSIM_*` variables are an
internal compatibility layer and remain useful only for one-off experiments
that intentionally have no profile.

After completion:

- Confirm the fps-derived Prg geometry printed by sim: normal prebuffer /
  jitter / physical delivery is 384/40/424 KiB at 15fps,
  399/25/424 KiB at 24fps, or 404/20/424 KiB at 30fps.
- Physical delivery failure is terminal for that sim. Do not lower the cold
  cap, synthesize local per-frame caps, or repeat allocation in response.
  Report the exact failing frame/resource so the user can choose the next
  non-cap investigation.
- Check the completion line: `starved_frames=N (X%)`.
- Check `body_useful_bps`, the mean useful BODY delivery rate shown by Band.
  It is weighted by total physical BODY read time, and each slot must remain at
  or below CD 1x (150 KiB/s). `codec_work_bps` is a separate
  quality-allocation diagnostic.
- Starvation is allowed, but report it.
- Output appears under `videos/<stem>/`:
  - `preview/`
  - `catmap/`
  - `stats.npz`
  - `audio_13k3_u8_mono.wav`, or for ADPCM22 both the packer input
    `audio_22k05_s16_mono.wav` and the analysis/straight-video playback model
    `audio_playback_adpcm22_rf5c.wav`

For ADPCM22, `stats.npz:audio_playback_file` is authoritative for waveform and
mux selection. It contains the shared packer-reference continuous IMA decode
after RF5C164 sign-magnitude conversion. Never select the first `audio_*.wav`
by filename order; that would silently restore the clean source audio.

### 4. Render the Analysis Video

Use `tools/render_analysis.py` directly.

When this skill is orchestrated by `$run`, do not perform the full render at
this point. Run the zero-frame data mode to write the persistent TSV, invoke
the `timeline` skill, publish/show its PNG, then return control to `$run` for
pack and DEBUG recording. `$run` may return here for the full analysis render
only after that exact stream's complete HUD recording gate passes. A standalone
`/sim` request remains allowed to render immediately because it does not claim
an end-to-end playback-qualified deliverable.

The canonical layout source is `tools/layout_preview.py`. Run it alone to
generate a dummy one-second preview at `tmp/layout_preview.png`. If the layout
must change, change it there first. `render_analysis.py` uses the same drawing
function on real data.

```sh
CBRSIM_SRCLABEL="Source (<source name>, <platform/year>)" \
ANALYSIS_OUT=videos/<stem>_analysis.mp4 \
tools/python.sh --gpu tools/render_analysis.py configs/<source>-<mode>.toml
```

Every invocation first writes the complete per-frame numeric sidecar to a
unique persistent file below `logs/`. Its filename includes local date/time,
the profile name, the first 10 profile-SHA characters, and the encoder version.
`videos/<stem>_analysis.tsv` (or `ANALYSIS_TSV` when explicitly set) is only a
compatibility symlink to that permanent log. Use the `logs/` file for maxima,
totals, and frame-to-frame comparisons instead of OCR. The full render then
generates all PNG frames in parallel (`nproc-2`) and calls FFmpeg, usually with
`h264_nvenc`, `-r 60`, and audio. Disposable PNG/MP4 bytes live in the managed
tmpfs workspace even though their public symlinks remain below `videos/`.

Frame-range check only:

```sh
tools/python.sh tools/render_analysis.py configs/<source>-<mode>.toml <A> <B>
```

For `$run`'s pre-recording TSV handoff, use `0 0`; this writes all numeric data
without constructing the analysis MP4. The later full render writes a new TSV,
so invoke `timeline` and publish its Gist again for that final analysis upload.

Important rendering notes:

- W/H, tile count, display aspect, resolution text, fps, and average useful
  BODY kbps are auto-derived from simulation output: `stats.npz`,
  `buffer_remaining.npz`, preview, and raw images.
- Only the source label is passed with `CBRSIM_SRCLABEL`.
- Layout details are implemented in `layout_preview.py`:
  - right column: Source / Category / whole-clip category totals / Audio
  - Miss in Category is a filled red hole
  - legend: `Raw / Same / Near / Flbk / Miss` on row 1 and
    `Prg / Wrd / Dic` on row 2; the displayed Wrd count is Wr0 + Wr1
  - each legend item shows one displayed-cell count (Wr shows the combined
    count while the underlying Wr0/Wr1 data remains separate)
  - Same uses the original light/dark checker legend swatch but no
    category-map tile border
  - Raw uses a thin black/white dashed border in both legend and category map
  - Dic/Prg/Wr use a thin colour-and-black dashed border; both Wr banks
    use the Wr1 cyan display colour
  - scrolling audio waveform with +/-2 seconds and now centered
  - status uses Req / Cold / Band / DMA / Run / Prg / Wrd / Pre
  - Pre is the number of future patterns actually written to VRAM in the frame;
    a prefetched pattern used later is displayed as Same
  - Band is physical-slot useful BODY data split left-to-right into Raw
    payload, Prg charge, and control, excluding all pad and HEADER, divided by
    that slot's actual CD read time (0 to 150 KiB/s)
  - four-row timeline: Req, Prg/Wrd remaining, physical cold runs, and Band;
    Raw is the bottom Band segment, followed by Prg and control
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
  - Are Miss cells rendered as filled red holes?
  - Is the content correct?
  - Use `ffprobe` to verify resolution, duration, and audio.

### 5. Upload If Requested

Under `$run`, this section is forbidden until the matching DEBUG recording has
a complete `PASS` or `WARNING` HUD gate JSON (`pass: true`). Standalone `/sim`
uploads are unaffected.

```sh
PY=~/.config/youtube/venv/bin/python
[ -x "$PY" ] || { echo "bootstrap the separate YouTube environment from README.md" >&2; exit 1; }
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
  `CBRSIM_EMIT_DEC` or `NOPANELS`, and through their profile-specific
  `videos/<stem>/tmp` working directory. Do not use a shared `tmp/sim` for
  player decision output. See `[[shared-machine-sim-coordination]]`.
- The analysis layout is consolidated into:
  - `tools/layout_preview.py`: canonical layout
  - `tools/render_analysis.py`: render real data using that layout
- Change the layout in `layout_preview.py`; `render_analysis.py` imports it.
- Keep `tools/sim.py` as the simulation core.
- Never set `CBRSIM_REUSE=1` manually for `/sim`. Completed-artifact reuse is
  automatic and authenticated; use `CBRSIM_FORCE_REENCODE=1` for an explicitly
  requested clean run.
- `render_analysis.py` is heavy for 3000-frame PIL rendering. It uses `nproc-2`
  parallelism. For long videos, run in the background or delegate to a forked
  context to avoid filling the main conversation context.
