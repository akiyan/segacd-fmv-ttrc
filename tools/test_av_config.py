#!/usr/bin/env python3
"""Regression tests for shared playback timing and PCM chunk sizing."""
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


if __name__ == "__main__":
    unittest.main()
