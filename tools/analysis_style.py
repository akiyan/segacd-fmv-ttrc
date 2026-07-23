#!/usr/bin/env python3
"""Canonical semantic colours and category-border styles for analysis output.

The sim category map, analysis-video legend/status/timeline, layout preview,
and whole-movie timeline must all import this module.  Do not duplicate these
values in individual renderers.
"""

from __future__ import annotations

from dataclasses import dataclass


RGB = tuple[int, int, int]

# Display categories.
CAT_RAW: RGB = (205, 205, 205)
CAT_SAME: RGB = (150, 150, 158)
CAT_NEAR: RGB = (128, 134, 144)
CAT_FLBK: RGB = (225, 185, 25)
CAT_MISS: RGB = (220, 70, 70)
CAT_DEDUP: RGB = (0, 190, 175)
CAT_PREFETCH: RGB = (85, 175, 225)

# Exact physical sources.
COL_PRG: RGB = (165, 105, 225)
COL_WRD: RGB = (65, 205, 195)
COL_WR0: RGB = COL_WRD
COL_WR1: RGB = COL_WRD
COL_DIC: RGB = (220, 120, 30)

# Status and timeline metrics.
COL_OVH: RGB = (95, 110, 122)
COL_DMA: RGB = (70, 190, 90)
COL_RUN: RGB = (215, 165, 65)
COL_LIMIT: RGB = (255, 214, 0)
COL_BAND_LIMIT: RGB = (210, 190, 90)
COL_OVER: RGB = (220, 130, 60)
COL_OVER_REMAINDER: RGB = (150, 60, 60)

# Category-border alternating colours.
COL_BORDER_BLACK: RGB = (15, 15, 15)
COL_BORDER_WHITE: RGB = (235, 235, 235)

DISPLAY_SOURCE_ORDER = ("Prg", "Wr0", "Wr1", "Dic")
METER_SUPPLY_ORDER = ("Prg", "Wr0", "Wr1")
REQ_TIMELINE_CATS = (
    "Raw", "Prg", "Wr0", "Wr1", "Dic", "Near", "Flbk", "Miss")
LEGEND_ORDER = (
    "Raw", "Same", "Near", "Flbk", "Miss", "Prg", "Wrd", "Dic")

CATEGORY_COLORS: dict[str, RGB] = {
    "Raw": CAT_RAW,
    "Same": CAT_SAME,
    "Near": CAT_NEAR,
    "Flbk": CAT_FLBK,
    "Miss": CAT_MISS,
    "Prg": COL_PRG,
    "Wr0": COL_WR0,
    "Wr1": COL_WR1,
    "Wrd": COL_WRD,
    "Dic": COL_DIC,
}
SUPPLY_COLORS = {
    name: CATEGORY_COLORS[name] for name in DISPLAY_SOURCE_ORDER
}
QUALITY_CATS = tuple(
    (name, CATEGORY_COLORS[name])
    for name in ("Raw", "Same", "Near", "Flbk", "Miss")
)
SOURCE_CATS = tuple(
    (name, CATEGORY_COLORS[name]) for name in DISPLAY_SOURCE_ORDER
)
CATS = QUALITY_CATS + SOURCE_CATS
LEGEND_CATS = tuple(
    (name, CATEGORY_COLORS[name]) for name in LEGEND_ORDER
)


@dataclass(frozen=True)
class BorderStyle:
    """One category-map/legend border style."""

    kind: str
    primary: RGB | None = None
    alternate: RGB | None = None
    dash: int = 1
    width: int = 1


CATEGORY_STYLES: dict[str, BorderStyle] = {
    "Raw": BorderStyle(
        "dashed", COL_BORDER_WHITE, COL_BORDER_BLACK, dash=1, width=1),
    "Same": BorderStyle("none"),
    "Near": BorderStyle("solid", CAT_NEAR, width=1),
    "Flbk": BorderStyle("solid", CAT_FLBK, width=1),
    "Miss": BorderStyle("fill", CAT_MISS),
    "Prg": BorderStyle(
        "dashed", COL_PRG, COL_BORDER_BLACK, dash=2, width=1),
    "Wr0": BorderStyle(
        "dashed", COL_WR0, COL_BORDER_BLACK, dash=2, width=1),
    "Wr1": BorderStyle(
        "dashed", COL_WR1, COL_BORDER_BLACK, dash=2, width=1),
    "Wrd": BorderStyle(
        "dashed", COL_WRD, COL_BORDER_BLACK, dash=2, width=1),
    "Dic": BorderStyle(
        "dashed", COL_DIC, COL_BORDER_BLACK, dash=2, width=1),
}


def draw_dashed_rect(draw, box, style: BorderStyle) -> None:
    """Draw a thin two-colour dashed rectangle from a canonical style."""

    x0, y0, x1, y1 = map(int, box)
    if style.primary is None or style.alternate is None:
        raise ValueError("dashed borders require primary and alternate colours")
    dash = max(1, int(style.dash))
    width = max(1, int(style.width))
    for start in range(x0, x1 + 1, dash):
        end = min(start + dash - 1, x1)
        phase = ((start - x0) // dash) & 1
        top = style.primary if not phase else style.alternate
        bottom = style.alternate if not phase else style.primary
        draw.line((start, y0, end, y0), fill=top, width=width)
        draw.line((start, y1, end, y1), fill=bottom, width=width)
    for start in range(y0, y1 + 1, dash):
        end = min(start + dash - 1, y1)
        phase = ((start - y0) // dash) & 1
        left = style.alternate if not phase else style.primary
        right = style.primary if not phase else style.alternate
        draw.line((x0, start, x0, end), fill=left, width=width)
        draw.line((x1, start, x1, end), fill=right, width=width)


def draw_category_border(draw, box, name: str) -> None:
    """Draw the category's canonical fill or border into a PIL image."""

    style = CATEGORY_STYLES[name]
    if style.kind == "none":
        return
    if style.kind == "fill":
        draw.rectangle(box, fill=style.primary)
        return
    if style.kind == "solid":
        draw.rectangle(box, outline=style.primary, width=style.width)
        return
    if style.kind == "dashed":
        draw_dashed_rect(draw, box, style)
        return
    raise ValueError(f"unknown category border kind: {style.kind}")


def draw_category_swatch(draw, box, name: str) -> None:
    """Draw a legend swatch using the same style as the category map."""

    x0, y0, x1, y1 = map(int, box)
    if name == "Same":
        hi, lo = (210, 210, 210), (45, 45, 45)
        cell = max(2, (x1 - x0 + 1) // 4)
        for y in range(y0, y1 + 1, cell):
            for x in range(x0, x1 + 1, cell):
                on = (((x - x0) // cell) + ((y - y0) // cell)) % 2 == 0
                draw.rectangle(
                    (x, y, min(x + cell - 1, x1), min(y + cell - 1, y1)),
                    fill=hi if on else lo,
                )
        return
    draw_category_border(draw, box, name)


def apply_numpy_category_border(base_rgb, mask, name: str) -> None:
    """Apply the canonical category border to an ``N x H x W x 3`` array."""

    import numpy as np

    style = CATEGORY_STYLES[name]
    indices = np.where(mask)[0]
    if not indices.size or style.kind in {"none", "fill"}:
        return
    height, width = base_rgb.shape[1:3]
    if style.kind == "solid":
        colour = np.asarray(style.primary, dtype=base_rgb.dtype)
        for offset in range(style.width):
            base_rgb[indices, offset, :, :] = colour
            base_rgb[indices, height - 1 - offset, :, :] = colour
            base_rgb[indices, :, offset, :] = colour
            base_rgb[indices, :, width - 1 - offset, :] = colour
        return
    if style.kind != "dashed":
        raise ValueError(f"unknown category border kind: {style.kind}")
    colours = (
        np.asarray(style.primary, dtype=base_rgb.dtype),
        np.asarray(style.alternate, dtype=base_rgb.dtype),
    )
    dash = max(1, int(style.dash))
    for position in range(max(height, width)):
        phase = (position // dash) & 1
        if position < width:
            base_rgb[indices, 0, position, :] = colours[phase]
            base_rgb[indices, height - 1, position, :] = colours[1 - phase]
        if position < height:
            base_rgb[indices, position, 0, :] = colours[1 - phase]
            base_rgb[indices, position, width - 1, :] = colours[phase]
