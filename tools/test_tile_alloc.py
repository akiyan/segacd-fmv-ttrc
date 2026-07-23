#!/usr/bin/env python3
"""Tests for displayed and speculative VRAM residency."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tile_alloc import (
    TileAllocator,
    cold_transfer_order,
    count_slot_runs,
    evaluate_slot_locality,
    optimize_slot_locality,
    remap_placements,
    validate_physical_slots,
    verify_display_equivalence,
)


class TileAllocatorPrefetchTests(unittest.TestCase):
    def test_prefetched_pattern_is_warm_at_its_first_display_use(self) -> None:
        alloc = TileAllocator(2, 4)
        alloc.place_frame([(0, b"shown")], 0)
        slot, cold = alloc.prefetch(b"future", 0, 3)
        self.assertTrue(cold)
        self.assertEqual(alloc.place_frame([(1, b"future")], 3), [(slot, False)])
        self.assertEqual(int(alloc.slot_pin_until[slot]), -1)

    def test_normal_update_may_reclaim_a_pin_before_tearing_display(self) -> None:
        alloc = TileAllocator(1, 2)
        alloc.place_frame([(0, b"shown")], 0)
        _slot, cold = alloc.prefetch(b"future", 0, 5)
        self.assertTrue(cold)
        result = alloc.place_frame([(0, b"replacement")], 1)
        self.assertTrue(result[0][1])
        self.assertEqual(alloc.prefetch_evictions, 1)
        self.assertEqual(alloc.tearing, 0)

    def test_prefetch_skips_when_only_a_displayed_slot_exists(self) -> None:
        alloc = TileAllocator(1, 1)
        alloc.place_frame([(0, b"shown")], 0)
        self.assertIsNone(alloc.prefetch(b"future", 0, 2))

    def test_prefetch_may_replace_unused_cache_but_not_next_frame_key(self) -> None:
        alloc = TileAllocator(1, 3)
        alloc.place_frame([(0, b"old-shown")], 0)
        alloc.place_frame([(0, b"keep-next")], 1)
        alloc.place_frame([(0, b"shown")], 2)
        result = alloc.prefetch(
            b"future", 2, 3, avoid_keys={b"keep-next"})
        self.assertIsNotNone(result)
        self.assertTrue(result[1])
        self.assertTrue(alloc.is_resident(b"keep-next"))
        self.assertEqual(alloc.prefetch_cache_evictions, 1)


class SlotLocalityTests(unittest.TestCase):
    def test_identity_contiguous_allocation_matches_legacy_and_suffix_runs(self):
        alloc = TileAllocator(4, 6)
        frames = [
            [(0, b"a"), (1, b"b"), (2, b"c"), (3, b"d")],
            [(0, b"e"), (1, b"b"), (2, b"f"), (3, b"g")],
            [(0, b"h"), (1, b"i"), (2, b"f"), (3, b"j")],
        ]
        for frame, updates in enumerate(frames):
            placements = alloc.place_frame(updates, frame)
            legacy_slots = [slot for slot, cold in placements if cold]
            suffix_slots = [
                placements[index][0]
                for index in cold_transfer_order(placements)
            ]
            self.assertEqual(
                count_slot_runs(legacy_slots),
                count_slot_runs(suffix_slots),
            )

    def test_transfer_order_follows_physical_slots_without_changing_cold(self):
        placements = [(5, True), (2, False), (1, True), (3, True)]
        self.assertEqual(cold_transfer_order(placements), (2, 3, 0))

        mapping = validate_physical_slots([2, 0, 3, 1], 4)
        logical = [(0, True), (3, False), (2, True)]
        self.assertEqual(
            remap_placements(logical, mapping),
            [(2, True), (1, False), (3, True)],
        )

    def test_physical_permutation_is_display_equivalent_for_every_frame(self):
        frames = [
            [(0, b"a"), (1, b"b"), (2, b"c")],
            [(0, b"d"), (2, b"a")],
            [(1, b"d"), (2, b"e")],
            [(0, b"b"), (1, b"e")],
        ]
        result = verify_display_equivalence(
            frames, 3, 6, [4, 1, 5, 0, 3, 2])
        self.assertEqual(result["frames"], len(frames))
        self.assertEqual(result["tearing"], 0)

    def test_optimizer_targets_heavy_runs_without_changing_membership(self):
        heavy = tuple(range(0, 100, 2))
        trace = [(), heavy, tuple(range(1, 100, 2)), heavy]
        plan = optimize_slot_locality(trace, 100, cold_cap=50, iterations=6)
        validate_physical_slots(plan.physical_by_logical, 100)
        self.assertEqual(plan.cold.tolist(), [0, 50, 50, 50])
        self.assertLess(
            int(plan.optimized_runs[plan.risk_frames].max()),
            int(plan.baseline_runs[plan.risk_frames].max()),
        )

    def test_fixed_map_evaluation_uses_the_supplied_permutation(self):
        trace = [(), (0, 2, 4), (1, 3, 5)]
        plan = evaluate_slot_locality(
            trace, 6, [0, 3, 1, 4, 2, 5], cold_cap=3)
        self.assertEqual(plan.baseline_runs.tolist(), [0, 3, 3])
        self.assertEqual(plan.optimized_runs.tolist(), [0, 1, 1])

    def test_run_groups_keep_physical_sources_separate(self):
        trace = [(), (0, 1, 2, 3)]
        groups = [(), ((0, 2), (1, 3))]
        plan = evaluate_slot_locality(
            trace, 4, [0, 2, 1, 3], cold_cap=4,
            run_groups_by_frame=groups)
        self.assertEqual(plan.baseline_runs.tolist(), [0, 4])
        self.assertEqual(plan.optimized_runs.tolist(), [0, 2])


if __name__ == "__main__":
    unittest.main()
