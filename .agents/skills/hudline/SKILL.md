---
name: hudline
description: Render, inspect, and publish one large whole-movie PNG from a DEBUG playback HUD TSV and its matching gate JSON. Use after every full emulator or hardware recording, when the user invokes /hudline, or when S/D/R/C/M/J and the remaining HUD fields need frame-by-frame visual comparison.
---

# Playback HUD Timeline

Create one deterministic full-recording diagnostic image after the complete HUD
OCR pass. Keep the image frame-aligned with `/timeline` so a future `/mixline`
can combine both without resampling.

## Workflow

1. Require the HUD TSV and gate JSON generated from the same lossless recording.
   The renderer checks the first loop, contiguous frame numbers, expected frame
   count, profile SHA, gate maxima, and recording size/mtime when available.
   Render failed gates too; the image is evidence and must not hide a failure.
2. Run:

```sh
tools/python.sh .agents/skills/hudline/scripts/render_hudline.py \
  videos/STEM_emu_hud.tsv \
  --gate-json videos/STEM_emu_hud_gate.json \
  --config configs/PROFILE.toml \
  --label "short run label" \
  --output videos/STEM_hudline.png
```

3. Inspect the PNG with `view_image`. Confirm that every frame is present, the
   gate summary matches the JSON, gate-limit lines are visible, palette
   boundaries and their `Pxx` labels align, and all HUD rows are legible.
4. Generate the exact warning/over-limit frame table:

```sh
tools/python.sh .agents/skills/hudline/scripts/report_overages.py \
  videos/STEM_emu_hud.tsv \
  --gate-json videos/STEM_emu_hud_gate.json \
  --output videos/STEM_emu_hud_warnings.md
```

   For exact integer-VBlank rates, treat every derived `VBLANK` value different
   from the normal cadence as a warning: 15 fps expects 4 and 30 fps expects 2.
   Report VBLANK only as `warning rate / warning count / evaluated total`; do
   not add individual VBLANK-warning frames to the event table. This warning
   does not turn an otherwise passing upload gate into a failure; report the
   overall HUD state as `WARNING`. Do not apply this generic rule to 24 fps: its
   expected 2/3 cadence needs a separate profile-specific rule when a 24 fps
   work is tuned.

   Show the resulting Markdown table in the response. It must include
   hexadecimal `F`, every gate-overage value/limit, derived `VBLANK`, and
   every HUD value available in the TSV (`P/S/D/R/L/C/W/M/A/U/N/J/V/O/E`).
   For per-frame `C/M`, include every over-limit frame. Label a `C` over-limit
   event `WARNING`; it remains upload-capable. Label other over-limit events
   `FAIL`. For cumulative `S/D/R`
   and sticky-peak `J`, include only transitions to a new over-limit value
   rather than repeating unchanged state on every later frame. A gate value
   equal to its limit is not an overage.
5. Publish the exact PNG to a public Gist:

```sh
tools/python.sh .agents/skills/timeline/scripts/publish_gist.py \
  videos/STEM_hudline.png \
  --description "SEGA-CD FMV playback HUD timeline: short run label"
```

6. Show the image inline in the conversation and provide the Gist page plus raw
   PNG URL. Do this after every completed recording, whether the gate passes or
   fails. Public Gist publication is authorized only when the user requested
   this workflow.

## Image contract

- Use the complete first movie loop. Frame 0 remains visible.
- Keep `/timeline`'s horizontal contract: left edge 220 px, the same automatic
  pixels-per-frame rule, and `x = 220 + frame * pixels_per_frame`.
- Put `VBLANK` first. Derive it from the difference between consecutive
  `capture_first` values: because HUD `F` is published with the displayed
  image, this is the number of scanouts for which that content frame was
  actually visible. Draw the expected cadence
  (`vsync_n_for_fps(content_fps)`, so 15 fps is 4) as a green guide line. Use
  neutral gray for healthy samples and yellow for nonzero deviations.
  Leave the final frame unknown and exclude it from the statistics because its span is
  the recorder's terminal hold, not playback cadence. For 24 fps, retain the
  measured row but defer its normal-line and warning rule until its 2/3 cadence
  is specified.
- Include the values-only HUD fields `S/D/R/L/C/W/M/A/U/N/J` and `V/O/E` when
  present. `F` is the x-axis. Do not allocate a separate `P` row: palette is
  represented by the `Pxx` switch labels and vertical boundaries on the shared
  horizontal axis.
- Put the six upload-gate rows first and show their exact limits:
  `S/D/R/C/M/J`. Show the cadence's normal jitter interval separately from the
  absolute J gate limit. Keep `S/D/R` at 23 px (half the normal row height)
  with no unit subheading, but use the same heading font size as every other
  HUD metric.
- Follow with the remaining player-state, Sub, Main, and phase rows. Preserve
  HUD units instead of normalizing each recording to its observed peak. Every
  HUD vertical axis shows only its maximum label; omit all midpoint and zero
  labels.
- Preserve the established per-metric colors for normal playback values:
  C yellow, M orange, J and W purple, L blue, A pink, U cyan, N orange, and
  the corresponding established colors for V/O/E. Only override that metric
  color when the individual sample is `WARNING` or `FAIL`; use yellow for a
  warning and red for a failure. Do not recolor an entire row because another
  frame or another metric changed the overall gate status.
  Keep the guide scale colorful: gate limits are orange, J's normal jitter
  interval is yellow, and the VBLANK normal cadence is green. The one
  deliberate severity exception is a `C` over-limit sample: the gate remains
  `WARNING`, but its bar is red as the requested fail-like visual emphasis.
- Show horizontal frame labels as hexadecimal `f0xHEX`. Show every HUD
  vertical-axis maximum and gate/normal limit as `0xHEX`. Full 8-bit rows use
  the compact row height. Keep the unit subheading at 13 px.
- `/mixline` consumes this image and its layout receipt directly. Any hudline
  row-height, scale, or colour change must therefore appear automatically in
  the next mixline without a second hard-coded layout.
- Write a `<output>.json` layout receipt containing the input hashes, frame
  mapping, row geometry, fixed scales, gate limits, and recording identity.
  `/mixline` should consume this receipt rather than rediscovering geometry
  from pixels.

## Interpretation safeguards

- `S`, `D`, and `R` are cumulative counters; their transition is the event.
- `VBLANK` is derived after recording from consecutive displayed `F`
  transitions; it is not another player HUD field. A 15 fps frame at 4 is
  normal, while every other value is a warning. This rule is deliberately not
  generalized to 24 fps yet.
- `J` is a cumulative PrgBuf excess high-water mark, not current occupancy.
- When a gate fails, never report only the maximum. Include the over-limit
  table from `report_overages.py` so the exact workload and phase values at
  each gate event are preserved. Keep VBLANK warnings aggregate-only.
- Nonzero `C` or `M` is not automatically a failure; compare it with the
  cadence-specific gate line. `C` above that line is `WARNING`, not `FAIL`,
  but draw the over-limit bar in failure red so the exact frames stand out.
- `V` and `O` displayed on frame F describe the flip that published frame
  F-1. `E` belongs to frame F.
- OCR confidence and sample repetition are extraction evidence, not player HUD
  fields. Keep their minima/totals in the heading rather than inventing rows.
- Call an emulator capture an emulator recording, not a physical-hardware
  recording.

## Resource

`scripts/render_hudline.py` is the canonical renderer and
`scripts/report_overages.py` is the canonical over-limit event reporter.
Update and test these scripts instead of writing one-off plots or tables.
