# Source geometry harness

This is the single source of truth for converting square-pixel source video
to Mega Drive H32/H40 rasters.  H32 and H40 have different horizontal dot
ratios, so scaling a source directly to `256x224` or `320x224` stretches it.

The helper uses the hardware dot ratios and fits the complete source to the
resulting display aspect before scaling.  It also reads the source video's
sample-aspect-ratio metadata, so an anamorphic input is measured by its
displayed width rather than its coded pixel width:

| mode | raster | HAR/PAR | visible DAR at 224 lines |
| --- | ---: | ---: | ---: |
| H32 | 256x224 | 8:7 | 64:49 (about 1.306) |
| H40 | 320x224 | 32:35 | 64:49 (about 1.306) |

Inspect a plan for a source:

```sh
tools/python.sh tools/video_geometry.py --src assets/SonicJamOp.mp4 --mode H32
tools/python.sh tools/video_geometry.py --src assets/SonicJamOp.mp4 --mode H40
```

`tools/sim.py` uses the same helper whenever `CBRSIM_MASTER_VF` or
`CBRSIM_RAW_VF` is not explicitly supplied.  The default `pad` fit preserves
all source pixels with the smallest possible border.  Set
`CBRSIM_GEOMETRY_FIT=crop` only when the outer margins are confirmed blank;
explicit filters remain available for unusual sources.
If the file has missing or incorrect SAR metadata, pass `--source-sar` to the
CLI or set `CBRSIM_SOURCE_SAR`.  For a 576x400 raster authored as 4:3, use
`25:27`; the pad border then shrinks to about three lines.
When `CBRSIM_MODE=H40` is selected without an explicit width, the sim also
selects the matching 320-pixel raster (H32 keeps its 256-pixel default).

For a 576x400 square-pixel source, the default pad fit keeps all pixels and
places a 256x202 (H32) or 320x202 (H40) image inside the target raster.  The
resulting top/bottom border is only 11 lines.  The former 522x400 centred
crop remains available with `--fit crop`, but it must not be used when the
side edges contain picture content.  The same plan is printed as JSON by the
CLI and can be used by standalone ffmpeg harnesses.

The helper also builds the optional RGB `endpoint_snap` source-preprocessing
filter. It runs before both the denoised master path and the raw Source-panel
path; its public settings are documented in `CONFIG.md`.
The same TOML video section can select the resize filter and disable the
master-only denoise/blur pass for already-clean sources such as Bad Apple.
