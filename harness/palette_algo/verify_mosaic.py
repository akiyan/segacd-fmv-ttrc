#!/usr/bin/env python3
"""Exercise MOSAIC-GM one-line stopping and shared-core growth."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from palette_algorithms import (  # noqa: E402
    MOSAIC_GM, PaletteEvaluator, _counts, build_mosaic_palettes, coherent_assign_idx,
    refine_one_line_palette,
)
from quantize_global4_tiles import (  # noqa: E402
    edge_strengths, rgb333_keys,
)


def tiled(colors, count, seed):
    rng = np.random.default_rng(seed)
    colors = np.asarray(colors, dtype=np.uint8)
    choice = rng.integers(0, len(colors), size=(count, 64))
    return colors[choice]


def main() -> int:
    # Ten exact grayscale colours fit one line. Duplicated hardware rows must
    # then force every tile to line 0 through first-minimum tie behaviour.
    grayscale = [(value, value, value) for value in range(8)] + [(1, 0, 0), (1, 0, 1)]
    palettes, stats = build_mosaic_palettes(
        tiled(grayscale, 512, 1), return_stats=True)
    assert stats["algo"] == MOSAIC_GM
    assert stats["active_lines"] == 1, stats
    np.testing.assert_array_equal(palettes[0], palettes[1])
    np.testing.assert_array_equal(palettes[0], palettes[2])
    np.testing.assert_array_equal(palettes[0], palettes[3])
    for row in palettes:
        brightness = row.astype(np.int16).sum(1)
        assert brightness[0] == brightness.min()
        assert brightness[14] == brightness.max()

    # A full-stream histogram can reveal colours missed by every sample. Spare
    # duplicate slots absorb them without changing already exact colours.
    counts = np.zeros(512, dtype=np.int64)
    for r, g, b in grayscale:
        counts[(r << 6) | (g << 3) | b] = 100
    missed_key = (7 << 6) | (0 << 3) | 7
    counts[missed_key] = 1
    refined, refinement = refine_one_line_palette(palettes[0], counts)
    assert refinement["exact"], refinement
    assert refinement["after_error"] == 0
    assert any(np.array_equal(color, (7, 0, 7)) for color in refined)

    # Two families share a grayscale spine but each needs more specialist
    # colours than one 15-colour line can retain without visible error.
    common = [(value, value, value) for value in range(8)]
    warm = common + [(r, g, 0) for r, g in ((7, 1), (7, 2), (6, 1), (6, 2), (5, 1), (7, 3), (6, 3), (5, 2))]
    cool = common + [(0, g, b) for g, b in ((1, 7), (2, 7), (1, 6), (2, 6), (1, 5), (3, 7), (3, 6), (2, 5))]
    mixed = np.concatenate([tiled(warm, 512, 2), tiled(cool, 512, 3)])
    assignment = np.arange(len(mixed), dtype=np.int16) % 4
    strengths = edge_strengths(mixed)
    evaluator = PaletteEvaluator(
        mixed, weight_strengths=strengths, weight_alpha=3.0)
    keys = rgb333_keys(mixed).reshape(len(mixed), 64)
    expected_raw = np.stack([
        _counts(keys, tile_mask=assignment == line)
        for line in range(4)
    ])
    expected_strength = np.stack([
        _counts(keys, strengths, assignment == line)
        for line in range(4)
    ])
    expected_weighted = expected_raw + 3.0 * expected_strength / 21.0
    np.testing.assert_array_equal(
        evaluator.color_histograms(assignment, groups=4), expected_raw)
    np.testing.assert_array_equal(
        evaluator.color_histograms(assignment, groups=4, weighted=True),
        expected_weighted)

    palettes, stats = build_mosaic_palettes(mixed, return_stats=True)
    assert 2 <= stats["active_lines"] <= 4, stats
    assert stats["core_colors"] >= 2, stats
    assert stats["grows"][0]["accepted"], stats

    # Every emitted row pins the selected darkest/brightest colours at the HUD
    # fixed slots, so sim.py does not need a cross-row swap afterwards.
    for row in palettes:
        brightness = row.astype(np.int16).sum(1)
        assert brightness[0] == brightness.min()
        assert brightness[14] == brightness.max()

    # Two adjacent tiles prefer different lines from their interiors, but the
    # shared source colour maps in opposite directions at the boundary. A
    # sufficiently strong spatial cost should choose one consistent line.
    color_a = np.array((4, 3, 3), dtype=np.uint8)
    color_b = np.array((2, 3, 3), dtype=np.uint8)
    shared = np.array((3, 3, 3), dtype=np.uint8)
    left = np.tile(color_a, (64, 1)).reshape(8, 8, 3)
    right = np.tile(color_b, (64, 1)).reshape(8, 8, 3)
    left[:, -1] = shared
    right[:, 0] = shared
    adjacent = np.stack([left.reshape(64, 3), right.reshape(64, 3)])
    zero = np.zeros((13, 3), dtype=np.uint8)
    spatial_palettes = np.stack([
        np.concatenate([[color_a, (2, 2, 2)], zero]),
        np.concatenate([[color_b, (4, 4, 4)], zero]),
    ])
    independent, _index = coherent_assign_idx(
        adjacent, spatial_palettes, 1, 2, seam_weight=0)
    coherent, _index = coherent_assign_idx(
        adjacent, spatial_palettes, 1, 2, seam_weight=8, iterations=2)
    np.testing.assert_array_equal(independent, (0, 1))
    assert coherent[0] == coherent[1], coherent
    print(
        "MOSAIC-GM exact: grouped histograms, one-line stop, shared-core growth, "
        "and HUD extrema verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
