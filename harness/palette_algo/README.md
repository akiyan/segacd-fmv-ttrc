# Palette algorithm harness

This harness verifies and benchmarks the shared RGB333 lookup-table foundation
used by the legacy `STL4` palette selector and the new `MOSAIC-GM` selector.

Run from the repository root:

```sh
python3 harness/palette_algo/verify_lut.py
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
