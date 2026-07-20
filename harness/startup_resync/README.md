# Startup audio re-sync HUD extractor

This harness reads a native DEBUG playback recording sequentially and finds the
first audio re-sync (`R`) without seeking by eye. It uses the player's fixed
top-row values-only HUD; the internal key order remains
`F/P/S/D/R/L/C/W/M/A`, followed by `U/N` in H40:

```text
H32: xxxx xx xx xx xx xx xx xx xx xx
H40: xxxx xx xx xx xx xx xx xx xx xx xxxx xx
```

The startup-specific fields are:

- `L`: audio reserve in 256-byte units;
- `C`: blocking CD sector pumps (current control plus older BODY payload/pad);
- `W`: Main's wait for Sub completion at `CMD_SWAP`, in approximate scanlines;
- `M`: Main-side VBlank-start waits while applying pattern DMA;
- `A`: Sub ADPCM decode time in four-stopwatch-tick units (about 0.1229 ms
  per displayed unit); PCM builds show zero.
- `U` (H40): Main pattern-transfer time in 30.72 us Mega-CD stopwatch ticks;
- `N` (H40): low byte of the packed cold-run descriptor count before VBlank
  splits; it wraps at 256.

The startup fields use two hexadecimal digits. `U` uses four digits and `N`
uses two. The extra counters exist only in a `DEBUG=1` player and add no DMA.

Every capture frame is decoded by `ffmpeg` as a small grayscale rawvideo crop.
`tools/read_frameno.py:read_hud` reads all visible fields.  A sample is accepted only
when every field meets the confidence threshold, then repeated capture frames
with the same `F` value are aggregated.  This matters because a 29.97 fps movie
frame normally appears in about two frames of a 59.94 fps recording.

Run it against the lossless output from `/record`:

```sh
tools/python.sh harness/startup_resync/analyze.py \
  videos/SonicJamOp_startup_audio2_ab_debug_lossless.mkv \
  --csv videos/SonicJamOp_startup_audio2_ab_debug_hud.csv
```

The console report shows every `R` transition, its movie-frame number in hex and
decimal, and the surrounding `L/C/W/M/A` values. The CSV contains one row per
aggregated movie frame. Transition rows additionally carry the previous and next
lead, which makes preload-to-live boundary failures easy to compare between A/B
recordings.

The default crop begins at native x=0.  A legacy 320-pixel recording whose H32
image is centered with 32 pixels on the left can be read with `--crop-x 32`.
Lower `--confidence` only for older transparent-background HUD recordings; the
current black diagnostic row should pass the default `0.90` threshold.

This harness is diagnostic only.  Do **not** use its HUD timestamps to trim an
upload, remove the Mega-CD startup, or place YouTube chapters.  Publication
recordings keep the startup intact, and chapter offsets are determined by
ordinary visual playback as specified in `AGENTS.md`.
