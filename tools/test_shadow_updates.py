"""Regression tests for the mixed bitmap/completed-list shadow format."""

from __future__ import annotations

import struct
import unittest

import shadow_updates


class ShadowUpdateTests(unittest.TestCase):
    def test_v16_bitmap_entry_boundary_is_word_aligned(self) -> None:
        self.assertEqual(shadow_updates.bitmap_bytes(760), 95)
        self.assertEqual(shadow_updates.aligned_bitmap_bytes(760), 96)
        self.assertEqual(shadow_updates.bitmap_bytes(1120), 140)
        self.assertEqual(shadow_updates.aligned_bitmap_bytes(1120), 140)

    def test_bitmap_requires_sorted_unique_in_range_cells(self) -> None:
        self.assertEqual(
            shadow_updates.build_bitmap([0, 7, 8, 15], 16), b"\x81\x81")
        for cells in ([1, 1], [2, 1], [-1], [16]):
            with self.subTest(cells=cells), self.assertRaises(ValueError):
                shadow_updates.build_bitmap(cells, 16)

    def test_completed_list_strips_cold_and_source_bits(self) -> None:
        packed = shadow_updates.build_update_list(
            [0, 1119], [0xFFFF, 0xA801], 1120)
        self.assertEqual(
            struct.unpack(">4H", packed),
            (0, 0x67FF, 2238, 0x2001),
        )

    def test_cycle_model_includes_runtime_offset_guard(self) -> None:
        self.assertEqual(shadow_updates.update_list_cycles(1), 88)
        self.assertEqual(shadow_updates.update_list_cycles(1120), 53_800)
        sparse = shadow_updates.frame_cost([0, 1119], 1120)
        self.assertEqual(sparse.list_cycles, 136)
        self.assertEqual(sparse.added_bytes, 8 - (140 + 4))

    def test_count_tag_roundtrip_and_bounds(self) -> None:
        self.assertEqual(
            shadow_updates.decode_count(
                shadow_updates.encode_count(1120, True)),
            (1120, True),
        )
        with self.assertRaises(ValueError):
            shadow_updates.encode_count(0x8000, False)


if __name__ == "__main__":
    unittest.main()
