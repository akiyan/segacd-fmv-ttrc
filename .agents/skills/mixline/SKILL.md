---
name: mixline
description: Validate, align, combine, inspect, and publicly publish matching codec /timeline and playback /hudline PNGs on one shared whole-movie frame axis with a consolidated header. Use when the user invokes /mixline or wants encoder decisions and recorded HUD behavior compared together.
---

# Mixed Codec and HUD Timeline

Combine one matching `/timeline` and `/hudline` result. Trust their machine-
readable layout receipts, not visual guesses.

## Workflow

1. Require both PNGs and their adjacent `<image>.json` receipts. Regenerate an
   older timeline if its receipt is missing.
2. Run:

```sh
tools/python.sh .agents/skills/mixline/scripts/render_mixline.py \
  videos/STEM_timeline.png \
  videos/STEM_hudline.png \
  --output videos/STEM_mixline.png
```

3. The renderer must reject mismatched frame count, fps, pixels per frame,
   plot-left coordinate, plot width, or source image hash. Never resize,
   stretch, or shift one graph to force a match.
4. Inspect the PNG with `view_image`. The codec timeline must be directly
   above the HUD timeline, and the same frame/time gridline must have the same
   x coordinate in both panels.
5. Always publish the combined PNG to a public Gist and show it inline:

```sh
tools/python.sh .agents/skills/timeline/scripts/publish_gist.py \
  videos/STEM_mixline.png \
  --description "SEGA-CD FMV mixed codec/HUD timeline: run label"
```

Report the Gist page, raw PNG URL, and clickable local image path.

## Output contract

- Consolidate both source titles and run specifications into one header at the
  top. Do not retain two full, repetitive headers.
- Preserve both source graph bodies pixel-for-pixel. Crop the duplicated
  headers. Also crop `/timeline` immediately after its final data row so its
  horizontal ticks and lower explanation are omitted; `/hudline` owns the one
  shared horizontal scale and footer.
- Put the `/timeline` graph immediately above the `/hudline` graph with no
  gap or separator.
- Consume current source images and layout receipts on every render; never
  duplicate timeline or hudline row geometry, scales, labels, or colours in
  the compositor. Source presentation changes must flow through automatically.
- Keep the common frame equation:
  `x = 220 + frame * pixels_per_frame`.
- Preserve the hexadecimal `f0xHEX` horizontal frame labels from both source
  graphs and use the same notation for the consolidated EVAL range.
- Write `<output>.json` with both source hashes, source receipts, shared frame
  geometry, and the y range of each panel.
- Treat the mixed image as diagnostic evidence. Preserve green `PASS`, yellow
  `WARNING`, and red `FAIL`; never hide or relabel a warning or failure.

## Resource

`scripts/render_mixline.py` is the canonical deterministic compositor.
