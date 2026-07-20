# /analysis: update the analysis overlay as one atomic set

Use this whenever you change ANYTHING in the analysis frame (a meter, a colour,
a category, a timeline, a heading, the effective-Band definition, etc.). It
keeps the code, the reference doc, and the user notification in lockstep so the
"what does this number mean" understanding never drifts again.

The analysis frame is 1920x1080, drawn by `tools/render_analysis.py` using the
drawing functions and layout constants in `tools/layout_preview.py`
(the layout source of truth). The reference for every element is `ANALYSIS.md`.

## The set (do all four, in order)

1. **Change the layout in `tools/layout_preview.py`** (the source of truth).
   Then propagate the same change to `tools/render_analysis.py`, which reuses
   `layout_preview`'s drawing helpers on real encoder data. Anything the real
   renderer needs that the encoder must supply (a new per-frame value, etc.)
   goes into `tools/sim.py` and its saved `stats.npz` /
   `buffer_remaining.npz`, and is read back in `render_analysis.py`.

2. **Regenerate and eyeball the dummy preview**:
   ```sh
   tools/python.sh tools/layout_preview.py     # writes tmp/layout_preview.png
   ```
   Crop and view the changed region to confirm it looks right. If the change
   depends on real encoder values (e.g. a value newly saved by the sim), also
   render one real frame to verify - respecting the exclusion rule
   (see AGENTS.md "Shared-Machine Exclusion"):
   ```sh
   CBRSIM_OUT=tmp/<somesim> CBRSIM_SRC=<src> CBRSIM_MODE=<mode> \
     tools/python.sh tools/render_analysis.py <N> <N+1>   # one frame, no mp4
   ```

3. **Update `ANALYSIS.md`** so every element still matches exactly: the ASCII
   layout map at the top, the affected meter/timeline/category description, the
   colour list, and any threshold/formula. `ANALYSIS.md` must be complete and
   correct - it is the contract that prevents future misunderstandings. Be
   especially precise about the tile categories (Raw/Same/Near/Coa/Flbk/
   Buf/Miss): their meaning, byte cost, thresholds, and selection order.

4. **Notify with the preview via Telegram** (the user reviews layout there):
   ```sh
   ~/.claude/skills/telegram-notify/telegram_send.sh file tmp/layout_preview.png \
     "<one line: what changed and why>"
   ```

## Then

- Commit `tools/layout_preview.py`, `tools/render_analysis.py`, any
  `tools/sim.py` change, and `ANALYSIS.md` together (Japanese commit
  message per AGENTS.md). Push only if asked.

## Notes

- Do not let the code and `ANALYSIS.md` diverge - they change in the same commit.
- Layout edits start in `layout_preview.py`; `render_analysis.py` mirrors them.
- Meter widths are each label-width (no unified width). Band is useful
  `BODY.DAT` delivery in the physical slot (payload + control, excluding pad,
  HEADER, and frame 0), with CD 1x retained as a comparison line.
- If a new value must come from the encoder, add it to the sim's saved npz and
  read it in `render_analysis.py`. Do not infer physical delivery metrics from
  older sim outputs; require a re-sim when the trace is absent.
