---
name: timeline
description: Render and show one large, detailed whole-movie timeline PNG from a codec-analysis TSV, with the canonical Req/supply/run/Band heatmaps and fixed scales. Use after every encoder adjustment or comparison, when the user asks for a timeline, heatmap, TSV visualization, or a visual A/B summary.
---

# Analysis Timeline

Create a consistent whole-movie diagnostic image from the exact TSV used by
the analysis overlay. The image is a comparison artifact, so preserve scales
and include the settings that explain the result.

## Workflow

1. Locate the adjustment-specific analysis TSV. If it does not exist, run
   `tools/render_analysis.py` for that simulation output; it writes the TSV
   before rendering frames.
2. Pass the matching profile and simulation output directory. Do not combine a
   TSV from one run with metadata from another.
3. When an intentional tail-drain rule would distort evaluation, pass its first
   frame with `--evaluation-end-frame`. The timeline still shows the complete
   movie; the excluded tail is shaded and totals show both scopes.
4. Run the bundled renderer with the locked project environment:

```sh
tools/python.sh .agents/skills/timeline/scripts/render_timeline.py \
  logs/YYYYMMDD-HHMMSS-ffffff_PROFILE_SHA10_eNN.tsv \
  --config configs/PROFILE.toml \
  --sim-out videos/STEM/ADJUSTMENT \
  --label "short adjustment label" \
  --evaluation-end-frame FRAME \
  --output videos/STEM_ADJUSTMENT_timeline.png
```

5. Inspect the PNG with `view_image`. Check that the full time axis, tail
   marker, category colours, four row scales, and labels are legible.
6. Publish the PNG as a public GitHub Gist. The helper creates a Git-backed
   public Gist so the binary PNG is preserved exactly, writes a
   `<image>.gist.json` receipt, and returns both the Gist page and raw PNG URL:

```sh
tools/python.sh .agents/skills/timeline/scripts/publish_gist.py \
  videos/STEM_ADJUSTMENT_timeline.png \
  --description "SEGA-CD FMV codec timeline: adjustment label"
```

7. Show the image inline in the conversation on every reported adjustment.
   Also give a clickable path, but never substitute a path-only response for
   the inline image.
8. Before uploading the matching analysis video to YouTube, add the public raw
   PNG URL and Gist page URL to both the English and Japanese sections of the
   video description. Keep the links after the encoder details and before each
   language section's project link. The helper updates the local description
   idempotently and, when the video is already uploaded, synchronizes that
   description through the ordinary YouTube upload/edit credentials:

```sh
PY="$HOME/.config/youtube/venv/bin/python"
"$PY" .agents/skills/timeline/scripts/sync_youtube_description.py \
  videos/STEM_ADJUSTMENT_analysis.mp4 \
  --timeline-receipt videos/STEM_ADJUSTMENT_timeline.png.gist.json \
  --description-file videos/STEM_ADJUSTMENT_analysis_description.txt
```

For a video that has not been uploaded yet, pass `--local-only`, then use the
updated description file for the upload. Do not put the link in a YouTube
comment unless the user separately asks for a comment.

Keep the Gist public and the video unlisted unless the user asks for a
different video privacy level. Public Gist publication is an external write;
perform it only when the user requested this timeline workflow.

## Required content

Keep these parts in every image:

- The canonical four whole-movie rows, using the analysis colours and fixed
  scales: Req categories; physical Prg plus combined Wrd remaining; physical
  cold-run count versus the measured cold cap; and useful BODY delivery split
  into Raw payload, Prg charge, and control versus physical slot bytes. Raw is
  the bottom Band segment, matching its leftmost position in the status bar.
  Keep Supply and Band compact so the dedicated RUN row remains visible without
  increasing the overall timeline height.
- Show explicit vertical-axis ticks and horizontal guides at zero, half-scale,
  and full-scale on every row. Put the units below each row heading, not on
  every tick: Req uses cells, Supply uses patterns, and RUN uses runs. Band
  remains a percentage of each frame's physical slot.
- Segment boundaries, five-second labels, exact frame-per-pixel mapping, and a
  clearly shaded excluded tail when requested.

Use at least two pixels per frame when practical. Do not normalize each run to
its own observed peak: fixed scales are what make successive images visually
comparable.

## Interpretation safeguards

- `Prg` in the supply row is physical PrgBuf occupancy. It is not the virtual
  whole-movie quality allowance.
- `quality allowance` is encoder-only accounting and does not appear in the
  physical supply stack.
- `Buf` is not a physical meter. Report exact Prg/Wr/Dic sources instead.
- Run consolidation is diagnostic opportunity, not a promise that every saved
  32B could become one useful exact tile; changed residency and run grouping
  can alter the result.
- Do not treat raw Miss-frame or tile-frame totals as direct visual loss.
  Isolated one-frame Miss cells at 30 fps may be imperceptible and are often
  rescued immediately. Flag large simultaneous areas, consecutive any-Miss
  frames, and repeated same-cell streaks separately.
- Frame 0 is boot construction. Exclude it from timed totals.

## Resource

`scripts/render_timeline.py` is the canonical deterministic renderer. Update
and test that script instead of writing one-off plotting snippets.
