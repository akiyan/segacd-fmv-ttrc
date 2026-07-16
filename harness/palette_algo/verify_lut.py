#!/usr/bin/env python3
"""Verify RGB333 palette LUTs against the former direct-distance path."""

from __future__ import annotations

import sys
import os
from pathlib import Path
from time import perf_counter

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from quantize_global4_tiles import (  # noqa: E402
    build_palettes, edge_weights, palette15, palette_lut, rgb333_keys, tile_errors,
)
from palette_algorithms import coherent_assign_idx  # noqa: E402


def direct_tile_errors(tiles, pal):
    pixels = tiles.reshape(-1, 3).astype(np.int64)
    distance = np.abs(pixels[:, None, :] - pal[None, :, :].astype(np.int64)).sum(2)
    return distance.min(1).reshape(len(tiles), 64).sum(1)


def direct_assign_idx(tiles, palettes):
    cells = len(tiles)
    pixels = tiles.reshape(cells, 64, 1, 3).astype(np.int64)
    error = np.stack([
        ((pixels - pal.reshape(1, 1, 15, 3).astype(np.int64)) ** 2)
        .sum(3).min(2).sum(1)
        for pal in palettes
    ], axis=1)
    assign = error.argmin(1).astype(np.int8)
    selected = palettes[assign]
    distance = ((tiles.reshape(cells, 64, 1, 3).astype(np.int64)
                 - selected.reshape(cells, 1, 15, 3).astype(np.int64)) ** 2).sum(3)
    return assign, (distance.argmin(2) + 1).astype(np.uint8)


def direct_build_palettes(tiles, n_pal=4, iterations=6):
    """Former STL4 learning loop, kept here as a bit-exact reference."""
    weight = edge_weights(tiles, float(os.environ.get("CBRSIM_EDGE_WEIGHT", "3.0")))

    def selected_weight(mask):
        return None if weight is None else weight[mask].reshape(-1)

    means = tiles.reshape(len(tiles), 64, 3).mean(1)
    groups = np.array_split(np.argsort(means.sum(1)), n_pal)
    palettes = [
        palette15(tiles[group].reshape(-1, 3), weights=selected_weight(group))
        for group in groups
    ]
    for _ in range(iterations):
        error = np.stack([direct_tile_errors(tiles, pal) for pal in palettes], axis=1)
        assign = error.argmin(1)
        palettes = [
            palettes[line] if not (assign == line).any()
            else palette15(
                tiles[assign == line].reshape(-1, 3),
                weights=selected_weight(assign == line),
            )
            for line in range(n_pal)
        ]
    return np.stack(palettes)


def lut_assign_idx(tiles, palettes):
    keys = rgb333_keys(tiles)
    tables = [palette_lut(pal, squared=True) for pal in palettes]
    cost = np.stack([table[0] for table in tables])
    index = np.stack([table[1] for table in tables])
    assign = cost[:, keys].sum(2, dtype=np.int64).T.argmin(1).astype(np.int8)
    return assign, (index[assign[:, None], keys] + 1).astype(np.uint8)


def timed(callable_, repeats=3):
    best = float("inf")
    for _ in range(repeats):
        start = perf_counter()
        callable_()
        best = min(best, perf_counter() - start)
    return best


def main() -> int:
    rng = np.random.default_rng(0x4D4F53414943)
    tiles = rng.integers(0, 8, size=(8192, 64, 3), dtype=np.uint8)
    palettes = rng.integers(0, 8, size=(4, 15, 3), dtype=np.uint8)
    palettes[:, -2:] = palettes[:, :2]  # exercise first-minimum tie behaviour

    for pal in palettes:
        np.testing.assert_array_equal(tile_errors(tiles, pal), direct_tile_errors(tiles, pal))
    expected_assign, expected_index = direct_assign_idx(tiles, palettes)
    actual_assign, actual_index = lut_assign_idx(tiles, palettes)
    np.testing.assert_array_equal(actual_assign, expected_assign)
    np.testing.assert_array_equal(actual_index, expected_index)
    frame_tiles = tiles[:896]
    coherent_assign, coherent_index = coherent_assign_idx(
        frame_tiles, palettes, 28, 32, seam_weight=8, iterations=2)

    training_tiles = tiles[:768]
    np.testing.assert_array_equal(
        np.stack(build_palettes(training_tiles, n_pal=4)),
        direct_build_palettes(training_tiles, n_pal=4),
    )
    print("STL4 training exact: learned palettes match the direct-distance reference")

    direct_s = timed(lambda: direct_assign_idx(tiles, palettes))
    lut_s = timed(lambda: lut_assign_idx(tiles, palettes))
    print(f"CPU exact: 8192 tiles  direct={direct_s:.4f}s  lut={lut_s:.4f}s  speedup={direct_s / lut_s:.2f}x")

    try:
        import gpu_quant
        if gpu_quant.enabled():
            cache = gpu_quant.PalCache()
            gpu_assign, gpu_index = gpu_quant.assign_idx_one(tiles, 0, [palettes], cache)
            np.testing.assert_array_equal(gpu_assign, expected_assign)
            np.testing.assert_array_equal(gpu_index, expected_index)
            gpu_coherent_assign, gpu_coherent_index = gpu_quant.assign_idx_one(
                frame_tiles, 0, [palettes], cache, coherent_shape=(28, 32),
                seam_weight=8, seam_iterations=2)
            np.testing.assert_array_equal(gpu_coherent_assign, coherent_assign)
            np.testing.assert_array_equal(gpu_coherent_index, coherent_index)
            gpu_independent_s = timed(lambda: gpu_quant.assign_idx_one(
                frame_tiles, 0, [palettes], cache))
            gpu_coherent_s = timed(lambda: gpu_quant.assign_idx_one(
                frame_tiles, 0, [palettes], cache, coherent_shape=(28, 32),
                seam_weight=8, seam_iterations=2))
            print("GPU exact: independent and coherent assignment match CPU references")
            print(
                f"GPU frame: 896 tiles independent={gpu_independent_s:.4f}s "
                f"coherent={gpu_coherent_s:.4f}s")
        else:
            print("GPU skipped: CuPy/CUDA unavailable")
    except Exception as exc:  # keep the CPU proof useful on non-GPU systems
        raise SystemExit(f"GPU verification failed: {exc}") from exc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
