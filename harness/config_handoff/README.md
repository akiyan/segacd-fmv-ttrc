# TOML-to-decision-log handoff regression

This harness proves the configuration boundary used by the encoder and packer:

1. A per-source TOML profile overwrites inherited per-source `CBRSIM_*` values.
2. `sim.py` freezes the resolved geometry, timing, audio, hardware and pack
   settings in `decisions.pkl`.
3. `pack_stream.py` reads those values from the decision log and produces the
   same `HEADER.DAT`, `BODY.DAT`, `MOVIE.DAT` and `palettes.bin` even when its
   process receives deliberately wrong H40/15fps/ADPCM environment values.

Run it against an existing full decision log:

```sh
tools/python.sh harness/config_handoff/verify.py \
  videos/SonicJamOp_H32_256x224_pcm13_mosaic_gm/decisions.pkl
```

The test uses temporary output directories and does not modify `out/movieplay`.
