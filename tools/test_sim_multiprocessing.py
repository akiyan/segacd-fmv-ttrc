#!/usr/bin/env python3
"""Regression tests for safe sim multiprocessing selection."""
from __future__ import annotations

import unittest

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

    def test_python314_defaults_to_synchronous_png_writes(self) -> None:
        self.assertEqual(sim.default_png_workers((3, 14, 0)), 1)
        self.assertEqual(sim.default_png_workers((3, 13, 9)), 6)


if __name__ == "__main__":
    unittest.main()
