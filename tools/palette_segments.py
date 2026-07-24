#!/usr/bin/env python3
"""Pure palette-segment boundary selection.

The metric peak is the safe transition frame: it is already dark or uniform
enough to hide the final old-palette update. CRAM changes on the following
frame, after that transition frame has been displayed once.
"""
from __future__ import annotations

import numpy as np


def segment_ranges(
        dark, uniform, *, gap=24, min_frames=2, dark_threshold=0.90,
        uniform_threshold=0.88, uniform_near=8):
    """Return half-open palette ranges from dark and uniform frame metrics."""
    dark = np.asarray(dark, dtype=np.float64)
    uniform = np.asarray(uniform, dtype=np.float64)
    if dark.ndim != 1 or uniform.shape != dark.shape:
        raise ValueError("dark and uniform metrics must be equal-length vectors")
    if gap < 0 or min_frames <= 0 or uniform_near < 0:
        raise ValueError("gap/uniform_near must be non-negative and min_frames positive")
    n = len(dark)

    def cluster(metric, hit):
        hits = np.flatnonzero(hit)
        boundaries = []
        if len(hits):
            start = previous = int(hits[0])
            for raw_value in hits[1:]:
                value = int(raw_value)
                if value - previous <= gap:
                    previous = value
                else:
                    peak = start + int(np.argmax(metric[start:previous + 1]))
                    boundaries.append(min(n, peak + 1))
                    start = previous = value
            peak = start + int(np.argmax(metric[start:previous + 1]))
            boundaries.append(min(n, peak + 1))
        return boundaries

    dark_boundaries = cluster(dark, dark >= dark_threshold)
    uniform_boundaries = cluster(uniform, uniform >= uniform_threshold)
    additions = [
        value for value in uniform_boundaries
        if min(
            [abs(value - dark_value) for dark_value in dark_boundaries]
            + [1 << 30]
        ) > uniform_near
    ]
    edges = sorted(set([0, *dark_boundaries, *additions, n]))
    return [
        (edges[index], edges[index + 1])
        for index in range(len(edges) - 1)
        if edges[index + 1] - edges[index] >= min_frames
    ]
