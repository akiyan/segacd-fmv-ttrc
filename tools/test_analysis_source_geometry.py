#!/usr/bin/env python3
"""Regression tests for Source-panel display geometry."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout_preview as layout


class AnalysisSourceGeometryTests(unittest.TestCase):
    def test_sonic_h40_keeps_centered_black_border_and_source_sar(self) -> None:
        geometry = layout.source_panel_geometry(
            288, 200, 320, 224, 32, 35, 545, 403)

        self.assertAlmostEqual(
            geometry["display_aspect"], 320 / 224 * 32 / 35)
        panel_w, panel_h = geometry["panel_size"]
        content_w, content_h = geometry["content_size"]
        content_x, content_y = geometry["content_offset"]
        self.assertEqual((panel_w, panel_h), (526, 403))
        self.assertEqual((content_w, content_h), (473, 360))
        self.assertEqual((content_x, content_y), (26, 22))
        self.assertLess(content_w, panel_w)
        self.assertLess(content_h, panel_h)

    def test_source_larger_than_canvas_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "exceeds"):
            layout.source_panel_geometry(
                321, 200, 320, 224, 32, 35, 545, 403)


if __name__ == "__main__":
    unittest.main()
