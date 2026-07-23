#!/usr/bin/env python3
"""Regression tests for shared analysis colours and category borders."""

from __future__ import annotations

import unittest

import numpy as np
from PIL import Image, ImageDraw

import analysis_style as style
import layout_preview as layout


class AnalysisStyleTests(unittest.TestCase):
    def test_layout_uses_the_canonical_semantic_colours(self) -> None:
        self.assertIs(layout.CATS, style.CATS)
        self.assertIs(layout.LEGEND_CATS, style.LEGEND_CATS)
        self.assertIs(layout.SUPPLY_COLORS, style.SUPPLY_COLORS)
        self.assertEqual(layout.CAT_FLBK, style.CAT_FLBK)
        self.assertEqual(layout.COL_PRG, style.COL_PRG)
        self.assertEqual(layout.COL_WRD, style.COL_WRD)
        self.assertEqual(layout.COL_DIC, style.COL_DIC)

    def test_flbk_border_uses_unblended_yellow(self) -> None:
        tiles = np.full((1, 8, 8, 3), 73.0, np.float64)
        style.apply_numpy_category_border(
            tiles, np.asarray([True]), "Flbk")
        self.assertTrue(np.all(tiles[0, 0, :, :] == style.CAT_FLBK))
        self.assertTrue(np.all(tiles[0, -1, :, :] == style.CAT_FLBK))
        self.assertTrue(np.all(tiles[0, 1:-1, 1:-1, :] == 73.0))

    def test_source_border_is_thin_colour_and_black_dash(self) -> None:
        tiles = np.full((1, 8, 8, 3), 91.0, np.float64)
        style.apply_numpy_category_border(
            tiles, np.asarray([True]), "Prg")
        perimeter = np.concatenate((
            tiles[0, 0, :, :],
            tiles[0, -1, :, :],
            tiles[0, 1:-1, 0, :],
            tiles[0, 1:-1, -1, :],
        ))
        colours = {tuple(int(channel) for channel in pixel)
                   for pixel in perimeter}
        self.assertEqual(
            colours, {style.COL_PRG, style.COL_BORDER_BLACK})
        self.assertTrue(np.all(tiles[0, 1:-1, 1:-1, :] == 91.0))

    def test_legend_swatch_uses_the_same_source_dash(self) -> None:
        image = Image.new("RGB", (18, 18), (91, 91, 91))
        style.draw_category_swatch(
            ImageDraw.Draw(image), (1, 1, 16, 16), "Dic")
        perimeter = (
            [image.getpixel((x, 1)) for x in range(1, 17)]
            + [image.getpixel((x, 16)) for x in range(1, 17)]
            + [image.getpixel((1, y)) for y in range(2, 16)]
            + [image.getpixel((16, y)) for y in range(2, 16)]
        )
        self.assertEqual(
            set(perimeter), {style.COL_DIC, style.COL_BORDER_BLACK})
        self.assertEqual(image.getpixel((2, 2)), (91, 91, 91))


if __name__ == "__main__":
    unittest.main()
