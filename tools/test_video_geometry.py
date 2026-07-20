#!/usr/bin/env python3
"""Regression tests for HAR-aware source geometry conversion."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from video_geometry import geometry_plan, raw_filter, source_filter


class VideoGeometryTests(unittest.TestCase):
    def test_h32_crop_is_object_fit_cover(self) -> None:
        plan = geometry_plan("H32", 256, 224, 512, 384, fit="crop")
        self.assertEqual(plan["crop"], [500, 384, 6, 0])
        self.assertEqual(
            raw_filter("H32", 256, 224, 512, 384,
                       fit="crop", resize_filter="area"),
            "setsar=1,crop=500:384:6:0,scale=256:224:flags=area")

    def test_h40_crop_uses_same_visible_aperture(self) -> None:
        plan = geometry_plan("H40", 320, 224, 512, 384, fit="crop")
        self.assertEqual(plan["crop"], [500, 384, 6, 0])
        self.assertEqual(
            raw_filter("H40", 320, 224, 512, 384, fit="crop"),
            "setsar=1,crop=500:384:6:0,scale=320:224:flags=lanczos")

    def test_crop_denoise_finishes_at_output_raster(self) -> None:
        self.assertEqual(
            source_filter("H32", 256, 224, 512, 384, fit="crop"),
            "setsar=1,crop=500:384:6:0,scale=512:448:flags=lanczos,"
            "hqdn3d=6:6:8:8,gblur=sigma=1.6,scale=256:224:flags=lanczos")

    def test_pad_preserves_complete_source_and_adds_bars(self) -> None:
        self.assertEqual(
            raw_filter("H32", 256, 224, 512, 384, fit="pad"),
            "setsar=1,scale=256:218:flags=lanczos,"
            "pad=256:224:(ow-iw)/2:(oh-ih)/2:color=black")


if __name__ == "__main__":
    unittest.main()
