#!/usr/bin/env python3
"""Regression tests for shared playback timing, ADPCM sizing, and cold caps."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import av_config


class RingGeometryTests(unittest.TestCase):
    def test_full_reclaimed_ring_geometry(self) -> None:
        self.assertEqual(av_config.RING_SIZE_KB, 428)
        self.assertEqual(av_config.RING_PHYSICAL_GUARD_KB, 4)
        self.assertEqual(av_config.RING_JITTER_HEADROOM_KB, 20)
        self.assertEqual(av_config.FRAME0_PATTERN_STAGING_KB, 36)
        self.assertEqual(av_config.RING_CAP_KB, 404)
        self.assertEqual(av_config.PRG_BUF_CAP_KB, 404)
        self.assertEqual(av_config.QUALITY_BUDGET_KB, 404)
        self.assertEqual(av_config.BACKPRESSURE_KB, 424)
        self.assertEqual(
            av_config.BACKPRESSURE_KB - av_config.RING_CAP_KB, 20)

    def test_jitter_reserve_scales_with_frame_interval(self) -> None:
        self.assertEqual(av_config.ring_jitter_headroom_kb(30), 20)
        self.assertEqual(av_config.ring_jitter_headroom_kb(24), 25)
        self.assertEqual(av_config.ring_jitter_headroom_kb(15), 40)
        self.assertEqual(av_config.prg_buf_cap_kb(30), 404)
        self.assertEqual(av_config.prg_buf_cap_kb(24), 399)
        self.assertEqual(av_config.prg_buf_cap_kb(15), 384)
        for fps in (15, 24, 30):
            self.assertEqual(av_config.physical_delivery_cap_kb(fps), 424)
            self.assertEqual(
                av_config.prg_buf_cap_kb(fps)
                + av_config.ring_jitter_headroom_kb(fps),
                av_config.BACKPRESSURE_KB,
            )

    def test_ntsc_like_rates_use_named_content_cadence(self) -> None:
        self.assertEqual(
            av_config.ring_jitter_headroom_kb(30_000 / 1001), 20)
        self.assertEqual(
            av_config.ring_jitter_headroom_kb(24_000 / 1001), 25)

    def test_fixed_encoder_and_pack_resources(self) -> None:
        self.assertEqual(av_config.VRAM_PATTERN_BASE_TILE, 1)
        self.assertEqual(av_config.VRAM_FIRST_MOVIE_NT_TILE, 1536)
        self.assertEqual(av_config.VRAM_PATTERN_POOL_TILES, 1535)
        self.assertEqual(av_config.VRAM_HUD_FONT_TILE, 1664)
        self.assertTrue(av_config.PACK_FORWARD_FILL)
        self.assertEqual(av_config.STARTUP_AUDIO_PREFETCH_FRAMES, 30)

    def test_boot_sidecar_capacity_preserves_fixed_word_ram_holes(self) -> None:
        # A one-segment Sonic-style clip has 469 physical record slots.  The
        # resident-pool limit, rather than this stage, caps its 398 requests.
        self.assertEqual(av_config.boot_vram_sidecar_capacity(1), 469)
        # At the maximum palette count, the middle hole is fully occupied;
        # the two outer holes still hold 232 records.
        self.assertEqual(
            av_config.boot_vram_sidecar_capacity(av_config.PALTAB_MAX_SEG),
            232)

    def test_boot_sidecar_rejects_palette_stage_overflow(self) -> None:
        with self.assertRaisesRegex(ValueError, "palette table exceeds"):
            av_config.boot_vram_sidecar_capacity(
                av_config.PALTAB_MAX_SEG + 1)


class PlaybackTimingTests(unittest.TestCase):
    def test_cd_1x_physical_constants(self) -> None:
        self.assertEqual(av_config.CD_SECTOR_BYTES, 2048)
        self.assertEqual(av_config.CD_SECTORS_PER_SECOND, 75)
        self.assertEqual(av_config.CD_BYTES_PER_SECOND, 153_600)

    def test_ntsc_integer_vblank_rates_keep_existing_chunks(self) -> None:
        self.assertEqual(av_config.vsync_n_for_fps(15), 4)
        self.assertAlmostEqual(av_config.playback_fps_for_content(15), 15_000 / 1001)
        self.assertEqual(av_config.adpcm_frame_samples(15), 1472)
        self.assertEqual(av_config.audio_frame_layout(15), (22_050, 1472, 740))

        self.assertEqual(av_config.vsync_n_for_fps(30), 2)
        self.assertAlmostEqual(av_config.playback_fps_for_content(30), 30_000 / 1001)
        self.assertEqual(av_config.adpcm_frame_samples(30), 736)
        self.assertEqual(av_config.audio_frame_layout(30), (22_050, 736, 372))

    def test_24fps_is_delivery_paced_not_rounded_to_n2(self) -> None:
        self.assertEqual(av_config.vsync_n_for_fps(24), 2)
        self.assertEqual(av_config.playback_fps_for_content(24), 24)
        self.assertEqual(av_config.adpcm_frame_samples(24), 920)
        self.assertEqual(av_config.audio_frame_layout(24), (22_050, 920, 464))
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
    def test_baseline_scales_only_with_frame_rate(self) -> None:
        self.assertEqual(av_config.baseline_cold_cap_for_fps(15), 360)
        self.assertEqual(av_config.baseline_cold_cap_for_fps(24), 225)
        self.assertEqual(av_config.baseline_cold_cap_for_fps(30), 180)
        self.assertEqual(av_config.baseline_cold_cap_for_fps(60), 90)

    def test_fractional_rate_is_rounded_to_nearest_pattern(self) -> None:
        self.assertEqual(
            av_config.baseline_cold_cap_for_fps(30_000 / 1_001), 180)

    def test_profile_cap_may_raise_but_not_lower_baseline(self) -> None:
        qualification = av_config.cold_cap_qualification(
            30, requested_cap=190)
        self.assertEqual(qualification.cap, 190)
        self.assertEqual(qualification.baseline_cap, 180)
        self.assertEqual(qualification.source, "profile")
        with self.assertRaisesRegex(ValueError, "below baseline 180"):
            av_config.cold_cap_qualification(
                30, requested_cap=179)

    def test_baseline_selector_ignores_profile_environment(self) -> None:
        with patch.dict(os.environ, {"CBRSIM_COLD_CAP": "190"}):
            self.assertEqual(
                av_config.baseline_cold_cap_for_fps(30), 180)
            self.assertEqual(av_config.cold_cap_for_fps(30), 190)

    def test_pack_ceiling_uses_the_same_fps_selector(self) -> None:
        self.assertEqual(av_config.cold_realized_ceiling_for_fps(15), 360)
        self.assertEqual(av_config.cold_realized_ceiling_for_fps(24), 225)
        self.assertEqual(av_config.cold_realized_ceiling_for_fps(30), 180)

    def test_nonpositive_fps_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            av_config.cold_cap_for_fps(0)


if __name__ == "__main__":
    unittest.main()
