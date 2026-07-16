#!/usr/bin/env python3
"""Regression tests for shared playback timing, PCM sizing, and cold caps."""
from __future__ import annotations

import unittest

import av_config


class PlaybackTimingTests(unittest.TestCase):
    def test_ntsc_integer_vblank_rates_keep_existing_chunks(self) -> None:
        self.assertEqual(av_config.vsync_n_for_fps(15), 4)
        self.assertAlmostEqual(av_config.playback_fps_for_content(15), 15_000 / 1001)
        self.assertEqual(av_config.pcm_frame_bytes(15), 888)

        self.assertEqual(av_config.vsync_n_for_fps(30), 2)
        self.assertAlmostEqual(av_config.playback_fps_for_content(30), 30_000 / 1001)
        self.assertEqual(av_config.pcm_frame_bytes(30), 444)

    def test_24fps_is_delivery_paced_not_rounded_to_n2(self) -> None:
        self.assertEqual(av_config.vsync_n_for_fps(24), 2)
        self.assertEqual(av_config.playback_fps_for_content(24), 24)
        self.assertEqual(av_config.pcm_frame_bytes(24), 555)

    def test_invalid_fps_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            av_config.vsync_n_for_fps(0)


class ColdCapTests(unittest.TestCase):
    def test_common_caps_are_unchanged(self) -> None:
        for mode in ("H32", "MODE4"):
            with self.subTest(mode=mode):
                self.assertEqual(av_config.cold_cap_for_fps(15, mode), 350)
                self.assertEqual(av_config.cold_cap_for_fps(24, mode), 219)
                self.assertEqual(av_config.cold_cap_for_fps(30, mode), 175)

    def test_h40_exception_is_limited_to_exactly_24fps(self) -> None:
        self.assertEqual(av_config.cold_cap_for_fps(15, "H40"), 350)
        self.assertEqual(av_config.cold_cap_for_fps(24, "H40"), 200)
        self.assertEqual(av_config.cold_cap_for_fps(30, "H40"), 175)

    def test_pack_ceiling_uses_the_same_h40_exception(self) -> None:
        self.assertEqual(
            av_config.cold_realized_ceiling_for_fps(24, "H40"), 200)
        self.assertEqual(
            av_config.cold_realized_ceiling_for_fps(30, "H40"), 175)


if __name__ == "__main__":
    unittest.main()
