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

import av_config
from quantize_global4_tiles import (
    edge_strengths, palette15, palette_lut, rgb333_keys,
)


STL4 = "stl4"
MOSAIC_GM = "mosaic-gm"
PALETTE_ALGOS = (STL4, MOSAIC_GM)

_KEYS = np.arange(512, dtype=np.uint16)
_RGB333 = np.stack([
    (_KEYS >> 6) & 7,
    (_KEYS >> 3) & 7,
    _KEYS & 7,
], axis=1).astype(np.uint8)
_RGB333_DISTANCE2 = (
    (_RGB333.astype(np.int16)[:, None, :] - _RGB333.astype(np.int16)[None, :, :]) ** 2
).sum(2).astype(np.int16)


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
        result = [darkest, *middle[:size - 2]]
        while len(result) < size - 1:
            result.append(np.asarray(darkest, dtype=np.uint8))
        result.append(brightest)
    while len(result) < size:
        result.append(np.asarray(darkest, dtype=np.uint8))
    return np.asarray(result[:size], dtype=np.uint8)


def refine_one_line_palette(palette, source_counts, candidate_limit=64):
    """Improve one line against a complete RGB333 histogram by slot swaps.

    Duplicate/low-value slots are naturally replaced first.  The same local
    search remains valid when the source uses more than 15 colours: a swap is
    accepted only when its full-histogram squared error decreases.
    """
    counts = np.asarray(source_counts, dtype=np.float64).reshape(512)
    current = np.asarray(palette, dtype=np.uint8).copy()

    def palette_keys(value):
        return rgb333_keys(value).astype(np.int16)

    def error_for(keys):
        nearest = _RGB333_DISTANCE2[:, keys].min(1)
        return int(np.dot(counts, nearest)), nearest

    current_keys = palette_keys(current)
    before, nearest = error_for(current_keys)
    swaps = []
    for _iteration in range(15):
        current_set = set(int(key) for key in current_keys)
        missing = np.asarray([
            key for key in np.flatnonzero(counts)
            if int(key) not in current_set
        ], dtype=np.int16)
        if not len(missing):
            break
        priority = counts[missing] * nearest[missing]
        if len(missing) > candidate_limit:
            keep = np.argpartition(priority, -candidate_limit)[-candidate_limit:]
            missing = missing[keep]

        distance = _RGB333_DISTANCE2[:, current_keys]
        best_error = before if not swaps else error_for(current_keys)[0]
        best = None
        for slot in range(15):
            other = np.delete(distance, slot, axis=1).min(1)
            candidate_distance = _RGB333_DISTANCE2[:, missing]
            score = (counts[:, None] * np.minimum(
                other[:, None], candidate_distance
            )).sum(0)
            choice = int(score.argmin())
            value = int(score[choice])
            if value < best_error:
                best_error = value
                best = slot, int(missing[choice])
        if best is None:
            break
        slot, key = best
        old_key = int(current_keys[slot])
        current[slot] = _RGB333[key]
        current_keys[slot] = key
        swaps.append({"slot": slot + 1, "old": old_key, "new": key})
        _score, nearest = error_for(current_keys)

    current = _force_source_extremes(current, counts)
    after, _nearest = error_for(palette_keys(current))
    return current, {
        "source_colours": int(np.count_nonzero(counts)),
        "before_error": before,
        "after_error": after,
        "swaps": swaps,
        "exact": after == 0,
    }


class PaletteEvaluator:
    """Keep RGB333 tile data resident while palette candidates change.

    MOSAIC-GM repeatedly needs a 512-colour histogram for every current
    palette-line assignment.  Building one histogram per line used to rescan
    and copy the same tile pixels many times.  Keep the optional edge weights
    beside the keys and build every line's histogram in one grouped pass.
    """

    def __init__(self, tiles, weights=None, weight_strengths=None,
                 weight_alpha=None):
        self.keys = rgb333_keys(tiles).reshape(len(tiles), 64)
        self.weights = (None if weights is None else
                        np.asarray(weights, dtype=np.float64).reshape(self.keys.shape))
        self.weight_strengths = (
            None if weight_strengths is None else
            np.asarray(weight_strengths, dtype=np.uint8).reshape(self.keys.shape))
        self.weight_alpha = (None if weight_alpha is None else float(weight_alpha))
        if self.weight_strengths is not None and self.weight_alpha is None:
            raise ValueError("edge strengths need their alpha coefficient")
        self._cp = None
        self._gpu_keys = None
        self._gpu_weights = None
        self._gpu_weight_strengths = None
        try:
            import gpu_quant
            if gpu_quant.enabled():
                self._cp = gpu_quant.cupy()
                self._gpu_keys = self._cp.asarray(self.keys)
                if self.weights is not None:
                    self._gpu_weights = self._cp.asarray(self.weights)
                if self.weight_strengths is not None:
                    self._gpu_weight_strengths = self._cp.asarray(
                        self.weight_strengths)
        except Exception as exc:  # GPU remains an optional acceleration path
            print(f"[MOSAIC-GM] GPU evaluator fallback: {exc}")

    def color_histograms(self, assign=None, groups=1, weighted=False):
        """Return all assigned groups' RGB333 histograms in one data pass.

        ``assign`` is one group number per tile.  Encoding the group into the
        upper bits of the 9-bit RGB333 key turns N separate masked bincounts
        into one contiguous bincount.  CuPy follows the same path while the
        keys and edge weights remain resident on the GPU.
        """
        groups = max(1, int(groups))
        if assign is None:
            if groups != 1:
                raise ValueError("unassigned colour histograms have one group")
            host_assign = None
        else:
            host_assign = np.asarray(assign, dtype=np.int16).reshape(-1)
            if len(host_assign) != len(self.keys):
                raise ValueError(
                    f"assignment count {len(host_assign)} differs from "
                    f"tile count {len(self.keys)}")
            if len(host_assign) and (
                    int(host_assign.min()) < 0 or int(host_assign.max()) >= groups):
                raise ValueError("palette assignment lies outside histogram groups")

        if self._cp is None:
            encoded = self.keys
            if host_assign is not None:
                encoded = encoded + host_assign[:, None].astype(np.uint16) * 512
            if weighted and self.weight_strengths is not None:
                count = np.bincount(
                    encoded.reshape(-1), minlength=groups * 512)
                strength = np.bincount(
                    encoded.reshape(-1),
                    weights=self.weight_strengths.reshape(-1),
                    minlength=groups * 512)
                return (count.astype(np.float64)
                        + self.weight_alpha * strength / 21.0).reshape(groups, 512)
            values = (None if not weighted or self.weights is None else
                      self.weights.reshape(-1))
            return np.bincount(
                encoded.reshape(-1), weights=values,
                minlength=groups * 512,
            ).reshape(groups, 512)

        cp = self._cp
        encoded = self._gpu_keys
        if host_assign is not None:
            gpu_assign = cp.asarray(host_assign, dtype=cp.uint16)
            encoded = encoded + gpu_assign[:, None] * 512
        if weighted and self._gpu_weight_strengths is not None:
            count = cp.bincount(
                encoded.reshape(-1), minlength=groups * 512)
            strength = cp.bincount(
                encoded.reshape(-1),
                weights=self._gpu_weight_strengths.reshape(-1),
                minlength=groups * 512)
            result = count.astype(cp.float64) + self.weight_alpha * strength / 21.0
            return cp.asnumpy(result.reshape(groups, 512))
        values = (self._gpu_weights.reshape(-1)
                  if weighted and self._gpu_weights is not None else None)
        result = cp.bincount(
            encoded.reshape(-1), weights=values,
            minlength=groups * 512,
        ).reshape(groups, 512)
        return cp.asnumpy(result)

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
    mapping_weight = (
        av_config.PALETTE_MAP_WEIGHT
        if mapping_weight is None else float(mapping_weight))
    palettes = [np.asarray(palette, dtype=np.uint8) for palette in palettes]
    errors = evaluator.errors(palettes)
    assign = errors.argmin(1).astype(np.int8)
    tile_error = errors[np.arange(len(errors)), assign]
    pixel_error = int(tile_error.sum())

    line_hist = evaluator.color_histograms(assign, groups=len(palettes))
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


def coherent_assign_idx(tiles, palettes, rows, cols, seam_weight=1.0, iterations=2):
    """Assign palette lines with an added 8x8-boundary residual penalty.

    The source edge itself is not penalized. The pair term compares each
    candidate tile's quantization residual (output minus source) with the
    selected neighbour residual, so only a boundary introduced by palette
    quantization costs energy. Checkerboard updates keep every pass
    deterministic and map directly to a future GPU kernel.
    """
    tiles = np.asarray(tiles, dtype=np.uint8).reshape(-1, 64, 3)
    palettes = np.asarray(palettes, dtype=np.uint8)
    if len(tiles) != rows * cols:
        raise ValueError(f"tile count {len(tiles)} differs from {rows}x{cols}")
    keys = rgb333_keys(tiles)
    tables = [palette_lut(palette, squared=True) for palette in palettes]
    cost = np.stack([table[0] for table in tables])
    index = np.stack([table[1] for table in tables])
    tile_error = cost[:, keys].sum(2, dtype=np.int64).T
    assign = tile_error.argmin(1).astype(np.int8)
    if seam_weight > 0 and len(palettes) > 1:
        quantized = np.stack([
            palettes[line][index[line, keys]]
            for line in range(len(palettes))
        ]).astype(np.int16)
        residual = (quantized - tiles[None].astype(np.int16)).reshape(
            len(palettes), rows, cols, 8, 8, 3)
        assignment = assign.reshape(rows, cols)
        for _iteration in range(max(1, int(iterations))):
            for parity in (0, 1):
                for row in range(rows):
                    for col in range((parity - row) & 1, cols, 2):
                        energy = tile_error[row * cols + col].astype(np.float64)
                        if row:
                            neighbour = int(assignment[row - 1, col])
                            delta = residual[:, row, col, 0] - residual[neighbour, row - 1, col, 7]
                            energy += seam_weight * (delta * delta).sum((1, 2))
                        if row + 1 < rows:
                            neighbour = int(assignment[row + 1, col])
                            delta = residual[:, row, col, 7] - residual[neighbour, row + 1, col, 0]
                            energy += seam_weight * (delta * delta).sum((1, 2))
                        if col:
                            neighbour = int(assignment[row, col - 1])
                            delta = residual[:, row, col, :, 0] - residual[neighbour, row, col - 1, :, 7]
                            energy += seam_weight * (delta * delta).sum((1, 2))
                        if col + 1 < cols:
                            neighbour = int(assignment[row, col + 1])
                            delta = residual[:, row, col, :, 7] - residual[neighbour, row, col + 1, :, 0]
                            energy += seam_weight * (delta * delta).sum((1, 2))
                        assignment[row, col] = int(energy.argmin())
        assign = assignment.reshape(-1).astype(np.int8)
    selected = index[assign[:, None], keys] + 1
    return assign, selected.astype(np.uint8)


def _fit_independent(initial, evaluator, iterations=3):
    palettes = [np.asarray(palette, dtype=np.uint8) for palette in initial]
    for _ in range(iterations):
        assign = evaluator.errors(palettes).argmin(1)
        grouped = evaluator.color_histograms(
            assign, groups=len(palettes), weighted=True)
        next_palettes = []
        for line, old in enumerate(palettes):
            mask = assign == line
            if not mask.any():
                next_palettes.append(old)
                continue
            counts = grouped[line]
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


def _shared_rows(independent, core_size, global_counts, group_counts):
    common = _force_source_extremes(
        _palette_from_counts(global_counts, core_size), global_counts)
    specialist_count = 15 - len(common)
    common_error = np.full(512, np.iinfo(np.int16).max, dtype=np.int16)
    for color in common:
        error, _index = palette_lut(np.asarray([color], dtype=np.uint8), squared=True)
        common_error = np.minimum(common_error, error)

    rows = []
    for line in range(len(independent)):
        counts = group_counts[line]
        # common_error depends only on the 512-colour key.  Multiplying the
        # already grouped histogram is equivalent to rescanning every selected
        # pixel with that lookup table.
        residual_counts = counts * common_error
        specialist = _specialists(
            common, specialist_count, counts, residual_counts, independent[line])
        if len(common) <= 1:
            row = np.vstack([common, specialist])
        else:
            row = np.vstack([common[:-1], specialist, common[-1:]])
        if row.shape != (15, 3):
            raise AssertionError(f"MOSAIC-GM row has shape {row.shape}, expected (15,3)")
        rows.append(row.astype(np.uint8))
    return rows


def build_mosaic_palettes(train_tiles, n_pal=4, return_stats=False,
                          train_weights=None, train_strengths=None):
    """Learn one to four shared-core lines with automatic Grow/Merge selection."""
    if n_pal < 1 or n_pal > 4:
        raise ValueError("MOSAIC-GM supports one to four hardware palette lines")
    tiles = np.asarray(train_tiles, dtype=np.uint8).reshape(-1, 64, 3)
    if not len(tiles):
        raise ValueError("cannot train palettes without tiles")
    alpha = float(os.environ.get("CBRSIM_EDGE_WEIGHT", "3.0"))
    if train_strengths is not None:
        strengths = np.asarray(
            train_strengths, dtype=np.uint8).reshape(len(tiles), 64)
        weights = None
    elif train_weights is None:
        strengths = edge_strengths(tiles) if alpha > 0 else None
        weights = None
    else:
        weights = np.asarray(train_weights, dtype=np.float64).reshape(len(tiles), 64)
        strengths = None
    evaluator = PaletteEvaluator(
        tiles, weights=weights, weight_strengths=strengths,
        weight_alpha=alpha if strengths is not None else None)
    global_counts = evaluator.color_histograms(weighted=True)[0]

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
        seed_mask = np.zeros(len(tiles), dtype=np.int8)
        seed_mask[order] = 1
        seed_counts = evaluator.color_histograms(
            seed_mask, groups=2, weighted=True)[1]
        seed = _force_source_extremes(_palette_from_counts(seed_counts, 15), seed_counts)
        independent = _fit_independent([*current.palettes, seed], evaluator)
        independent_assign = evaluator.errors(independent).argmin(1)
        independent_counts = evaluator.color_histograms(
            independent_assign, groups=len(independent), weighted=True)

        candidates = []
        for core_size in core_sizes:
            shared = _shared_rows(
                independent, core_size, global_counts, independent_counts)
            # Refit specialists once after the shared rows move tile ownership.
            first = score_palettes(
                tiles, shared, evaluator=evaluator, core_colors=core_size)
            first_counts = evaluator.color_histograms(
                first.assign, groups=len(independent), weighted=True)
            shared = _shared_rows(
                independent, core_size, global_counts, first_counts)
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
