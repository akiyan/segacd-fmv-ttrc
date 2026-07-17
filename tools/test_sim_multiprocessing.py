#!/usr/bin/env python3
"""Regression tests for safe sim multiprocessing selection."""
from __future__ import annotations

import unittest

import sim


class SimMultiprocessingTests(unittest.TestCase):
    def test_gpu_loader_pool_never_forks_cuda_parent(self) -> None:
        self.assertEqual(sim.quant_pool_start_method(True), "spawn")

    def test_cpu_loader_pool_keeps_fast_fork_path(self) -> None:
        self.assertEqual(sim.quant_pool_start_method(False), "fork")


if __name__ == "__main__":
    unittest.main()
