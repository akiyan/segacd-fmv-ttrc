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
    def test_boot_prefetch_shares_frame0_payload_without_a_name_update(self):
        key_a = bytes([1] * 64)
        key_b = bytes([2] * 64)
        log = {
            "frames": [
                [(0, 0, key_a), (1, 0, key_a)],
                [(0, 0, key_b)],
            ],
            "frame_seg": np.zeros(2, np.int64),
            "raw_prefetch": {
                "schema_version": 2,
                "enabled": True,
                "boot_enabled": True,
                "runtime_enabled": False,
                "requests": [[(key_b, 1, 1)], []],
                "cold": np.array([1, 0], np.uint16),
            },
        }

        old_cells = pack_stream.C_CELLS
        pack_stream.C_CELLS = 2
        try:
            (per, prefetch, transfer_orders, loads, updates, _pal,
             patterns, tearing) = pack_stream.resolve(
                log, 4, mode="contig")
        finally:
            pack_stream.C_CELLS = old_cells

        self.assertEqual(tearing, 0)
        np.testing.assert_array_equal(loads, [2, 0])
        np.testing.assert_array_equal(updates, [2, 1])
        self.assertEqual(len(patterns), 2)
        self.assertTrue(prefetch[0][0][1])
        self.assertFalse(per[1][2][0])

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

    def test_boot_sidecar_counts_as_cold_without_an_inline_run(self):
        key_a = bytes([1] * 64)
        key_b = bytes([2] * 64)
        key_c = bytes([3] * 64)
        log = {
            "frames": [
                [(0, 0, key_a), (1, 0, key_a)],
                [(0, 0, key_b), (1, 0, key_c)],
            ],
            "frame_seg": np.zeros(2, np.int64),
            "raw_prefetch": {
                "schema_version": 3,
                "enabled": True,
                "boot_enabled": True,
                "runtime_enabled": False,
                "boot_inline_requests": 1,
                "boot_sidecar_requests": 1,
                "requests": [[(key_b, 1, 1), (key_c, 1, 2)], []],
                "cold": np.array([2, 0], np.uint16),
            },
        }

        old_cells = pack_stream.C_CELLS
        pack_stream.C_CELLS = 2
        try:
            (per, prefetch, transfer_orders, loads, _updates, _pal,
             patterns, tearing) = pack_stream.resolve(log, 4, mode="contig")
            inline, sidecar = pack_stream.split_boot_prefetch(log, prefetch)
            plan = pattern_supply.plan_supply(
                log, per, patterns, prefetch_per=prefetch,
                transfer_orders=transfer_orders, enabled=False)
            packed_loads, packed_runs = pack_stream.run_stats(
                per, plan.sources, inline, transfer_orders=transfer_orders,
                boot_sidecar=sidecar)
        finally:
            pack_stream.C_CELLS = old_cells

        self.assertEqual(tearing, 0)
        self.assertEqual(len(inline[0]), 1)
        self.assertEqual(len(sidecar), 1)
        np.testing.assert_array_equal(loads, [3, 0])
        np.testing.assert_array_equal(packed_loads, loads)
        np.testing.assert_array_equal(packed_runs, [1, 0])
        np.testing.assert_array_equal(plan.prg_loads, loads)


if __name__ == "__main__":
    unittest.main()
