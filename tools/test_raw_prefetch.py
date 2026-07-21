#!/usr/bin/env python3
"""Tests for the cheap future raw-pattern forecast."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from raw_prefetch import forecast_requests


class RawPrefetchForecastTests(unittest.TestCase):
    def test_requests_shared_patterns_from_an_overfull_future_frame(self) -> None:
        a = np.zeros((4, 4), np.uint8)
        b = np.ones((4, 4), np.uint8)
        c = np.full((4, 4), 2, np.uint8)
        frames = [a, np.stack((b[0], b[1], c[2], c[3]))]
        palettes = [np.zeros(4, np.uint8), np.zeros(4, np.uint8)]
        protected = [np.ones(4, bool), np.ones(4, bool)]
        result = forecast_requests(
            frames, palettes, protected,
            vram_tiles=8, max_cold=1)
        self.assertEqual(result.protected_cold.tolist(), [1, 2])
        self.assertEqual(len(result.requests[1]), 1)
        self.assertEqual(result.requests[1][0], bytes(b[0]))

    def test_palette_only_change_does_not_request_a_pattern(self) -> None:
        frame = np.zeros((2, 4), np.uint8)
        result = forecast_requests(
            [frame, frame],
            [np.zeros(2, np.uint8), np.ones(2, np.uint8)],
            [np.ones(2, bool), np.ones(2, bool)],
            vram_tiles=4, max_cold=1)
        self.assertEqual(result.protected_cold.tolist(), [1, 0])
        self.assertEqual(result.requests[1], ())


if __name__ == "__main__":
    unittest.main()
