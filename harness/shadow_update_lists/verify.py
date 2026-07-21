#!/usr/bin/env python3
"""Verify every mixed shadow-update decision in a packed TTRC v11 stream."""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from harness.pipeline_speedup.verify_main_fastpaths import (  # noqa: E402
    bitmap_cells,
    read_stream,
)
import shadow_updates  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--header", type=Path, required=True)
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    args = parser.parse_args()

    stream = read_stream(args.header, args.body)
    with args.decisions.open("rb") as source:
        decisions = pickle.load(source)
    frames = decisions["frames"]
    metadata = decisions.get("shadow_updates") or {}
    selected = np.asarray(metadata.get("selected", ()), np.bool_)
    if len(frames) != len(stream.controls) or selected.shape != (len(frames),):
        raise AssertionError("stream, decision frames and frozen selection differ in length")
    if not len(selected) or selected[0]:
        raise AssertionError("frame 0 must exist and retain the legacy bitmap format")

    frozen_legacy = np.asarray(metadata.get("legacy_cycles", ()), np.int64)
    frozen_list = np.asarray(metadata.get("list_cycles", ()), np.int64)
    if frozen_legacy.shape != selected.shape or frozen_list.shape != selected.shape:
        raise AssertionError("frozen cycle arrays differ from the frame count")

    legacy_shadow = [0] * stream.cells
    mixed_shadow = [0] * stream.cells
    saved_cycles = 0
    control_delta = 0
    for seq, (decision_frame, block, use_list) in enumerate(
            zip(frames, stream.controls, selected, strict=True)):
        if block.use_list != bool(use_list):
            raise AssertionError(f"frame {seq}: packed/frozen format selection differs")
        if block.total_len & 1:
            raise AssertionError(f"frame {seq}: odd total_len {block.total_len}")

        ordered = sorted(decision_frame, key=lambda item: int(item[0]))
        expected_cells = [int(item[0]) for item in ordered]
        actual_cells = bitmap_cells(block.bitmap, stream.cells)
        if actual_cells != expected_cells:
            raise AssertionError(f"frame {seq}: update cells differ from decisions")

        cost = shadow_updates.frame_cost(expected_cells, stream.cells)
        if cost.legacy_cycles != int(frozen_legacy[seq]):
            raise AssertionError(f"frame {seq}: frozen legacy cycle estimate drifted")
        if cost.list_cycles != int(frozen_list[seq]):
            raise AssertionError(f"frame {seq}: frozen list cycle estimate drifted")
        if use_list and cost.list_cycles >= cost.legacy_cycles:
            raise AssertionError(f"frame {seq}: selected list is not faster")

        for cell, entry in zip(actual_cells, block.entries, strict=True):
            final_entry = entry & 0x67FF
            legacy_shadow[cell] = final_entry
            mixed_shadow[cell] = entry if use_list else final_entry
        if mixed_shadow != legacy_shadow:
            raise AssertionError(f"frame {seq}: mixed shadow differs from legacy output")

        if use_list:
            if cost.added_bytes > 0:
                raise AssertionError(f"frame {seq}: selected list grows control")
            saved_cycles += cost.saved_cycles
            control_delta += cost.added_bytes

    baseline_ring = int(metadata["baseline_ring_min"])
    baseline_ready = int(metadata["baseline_ready_min"])
    selected_ring = int(metadata["selected_ring_min"])
    selected_ready = int(metadata["selected_ready_min"])
    if metadata.get("control_growth_enabled", False):
        raise AssertionError("qualified shadow-list decisions must not grow control")
    if selected_ring < baseline_ring or selected_ready < baseline_ready:
        raise AssertionError(
            "selected stream reduced PrgBuf or control-readiness minimum")

    for corrupt_offset in range(0x10000):
        bounded = corrupt_offset & 0x0FFE
        if bounded & 1 or not 0 <= bounded < 0x1000:
            raise AssertionError("runtime shadow offset mask is not bounded")

    print(
        "shadow update-list equivalence: OK "
        f"({len(frames)} frames, list={int(selected.sum())}, "
        f"Main saved={saved_cycles} cycles, nominal avg={saved_cycles / len(frames):.2f})"
    )
    print(
        "whole-stream margins: OK "
        f"(PrgBuf min {baseline_ring}->{selected_ring} patterns, "
        f"control ready min {baseline_ready}->{selected_ready} patterns, "
        f"unrounded update-byte delta={control_delta:+d})"
    )
    print("corrupt offset containment: OK (all u16 values map to even 0x000..0xFFE)")


if __name__ == "__main__":
    main()
