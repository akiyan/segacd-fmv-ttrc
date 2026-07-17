#!/usr/bin/env python3
"""Compare recorded H40 HUD N values with packed cold-run descriptors."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from verify_run_descriptors import decode_descriptors, old_sub_runs, read_stream


def packed_run_counts(header: Path, body: Path) -> list[int]:
    stream = read_stream(header, body)
    counts = []
    for control in stream.controls:
        if control.descriptor_suffix is not None:
            counts.append(len(decode_descriptors(control.descriptor_suffix)))
        else:
            counts.append(len(old_sub_runs(control.entries, stream.base)))
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify OCR'd H40 HUD N against packed cold-run counts."
    )
    parser.add_argument("--header", type=Path, required=True)
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.95,
        help="ignore OCR rows below this confidence (default: 0.95)",
    )
    parser.add_argument(
        "--column",
        default="",
        help="HUD N column (default: cold_runs_low8, then legacy dma_calls)",
    )
    args = parser.parse_args()

    expected = packed_run_counts(args.header, args.body)
    compared = 0
    skipped_confidence = 0
    skipped_empty = 0
    mismatches: list[tuple[int, int, int, float]] = []
    with args.csv.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.DictReader(handle)
        fields = rows.fieldnames or []
        column = args.column
        if not column:
            if "cold_runs_low8" in fields:
                column = "cold_runs_low8"
            elif "dma_calls" in fields:
                # Early p45 diagnostic CSVs used this name before N was
                # documented as a descriptor count. It is not a DMA-call count.
                column = "dma_calls"
            else:
                raise SystemExit(
                    "CSV has neither cold_runs_low8 nor legacy dma_calls")
        if column not in fields:
            raise SystemExit(f"CSV has no {column!r} column")
        if "frame" not in fields:
            raise SystemExit("CSV has no 'frame' column")

        for row in rows:
            value = (row.get(column) or "").strip()
            frame_text = (row.get("frame") or "").strip()
            if not value or not frame_text:
                skipped_empty += 1
                continue
            confidence = float((row.get("confidence") or "1").strip())
            if confidence < args.min_confidence:
                skipped_confidence += 1
                continue
            frame = int(frame_text)
            if not 0 <= frame < len(expected):
                raise SystemExit(
                    f"CSV frame {frame} is outside packed stream 0..{len(expected) - 1}")
            observed = int(value)
            packed_low8 = expected[frame] & 0xFF
            compared += 1
            if observed != packed_low8:
                mismatches.append((frame, observed, packed_low8, confidence))

    if mismatches:
        sample = ", ".join(
            f"F{frame}: HUD={observed} packed={packed} conf={confidence:.3f}"
            for frame, observed, packed, confidence in mismatches[:8]
        )
        raise SystemExit(
            f"HUD N mismatch: {len(mismatches)}/{compared} observations; {sample}")
    if not compared:
        raise SystemExit("no eligible HUD N observations")
    print(
        "recorded HUD N equivalence: OK "
        f"({compared} observations, {len(expected)} packed frames, "
        f"column={column}, min_confidence={args.min_confidence:g}, "
        f"skipped_low_confidence={skipped_confidence}, skipped_empty={skipped_empty})"
    )


if __name__ == "__main__":
    main()
