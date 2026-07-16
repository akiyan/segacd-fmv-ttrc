#!/usr/bin/env python3
"""Prove the issue #27 boot-time NT0/NT1 straight-line blitters."""

from __future__ import annotations

import argparse
import random
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


CODEGEN_START = 0x00FF4900
CODEGEN_LIMIT = 0x00FF8000
SHADOW_ADDRESS = 0x00FF1000
NT0 = 0x0000C000
NT1 = 0x0000E000
VDP_DATA = 0x00C00000
VDP_CTRL = 0x00C00004

OP_LEA_SHADOW_A1 = 0x43F9       # lea shadow.l,a1
OP_MOVE_L_IMM_ABS = 0x23FC      # move.l #command,(VDP_CTRL).l
OP_MOVE_L_A1_ABS = 0x23D9       # move.l (a1)+,(VDP_DATA).l
OP_MOVE_W_A1_ABS = 0x33D9       # move.w (a1)+,(VDP_DATA).l
OP_RTS = 0x4E75


@dataclass(frozen=True)
class Blitter:
    nt_base: int
    start: int
    end: int
    tcols: int
    trows: int
    row0: int
    col0: int

    @property
    def size(self) -> int:
        return self.end - self.start


def append_word(image: bytearray, value: int) -> None:
    image += struct.pack(">H", value & 0xFFFF)


def append_long(image: bytearray, value: int) -> None:
    image += struct.pack(">I", value & 0xFFFFFFFF)


def read_word(image: bytes, offset: int) -> int:
    return struct.unpack_from(">H", image, offset)[0]


def read_long(image: bytes, offset: int) -> int:
    return struct.unpack_from(">I", image, offset)[0]


def vdp_write_command(address: int) -> int:
    """Match set_vram_write for a 16-bit VRAM byte address."""
    if not 0 <= address <= 0xFFFF:
        raise ValueError(f"VRAM address out of range: {address:#x}")
    return 0x40000000 | ((address & 0x3FFF) << 16) | ((address >> 14) & 3)


def validate_geometry(screen_cols: int, tcols: int, trows: int, row0: int, col0: int) -> None:
    if screen_cols not in (32, 40):
        raise ValueError(f"unsupported screen width: {screen_cols}")
    if not 1 <= tcols <= screen_cols or not 1 <= trows <= 28:
        raise ValueError("tile dimensions are outside the H32/H40 aperture")
    if row0 < 0 or row0 + trows > 28:
        raise ValueError("tile rows do not fit the aperture")
    if col0 < 0 or col0 + tcols > screen_cols:
        raise ValueError("tile columns do not fit the aperture")


def expected_blitter_size(tcols: int, trows: int) -> int:
    # LEA + RTS, plus one immediate VDP command and ceil(tcols/2) data writes per row.
    return 8 + trows * (10 + 6 * ((tcols + 1) // 2))


def emit_one(
    image: bytearray,
    nt_base: int,
    tcols: int,
    trows: int,
    row0: int,
    col0: int,
) -> Blitter:
    start = len(image)
    append_word(image, OP_LEA_SHADOW_A1)
    append_long(image, SHADOW_ADDRESS)

    for row in range(trows):
        address = nt_base + (row0 + row) * 128 + col0 * 2
        append_word(image, OP_MOVE_L_IMM_ABS)
        append_long(image, vdp_write_command(address))
        append_long(image, VDP_CTRL)
        for _pair in range(tcols // 2):
            append_word(image, OP_MOVE_L_A1_ABS)
            append_long(image, VDP_DATA)
        if tcols & 1:
            append_word(image, OP_MOVE_W_A1_ABS)
            append_long(image, VDP_DATA)

    append_word(image, OP_RTS)
    blitter = Blitter(nt_base, start, len(image), tcols, trows, row0, col0)
    expected = expected_blitter_size(tcols, trows)
    if blitter.size != expected:
        raise AssertionError(f"blitter size {blitter.size} != expected {expected}")
    return blitter


def emit_pair(
    screen_cols: int, tcols: int, trows: int, row0: int, col0: int
) -> tuple[bytes, tuple[Blitter, Blitter]]:
    validate_geometry(screen_cols, tcols, trows, row0, col0)
    image = bytearray()
    blitters = (
        emit_one(image, NT0, tcols, trows, row0, col0),
        emit_one(image, NT1, tcols, trows, row0, col0),
    )
    if CODEGEN_START + len(image) > CODEGEN_LIMIT:
        raise AssertionError(
            f"generated end {CODEGEN_START + len(image):#x} exceeds {CODEGEN_LIMIT:#x}"
        )
    return bytes(image), blitters


def parse_one(image: bytes, blitter: Blitter) -> list[tuple[int, int]]:
    """Return each generated (VRAM address, write width in words)."""
    pos = blitter.start
    if read_word(image, pos) != OP_LEA_SHADOW_A1 or read_long(image, pos + 2) != SHADOW_ADDRESS:
        raise AssertionError("missing shadow LEA")
    pos += 6
    writes: list[tuple[int, int]] = []
    for row in range(blitter.trows):
        expected_address = blitter.nt_base + (blitter.row0 + row) * 128 + blitter.col0 * 2
        if (
            read_word(image, pos) != OP_MOVE_L_IMM_ABS
            or read_long(image, pos + 2) != vdp_write_command(expected_address)
            or read_long(image, pos + 6) != VDP_CTRL
        ):
            raise AssertionError(f"row {row}: bad precomputed VDP command")
        pos += 10
        address = expected_address
        for _pair in range(blitter.tcols // 2):
            if read_word(image, pos) != OP_MOVE_L_A1_ABS or read_long(image, pos + 2) != VDP_DATA:
                raise AssertionError(f"row {row}: bad longword write")
            writes.append((address, 2))
            address += 4
            pos += 6
        if blitter.tcols & 1:
            if read_word(image, pos) != OP_MOVE_W_A1_ABS or read_long(image, pos + 2) != VDP_DATA:
                raise AssertionError(f"row {row}: bad tail word write")
            writes.append((address, 1))
            pos += 6
    if read_word(image, pos) != OP_RTS:
        raise AssertionError("missing RTS")
    pos += 2
    if pos != blitter.end:
        raise AssertionError(f"parser ended at {pos}, blitter ends at {blitter.end}")
    return writes


def verify_semantics(image: bytes, blitters: tuple[Blitter, Blitter]) -> int:
    rng = random.Random(0xFF4900)
    checked = 0
    for blitter in blitters:
        generated_writes = parse_one(image, blitter)
        shadow = [rng.randrange(0x10000) for _ in range(blitter.tcols * blitter.trows)]
        generated_vram: dict[int, int] = {}
        cursor = 0
        for address, words in generated_writes:
            for word in range(words):
                generated_vram[address + word * 2] = shadow[cursor]
                cursor += 1

        reference_vram = {
            blitter.nt_base + (blitter.row0 + row) * 128 + (blitter.col0 + col) * 2:
                shadow[row * blitter.tcols + col]
            for row in range(blitter.trows)
            for col in range(blitter.tcols)
        }
        if generated_vram != reference_vram or cursor != len(shadow):
            raise AssertionError(f"NT{1 if blitter.nt_base == NT1 else 0}: blit differs")
        checked += cursor
    return checked


def find_objdump(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    found = shutil.which("m68k-elf-objdump")
    if found:
        return Path(found)
    candidate = Path.home() / "toolchains/mars/m68k-elf/bin/m68k-elf-objdump"
    return candidate if candidate.is_file() else None


def verify_objdump(image: bytes, blitters: tuple[Blitter, Blitter], objdump: Path | None) -> str:
    if objdump is None:
        return "SKIP (m68k-elf-objdump not found)"
    for index, blitter in enumerate(blitters):
        with tempfile.NamedTemporaryFile(suffix=f"_nt{index}.bin") as temp:
            temp.write(image[blitter.start:blitter.end])
            temp.flush()
            result = subprocess.run(
                [
                    str(objdump), "-b", "binary", "-m", "68000",
                    f"--adjust-vma={CODEGEN_START + blitter.start:#x}", "-D", temp.name,
                ],
                check=True,
                text=True,
                capture_output=True,
            )
        disassembly = result.stdout
        expected_longs = blitter.trows * (blitter.tcols // 2)
        expected_words = blitter.trows * (blitter.tcols & 1)
        if disassembly.count("%a1@+,0xc00000") != expected_longs + expected_words:
            raise AssertionError(f"NT{index}: objdump data-write count differs")
        if disassembly.count("0xc00004") != blitter.trows or "rts" not in disassembly:
            raise AssertionError(f"NT{index}: objdump command/return count differs")
    return f"OK ({objdump})"


def verify_invalid_geometry() -> None:
    for case in ((31, 1, 1, 0, 0), (32, 0, 1, 0, 0), (40, 40, 29, 0, 0), (32, 2, 2, 27, 0)):
        try:
            emit_pair(*case)
        except ValueError:
            continue
        raise AssertionError(f"invalid geometry accepted: {case}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional H40 pair binary output")
    parser.add_argument("--objdump", type=Path, help="explicit m68k-elf-objdump path")
    args = parser.parse_args()

    cases = ((32, 32, 28, 0, 0), (40, 40, 28, 0, 0), (40, 17, 13, 7, 1), (32, 1, 1, 27, 31))
    objdump = find_objdump(args.objdump)
    for case in cases:
        image, blitters = emit_pair(*case)
        checked = verify_semantics(image, blitters)
        objdump_result = verify_objdump(image, blitters, objdump)
        print(
            f"geometry {case[1]}x{case[2]} at {case[4]},{case[3]}: OK "
            f"({len(image)} bytes, {checked} words, end={CODEGEN_START + len(image):#x}, "
            f"objdump={objdump_result})"
        )
    verify_invalid_geometry()

    h40_image, _ = emit_pair(40, 40, 28, 0, 0)
    if len(h40_image) != 7296 or CODEGEN_START + len(h40_image) != 0x00FF6580:
        raise AssertionError("unexpected maximum H40 generated size")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(h40_image)
        print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
