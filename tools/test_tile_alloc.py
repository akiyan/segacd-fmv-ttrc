#!/usr/bin/env python3
"""Tests for displayed and speculative VRAM residency."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tile_alloc import TileAllocator


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


if __name__ == "__main__":
    unittest.main()
