# CRAM switch opportunity (intelligent palette switching)

Measuring whether adding CRAM (palette) switch points beyond the current
darkness-based segmentation would improve colour, and where those points can go.

Background: the encoder swaps the whole 60-colour CRAM at "segments". Today a
segment boundary is only placed at a deep blackout (`CBRSIM_SEG_DARK` in
`tools/sim.py`), on the theory that a switch is cheap there (the screen is black,
so the cache loss and colour pop are hidden). Switching mid-scene is expensive:
cross-segment tile reuse is forbidden, so every visible tile must reload (a cold
burst), and the whole picture's colours change in one frame (a visible pop).

## Scripts

- `measure_gain.py` — for a candidate switch frame `i` and a forward window
  `[i, i+W)`, compares the quantization error of painting the window with the
  **current** segment palette (`E_keep`) vs a palette **freshly learned** on the
  window (`E_fresh`). `gain = (E_keep - E_fresh) / E_keep`. Runs the reference
  set (existing boundaries), the candidate set (mid-segment scene cuts detected
  from `mad_ed.npy`), and a control set (quiet mid-segment frames). Read-only;
  reuses the sim's cached `master/` frames and palette machinery.
  Run: `CBRSIM_OUT=… CBRSIM_MODE=H40 CBRSIM_W=320 CBRSIM_H=224 CBRSIM_GPU=1 python -u measure_gain.py`
  (needs the GPU venv for speed; CPU works but is ~10x slower).
- `mad_ed.npy` — per-frame consecutive-frame luma MAD (scene-change signal),
  produced inline (see git history); peaks `> mean+3σ` are the hard-cut candidates.

## Findings (machi_ed, full-screen H40, 3998 frames)

The darkness-based segmentation makes 14 segments. Thirteen are well sized
(~9.6 s each), but **segment 12 spans frames 2071–3904 = 1833 frames = 122 s —
almost half the 267 s movie on a single palette.** The ending's final stretch
(photo montage + theme song) never fully fades to black, so the darkness rule
never split it.

Gain measurement:

- **Existing boundaries**: 30–86 % error reduction (mean ~55 %). The darkness
  boundaries are genuinely good switch points.
- **Mid-segment scene cuts inside segment 12**: 5–55 % (mean ~30 %), 12/16 above
  10 %.
- **Control (quiet, non-cut frames inside segment 12)**: also high — 2500 → 48 %,
  3300 → 49 %.

The key insight: **the control is as high as the candidates.** The available gain
is not specific to the scene cuts — it is that segment 12's one palette is stale
almost everywhere in those 122 s. So the real opportunity is *"an under-served
long segment needs more palettes,"* not *"a hard cut needs a switch."*

## What this means for the design

- The **trigger** is colour staleness (`E_keep − E_fresh` large), not the hard
  cut by itself.
- The **allowed location** for a switch is still a hard cut (cache loss ≈ 0,
  colour pop hidden) — that constraint is unchanged.
- So the intelligent switch = **subdivide an under-served segment by inserting
  palette switches at the hard cuts inside it where the staleness gain clears a
  threshold, gated by the physical pattern-supply plan having room for the
  cold reload.**
- Segment 12 has ~10 usable hard cuts (2251, 2435, 2533, 3027, 3088, 3178, 3474,
  3531, 3610, 3681); splitting it there would bring each piece down toward the
  ~9.6 s size of the other segments and realise much of the 20–55 % gain across
  ~half the movie.
- Foundation already shipped: CRAM pre-load (PALTAB, MOVIE.DAT v3) means adding
  switch points costs no extra stream bytes and is slip-safe, and the table holds
  up to `PALTAB_MAX_SEG` = 64 segments (14 today, ample headroom).

## Generalizing the trigger: "uniform screen", not "deep black"

Deep black is really a proxy for *"the whole screen is one flat colour, so tile
reuse is maximal and a CRAM swap costs almost nothing and its colour pop is
hidden."* Black is just one such flat colour — a white-out or any flat-colour
fade is an equally cheap switch point. `uni_ed.npy` / `uni_op.npy` measure, per
frame, `uni` = fraction of pixels within 24 (RGB Euclidean) of the frame's mean
colour (flatness, colour-agnostic) alongside the current `dark` metric.

Finding — the two sources differ:

- **machi_op HAS them.** Three non-black uniform moments the black-only detector
  misses, two deep mid-segment: F1161 (meanL 236, uni 1.00, a white-out, 174
  frames from any boundary), F1874-1883 (meanL 160, a bright flat stretch, 252
  frames from a boundary), F1579 (white-out). Switching right after each gives a
  huge colour gain: F1163 → 73 %, F1581 → 49 %, F1884 → 83 % — as good as the
  best existing boundaries. So generalizing `dark >= 0.90` to `uniform >= thr`
  (black OR white OR any flat colour; seam at the most-uniform frame; everything
  downstream unchanged) directly adds high-value switch points on op. Low risk:
  it only broadens the existing safe mechanism.
- **machi_ed does NOT.** Segment 12 (122 s) has essentially zero non-black
  uniform frames — its transitions are detailed-to-detailed **hard cuts** (a
  single-frame MAD spike over a busy ~11 MAD background, screen never flat,
  meanL ~30-40). The uniform detector can't help ed; only hard-cut detection can.

So the two mechanisms are complementary:

1. **Generalized uniform-screen detector** (black/white/flat) — a low-risk
   broadening of today's code; demonstrably helps op. Do this first.
2. **Hard-cut detector inside under-served long segments** — for ed-like content
   whose long segments have no uniform moment; higher risk (pop at a detailed
   cut, cold burst), needs a pattern-supply feasibility gate.
