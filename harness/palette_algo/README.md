# Palette algorithm harness

This harness verifies and benchmarks the shared RGB333 lookup-table foundation
used by the legacy `STL4` palette selector and the new `MOSAIC-GM` selector.

Run from the repository root:

```sh
tools/python.sh harness/palette_algo/verify_lut.py
```

When the optional CuPy environment is available, run the same verification on
the GPU path:

```sh
~/.config/cbrsim-gpu/venv/bin/python harness/palette_algo/verify_lut.py
```

The check uses deterministic random RGB333 tiles and deliberate duplicate
palette entries. It requires exact agreement with the former direct-distance
implementation for:

- STL4's L1 palette-training error;
- playback's squared-error palette-line assignment;
- the selected 1-based palette index, including first-minimum ties;
- the CuPy assignment/index path when a GPU is available.

The benchmark is informational. Correctness is always checked before timings
are printed.

The MOSAIC-GM structural checks cover automatic one-line stopping, shared-core
growth for a source with more than 15 useful colours, and fixed HUD extrema:

```sh
tools/python.sh harness/palette_algo/verify_mosaic.py
```

Compare both algorithms on evenly sampled Bad Apple and Sonic master frames:

```sh
~/.config/cbrsim-gpu/venv/bin/python \
  harness/palette_algo/compare_sources.py --frames 60
```

Find the point where adding more learning frames stops improving a fixed
validation set:

```sh
~/.config/cbrsim-gpu/venv/bin/python \
  harness/palette_algo/sample_convergence.py \
  videos/sonic_H32_256x224_pcm13_geometry_pad_4by3/master
```

## Sample-count result (2026-07-16)

Tests used the RTX 3070 Laptop GPU path and a fixed 240-frame validation set.
The score is squared RGB333 reconstruction error plus line-dependent mapping
noise at the default weight of 1.0.

Bad Apple remained on one active line. Training on 30 through 240 frames left
only one RGB333 error unit in one validation pixel; 480 frames reached exact
zero.

| Sonic training frames | Time | Active/core | Pixel | Mapping | Combined |
|---:|---:|---:|---:|---:|---:|
| 30 | 1.560 s | 4 / 8 | 0.273433 | 0.066053 | 0.339486 |
| 60 | 2.445 s | 4 / 6 | 0.224786 | 0.125100 | 0.349886 |
| 120 | 5.339 s | 4 / 6 | 0.263306 | 0.103482 | 0.366789 |
| 240 | 11.386 s | 4 / 4 | 0.212835 | 0.105573 | **0.318408** |
| 480 | 23.672 s | 4 / 8 | 0.287381 | **0.055530** | 0.342911 |
| 960 | 50.596 s | 4 / 6 | 0.227478 | 0.110661 | 0.338138 |
| 1920 | 114.837 s | 4 / 6 | 0.234688 | 0.122989 | 0.357677 |

More frames do not improve this heuristic monotonically: 240 is the best
combined and pixel result, while 480 minimizes mapping noise alone. Doubling
mapping-noise weight still selected 240 on validation (0.403955 versus 0.447055
at 480 and 0.439887 at 960). Production sampling should therefore train the
120/240/480 candidates and select on a small fixed validation set instead of
blindly choosing the largest sample.

The selected one-line candidate is then refined against the complete flattened
RGB333 movie histogram. On the full 6576-frame Bad Apple encode, the histogram
contained 10 colours. Two sample-missed colours replaced duplicate slots,
reducing the complete pre-codec palette error from 13 to exactly zero. The
result uses one active palette line, one CRAM segment, and no CRAM switches.

Compare the actual segmented palettes stored in two decision logs on the same
fixed source frames. In addition to pixel and mapping errors, `seam` measures
how much quantization residual changes specifically across 8x8 boundaries:

```sh
tools/python.sh harness/palette_algo/compare_decisions.py \
  videos/sonic_H32_256x224_pcm13_geometry_pad_4by3/master \
  videos/sonic_H32_256x224_pcm13_geometry_pad_4by3/decisions.pkl \
  videos/SonicJamOp_H32_256x224_pcm13_mosaic_gm/decisions.pkl
```

On the fixed 240-frame validation set, full Bad Apple improved from pixel /
mapping / seam `0.031240 / 0.035559 / 0.073406` under STL4 to exact zero for
all three under MOSAIC-GM. Sonic's pixel error rose slightly from `0.096713` to
`0.100739`, while line-dependent mapping noise fell from `0.105189` to
`0.048295`; the combined score improved 26.2%. Its boundary seam metric moved
from `0.190608` to `0.192575` (+1.0%), showing that shared colours solve line
mapping inconsistency but spatially coherent tile assignment still needs its
own optimization stage.

Sweep the spatial assignment weight and checkerboard iteration count without
re-learning the palettes:

```sh
tools/python.sh harness/palette_algo/compare_decisions.py \
  videos/SonicJamOp_H32_256x224_pcm13_mosaic_gm/master \
  videos/SonicJamOp_H32_256x224_pcm13_mosaic_gm/decisions.pkl \
  --coherent-weight 0 0.25 1 4 8 \
  --coherent-iterations 1 2 4
```

The pair cost compares quantization residuals only at the shared 8x8 edge. It
therefore suppresses a discontinuity created by palette selection without
penalizing a real edge that already exists in the source image.

On Sonic's fixed 240-frame set, two checkerboard passes were enough to
converge. Four passes changed the seam result by at most 0.03%. With two
passes, the visual-error trade-off was:

| Weight | Pixel | Mapping | Seam |
|---:|---:|---:|---:|
| 0 | 0.100739 | 0.048295 | 0.192575 |
| 1 | 0.101206 | 0.048481 | 0.187682 |
| 4 | 0.103136 | 0.051275 | 0.183782 |
| 8 | 0.104619 | 0.053860 | 0.182618 |

Weight 8 lowers the introduced boundary discontinuity by 5.2% while adding
only 0.0039 squared RGB333 units per source pixel. Codec cost is deliberately
not part of this stage's selection criterion.
