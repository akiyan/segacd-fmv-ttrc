"""Per-frame shadow-update formats and nominal Main-CPU cycle model."""

from __future__ import annotations

from dataclasses import dataclass


LIST_TAG = 0x8000
COUNT_MASK = 0x7FFF
SHADOW_ENTRY_BYTES = 2
LIST_ITEM_BYTES = 4

# MC68000 User's Manual nominal timings used by the existing generated-bitmap
# harness. Bus wait states are deliberately outside this static comparison.
MOVE_B_POSTINC_DN = 8
MOVE_W_POSTINC_DN = 8
MOVE_L_POSTINC_DN = 12
MOVE_W_DN_INDIRECT = 8
MOVE_W_DN_DISP = 12
MOVE_L_DN_INDIRECT = 12
MOVE_W_INDEX_DN = 14
CMPI_B_DN = 8
ANDI_W_DN = 8
AND_W_DN_DN = 4
AND_L_DN_DN = 8
ADD_W_DN_DN = 4
LEA_DISP = 8
LEA_ABS_LONG = 12
MOVE_W_PC_DISP_DN = 12
MOVE_L_IMMEDIATE_DN = 12
LSR_B_ONE = 8
BCC_W_TAKEN = 10
BCC_W_NOT_TAKEN = 12
BRA_W = 10
DBRA_CONTINUE = 10
DBRA_EXPIRED = 14
JMP_INDEX = 14


@dataclass(frozen=True)
class FrameCost:
    legacy_cycles: int
    list_cycles: int
    saved_cycles: int
    legacy_bytes: int
    list_bytes: int
    added_bytes: int


def encode_count(count: int, use_list: bool) -> int:
    value = int(count)
    if not 0 <= value <= COUNT_MASK:
        raise ValueError(f"shadow update count outside 0..{COUNT_MASK}: {count}")
    return value | (LIST_TAG if use_list else 0)


def decode_count(raw: int) -> tuple[int, bool]:
    value = int(raw)
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"raw shadow update count outside u16: {raw}")
    return value & COUNT_MASK, bool(value & LIST_TAG)


def bitmap_bytes(total_cells: int) -> int:
    cells = int(total_cells)
    if cells <= 0:
        raise ValueError(f"total_cells must be positive, got {total_cells}")
    return (cells + 7) // 8


def build_bitmap(cells, total_cells: int) -> bytes:
    out = bytearray(bitmap_bytes(total_cells))
    previous = -1
    for item in cells:
        cell = int(item)
        if not 0 <= cell < int(total_cells):
            raise ValueError(f"cell {cell} outside 0..{int(total_cells) - 1}")
        if cell <= previous:
            raise ValueError("shadow update cells must be strictly ascending")
        out[cell >> 3] |= 1 << (cell & 7)
        previous = cell
    return bytes(out)


def _generated_byte_cycles(mask: int) -> int:
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


def legacy_bitmap_cycles(cells, total_cells: int) -> int:
    """Nominal cycles from the format branch through the generated bitmap path."""
    cell_tuple = tuple(int(value) for value in cells)
    if not cell_tuple:
        return 0
    bitmap = build_bitmap(cell_tuple, total_cells)
    setup = (
        MOVE_W_PC_DISP_DN + BCC_W_TAKEN + LEA_ABS_LONG
        + MOVE_L_IMMEDIATE_DN
    )
    outer = DBRA_CONTINUE * (len(bitmap) - 1) + DBRA_EXPIRED
    # BNE.W not-taken plus the bitmap/entry/shadow pointer setup preceding the
    # generated loop: MOVEA, ADDA, LEA, MOVE.W #imm and SUBQ.
    path_setup = 12 + 4 + 8 + 12 + 8 + 4
    return (
        path_setup + setup
        + sum(_generated_byte_cycles(value) for value in bitmap) + outer)


def update_list_cycles(count: int) -> int:
    """Nominal cycles for the bounded assembly walker implemented by issue #32."""
    value = int(count)
    if value < 0:
        raise ValueError("update count must not be negative")
    # BNE.W taken + LEA + SUBQ + the out-of-line BRA total 40 clocks.  Each
    # item is MOVE.W + ANDI + memory-to-memory indexed MOVE.W + DBRA = 48
    # clocks (the final DBRA's extra four clocks are included in the setup).
    # The original proposal's 40-cycle item model omitted the runtime guard.
    return 40 + 48 * value if value else 0


def frame_cost(cells, total_cells: int) -> FrameCost:
    cell_tuple = tuple(int(value) for value in cells)
    count = len(cell_tuple)
    legacy_b = bitmap_bytes(total_cells) + count * SHADOW_ENTRY_BYTES
    list_b = count * LIST_ITEM_BYTES
    legacy_c = legacy_bitmap_cycles(cell_tuple, total_cells)
    list_c = update_list_cycles(count)
    return FrameCost(
        legacy_cycles=legacy_c,
        list_cycles=list_c,
        saved_cycles=legacy_c - list_c,
        legacy_bytes=legacy_b,
        list_bytes=list_b,
        added_bytes=list_b - legacy_b,
    )


def final_name_entry(entry: int) -> int:
    """Strip cold/source metadata exactly like the Main legacy bitmap path."""
    return int(entry) & 0x67FF


def build_update_list(cells, entries, total_cells: int) -> bytes:
    import struct

    cell_tuple = tuple(int(value) for value in cells)
    entry_tuple = tuple(int(value) for value in entries)
    if len(cell_tuple) != len(entry_tuple):
        raise ValueError("shadow update cells and entries must have equal lengths")
    build_bitmap(cell_tuple, total_cells)
    out = bytearray()
    for cell, entry in zip(cell_tuple, entry_tuple):
        out += struct.pack(">HH", cell * SHADOW_ENTRY_BYTES, final_name_entry(entry))
    return bytes(out)
