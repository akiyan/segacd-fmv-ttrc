#!/usr/bin/env python3
"""Measure issue #27 bitmap-loop cycles over a real packed stream.

The instruction timings come from Section 8 of the MC68000 User's Manual.  The
model follows the assembled word-displacement branches and indexed dispatch in
boot/movieplay_ip.s, rather than treating the source as abstract pseudocode.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from harness.pipeline_speedup.verify_main_fastpaths import read_stream  # noqa: E402


DEFAULT_MAIN_CLOCK_HZ = 7_670_454

# MC68000 User's Manual, 16-bit instruction timings (Section 8).  These values
# include instruction fetch and the named operand access.  Memory is assumed to
# complete in the manual's standard four-clock bus cycle.
MOVE_B_POSTINC_DN = 8       # Table 8-2: MOVE.B (An)+,Dn
MOVE_W_POSTINC_DN = 8       # Table 8-2: MOVE.W (An)+,Dn
MOVE_W_DN_INDIRECT = 8      # Table 8-2: MOVE.W Dn,(An)/(An)+
MOVE_W_DN_DISP = 12         # Table 8-2: MOVE.W Dn,(d16,An)
MOVE_W_INDEX_DN = 14        # Table 8-2: MOVE.W (d8,An,Xn),Dn
MOVE_L_POSTINC_DN = 12      # Table 8-3: MOVE.L (An)+,Dn
MOVE_L_DN_INDIRECT = 12     # Table 8-3: MOVE.L Dn,(An)+
MOVE_L_IMMEDIATE_DN = 12    # Table 8-3: MOVE.L #data,Dn
MOVEQ = 4                   # Table 8-5
CMPI_B_DN = 8               # Table 8-5
ANDI_W_DN = 8               # Table 8-5
AND_W_DN_DN = 4             # Table 8-4
AND_L_DN_DN = 8             # Table 8-4 register-direct long special case
ADD_W_DN_DN = 4             # Table 8-4
ADDQ_L_AN = 8               # Table 8-5
LSR_B_ONE = 8               # Table 8-7: 6+2n, n=1
BCC_W_TAKEN = 10            # Table 8-9
BCC_W_NOT_TAKEN = 12        # Table 8-9
BRA_W = 10                  # Table 8-9
DBRA_CONTINUE = 10          # Table 8-9: condition false, count not expired
DBRA_EXPIRED = 14           # Table 8-9: condition false, counter expired
LEA_DISP = 8                # Table 8-10: LEA (d16,An),An
LEA_ABS_LONG = 12           # Table 8-10: LEA (xxx).L,An
JMP_INDEX = 14              # Table 8-10: JMP (d8,An,Xn)
TST_W_ABS_LONG = 16         # Table 8-6 base 4 + Table 8-1 absolute-long EA 12


def reference_byte_cycles(mask: int) -> int:
    """Cycles for one legacy bitmap byte, excluding the outer DBRA."""
    cycles = MOVE_B_POSTINC_DN
    if mask == 0:
        return cycles + BCC_W_TAKEN + LEA_DISP + BRA_W

    cycles += BCC_W_NOT_TAKEN + CMPI_B_DN
    if mask == 0xFF:
        cycles += BCC_W_TAKEN
        cycles += 8 * (MOVE_W_POSTINC_DN + ANDI_W_DN + MOVE_W_DN_INDIRECT)
        # bf_ufull falls directly into bf_unext.
        return cycles

    cycles += BCC_W_NOT_TAKEN + MOVEQ
    for bit in range(8):
        cycles += LSR_B_ONE
        if mask & (1 << bit):
            cycles += BCC_W_NOT_TAKEN
            cycles += MOVE_W_POSTINC_DN + ANDI_W_DN + MOVE_W_DN_INDIRECT
        else:
            cycles += BCC_W_TAKEN
        cycles += ADDQ_L_AN
        cycles += DBRA_CONTINUE if bit != 7 else DBRA_EXPIRED
    return cycles + BRA_W


def generated_byte_cycles(mask: int) -> int:
    """Cycles for one generated-path bitmap byte, excluding outer DBRA."""
    cycles = MOVE_B_POSTINC_DN
    if mask == 0:
        return cycles + BCC_W_TAKEN + LEA_DISP + BRA_W

    cycles += BCC_W_NOT_TAKEN + CMPI_B_DN
    if mask == 0xFF:
        cycles += BCC_W_TAKEN
        cycles += 4 * (MOVE_L_POSTINC_DN + AND_L_DN_DN + MOVE_L_DN_INDIRECT)
        return cycles

    cycles += BCC_W_NOT_TAKEN + ANDI_W_DN + ADD_W_DN_DN
    cycles += MOVE_W_INDEX_DN + JMP_INDEX
    for bit in range(8):
        if mask & (1 << bit):
            cycles += MOVE_W_POSTINC_DN + AND_W_DN_DN
            cycles += MOVE_W_DN_INDIRECT if bit == 0 else MOVE_W_DN_DISP
    return cycles + LEA_DISP + BRA_W


def frame_cycles(bitmap: bytes, has_entries: bool) -> tuple[int, int]:
    """Return reference/generated cycles for the complete bitmap-byte loop."""
    if not has_entries:
        # build_frame branches to bf_blit before either bitmap loop is entered.
        return 0, 0

    outer_loop = DBRA_CONTINUE * (len(bitmap) - 1) + DBRA_EXPIRED
    reference = sum(reference_byte_cycles(mask) for mask in bitmap) + outer_loop

    # Once per non-empty frame: prove md_codegen, load the table base, then
    # branch around the legacy loop after the final generated handler returns.
    generated_setup = (
        TST_W_ABS_LONG + BCC_W_NOT_TAKEN + LEA_ABS_LONG + MOVE_L_IMMEDIATE_DN
    )
    generated = (
        generated_setup
        + sum(generated_byte_cycles(mask) for mask in bitmap)
        + outer_loop
        + BRA_W
    )
    return reference, generated


def percentile(values: list[int], percent: int) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[percent - 1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure legacy versus generated Main bitmap-handler cycles."
    )
    parser.add_argument("--header", type=Path, required=True)
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--clock-hz", type=int, default=DEFAULT_MAIN_CLOCK_HZ)
    args = parser.parse_args()
    if args.clock_hz <= 0:
        parser.error("--clock-hz must be positive")

    stream = read_stream(args.header, args.body)
    measured = [
        frame_cycles(block.bitmap, bool(block.entries)) for block in stream.controls
    ]
    reference = [item[0] for item in measured]
    generated = [item[1] for item in measured]
    saved = [old - new for old, new in measured]
    average_saved = statistics.mean(saved)
    regressed = sum(value < 0 for value in saved)

    mask_counts = [0, 0, 0]
    for block in stream.controls:
        if not block.entries:
            continue
        for mask in block.bitmap:
            bucket = 0 if mask == 0 else 1 if mask == 0xFF else 2
            mask_counts[bucket] += 1

    print(
        f"stream: {stream.cols}x{stream.rows}, {len(stream.controls)} frames, "
        f"bitmap bytes zero/full/mixed={mask_counts[0]}/{mask_counts[1]}/{mask_counts[2]}"
    )
    print(
        "bitmap loop average: "
        f"reference={statistics.mean(reference):.2f} cycles, "
        f"generated={statistics.mean(generated):.2f} cycles"
    )
    print(
        "saved cycles/frame: "
        f"average={average_saved:.2f}, median={statistics.median(saved):.2f}, "
        f"p05={percentile(saved, 5):.2f}, p95={percentile(saved, 95):.2f}, "
        f"min={min(saved)}, max={max(saved)}"
    )
    print(
        f"average Main-CPU time saved: {average_saved / args.clock_hz * 1000:.3f} ms "
        f"at {args.clock_hz} Hz; regressed frames={regressed}"
    )
    if regressed:
        raise AssertionError(f"generated path regressed {regressed} packed frames")


if __name__ == "__main__":
    main()
