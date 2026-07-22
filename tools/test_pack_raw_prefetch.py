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
            (per, prefetch, transfer_orders, loads, updates, _pal,
             patterns, tearing) = (
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
            log, per, patterns, prefetch_per=prefetch,
            transfer_orders=transfer_orders, enabled=False)
        np.testing.assert_array_equal(plan.prg_loads, [1, 1, 0])
        packed_loads, packed_runs = pack_stream.run_stats(
            per, plan.sources, prefetch, transfer_orders=transfer_orders)
        np.testing.assert_array_equal(packed_loads, loads)
        np.testing.assert_array_equal(packed_runs, [1, 1, 0])

    def test_prefetch_payload_is_sorted_by_physical_slot(self):
        key_a = bytes([1] * 64)
        key_b = bytes([2] * 64)
        key_c = bytes([3] * 64)
        log = {
            "frames": [
                [(0, 0, key_a)],
                [],
                [(0, 0, key_b), (1, 0, key_c)],
            ],
            "frame_seg": np.zeros(3, np.int64),
            "raw_prefetch": {
                "schema_version": 1,
                "enabled": True,
                # Request order is descending, but payload/run order must be
                # the contiguous physical 2,3 sequence.
                "requests": [[], [(key_b, 2, 3), (key_c, 2, 2)], []],
                "cold": np.array([0, 2, 0], np.uint16),
            },
        }

        old_cells = pack_stream.C_CELLS
        pack_stream.C_CELLS = 2
        try:
            (per, prefetch, transfer_orders, loads, _updates, _pal,
             patterns, tearing) = pack_stream.resolve(log, 8, mode="contig")
        finally:
            pack_stream.C_CELLS = old_cells

        self.assertEqual(tearing, 0)
        self.assertEqual([item[0] for item in prefetch[1]], [2, 3])
        self.assertEqual(
            patterns[-2:],
            [pack_stream.pack_key(key_c), pack_stream.pack_key(key_b)],
        )
        sources = tuple(
            tuple(pattern_supply.SOURCE_PRG for _ in entries)
            for _cells, entries, _colds in per)
        packed_loads, packed_runs = pack_stream.run_stats(
            per, sources, prefetch, transfer_orders=transfer_orders)
        np.testing.assert_array_equal(packed_loads, loads)
        np.testing.assert_array_equal(packed_runs, [1, 1, 0])


if __name__ == "__main__":
    unittest.main()
