#!/usr/bin/env python3
"""Exercise MOSAIC-GM one-line stopping and shared-core growth."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from palette_algorithms import MOSAIC_GM, build_mosaic_palettes  # noqa: E402


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

    # Two families share a grayscale spine but each needs more specialist
    # colours than one 15-colour line can retain without visible error.
    common = [(value, value, value) for value in range(8)]
    warm = common + [(r, g, 0) for r, g in ((7, 1), (7, 2), (6, 1), (6, 2), (5, 1), (7, 3), (6, 3), (5, 2))]
    cool = common + [(0, g, b) for g, b in ((1, 7), (2, 7), (1, 6), (2, 6), (1, 5), (3, 7), (3, 6), (2, 5))]
    mixed = np.concatenate([tiled(warm, 512, 2), tiled(cool, 512, 3)])
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
    print("MOSAIC-GM exact: one-line stop, shared-core growth, and HUD extrema verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
