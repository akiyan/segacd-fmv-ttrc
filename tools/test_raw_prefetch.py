#!/usr/bin/env python3
"""Tests for the cheap future raw-pattern forecast."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from raw_prefetch import PrefetchForecast, forecast_requests, plan_boot_requests
from upgrade_planner import DemandPrediction


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

    def test_boot_plan_prefers_early_risk_and_returns_reclaim_order(self) -> None:
        frame0 = b"frame0"
        risk = b"risk"
        protected = b"protected"
        ordinary = b"ordinary"
        later = b"later"
        prediction = DemandPrediction(
            np.zeros(3, np.int64),
            np.zeros(3, np.int64),
            np.zeros(3, np.int64),
            np.zeros(3, np.int64),
            cold_keys=((), (ordinary, risk), (later,)),
            protected_keys=((), (protected, risk), (later,)),
        )
        forecast = PrefetchForecast(
            requests=((), (risk,), (later,)),
            protected_cold=np.zeros(3, np.int64),
            requested_patterns=np.zeros(3, np.int64),
        )
        # Priority is risk, protected, ordinary. Allocation order is reversed
        # so low speculative slots hold the least urgent selected pattern.
        self.assertEqual(
            plan_boot_requests(
                prediction, forecast, (frame0,), capacity=3),
            ((ordinary, 1), (protected, 1), (risk, 1)),
        )

    def test_forecast_sees_boot_pattern_as_warm(self) -> None:
        a = np.zeros((1, 4), np.uint8)
        b = np.ones((1, 4), np.uint8)
        result = forecast_requests(
            [a, b],
            [np.zeros(1, np.uint8), np.zeros(1, np.uint8)],
            [np.ones(1, bool), np.ones(1, bool)],
            vram_tiles=2,
            max_cold=1,
            boot_prefetch_requests=((bytes(b[0]), 1),),
        )
        self.assertEqual(result.protected_cold.tolist(), [1, 0])


if __name__ == "__main__":
    unittest.main()
