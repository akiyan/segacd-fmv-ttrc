#!/usr/bin/env python3
"""Measure a conservative issue #21 cycle saving over a packed fixed-N2 stream.

Only RAM-to-immediate replacements whose execution count is known from the
packed control blocks are included.  Extra ``pump_poll`` calls, additional
wave-chunk boundaries, DMA-budget refills and palette-switch savings are left
out, so the reported totals are lower bounds rather than optimistic estimates.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from harness.pipeline_speedup.verify_main_fastpaths import read_stream  # noqa: E402
import player_constants  # noqa: E402
import ttrc_routing  # noqa: E402


MAIN_CLOCK_HZ = 7_670_454
SUB_CLOCK_HZ = 12_500_000

# MC68000 User's Manual, Section 8.  For the word operations used here, an
# absolute-long source costs eight clocks more than an immediate source.
FIXED_SOURCE_SAVING = 8
TST_ABS_LONG = 16
BCC_SHORT_NOT_TAKEN = 8


def cold_run_count(entries: tuple[int, ...]) -> int:
    runs = 0
    previous = None
    for entry in entries:
        if not entry & 0x8000:
            continue
        slot = (entry & 0x07FF) - 1
        if previous is None or slot != previous + 1:
            runs += 1
        previous = slot
    return runs


def accumulator_carry_flags(frames: int, remainder: int, modulus: int) -> list[bool]:
    accumulator = 0
    carries = []
    for _ in range(frames):
        accumulator += remainder
        if accumulator >= modulus:
            accumulator -= modulus
            carries.append(True)
        else:
            carries.append(False)
    return carries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure conservative generic-to-specialized player cycle savings."
    )
    parser.add_argument("--header", type=Path, required=True)
    parser.add_argument("--body", type=Path, required=True)
    args = parser.parse_args()

    constants = player_constants.parse_header_sector(
        args.header.read_bytes()[:player_constants.SECTOR]
    )
    if not constants.features & ttrc_routing.FEATURE_FIXED_N2:
        parser.error("the current conservative model requires a fixed-N2 stream")

    stream = read_stream(args.header, args.body)
    runs = [cold_run_count(block.entries) for block in stream.controls]

    # Main, per frame:
    # - fixed-N2 bf_doflip and do_flip each lose TST.W abs.l + untaken BEQ;
    # - non-empty updates replace two bmbytes reads;
    # - a frame with cold runs replaces at least the initial VBlank-budget read.
    fixed_flip = 2 * (TST_ABS_LONG + BCC_SHORT_NOT_TAKEN)
    main_saved = [
        fixed_flip
        + (2 * FIXED_SOURCE_SAVING if block.entries else 0)
        + (FIXED_SOURCE_SAVING if run_count else 0)
        for block, run_count in zip(stream.controls, runs)
    ]

    # Sub, per displayed frame:
    # - stream-loop frame limit;
    # - two bmbytes, features, descriptor audio size, wave audio size and at
    #   least one wave-poll mask reference;
    # - one pool bound per packed cold run.
    sub_saved = [
        7 * FIXED_SOURCE_SAVING + run_count * FIXED_SOURCE_SAVING
        for run_count in runs
    ]

    # BODY slots are frames 1..end.  Their first pump1_core visit replaces the
    # frame bound plus sec_base/sec_rem/sec_mod, and carry slots also replace
    # sec_mod in SUB.W.  Calls made by opportunistic polling are excluded.
    body_frames = max(0, len(stream.controls) - 1)
    carry_flags = accumulator_carry_flags(
        body_frames, constants.sec_rem, constants.sec_mod
    )
    for index, carry in enumerate(carry_flags, 1):
        sub_saved[index] += 4 * FIXED_SOURCE_SAVING
        if carry:
            sub_saved[index] += FIXED_SOURCE_SAVING

    combined = [main_cycles + sub_cycles for main_cycles, sub_cycles in zip(
        main_saved, sub_saved
    )]
    print(
        f"stream: {stream.cols}x{stream.rows}, {len(stream.controls)} frames, "
        f"cold runs total/average/max={sum(runs)}/{statistics.mean(runs):.3f}/{max(runs)}"
    )
    print(
        "Main saved lower bound: "
        f"average={statistics.mean(main_saved):.2f} cycles/frame, "
        f"min={min(main_saved)}, max={max(main_saved)}, "
        f"time={statistics.mean(main_saved) / MAIN_CLOCK_HZ * 1000:.4f} ms/frame"
    )
    print(
        "Sub saved lower bound: "
        f"average={statistics.mean(sub_saved):.2f} cycles/frame, "
        f"min={min(sub_saved)}, max={max(sub_saved)}, "
        f"time={statistics.mean(sub_saved) / SUB_CLOCK_HZ * 1000:.4f} ms/frame"
    )
    print(
        "combined instruction clocks saved lower bound: "
        f"average={statistics.mean(combined):.2f}, total={sum(combined)}, "
        f"rate carries={sum(carry_flags)}/{body_frames}"
    )
    if min(main_saved) <= 0 or min(sub_saved) <= 0:
        raise AssertionError("specialization did not save cycles on every frame")


if __name__ == "__main__":
    main()
