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
- Because playback is steady (constant offset, verified), the renderer does not
  read every frame; it finds the **head offset** once
  (`offset = median(k - F)` over confident reads) and uses `F = k - offset`. This
  advances the right panel and the footer smoothly, one frame at a time (same
  update cadence as the analysis overlay), with no stutter.
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
- `tools/export_sim_video.py` — writes a straight sim video (video+audio, no
  overlay); a standalone clean "Encoder ideal output" clip.

## Output paths

Follows AGENTS.md "Output Paths": `videos/<stem>_comparison.mp4` for the
side-by-side, `videos/<stem>_sim.mp4` for the straight sim clip,
`videos/<stem>_emu.mp4` for the emulator recording.

## Known limitations / TODO

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
