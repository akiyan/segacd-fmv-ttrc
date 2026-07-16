"""Palette-line selection algorithms for the RGB333 tile encoder.

``STL4`` is the legacy four-line tile Lloyd learner in
``quantize_global4_tiles.py``.  ``MOSAIC-GM`` starts with one global line and
grows only when a shared-core specialist line materially improves the rendered
result.  Redundant lines are pruned before the four hardware rows are emitted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

from quantize_global4_tiles import edge_weights, palette15, palette_lut, rgb333_keys


STL4 = "stl4"
MOSAIC_GM = "mosaic-gm"
PALETTE_ALGOS = (STL4, MOSAIC_GM)

_KEYS = np.arange(512, dtype=np.uint16)
_RGB333 = np.stack([
    (_KEYS >> 6) & 7,
    (_KEYS >> 3) & 7,
    _KEYS & 7,
], axis=1).astype(np.uint8)


def normalize_palette_algo(value: str | None = None) -> str:
    name = (value or os.environ.get("CBRSIM_PAL_ALGO", STL4)).strip().lower()
    aliases = {
        "stl": STL4,
        "stl4": STL4,
        "mosaic": MOSAIC_GM,
        "mosaic_gm": MOSAIC_GM,
        "mosaic-gm": MOSAIC_GM,
    }
    if name not in aliases:
        raise ValueError(f"unknown palette algorithm {name!r}; choose one of {PALETTE_ALGOS}")
    return aliases[name]


def _counts(keys, weights=None, tile_mask=None, extra_lut=None):
    selected = keys if tile_mask is None else keys[tile_mask]
    if selected.size == 0:
        return np.zeros(512, dtype=np.float64)
    if weights is None:
        pixel_weights = None
    else:
        pixel_weights = weights if tile_mask is None else weights[tile_mask]
        pixel_weights = pixel_weights.reshape(-1)
    flat = selected.reshape(-1)
    if extra_lut is not None:
        extra = np.asarray(extra_lut, dtype=np.float64)[flat]
        pixel_weights = extra if pixel_weights is None else pixel_weights * extra
    return np.bincount(flat, weights=pixel_weights, minlength=512).astype(np.float64)


def _palette_from_counts(counts, colors):
    if colors <= 0:
        return np.empty((0, 3), dtype=np.uint8)
    used = np.flatnonzero(counts > 0)
    if not len(used):
        return np.zeros((colors, 3), dtype=np.uint8)
    # palette15 applies the same saturation weighting and weighted RGB333
    # snapping as STL4, while this 512-entry input avoids rescanning pixels.
    return palette15(_RGB333[used], colors=colors, weights=counts[used])


def _force_source_extremes(colors, source_counts):
    """Keep source darkest/brightest in a fixed-size palette, then order them."""
    colors = np.asarray(colors, dtype=np.uint8)
    size = len(colors)
    used = np.flatnonzero(source_counts > 0)
    if not len(used) or size == 0:
        return colors.copy()
    values = _RGB333[used].astype(np.int16).sum(1)
    darkest = _RGB333[int(used[int(values.argmin())])]
    brightest = _RGB333[int(used[int(values.argmax())])]

    ordered = []
    seen = set()
    for color in (darkest, *colors, brightest):
        key = tuple(int(channel) for channel in color)
        if key not in seen:
            seen.add(key)
            ordered.append(np.asarray(color, dtype=np.uint8))
    middle = [color for color in ordered
              if not np.array_equal(color, darkest) and not np.array_equal(color, brightest)]
    if np.array_equal(darkest, brightest):
        result = [darkest, *middle]
    elif size == 1:
        result = [darkest]
    else:
        result = [darkest, *middle[:size - 2], brightest]
    while len(result) < size:
        result.append(np.asarray(result[0], dtype=np.uint8))
    return np.asarray(result[:size], dtype=np.uint8)


class PaletteEvaluator:
    """Keep RGB333 tile keys resident and score changing palette candidates."""

    def __init__(self, tiles):
        self.keys = rgb333_keys(tiles).reshape(len(tiles), 64)
        self._cp = None
        self._gpu_keys = None
        try:
            import gpu_quant
            if gpu_quant.enabled():
                self._cp = gpu_quant.cupy()
                self._gpu_keys = self._cp.asarray(self.keys)
        except Exception as exc:  # GPU remains an optional acceleration path
            print(f"[MOSAIC-GM] GPU evaluator fallback: {exc}")

    def errors(self, palettes):
        cost = np.stack([palette_lut(palette, squared=True)[0] for palette in palettes])
        if self._cp is None:
            return cost[:, self.keys].sum(2, dtype=np.int64).T
        gpu_cost = self._cp.asarray(cost)
        result = gpu_cost[:, self._gpu_keys].sum(2).T
        return self._cp.asnumpy(result).astype(np.int64)


@dataclass
class PaletteScore:
    palettes: list[np.ndarray]
    assign: np.ndarray
    tile_error: np.ndarray
    pixel_error: int
    mapping_noise: int
    score: float
    line_fraction: list[float]
    core_colors: int

    def summary(self):
        pixels = max(1, len(self.assign) * 64)
        return {
            "active_lines": len(self.palettes),
            "core_colors": int(self.core_colors),
            "pixel_error_per_pixel": self.pixel_error / pixels,
            "mapping_noise_per_pixel": self.mapping_noise / pixels,
            "score_per_pixel": self.score / pixels,
            "line_fraction": self.line_fraction,
        }


def score_palettes(tiles, palettes, evaluator=None, mapping_weight=None, core_colors=0):
    """Score reconstruction plus palette-dependent mapping inconsistency.

    Mapping noise is charged only when the same RGB333 source colour is used by
    more than one selected line and those lines render it differently.  It is a
    direct proxy for palette-created 8x8 texture changes without punishing real
    source edges.
    """
    evaluator = evaluator or PaletteEvaluator(tiles)
    mapping_weight = (float(os.environ.get("CBRSIM_PAL_MAP_WEIGHT", "1.0"))
                      if mapping_weight is None else float(mapping_weight))
    palettes = [np.asarray(palette, dtype=np.uint8) for palette in palettes]
    errors = evaluator.errors(palettes)
    assign = errors.argmin(1).astype(np.int8)
    tile_error = errors[np.arange(len(errors)), assign]
    pixel_error = int(tile_error.sum())

    line_hist = np.stack([
        np.bincount(evaluator.keys[assign == line].reshape(-1), minlength=512)
        for line in range(len(palettes))
    ])
    maps = []
    for palette in palettes:
        _error, index = palette_lut(palette, squared=True)
        maps.append(palette[index].astype(np.int16))
    mapping_noise = 0
    for left in range(len(palettes)):
        for right in range(left + 1, len(palettes)):
            shared = np.minimum(line_hist[left], line_hist[right])
            difference = ((maps[left] - maps[right]) ** 2).sum(1)
            mapping_noise += int((shared * difference).sum())

    fraction = np.bincount(assign, minlength=len(palettes)) / max(1, len(assign))
    return PaletteScore(
        palettes=palettes,
        assign=assign,
        tile_error=tile_error,
        pixel_error=pixel_error,
        mapping_noise=mapping_noise,
        score=float(pixel_error + mapping_weight * mapping_noise),
        line_fraction=[float(value) for value in fraction],
        core_colors=int(core_colors),
    )


def _fit_independent(tiles, keys, weights, initial, evaluator, iterations=3):
    palettes = [np.asarray(palette, dtype=np.uint8) for palette in initial]
    for _ in range(iterations):
        assign = evaluator.errors(palettes).argmin(1)
        next_palettes = []
        for line, old in enumerate(palettes):
            mask = assign == line
            if not mask.any():
                next_palettes.append(old)
                continue
            counts = _counts(keys, weights, mask)
            next_palettes.append(_force_source_extremes(
                _palette_from_counts(counts, 15), counts))
        palettes = next_palettes
    return palettes


def _specialists(common, count, group_counts, residual_counts, fallback):
    if count <= 0:
        return np.empty((0, 3), dtype=np.uint8)
    learned = _palette_from_counts(residual_counts, count)
    common_set = {tuple(int(channel) for channel in color) for color in common}
    result = []
    seen = set(common_set)
    for source in (learned, fallback, _RGB333[np.argsort(group_counts)[::-1]]):
        for color in source:
            key = tuple(int(channel) for channel in color)
            if key in seen or group_counts[(key[0] << 6) | (key[1] << 3) | key[2]] <= 0:
                continue
            seen.add(key)
            result.append(np.asarray(color, dtype=np.uint8))
            if len(result) == count:
                return np.asarray(result, dtype=np.uint8)
    filler = np.asarray(common[0] if len(common) else (0, 0, 0), dtype=np.uint8)
    while len(result) < count:
        result.append(filler.copy())
    return np.asarray(result, dtype=np.uint8)


def _shared_rows(tiles, keys, weights, assign, independent, core_size, global_counts):
    common = _force_source_extremes(
        _palette_from_counts(global_counts, core_size), global_counts)
    specialist_count = 15 - len(common)
    common_error = np.full(512, np.iinfo(np.int16).max, dtype=np.int16)
    for color in common:
        error, _index = palette_lut(np.asarray([color], dtype=np.uint8), squared=True)
        common_error = np.minimum(common_error, error)

    rows = []
    for line in range(len(independent)):
        mask = assign == line
        group_counts = _counts(keys, weights, mask)
        residual_counts = _counts(keys, weights, mask, extra_lut=common_error)
        specialist = _specialists(
            common, specialist_count, group_counts, residual_counts, independent[line])
        if len(common) <= 1:
            row = np.vstack([common, specialist])
        else:
            row = np.vstack([common[:-1], specialist, common[-1:]])
        if row.shape != (15, 3):
            raise AssertionError(f"MOSAIC-GM row has shape {row.shape}, expected (15,3)")
        rows.append(row.astype(np.uint8))
    return rows


def build_mosaic_palettes(train_tiles, n_pal=4, return_stats=False):
    """Learn one to four shared-core lines with automatic Grow/Merge selection."""
    if n_pal < 1 or n_pal > 4:
        raise ValueError("MOSAIC-GM supports one to four hardware palette lines")
    tiles = np.asarray(train_tiles, dtype=np.uint8).reshape(-1, 64, 3)
    if not len(tiles):
        raise ValueError("cannot train palettes without tiles")
    alpha = float(os.environ.get("CBRSIM_EDGE_WEIGHT", "3.0"))
    weights = edge_weights(tiles, alpha)
    keys = rgb333_keys(tiles).reshape(len(tiles), 64)
    global_counts = _counts(keys, weights)
    evaluator = PaletteEvaluator(tiles)

    base = _force_source_extremes(_palette_from_counts(global_counts, 15), global_counts)
    current = score_palettes(tiles, [base], evaluator=evaluator, core_colors=15)
    grows = []
    grow_rel = float(os.environ.get("CBRSIM_PAL_GROW_REL", "0.005"))
    grow_abs = float(os.environ.get("CBRSIM_PAL_GROW_ABS", "0.002"))
    min_usage = float(os.environ.get("CBRSIM_PAL_MIN_USAGE", "0.002"))
    core_sizes = sorted({
        max(2, min(14, int(value)))
        for value in os.environ.get("CBRSIM_PAL_CORE_SIZES", "4,6,8,10,12,14").split(",")
        if value.strip()
    })

    while len(current.palettes) < n_pal:
        positive = np.flatnonzero(current.tile_error > 0)
        if not len(positive):
            break
        seed_count = min(len(positive), max(64, len(tiles) // 8))
        order = positive[np.argsort(current.tile_error[positive], kind="stable")[-seed_count:]]
        seed_counts = _counts(keys, weights, np.isin(np.arange(len(tiles)), order))
        seed = _force_source_extremes(_palette_from_counts(seed_counts, 15), seed_counts)
        independent = _fit_independent(
            tiles, keys, weights, [*current.palettes, seed], evaluator)
        independent_assign = evaluator.errors(independent).argmin(1)

        candidates = []
        for core_size in core_sizes:
            shared = _shared_rows(
                tiles, keys, weights, independent_assign, independent,
                core_size, global_counts)
            # Refit specialists once after the shared rows move tile ownership.
            first = score_palettes(
                tiles, shared, evaluator=evaluator, core_colors=core_size)
            shared = _shared_rows(
                tiles, keys, weights, first.assign, independent,
                core_size, global_counts)
            candidates.append(score_palettes(
                tiles, shared, evaluator=evaluator, core_colors=core_size))
        candidate = min(candidates, key=lambda result: result.score)
        improvement = current.score - candidate.score
        relative = improvement / max(1.0, current.score)
        per_pixel = improvement / (len(tiles) * 64)
        used = min(candidate.line_fraction)
        accepted = improvement > 0 and relative >= grow_rel and per_pixel >= grow_abs and used >= min_usage
        grows.append({
            "from_lines": len(current.palettes),
            "to_lines": len(candidate.palettes),
            "relative_gain": relative,
            "gain_per_pixel": per_pixel,
            "least_used_fraction": used,
            "core_colors": candidate.core_colors,
            "accepted": bool(accepted),
        })
        if not accepted:
            break
        current = candidate

    # Merge pass: later growth can make an older specialist redundant.  Remove
    # lines below the same minimum-use floor when doing so does not exceed the
    # inverse of the Grow threshold.
    merged = []
    while len(current.palettes) > 1:
        line = int(np.argmin(current.line_fraction))
        if current.line_fraction[line] >= min_usage:
            break
        remaining = [palette for i, palette in enumerate(current.palettes) if i != line]
        candidate = score_palettes(
            tiles, remaining, evaluator=evaluator,
            core_colors=current.core_colors)
        increase = (candidate.score - current.score) / (len(tiles) * 64)
        if increase > grow_abs:
            break
        merged.append(line)
        current = candidate

    active = current.palettes
    hardware = [palette.copy() for palette in active]
    while len(hardware) < 4:
        hardware.append(active[0].copy())
    stats = {
        "algo": MOSAIC_GM,
        **current.summary(),
        "grows": grows,
        "merged_lines": merged,
        "hardware_lines": 4,
    }
    print(
        f"[MOSAIC-GM] active={stats['active_lines']} core={stats['core_colors']} "
        f"pixel={stats['pixel_error_per_pixel']:.6f} "
        f"map={stats['mapping_noise_per_pixel']:.6f} "
        f"fractions={','.join(f'{value:.3f}' for value in stats['line_fraction'])}"
    )
    result = [np.asarray(palette, dtype=np.uint8) for palette in hardware]
    return (result, stats) if return_stats else result
