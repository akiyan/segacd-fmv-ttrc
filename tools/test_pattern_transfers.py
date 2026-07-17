#!/usr/bin/env python3
"""Regression tests for sim -> pack -> player cold-run accounting."""
from __future__ import annotations

import unittest

import numpy as np

import pack_stream as pack


class PatternTransferTests(unittest.TestCase):
    def test_packer_omits_reuse_without_splitting_a_run(self) -> None:
        entries = [pack.BASE + 10, pack.BASE + 99, pack.BASE + 11]
        self.assertEqual(pack.cold_runs(entries, [True, False, True]), [(10, 2)])

    def test_packer_keeps_pool_wrap_as_two_runs(self) -> None:
        entries = [pack.BASE + 1399, pack.BASE]
        self.assertEqual(pack.cold_runs(entries, [True, True]), [(1399, 1), (0, 1)])

    def test_frozen_sim_counts_match_all_pack_frames(self) -> None:
        log = {
            "pattern_transfers": {
                "schema_version": 1,
                "tiles": np.array([0, 3, 4], np.uint16),
                "runs": np.array([0, 1, 3], np.uint16),
            }
        }
        self.assertTrue(
            pack.verify_sim_pattern_transfers(
                log, np.array([0, 3, 4]), np.array([0, 1, 3])
            )
        )

    def test_first_mismatch_fails_instead_of_guessing(self) -> None:
        log = {
            "pattern_transfers": {
                "schema_version": 1,
                "tiles": np.array([0, 3, 4], np.uint16),
                "runs": np.array([0, 2, 3], np.uint16),
            }
        }
        with self.assertRaisesRegex(SystemExit, "runs mismatch at frame 1"):
            pack.verify_sim_pattern_transfers(
                log, np.array([0, 3, 4]), np.array([0, 1, 3])
            )


if __name__ == "__main__":
    unittest.main()
