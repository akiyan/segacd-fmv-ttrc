#!/usr/bin/env python3
"""Regression tests for safe sim multiprocessing selection."""
from __future__ import annotations

import unittest

import numpy as np

import sim


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


if __name__ == "__main__":
    unittest.main()
