"""One-pass forecast for optional raw-pattern VRAM prefetch.

The forecast is deliberately cheap: it walks the already-quantized exact
movie once, marks frames whose protected exact demand exceeds the measured
cold cap, and returns the most widely shared missing patterns first.  The
caller currently considers only the immediately following frame.  The real
encoder pass remains the authority for available cold/BODY/VRAM headroom and
may skip any request.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from collections.abc import Sequence

import numpy as np

from tile_alloc import TileAllocator


@dataclass(frozen=True)
class PrefetchForecast:
    """Future exact patterns requested per deadline frame."""

    requests: tuple[tuple[bytes, ...], ...]
    protected_cold: np.ndarray
    requested_patterns: np.ndarray


def forecast_requests(
    pattern_frames: Sequence[np.ndarray],
    palette_frames: Sequence[np.ndarray],
    protected_frames: Sequence[np.ndarray],
    *,
    vram_tiles: int,
    max_cold: int,
) -> PrefetchForecast:
    """Return a conservative distinct-pattern request list for each frame.

    This is not another quality simulation.  It uses the exact-target
    allocator trace already needed by the reserve planner, estimates the
    number of patterns that must move out of a future burst, and ranks keys by
    how many protected cells that one 32-byte load can serve.
    """
    n = len(pattern_frames)
    if len(palette_frames) != n or len(protected_frames) != n:
        raise ValueError("prefetch forecast frame counts differ")
    if n == 0:
        empty = np.zeros(0, np.int64)
        return PrefetchForecast((), empty, empty.copy())
    if min(vram_tiles, max_cold) < 0:
        raise ValueError("prefetch forecast limits must be non-negative")

    first_patterns = np.asarray(pattern_frames[0])
    first_palettes = np.asarray(palette_frames[0])
    if first_patterns.ndim != 2:
        raise ValueError("pattern frames must have shape (cells, pixels)")
    cells = int(first_patterns.shape[0])
    if first_palettes.shape != (cells,):
        raise ValueError("palette frames must have shape (cells,)")

    alloc = TileAllocator(cells, vram_tiles, 1)
    previous_keys: list[bytes | None] = [None] * cells
    previous_palettes = np.full(cells, -1, np.int64)
    requests: list[tuple[bytes, ...]] = []
    protected_cold = np.zeros(n, np.int64)
    requested_patterns = np.zeros(n, np.int64)

    for frame in range(n):
        patterns = np.asarray(pattern_frames[frame])
        palettes = np.asarray(palette_frames[frame])
        protected = np.asarray(protected_frames[frame], bool)
        if patterns.shape != first_patterns.shape:
            raise ValueError("prefetch pattern frame shapes differ")
        if palettes.shape != (cells,) or protected.shape != (cells,):
            raise ValueError("prefetch palette/protected frame shapes differ")

        keys = [patterns[cell].tobytes() for cell in range(cells)]
        changed = [
            cell for cell in range(cells)
            if keys[cell] != previous_keys[cell]
            or int(palettes[cell]) != int(previous_palettes[cell])
        ]
        protected_cells = [cell for cell in changed if protected[cell]]
        cold_counts = Counter(
            keys[cell] for cell in protected_cells
            if not alloc.is_resident(keys[cell]))
        cold = len(cold_counts)
        protected_cold[frame] = cold

        if frame == 0 or not cold:
            selected: tuple[bytes, ...] = ()
        else:
            move = min(cold, max(0, cold - max_cold)) if max_cold else 0
            ranked = sorted(
                cold_counts,
                key=lambda key: (-cold_counts[key], key),
            )
            selected = tuple(ranked[:move])
        requests.append(selected)
        requested_patterns[frame] = len(selected)

        alloc.place_frame([(cell, keys[cell]) for cell in changed], frame)
        for cell in changed:
            previous_keys[cell] = keys[cell]
            previous_palettes[cell] = int(palettes[cell])

    return PrefetchForecast(
        requests=tuple(requests),
        protected_cold=protected_cold,
        requested_patterns=requested_patterns,
    )
