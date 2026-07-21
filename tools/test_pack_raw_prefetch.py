#!/usr/bin/env python3
"""Integration checks for raw-prefetch decision replay and pattern supply."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pack_stream
import pattern_supply


class PackRawPrefetchTests(unittest.TestCase):
    def test_prefetch_load_precedes_first_name_use(self):
        key_a = bytes([1] * 64)
        key_b = bytes([2] * 64)
        log = {
            "frames": [
                [(0, 0, key_a)],
                [],
                [(0, 0, key_b)],
            ],
            "frame_seg": np.zeros(3, np.int64),
            "raw_prefetch": {
                "schema_version": 1,
                "enabled": True,
                "requests": [[], [(key_b, 2, 1)], []],
                "cold": np.array([0, 1, 0], np.uint16),
            },
        }

        old_cells = pack_stream.C_CELLS
        pack_stream.C_CELLS = 1
        try:
            per, prefetch, loads, updates, _pal, patterns, tearing = (
                pack_stream.resolve(log, 8, mode="contig"))
        finally:
            pack_stream.C_CELLS = old_cells

        self.assertEqual(tearing, 0)
        np.testing.assert_array_equal(loads, [1, 1, 0])
        np.testing.assert_array_equal(updates, [1, 0, 1])
        self.assertEqual(len(patterns), 2)
        self.assertTrue(prefetch[1][0][1])
        self.assertFalse(per[2][2][0])

        plan = pattern_supply.plan_supply(
            log, per, patterns, prefetch_per=prefetch, enabled=False)
        np.testing.assert_array_equal(plan.prg_loads, [1, 1, 0])
        packed_loads, packed_runs = pack_stream.run_stats(
            per, plan.sources, prefetch)
        np.testing.assert_array_equal(packed_loads, loads)
        np.testing.assert_array_equal(packed_runs, [1, 1, 0])


if __name__ == "__main__":
    unittest.main()
