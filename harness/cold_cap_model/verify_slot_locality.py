#!/usr/bin/env python3
"""Prove that movie-wide physical slot locality preserves every display cell."""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from tile_alloc import (  # noqa: E402
    TileAllocator,
    optimize_slot_locality,
    validate_physical_slots,
    verify_display_equivalence,
)


def decision_frames(log: dict) -> list[list[tuple[int, bytes]]]:
    return [
        [(int(cell), key) for cell, _palette, key in sorted(frame)]
        for frame in log["frames"]
    ]


def logical_cold_trace(frames, cells: int, pool: int):
    allocator = TileAllocator(cells, pool, 1)
    trace = []
    for frame_index, frame in enumerate(frames):
        placements = allocator.place_frame(frame, frame_index)
        trace.append(tuple(
            int(slot) for slot, cold in placements if cold))
    if allocator.tearing:
        raise AssertionError(
            f"logical replay tore {allocator.tearing} displayed patterns")
    return tuple(trace)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("decision_log", type=Path)
    parser.add_argument(
        "--derive", action="store_true",
        help="derive a locality map from this log when it predates slot_locality")
    parser.add_argument("--cold-cap", type=int, default=0)
    args = parser.parse_args()

    with args.decision_log.open("rb") as source:
        log = pickle.load(source)
    frames = decision_frames(log)
    cells = int((log.get("geom") or (0, 0, 0, 0))[2])
    pool = int(log["vram_tiles"])
    if cells <= 0:
        raise SystemExit("decision log has no valid cell count")

    trace = logical_cold_trace(frames, cells, pool)
    locality = log.get("slot_locality") or {}
    if int(locality.get("schema_version", 0)) == 1:
        physical_by_logical = validate_physical_slots(
            locality["physical_by_logical"], pool)
        stored = True
    elif args.derive:
        plan = optimize_slot_locality(
            trace, pool, cold_cap=args.cold_cap or int(log.get("max_cold", 0)))
        physical_by_logical = np.asarray(
            plan.physical_by_logical, np.int64)
        stored = False
    else:
        raise SystemExit(
            "decision log has no slot_locality map; pass --derive for an offline proof")

    # Re-evaluate run counts for the final decisions using the stored map.
    membership = np.zeros((len(trace), pool), bool)
    for frame, slots in enumerate(trace):
        membership[frame, tuple(slots)] = True
    logical_at_physical = np.argsort(physical_by_logical)
    ordered = membership[:, logical_at_physical]
    optimized_runs = (
        ordered[:, 0].astype(np.int64)
        + np.count_nonzero(ordered[:, 1:] & ~ordered[:, :-1], axis=1)
    )
    baseline_runs = (
        membership[:, 0].astype(np.int64)
        + np.count_nonzero(membership[:, 1:] & ~membership[:, :-1], axis=1)
    )

    proof = verify_display_equivalence(
        frames, cells, pool, physical_by_logical)
    cold = membership.sum(axis=1)
    cap = args.cold_cap or int(log.get("max_cold", 0)) or int(cold[1:].max())
    risk = (cold >= int(np.ceil(cap * 0.85))) & (baseline_runs >= 40)
    risk[0] = False
    print(f"map={'stored' if stored else 'derived'} pool={pool}")
    print(
        f"display={proof['frames']}/{len(frames)} exact "
        f"cold={proof['cold']} tearing={proof['tearing']}")
    print(
        f"stream max runs {int(baseline_runs[1:].max(initial=0))} -> "
        f"{int(optimized_runs[1:].max(initial=0))}")
    print(
        f"deadline-risk frames={int(risk.sum())} max runs "
        f"{int(baseline_runs[risk].max(initial=0))} -> "
        f"{int(optimized_runs[risk].max(initial=0))}")
    print(
        f"total runs {int(baseline_runs[1:].sum())} -> "
        f"{int(optimized_runs[1:].sum())} (not an optimization constraint)")


if __name__ == "__main__":
    main()
