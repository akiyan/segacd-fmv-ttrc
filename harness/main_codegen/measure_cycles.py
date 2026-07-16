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
MOVE_W_POSTINC_ABS_LONG = 20  # Table 8-2: MOVE.W (An)+,(xxx).L
MOVE_L_POSTINC_ABS_LONG = 28  # Table 8-3: MOVE.L (An)+,(xxx).L
MOVE_L_IMMEDIATE_ABS_LONG = 28  # Table 8-3: MOVE.L #data,(xxx).L
MOVE_W_ABS_LONG_DN = 16     # Table 8-2: MOVE.W (xxx).L,Dn
MOVE_L_INDEX_AN = 18        # Table 8-3: MOVEA.L (d8,An,Xn),An
MOVE_W_DN_DN = 4            # Table 8-2: MOVE.W Dn,Dn
MOVE_L_DN_DN = 4            # Table 8-3: MOVE.L Dn,Dn
MOVEQ = 4                   # Table 8-5
CMPI_B_DN = 8               # Table 8-5
ANDI_W_DN = 8               # Table 8-5
ANDI_L_DN = 14              # Table 8-5
AND_W_DN_DN = 4             # Table 8-4
AND_L_DN_DN = 8             # Table 8-4 register-direct long special case
ADD_W_DN_DN = 4             # Table 8-4
ADD_W_ABS_LONG_DN = 16      # Table 8-4 base 4 + Table 8-1 absolute-long EA 12
ADD_L_DN_DN = 8             # Table 8-4 long register-direct special case
OR_W_DN_DN = 4              # Table 8-4
ADDQ_L_AN = 8               # Table 8-5
ADDQ_W_DN = 4               # Table 8-5
SUBQ_W_DN = 4               # Table 8-5
LSR_B_ONE = 8               # Table 8-7: 6+2n, n=1
LSL_W_TWO = 10              # Table 8-7: 6+2n, n=2
LSR_W_THREE = 12            # Table 8-7: 6+2n, n=3
LSL_W_SEVEN = 20            # Table 8-7: 6+2n, n=7
BCC_W_TAKEN = 10            # Table 8-9
BCC_W_NOT_TAKEN = 12        # Table 8-9
BCC_B_TAKEN = 10            # Table 8-9
BCC_B_NOT_TAKEN = 8         # Table 8-9
BRA_W = 10                  # Table 8-9
BSR_W = 18                  # Table 8-9
DBRA_CONTINUE = 10          # Table 8-9: condition false, count not expired
DBRA_EXPIRED = 14           # Table 8-9: condition false, counter expired
LEA_DISP = 8                # Table 8-10: LEA (d16,An),An
LEA_ABS_LONG = 12           # Table 8-10: LEA (xxx).L,An
LEA_PC_DISP = 8             # Table 8-10: LEA (d16,PC),An
JMP_INDEX = 14              # Table 8-10: JMP (d8,An,Xn)
JSR_INDIRECT = 16           # Table 8-10: JSR (An)
MOVE_W_PC_DISP_DN = 12      # Table 8-2: MOVE.W (d16,PC),Dn
SWAP_DN = 4                 # Table 8-12
RTS = 16                    # Table 8-12


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
        return cycles + BRA_W

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

    # Once per non-empty frame: check the success flag, load the table base and
    # shared cold-flag mask.  The generated loop falls directly into bf_blit.
    generated_setup = (
        MOVE_W_PC_DISP_DN
        + BCC_W_TAKEN
        + LEA_ABS_LONG
        + MOVE_L_IMMEDIATE_DN
    )
    generated = (
        generated_setup
        + sum(generated_byte_cycles(mask) for mask in bitmap)
        + outer_loop
    )
    return reference, generated


def set_vram_write_cycles() -> int:
    """Cycles for BSR set_vram_write through its RTS."""
    return (
        BSR_W
        + MOVE_L_DN_DN
        + ANDI_L_DN
        + SWAP_DN
        + 16                 # ORI.L #data,Dn, Table 8-5
        + 2 * LSL_W_SEVEN    # LSR has the same Table 8-7 timing
        + ANDI_W_DN
        + OR_W_DN_DN
        + 20                 # MOVE.L Dn,(xxx).L, Table 8-3
        + RTS
    )


def reference_blit_cycles(tcols: int, trows: int) -> int:
    """Cycles from the generic shadow LEA through the final row DBRA."""
    if tcols <= 0 or trows <= 0:
        raise ValueError("blit geometry must be positive")
    cycles = LEA_ABS_LONG + 2 * MOVE_W_ABS_LONG_DN + SUBQ_W_DN
    for row in range(trows):
        cycles += (
            MOVE_W_DN_DN
            + LSL_W_SEVEN
            + 2 * ADD_W_ABS_LONG_DN
            + MOVE_L_DN_DN
            + ANDI_L_DN
            + ADD_L_DN_DN
            + set_vram_write_cycles()
            + MOVE_W_ABS_LONG_DN
            + MOVE_W_DN_DN
            + LSR_W_THREE
        )

        groups = tcols // 8
        if groups:
            cycles += BCC_B_NOT_TAKEN + SUBQ_W_DN
            cycles += groups * 4 * MOVE_L_POSTINC_ABS_LONG
            cycles += (groups - 1) * DBRA_CONTINUE + DBRA_EXPIRED
        else:
            cycles += BCC_B_TAKEN

        tail = tcols & 7
        cycles += ANDI_W_DN
        if tail:
            cycles += BCC_B_NOT_TAKEN + SUBQ_W_DN
            cycles += tail * MOVE_W_POSTINC_ABS_LONG
            cycles += (tail - 1) * DBRA_CONTINUE + DBRA_EXPIRED
        else:
            cycles += BCC_B_TAKEN
        cycles += ADDQ_W_DN
        cycles += DBRA_CONTINUE if row != trows - 1 else DBRA_EXPIRED
    return cycles


def generated_blit_cycles(tcols: int, trows: int) -> int:
    """Cycles for successful dispatch and the fixed NT0/NT1 function."""
    if tcols <= 0 or trows <= 0:
        raise ValueError("blit geometry must be positive")
    dispatch = (
        MOVE_W_PC_DISP_DN
        + BCC_W_NOT_TAKEN
        + MOVE_W_PC_DISP_DN
        + LSL_W_TWO
        + LEA_PC_DISP
        + MOVE_L_INDEX_AN
        + JSR_INDIRECT
    )
    row = (
        MOVE_L_IMMEDIATE_ABS_LONG
        + (tcols // 2) * MOVE_L_POSTINC_ABS_LONG
        + (tcols & 1) * MOVE_W_POSTINC_ABS_LONG
    )
    return dispatch + LEA_ABS_LONG + trows * row + RTS + BRA_W


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
    blit_reference = reference_blit_cycles(stream.cols, stream.rows)
    blit_generated = generated_blit_cycles(stream.cols, stream.rows)
    blit_saved = blit_reference - blit_generated
    combined_saved = [value + blit_saved for value in saved]
    combined_regressed = sum(value < 0 for value in combined_saved)

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
    print(
        "name-table blit: "
        f"reference={blit_reference} cycles, generated={blit_generated} cycles, "
        f"saved={blit_saved} cycles/{blit_saved / args.clock_hz * 1000:.3f} ms"
    )
    combined_average = statistics.mean(combined_saved)
    print(
        "combined saved cycles/frame: "
        f"average={combined_average:.2f}/{combined_average / args.clock_hz * 1000:.3f} ms, "
        f"min={min(combined_saved)}, max={max(combined_saved)}, "
        f"regressed frames={combined_regressed}"
    )
    if regressed:
        raise AssertionError(f"generated path regressed {regressed} packed frames")
    if blit_saved <= 0 or combined_regressed:
        raise AssertionError(
            f"combined generated path regressed {combined_regressed} packed frames"
        )


if __name__ == "__main__":
    main()
