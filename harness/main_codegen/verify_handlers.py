#!/usr/bin/env python3
"""Prove the issue #27 boot-time bitmap handlers before assembly integration."""

from __future__ import annotations

import argparse
import random
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


CODEGEN_BASE = 0x00FF2000
TABLE_BYTES = 256 * 2
HANDLERS_BASE = CODEGEN_BASE + TABLE_BYTES
CODEGEN_LIMIT = 0x00FF8000

# The real address moves as movieplay_ip.s changes. It is deliberately placed
# below CODEGEN_BASE here so every generated BRA.W exercises the same backward
# branch range required by the player.
CONTINUE_ADDRESS = 0x00FF1000
PLAYER_SOURCE = Path("boot/movieplay_ip.s")

OP_MOVE_ENTRY_D3 = 0x3618       # move.w (a0)+,d3
OP_STRIP_COLD_D6_D3 = 0xC646    # and.w d6,d3
IMM_ENTRY_MASK = 0x7FFF
ENTRY_MASK_LONG = 0x7FFF7FFF
OP_STORE_D3_A1 = 0x3283         # move.w d3,(a1)
OP_STORE_D3_D16_A1 = 0x3343     # move.w d3,disp(a1)
OP_ADVANCE_SHADOW = 0x43E9      # lea 16(a1),a1
SHADOW_BYTE_ADVANCE = 16
OP_BRA_W = 0x6000

PLAYER_CONSTANTS = {
    "MAIN_CODEGEN_BASE": CODEGEN_BASE,
    "MAIN_CODEGEN_TABLE_BYTES": TABLE_BYTES,
    "MAIN_CODEGEN_HANDLER_MAX": 70,
    "MAIN_CODEGEN_EXPECTED_END": 0x00FF4900,
    "CG_OP_MOVE_ENTRY_D3": OP_MOVE_ENTRY_D3,
    "CG_OP_STRIP_COLD_D6_D3": OP_STRIP_COLD_D6_D3,
    "CG_ENTRY_MASK_LONG": ENTRY_MASK_LONG,
    "CG_OP_STORE_D3_A1": OP_STORE_D3_A1,
    "CG_OP_STORE_D3_D16_A1": OP_STORE_D3_D16_A1,
    "CG_OP_ADVANCE_SHADOW": OP_ADVANCE_SHADOW,
    "CG_SHADOW_BYTE_ADVANCE": SHADOW_BYTE_ADVANCE,
    "CG_OP_BRA_W": OP_BRA_W,
}


@dataclass(frozen=True)
class Handler:
    mask: int
    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start


def append_word(image: bytearray, value: int) -> None:
    image += struct.pack(">H", value & 0xFFFF)


def read_word(image: bytes, offset: int) -> int:
    return struct.unpack_from(">H", image, offset)[0]


def signed_word(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def emit_handlers(continue_address: int = CONTINUE_ADDRESS) -> tuple[bytes, tuple[Handler, ...]]:
    """Emit the table and the exact straight-line handler instruction bytes."""
    image = bytearray(TABLE_BYTES)
    handlers: list[Handler] = []

    for mask in range(256):
        start = len(image)
        offset = start
        if offset > 0x7FFF:
            raise AssertionError(f"mask {mask:02X}: table offset {offset} is not signed-word safe")
        struct.pack_into(">H", image, mask * 2, offset)

        for bit in range(8):
            if not (mask & (1 << bit)):
                continue
            append_word(image, OP_MOVE_ENTRY_D3)
            append_word(image, OP_STRIP_COLD_D6_D3)
            if bit == 0:
                append_word(image, OP_STORE_D3_A1)
            else:
                append_word(image, OP_STORE_D3_D16_A1)
                append_word(image, bit * 2)

        append_word(image, OP_ADVANCE_SHADOW)
        append_word(image, SHADOW_BYTE_ADVANCE)

        branch_address = CODEGEN_BASE + len(image)
        displacement = continue_address - (branch_address + 4)
        if not -0x8000 <= displacement <= 0x7FFF:
            raise AssertionError(
                f"mask {mask:02X}: BRA.W displacement {displacement} is out of range"
            )
        append_word(image, OP_BRA_W)
        append_word(image, displacement)
        handlers.append(Handler(mask, start, len(image)))

    if CODEGEN_BASE + len(image) > CODEGEN_LIMIT:
        raise AssertionError(
            f"generated end {CODEGEN_BASE + len(image):#x} exceeds {CODEGEN_LIMIT:#x}"
        )
    return bytes(image), tuple(handlers)


def verify_instruction_bytes(
    image: bytes, handlers: tuple[Handler, ...], continue_address: int
) -> None:
    """Parse every emitted handler and require only the approved instructions."""
    for handler in handlers:
        table_offset = read_word(image, handler.mask * 2)
        if table_offset != handler.start:
            raise AssertionError(
                f"mask {handler.mask:02X}: table offset {table_offset} != {handler.start}"
            )

        pos = handler.start
        for bit in range(8):
            if not (handler.mask & (1 << bit)):
                continue
            if read_word(image, pos) != OP_MOVE_ENTRY_D3:
                raise AssertionError(f"mask {handler.mask:02X}: missing entry read at bit {bit}")
            pos += 2
            if read_word(image, pos) != OP_STRIP_COLD_D6_D3:
                raise AssertionError(f"mask {handler.mask:02X}: missing cold strip at bit {bit}")
            pos += 2
            if bit == 0:
                if read_word(image, pos) != OP_STORE_D3_A1:
                    raise AssertionError(f"mask {handler.mask:02X}: bad bit-0 shadow write")
                pos += 2
            else:
                if (
                    read_word(image, pos) != OP_STORE_D3_D16_A1
                    or read_word(image, pos + 2) != bit * 2
                ):
                    raise AssertionError(
                        f"mask {handler.mask:02X}: bad shadow displacement for bit {bit}"
                    )
                pos += 4

        if (
            read_word(image, pos) != OP_ADVANCE_SHADOW
            or read_word(image, pos + 2) != SHADOW_BYTE_ADVANCE
        ):
            raise AssertionError(f"mask {handler.mask:02X}: missing shadow cursor advance")
        pos += 4
        if read_word(image, pos) != OP_BRA_W:
            raise AssertionError(f"mask {handler.mask:02X}: missing final BRA.W")
        displacement = signed_word(read_word(image, pos + 2))
        branch_target = CODEGEN_BASE + pos + 4 + displacement
        if branch_target != continue_address:
            raise AssertionError(
                f"mask {handler.mask:02X}: branch target {branch_target:#x} "
                f"!= {continue_address:#x}"
            )
        pos += 4
        if pos != handler.end:
            raise AssertionError(
                f"mask {handler.mask:02X}: parser ended at {pos}, handler ends at {handler.end}"
            )


def apply_reference(mask: int, entries: list[int], shadow: list[int], cursor: int) -> tuple[int, int]:
    """Model the current LSR/BCC loop for one bitmap byte."""
    entry_pos = 0
    for bit in range(8):
        if mask & (1 << bit):
            shadow[cursor + bit] = entries[entry_pos] & IMM_ENTRY_MASK
            entry_pos += 1
    return entry_pos, cursor + 8


def apply_generated(mask: int, entries: list[int], shadow: list[int], cursor: int) -> tuple[int, int]:
    """Model the generated straight-line handler for one bitmap byte."""
    entry_pos = 0
    for bit in range(8):
        if mask & (1 << bit):
            shadow[cursor + bit] = entries[entry_pos] & IMM_ENTRY_MASK
            entry_pos += 1
    return entry_pos, cursor + SHADOW_BYTE_ADVANCE // 2


def apply_full_longwords(entries: list[int], shadow: list[int], cursor: int) -> tuple[int, int]:
    """Model bf_cg_ufull's four big-endian masked longword writes."""
    if len(entries) != 8:
        raise AssertionError(f"full path needs 8 entries, got {len(entries)}")
    for pair in range(4):
        packed = (entries[pair * 2] << 16) | entries[pair * 2 + 1]
        packed &= ENTRY_MASK_LONG
        shadow[cursor + pair * 2] = packed >> 16
        shadow[cursor + pair * 2 + 1] = packed & 0xFFFF
    return 8, cursor + 8


def verify_semantics(cases_per_mask: int = 64) -> int:
    rng = random.Random(0xFF2000)
    checked_entries = 0
    for mask in range(256):
        count = mask.bit_count()
        for _case in range(cases_per_mask):
            entries = [rng.randrange(0x10000) for _ in range(count)]
            initial = [rng.randrange(0x8000) for _ in range(24)]
            reference = initial.copy()
            generated = initial.copy()
            ref_entry, ref_cursor = apply_reference(mask, entries, reference, 8)
            gen_entry, gen_cursor = apply_generated(mask, entries, generated, 8)
            if generated != reference:
                mismatch = next(
                    index
                    for index, (expected, actual) in enumerate(zip(reference, generated))
                    if expected != actual
                )
                raise AssertionError(
                    f"mask {mask:02X}: shadow differs at word {mismatch}: "
                    f"{reference[mismatch]:04X} != {generated[mismatch]:04X}"
                )
            if (gen_entry, gen_cursor) != (ref_entry, ref_cursor):
                raise AssertionError(
                    f"mask {mask:02X}: cursor mismatch entries {gen_entry}/{ref_entry}, "
                    f"shadow {gen_cursor}/{ref_cursor}"
                )
            if mask == 0xFF:
                full_shadow = initial.copy()
                full_entry, full_cursor = apply_full_longwords(entries, full_shadow, 8)
                if full_shadow != reference or (full_entry, full_cursor) != (
                    ref_entry,
                    ref_cursor,
                ):
                    raise AssertionError("mask FF: four-longword path differs from reference")
            checked_entries += count
    return checked_entries


def verify_player_constants(path: Path = PLAYER_SOURCE) -> None:
    """Keep the Python byte model tied to the assembly emitter constants."""
    source = path.read_text()
    found = {
        name: int(value, 0)
        for name, value in re.findall(
            r"^\.equ\s+([A-Z0-9_]+),\s*(0x[0-9A-Fa-f]+|[0-9]+)\b",
            source,
            flags=re.MULTILINE,
        )
    }
    for name, expected in PLAYER_CONSTANTS.items():
        actual = found.get(name)
        if actual != expected:
            raise AssertionError(
                f"{path}: {name}={actual!r}, Python contract expects {expected:#x}"
            )


def find_objdump(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    found = shutil.which("m68k-elf-objdump")
    if found:
        return Path(found)
    candidate = Path.home() / "toolchains/mars/m68k-elf/bin/m68k-elf-objdump"
    return candidate if candidate.is_file() else None


def verify_objdump(image: bytes, handlers: tuple[Handler, ...], objdump: Path | None) -> str:
    if objdump is None:
        return "SKIP (m68k-elf-objdump not found)"
    for mask in (0x00, 0x01, 0xFE, 0xFF):
        handler = handlers[mask]
        with tempfile.NamedTemporaryFile(suffix=f"_{mask:02x}.bin") as temp:
            temp.write(image[handler.start : handler.end])
            temp.flush()
            result = subprocess.run(
                [
                    str(objdump),
                    "-b",
                    "binary",
                    "-m",
                    "68000",
                    f"--adjust-vma={CODEGEN_BASE + handler.start:#x}",
                    "-D",
                    temp.name,
                ],
                check=True,
                text=True,
                capture_output=True,
            )
        disassembly = result.stdout
        expected_updates = mask.bit_count()
        if disassembly.count("%a0@+,%d3") != expected_updates:
            raise AssertionError(f"mask {mask:02X}: objdump entry-read count differs")
        if disassembly.count("andw %d6,%d3") != expected_updates:
            raise AssertionError(f"mask {mask:02X}: objdump cold-strip count differs")
        if "lea %a1@(16),%a1" not in disassembly or "braw" not in disassembly:
            raise AssertionError(f"mask {mask:02X}: objdump missing handler tail")
    return f"OK ({objdump})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional generated binary output")
    parser.add_argument("--objdump", type=Path, help="explicit m68k-elf-objdump path")
    args = parser.parse_args()

    image, handlers = emit_handlers()
    verify_player_constants()
    verify_instruction_bytes(image, handlers, CONTINUE_ADDRESS)
    checked_entries = verify_semantics()
    objdump_result = verify_objdump(image, handlers, find_objdump(args.objdump))

    expected_size = 0x2900
    if len(image) != expected_size:
        raise AssertionError(f"generated size {len(image)} != expected {expected_size}")
    if CODEGEN_BASE + len(image) != 0x00FF4900:
        raise AssertionError("unexpected generated end address")
    maximum = max(handler.size for handler in handlers)
    if maximum != 70:
        raise AssertionError(f"maximum handler size {maximum} != 70")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(image)

    print(
        "bitmap handler semantics: OK "
        f"(256 masks, 64 cases/mask, {checked_entries} entry consumptions)"
    )
    print(
        "generated byte contract: OK "
        f"({len(image)} bytes, max handler {maximum} bytes, "
        f"end={CODEGEN_BASE + len(image):#x}, free={CODEGEN_LIMIT - CODEGEN_BASE - len(image)} bytes)"
    )
    print(f"assembly emitter constants: OK ({PLAYER_SOURCE})")
    print(f"68000 objdump: {objdump_result}")
    if args.output:
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
