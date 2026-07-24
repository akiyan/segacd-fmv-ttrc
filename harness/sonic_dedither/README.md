# Sonic source-dither comparison

This harness compares source-side filters for the one-pixel checkerboard
dither in `SonicJamOp.avi`. It exists to keep that source pattern separate from
the codec's later position-fixed Bayer output dither.

The comparison reports:

- `flat_checker`: the mean 2x2 diagonal-checker response where a lightly
  blurred source has little local gradient. Smaller means less source dither.
- `edge_ratio`: filtered strong-edge amplitude divided by the source value.
  Values near 1 retain the drawn edges.
- `edge_correlation`: agreement between source and filtered strong-edge
  shapes. Values near 1 mean the edges remain in the same places.
- `change_mae`: mean absolute RGB change from the source.

Run it with the project Python environment and a new output directory:

```sh
out=$(mktemp -d /tmp/sonic-dedither.XXXXXX)
rmdir "$out"
tools/python.sh harness/sonic_dedither/compare.py \
  --source assets/SonicJamOp.avi \
  --output "$out"
```

The default frames are `0x012B` and `0x0232`. The harness writes UTF-8 TSV
metrics plus a nearest-neighbour crop sheet for each frame. It refuses to reuse
an existing output directory so stale frames cannot enter a comparison.

For these frames, `guided=radius=1:eps=0.002:planes=15` gives the preferred
balance. It reduces the flat checker response to roughly one sixth of the
source while retaining the gentle sky gradients and more than 99% edge-location
correlation on frame `0x0232` (about 99% across both checks). `radius=1` keeps
the neighbourhood tight, `eps=0.002` makes the
edge threshold strict, and `planes=15` covers luma and chroma. The older
upscale + `hqdn3d` + Gaussian blur path removes more edge and gradient detail
because its blur is not edge-aware.
