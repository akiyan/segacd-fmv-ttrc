"""Whole-movie reserve planning for encoder quality allocation.

The virtual quality budget is deliberately separate from the physical PrgBuf
scheduler in :mod:`stream_schedule`.

This module replaces fixed occupancy percentages with a reserve curve derived
from the already-quantized movie.  The final reserve is zero by definition;
working backwards raises the reserve only where future exact-update demand is
larger than the per-frame supply.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from tile_alloc import TileAllocator


@dataclass(frozen=True)
class DemandPrediction:
    """Exact/protected byte demand plus their new-pattern counts."""

    exact_bytes: np.ndarray
    protected_bytes: np.ndarray
    exact_cold: np.ndarray
    protected_cold: np.ndarray
    # Exact packed-pattern keys encountered by the predictive allocator.  The
    # sequence is deterministic; callers that only need byte demand may leave
    # these empty for compatibility with older tests/logs.
    cold_keys: tuple[tuple[bytes, ...], ...] = ()
    protected_keys: tuple[tuple[bytes, ...], ...] = ()


def predict_update_demands(
    pattern_frames: Sequence[np.ndarray],
    palette_frames: Sequence[np.ndarray],
    *,
    vram_tiles: int,
    name_bytes: int = 2,
    pattern_bytes: int = 32,
    max_cold: int = 0,
    protected_frames: Sequence[np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Estimate exact and protected byte demand in one VRAM dry run.

    The dry run follows the exact quantized target rather than the encoder's
    eventual approximations.  It uses the shared VRAM allocator, so recurring
    patterns cost only a name-table write while newly allocated patterns cost
    both a name-table write and pattern payload.  ``max_cold`` clips demand
    that the hardware could not apply in one frame anyway.

    ``protected_frames`` optionally selects the changes that count toward the
    second, narrower demand trace. The dry run still advances through the
    complete exact target. Frame zero is loaded from HEADER.DAT and therefore
    has zero streaming demand, while still seeding the predictive VRAM state.
    """

    prediction = predict_update_demand_details(
        pattern_frames,
        palette_frames,
        vram_tiles=vram_tiles,
        name_bytes=name_bytes,
        pattern_bytes=pattern_bytes,
        max_cold=max_cold,
        protected_frames=protected_frames,
    )
    return prediction.exact_bytes, prediction.protected_bytes


def predict_update_demand_details(
    pattern_frames: Sequence[np.ndarray],
    palette_frames: Sequence[np.ndarray],
    *,
    vram_tiles: int,
    name_bytes: int = 2,
    pattern_bytes: int = 32,
    max_cold: int = 0,
    protected_frames: Sequence[np.ndarray] | None = None,
) -> DemandPrediction:
    """Return the byte-demand traces and the cold counts behind them."""

    n = len(pattern_frames)
    if len(palette_frames) != n:
        raise ValueError("pattern and palette frame counts differ")
    if protected_frames is not None and len(protected_frames) != n:
        raise ValueError("protected frame count differs")
    if n == 0:
        empty = np.zeros(0, np.int64)
        return DemandPrediction(
            empty, empty.copy(), empty.copy(), empty.copy(), (), ())
    if vram_tiles <= 0:
        raise ValueError("vram_tiles must be positive")
    if name_bytes < 0 or pattern_bytes < 0 or max_cold < 0:
        raise ValueError("byte costs and max_cold must be non-negative")

    first_patterns = np.asarray(pattern_frames[0])
    first_palettes = np.asarray(palette_frames[0])
    if first_patterns.ndim != 2:
        raise ValueError("each pattern frame must have shape (cells, pixels)")
    cells = int(first_patterns.shape[0])
    if first_palettes.shape != (cells,):
        raise ValueError("each palette frame must have shape (cells,)")

    allocator = TileAllocator(cells, vram_tiles, 1)
    previous_keys: list[bytes | None] = [None] * cells
    previous_palettes = np.full(cells, -1, np.int64)
    exact_demand = np.zeros(n, np.int64)
    protected_demand = np.zeros(n, np.int64)
    exact_cold_demand = np.zeros(n, np.int64)
    protected_cold_demand = np.zeros(n, np.int64)
    cold_keys_by_frame: list[tuple[bytes, ...]] = []
    protected_keys_by_frame: list[tuple[bytes, ...]] = []

    for frame_idx in range(n):
        patterns = np.asarray(pattern_frames[frame_idx])
        palettes = np.asarray(palette_frames[frame_idx])
        if patterns.shape != first_patterns.shape:
            raise ValueError("pattern frame shapes differ")
        if palettes.shape != (cells,):
            raise ValueError("palette frame shapes differ")

        keys = [patterns[cell].tobytes() for cell in range(cells)]
        changed_cells = [
            cell for cell in range(cells)
            if keys[cell] != previous_keys[cell]
            or int(palettes[cell]) != int(previous_palettes[cell])
        ]
        if protected_frames is None:
            protected = np.ones(cells, bool)
        else:
            protected = np.asarray(protected_frames[frame_idx], dtype=bool)
            if protected.shape != (cells,):
                raise ValueError("each protected frame must have shape (cells,)")
        protected_cells = [cell for cell in changed_cells if protected[cell]]
        # Preserve first-cell order while deduplicating same-frame users.  The
        # allocator needs one physical pattern per key, and the deterministic
        # order later gives dictionary placement a stable tie-break.
        exact_cold_keys = tuple(dict.fromkeys(
            keys[cell] for cell in changed_cells
            if not allocator.is_resident(keys[cell])))
        protected_cold_keys = tuple(dict.fromkeys(
            keys[cell] for cell in protected_cells
            if not allocator.is_resident(keys[cell])))
        allocator.place_frame(
            [(cell, keys[cell]) for cell in changed_cells], frame_idx)

        if frame_idx > 0:
            exact_cold = len(exact_cold_keys)
            protected_cold = len(protected_cold_keys)
            if max_cold:
                exact_cold = min(exact_cold, max_cold)
                protected_cold = min(protected_cold, max_cold)
                exact_cold_keys = exact_cold_keys[:max_cold]
                protected_cold_keys = protected_cold_keys[:max_cold]
            exact_demand[frame_idx] = (
                len(changed_cells) * name_bytes + exact_cold * pattern_bytes)
            protected_demand[frame_idx] = (
                len(protected_cells) * name_bytes
                + protected_cold * pattern_bytes)
            exact_cold_demand[frame_idx] = exact_cold
            protected_cold_demand[frame_idx] = protected_cold

        cold_keys_by_frame.append(exact_cold_keys if frame_idx > 0 else ())
        protected_keys_by_frame.append(
            protected_cold_keys if frame_idx > 0 else ())

        for cell in changed_cells:
            previous_keys[cell] = keys[cell]
            previous_palettes[cell] = int(palettes[cell])

    return DemandPrediction(
        exact_demand,
        protected_demand,
        exact_cold_demand,
        protected_cold_demand,
        tuple(cold_keys_by_frame),
        tuple(protected_keys_by_frame),
    )


def build_reserve_curve(
    demand: Sequence[int] | np.ndarray,
    supply: int | Sequence[int] | np.ndarray,
    capacity: int,
) -> np.ndarray:
    """Return the minimum quality-budget bytes to retain after each frame.

    ``reserve[-1]`` is always zero.  For every earlier frame, the backwards
    pass retains only the bytes that future demand cannot replenish from its
    own per-frame supply.  Values are clipped to the physical usable capacity;
    demand beyond that cannot be made feasible by optional-upgrade restraint.
    """

    demand_arr = np.asarray(demand, dtype=np.int64)
    if demand_arr.ndim != 1:
        raise ValueError("demand must be one-dimensional")
    if capacity < 0:
        raise ValueError("capacity must be non-negative")
    if np.any(demand_arr < 0):
        raise ValueError("demand must be non-negative")

    if np.isscalar(supply):
        supply_arr = np.full(len(demand_arr), int(supply), np.int64)
    else:
        supply_arr = np.asarray(supply, dtype=np.int64)
        if supply_arr.shape != demand_arr.shape:
            raise ValueError("supply must be scalar or match demand")
    if np.any(supply_arr < 0):
        raise ValueError("supply must be non-negative")

    reserve = np.zeros(len(demand_arr), np.int64)
    for frame_idx in range(len(reserve) - 2, -1, -1):
        next_frame = frame_idx + 1
        needed = (
            int(reserve[next_frame])
            + int(demand_arr[next_frame])
            - int(supply_arr[next_frame])
        )
        reserve[frame_idx] = min(capacity, max(0, needed))
    return reserve


def planned_spend_limit(
    *,
    budget_before: int,
    frame_supply: int,
    reserve_after: int,
    already_spent: int,
) -> int:
    """Return this frame's total byte limit after protecting its reserve.

    A caller may pass work already committed before planning. In that case it
    remains authoritative and later work receives no additional bytes.
    """

    if min(budget_before, frame_supply, reserve_after, already_spent) < 0:
        raise ValueError(
            "budget, supply, reserve, and spending must be non-negative")
    spendable = max(0, budget_before + frame_supply - reserve_after)
    return max(already_spent, spendable)
