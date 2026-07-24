from __future__ import annotations

import unittest

import palette_segments


class PaletteSegmentTests(unittest.TestCase):
    def test_switch_follows_the_dark_transition_frame(self) -> None:
        ranges = palette_segments.segment_ranges(
            [0.0, 0.1, 1.0, 0.2, 0.0],
            [0.0, 0.1, 1.0, 0.2, 0.0],
            gap=0,
        )
        self.assertEqual(ranges, [(0, 3), (3, 5)])

    def test_first_frame_of_a_dark_plateau_is_displayed_before_switch(self) -> None:
        ranges = palette_segments.segment_ranges(
            [0.0, 0.0, 1.0, 1.0, 0.4, 0.0],
            [0.0, 0.0, 1.0, 1.0, 0.4, 0.0],
            gap=1,
        )
        self.assertEqual(ranges, [(0, 3), (3, 6)])

    def test_nearby_dark_and_uniform_hits_share_one_boundary(self) -> None:
        ranges = palette_segments.segment_ranges(
            [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            gap=0,
            uniform_near=2,
        )
        self.assertEqual(ranges, [(0, 3), (3, 6)])

    def test_separate_uniform_transition_adds_a_boundary(self) -> None:
        ranges = palette_segments.segment_ranges(
            [0.0] * 9,
            [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            gap=0,
            uniform_near=2,
        )
        self.assertEqual(ranges, [(0, 3), (3, 7), (7, 9)])


if __name__ == "__main__":
    unittest.main()
