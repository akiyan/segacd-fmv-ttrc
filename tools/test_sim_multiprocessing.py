#!/usr/bin/env python3
"""Regression tests for safe sim multiprocessing selection."""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

import numpy as np

# Import sim under a measured cold-cap tuple.  The module resolves playback
# geometry at import time even though these tests exercise only helper functions.
_sim_env = {
    "CBRSIM_FPS": "30",
    "CBRSIM_MODE": "H32",
    "CBRSIM_W": "256",
    "CBRSIM_H": "224",
    "CBRSIM_ACTIVE_TILES": "896",
}
_old_env = {name: os.environ.get(name) for name in _sim_env}
os.environ.update(_sim_env)
import sim
for _name, _value in _old_env.items():
    if _value is None:
        os.environ.pop(_name, None)
    else:
        os.environ[_name] = _value


class SimMultiprocessingTests(unittest.TestCase):
    def test_gpu_feeder_defaults_to_verified_four_processes(self) -> None:
        self.assertEqual(sim.quant_worker_count(True, 30), 4)
        self.assertEqual(sim.quant_worker_count(True, 2), 2)
        self.assertEqual(sim.quant_worker_count(True, 30, override_present=True), 30)
        self.assertEqual(sim.quant_worker_count(False, 30), 30)

    def test_gpu_loader_pool_never_forks_cuda_parent(self) -> None:
        self.assertEqual(sim.quant_pool_start_method(True), "spawn")

    def test_cpu_loader_pool_keeps_fast_fork_path(self) -> None:
        self.assertEqual(sim.quant_pool_start_method(False), "fork")

    def test_all_supported_pythons_default_to_synchronous_png_writes(self) -> None:
        self.assertEqual(sim.default_png_workers((3, 14, 0)), 1)
        self.assertEqual(sim.default_png_workers((3, 13, 9)), 1)

    def test_pattern_cache_owns_compact_arrays(self) -> None:
        rgb_frame = np.zeros((4, 8, 8, 3), np.uint8)
        sig_frame = np.zeros((4, 12), np.float32)
        rgb, sig = sim.own_pattern_cache_arrays(rgb_frame[2], sig_frame[2])
        self.assertEqual(rgb.shape, (8, 8, 3))
        self.assertEqual(sig.shape, (12,))
        self.assertTrue(rgb.flags.c_contiguous)
        self.assertTrue(sig.flags.c_contiguous)
        self.assertFalse(np.shares_memory(rgb, rgb_frame))
        self.assertFalse(np.shares_memory(sig, sig_frame))

    def test_pattern_cache_rejects_wrong_shapes(self) -> None:
        with self.assertRaisesRegex(ValueError, "pattern RGB shape"):
            sim.own_pattern_cache_arrays(
                np.zeros((1, 8, 8, 3), np.uint8), np.zeros(12, np.float32))

    def test_rendered_colour_keys_round_trip_all_rgb333_colours(self) -> None:
        keys = sim.rendered_color_keys(sim._MD_RGB888.astype(np.uint8))
        np.testing.assert_array_equal(keys, np.arange(512, dtype=np.uint16))

    def test_distance_aging_uses_mean_rgb_error_and_step_cap(self) -> None:
        summed = np.array([
            0,
            8 * 8 * 3 * 24,
            8 * 8 * 3 * 48,
            8 * 8 * 3 * 96,
        ])
        np.testing.assert_array_equal(
            sim.distance_aging_step(summed),
            np.array([0.0, 1.0, 2.0, 2.0]),
        )

    def test_distance_aging_multiplier_saturates_at_seven(self) -> None:
        np.testing.assert_array_equal(
            sim.priority_aging(np.array([0.0, 1.0, 10.0, 30.0])),
            np.array([1.0, 1.6, 7.0, 7.0]),
        )

    def test_distance_aging_excludes_near_and_exact_cells(self) -> None:
        diff = np.full(5, 8 * 8 * 3 * 24)
        pressure = sim.update_age_pressure(
            np.full(5, 4.0), np.array([0, 1, 2, 3, 9]), diff)
        np.testing.assert_array_equal(
            pressure, np.array([5.0, 5.0, 5.0, 0.0, 0.0]))

    def test_default_detail_weight_is_disabled(self) -> None:
        self.assertEqual(sim.DETAIL_ALPHA, 0.0)

    def test_ghost_escalation_uses_point_two_seconds_and_floor(self) -> None:
        self.assertEqual(sim.ghost_escalate_frames(0.2, 30), 6)
        self.assertEqual(sim.ghost_escalate_frames(0.2, 24), 4)
        self.assertEqual(sim.ghost_escalate_frames(0.2, 15), 3)

    def test_boot_inline_selection_follows_packed_physical_order(self) -> None:
        # Request order is logical 1,2,3, but the physical permutation puts
        # logical 2 and 3 first.  These are the records pack_stream places in
        # frame 0's inline suffix; logical 1 belongs in the sidecar.
        mapping = np.array([3, 2, 0, 1], np.int64)
        self.assertEqual(
            sim._inline_boot_prefetch_slots((1, 2, 3), 2, mapping),
            (2, 3),
        )
        self.assertEqual(
            sim._inline_boot_prefetch_slots((1, 2, 3), 2),
            (1, 2),
        )

    def test_untimed_frame_zero_is_absent_from_run_optimization(self) -> None:
        replay = SimpleNamespace(cold_slots=((0, 1, 2), (3, 4)))
        self.assertEqual(
            sim._run_accounted_cold_slots(replay, 2),
            ((), (3, 4)),
        )

    def test_resident_distance_luts_match_direct_f3_math(self) -> None:
        rng = np.random.default_rng(7)
        candidates = sim.MD_LEVELS[
            rng.integers(0, 8, (24, 8, 8, 3))]
        target = sim.MD_LEVELS[rng.integers(0, 8, (8, 8, 3))]

        direct_y = np.abs(candidates @ sim._LWv - target @ sim._LWv)
        direct_ym = direct_y.reshape(24, -1).mean(1)
        direct_yp = direct_y.reshape(24, -1).max(1)
        direct_cm = np.sqrt(
            (candidates @ sim._CBv - target @ sim._CBv) ** 2
            + (candidates @ sim._CRv - target @ sim._CRv) ** 2
        ).reshape(24, -1).mean(1)

        candidate_keys = sim.rendered_color_keys(candidates).reshape(24, 64)
        target_keys = sim.rendered_color_keys(target).reshape(64)
        table_y = sim._F3_DY_LUT[candidate_keys, target_keys]
        table_ym = table_y.mean(1)
        table_yp = table_y.max(1)
        table_cm = sim._F3_DC_LUT[candidate_keys, target_keys].mean(1)

        np.testing.assert_array_equal(table_ym, direct_ym)
        np.testing.assert_array_equal(table_yp, direct_yp)
        np.testing.assert_array_equal(table_cm, direct_cm)


if __name__ == "__main__":
    unittest.main()
