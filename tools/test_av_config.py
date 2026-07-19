#!/usr/bin/env python3
"""Regression tests for shared playback timing, PCM sizing, and cold caps."""
from __future__ import annotations

import unittest

import av_config


class RingGeometryTests(unittest.TestCase):
    def test_full_reclaimed_ring_geometry(self) -> None:
        self.assertEqual(av_config.RING_SIZE_KB, 428)
        self.assertEqual(av_config.RING_JITTER_MARGIN_KB, 40)
        self.assertEqual(av_config.RING_CAP_KB, 388)
        self.assertEqual(av_config.TANK_KB, 388)
        self.assertEqual(av_config.BACKPRESSURE_KB, 424)
        self.assertEqual(
            av_config.BACKPRESSURE_KB - av_config.RING_CAP_KB, 36)


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
        self.assertFalse(av_config.uses_fixed_n2_cadence(24))

    def test_only_ntsc_n2_rates_use_fixed_cadence(self) -> None:
        self.assertTrue(av_config.uses_fixed_n2_cadence(30))
        self.assertTrue(av_config.uses_fixed_n2_cadence(30_000 / 1001))
        self.assertFalse(av_config.uses_fixed_n2_cadence(15))
        self.assertFalse(av_config.uses_fixed_n2_cadence(60))

    def test_30fps_cd_rate_matches_two_ntsc_vblanks(self) -> None:
        numerator, modulus = av_config.cd_sector_rate(30)
        self.assertEqual((numerator, modulus), (1001, 400))
        acc = 0
        deltas = []
        for _ in range(400):
            acc += numerator
            delta, acc = divmod(acc, modulus)
            deltas.append(delta)
        self.assertEqual(sum(deltas), 1001)
        self.assertEqual(deltas.count(2), 199)
        self.assertEqual(deltas.count(3), 201)
        self.assertEqual(acc, 0)

    def test_non_n2_cd_rates_keep_legacy_schedule(self) -> None:
        self.assertEqual(av_config.cd_sector_rate(15), (75, 15))
        self.assertEqual(av_config.cd_sector_rate(24), (75, 24))

    def test_near_30_but_non_ntsc_rate_stays_delivery_paced(self) -> None:
        self.assertFalse(av_config.uses_fixed_n2_cadence(29.8))
        self.assertEqual(av_config.cd_sector_rate(29.8), (75, 30))

    def test_invalid_fps_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            av_config.vsync_n_for_fps(0)
        with self.assertRaises(ValueError):
            av_config.cd_sector_rate(0)


class ColdCapTests(unittest.TestCase):
    def test_common_caps_are_unchanged(self) -> None:
        for mode in ("H32", "MODE4"):
            with self.subTest(mode=mode):
                self.assertEqual(
                    av_config.cold_cap_for_fps(15, mode, 896), 350)
                self.assertEqual(
                    av_config.cold_cap_for_fps(24, mode, 896), 219)
                self.assertEqual(
                    av_config.cold_cap_for_fps(30, mode, 896), 175)

    def test_h40_15fps_400_is_limited_to_720_active_tiles(self) -> None:
        self.assertEqual(av_config.cold_cap_for_fps(15, "H40", 720), 400)
        self.assertEqual(av_config.cold_cap_for_fps(15, "H40", 719), 350)
        self.assertEqual(av_config.cold_cap_for_fps(15, "H40", 721), 350)
        self.assertEqual(av_config.cold_cap_for_fps(15, "H40", 1120), 350)

    def test_other_h40_cadence_exceptions_are_unchanged(self) -> None:
        self.assertEqual(av_config.cold_cap_for_fps(24, "H40", 1120), 200)
        self.assertEqual(av_config.cold_cap_for_fps(30, "H40", 1120), 175)

    def test_pack_ceiling_uses_the_same_h40_exceptions(self) -> None:
        self.assertEqual(
            av_config.cold_realized_ceiling_for_fps(15, "H40", 720), 400)
        self.assertEqual(
            av_config.cold_realized_ceiling_for_fps(15, "H40", 1120), 350)
        self.assertEqual(
            av_config.cold_realized_ceiling_for_fps(24, "H40", 1120), 200)

    def test_nonpositive_active_tile_count_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            av_config.cold_cap_for_fps(15, "H40", 0)


if __name__ == "__main__":
    unittest.main()
