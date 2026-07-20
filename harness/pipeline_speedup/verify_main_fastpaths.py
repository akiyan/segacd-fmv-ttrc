#!/usr/bin/env python3
"""Prove the Main-CPU bitmap and name-table fast paths are lossless.

The bitmap proof replays every packed control block and compares the
current per-bit indexed shadow update with a pointer walk that has dedicated
0x00 (skip eight cells) and 0xFF (write eight entries) paths.  It also checks
that the packed bitmap/palette order still matches the encoder decisions.

The name-table proof models the 68000's big-endian longword write as two VDP
data-port word writes, high word first.  Four longword writes therefore copy
one eight-word group, followed by a scalar tail when needed. Every real H32
shadow row and deterministic synthetic width from 1 through 40 must match the
scalar word loop exactly.
"""

from __future__ import annotations

import argparse
import pickle
import random
import struct
from dataclasses import dataclass
from pathlib import Path


SECTOR = 2048
ROUTING_TOTAL_MAX = 5
FEATURE_FIXED_N2 = 0x0002
FEATURE_ADPCM22 = 0x0004
FEATURE_PATTERN_SUPPLY = 0x0008
ADPCM_TABLE_SECTORS = 5
PATTERN_SUPPLY_OFFSET = 196
NAME_ENTRY_MASK = 0x67FF
DEFAULT_DECISIONS = Path(
    "videos/sonic_H32_256x224_pcm13_geometry_pad_4by3/decisions.pkl"
)


@dataclass(frozen=True)
class ControlBlock:
    seq: int
    bitmap: bytes
    entries: tuple[int, ...]


@dataclass(frozen=True)
class Stream:
    cols: int
    rows: int
    cells: int
    controls: tuple[ControlBlock, ...]


def frame_sectors(
    routes: list[tuple[int, int]], version: int, fps: int, vsync_n: int,
    features: int,
) -> list[int]:
    """Reproduce the packer's versioned bounded accumulator for BODY slots."""
    if version >= 8 and features & FEATURE_FIXED_N2:
        rate_numerator, rate_modulus = 1001, 400
    else:
        rate_numerator, rate_modulus = 75, fps
    accumulator = 0
    lead = 0
    out = [0]
    for n_pay, n_ctrl in routes[1:]:
        accumulator += rate_numerator
        rated, accumulator = divmod(accumulator, rate_modulus)
        actual = n_pay + n_ctrl
        sectors = max(actual, rated - lead)
        lead += sectors - rated
        out.append(sectors)
    return out


def decode_routes(
    routing: bytes, nframes: int, version: int
) -> list[tuple[int, int]]:
    """Decode routing without depending on the production packer."""
    compact = version >= 7
    entry_bytes = 1 if compact else 2
    required = nframes * entry_bytes
    if len(routing) < required:
        raise AssertionError("routing table is truncated")
    if not compact:
        return [
            (routing[frame * 2], routing[frame * 2 + 1])
            for frame in range(nframes)
        ]

    expected_bytes = ((nframes + SECTOR - 1) // SECTOR) * SECTOR
    if len(routing) != expected_bytes:
        raise AssertionError(
            f"v7+ routing region is {len(routing)} bytes, expected {expected_bytes}"
        )
    if not nframes or routing[0] != 0:
        raise AssertionError("v7+ frame 0 routing entry must be zero")
    if any(routing[nframes:]):
        raise AssertionError("v7+ routing sector padding must be zero")

    routes = []
    for frame, packed in enumerate(routing[:nframes]):
        if packed & 0xC0:
            raise AssertionError(
                f"frame {frame}: routing reserved bits are set in 0x{packed:02X}"
            )
        n_ctrl = packed & 0x07
        total = (packed >> 3) & 0x07
        if total > ROUTING_TOTAL_MAX:
            raise AssertionError(
                f"frame {frame}: routing total {total} exceeds "
                f"{ROUTING_TOTAL_MAX} sectors"
            )
        if n_ctrl > total:
            raise AssertionError(
                f"frame {frame}: routing control {n_ctrl} exceeds total {total}"
            )
        routes.append((total - n_ctrl, n_ctrl))
    return routes


def pattern_supply_sectors(header: bytes, version: int, features: int) -> int:
    """Return the validated v10 boot-preload sector total."""
    if version < 10 or not features & FEATURE_PATTERN_SUPPLY:
        return 0
    values = struct.unpack_from(">4s8H", header, PATTERN_SUPPLY_OFFSET)
    magic, supply_version, reserved = values[:3]
    if magic != b"PSUP" or supply_version != 1 or reserved:
        raise AssertionError(f"invalid pattern-supply extension: {values!r}")
    return sum(values[-3:])


def parse_control(raw: bytes, seq: int, cells: int) -> ControlBlock:
    if len(raw) < 8:
        raise AssertionError(f"frame {seq}: control block is shorter than 8 bytes")
    total_len, packed_seq, n_upd = struct.unpack_from(">HHH", raw)
    if total_len != len(raw):
        raise AssertionError(f"frame {seq}: total_len {total_len} != {len(raw)}")
    if packed_seq != seq:
        raise AssertionError(f"frame {seq}: packed sequence is {packed_seq}")
    if n_upd > cells:
        raise AssertionError(f"frame {seq}: {n_upd} updates exceed {cells} cells")

    bitmap_start = 8 + (22 if raw[7] else 0)
    bitmap_len = (cells + 7) // 8
    entries_start = bitmap_start + bitmap_len
    entries_end = entries_start + n_upd * 2
    if entries_end > len(raw):
        raise AssertionError(f"frame {seq}: entries extend beyond the control block")
    bitmap = raw[bitmap_start:entries_start]
    entries = (
        struct.unpack_from(f">{n_upd}H", raw, entries_start) if n_upd else ()
    )
    if sum(byte.bit_count() for byte in bitmap) != n_upd:
        raise AssertionError(f"frame {seq}: bitmap population differs from n_upd")
    for cell in range(cells, bitmap_len * 8):
        if bitmap[cell >> 3] & (1 << (cell & 7)):
            raise AssertionError(f"frame {seq}: padding bitmap bit {cell} is set")
    return ControlBlock(seq, bitmap, tuple(entries))


def read_stream(header_path: Path, body_path: Path) -> Stream:
    header = header_path.read_bytes()
    magic, version, nfr, cols, rows, cells, _pool = struct.unpack_from(
        ">4sHHHHHH", header
    )
    if magic != b"TTRC" or version not in (6, 7, 8, 9, 10):
        raise AssertionError(f"expected split TTRC v6-v10, got {magic!r} v{version}")
    if cols * rows != cells:
        raise AssertionError(f"grid {cols}x{rows} does not equal {cells} cells")

    routing_sec = struct.unpack_from(">L", header, 26)[0]
    prebuf_sec = struct.unpack_from(">L", header, 30)[0]
    f0_ctrl_sec, f0_pat_sec, paltab_sec = struct.unpack_from(">LLL", header, 40)
    vsync_n = struct.unpack_from(">H", header, 52)[0]
    fps = struct.unpack_from(">H", header, 56)[0] or 15
    audio_preload_sec = struct.unpack_from(">H", header, 60)[0]
    features = struct.unpack_from(">H", header, 62)[0]
    table_sec = ADPCM_TABLE_SECTORS if features & FEATURE_ADPCM22 else 0
    supply_sec = pattern_supply_sectors(header, version, features)

    frame0_offset = (
        1 + paltab_sec + table_sec + supply_sec + audio_preload_sec
    ) * SECTOR
    frame0_len = struct.unpack_from(">H", header, frame0_offset)[0]
    controls = [
        parse_control(
            header[frame0_offset : frame0_offset + frame0_len], 0, cells
        )
    ]

    routing_offset = (
        1 + paltab_sec + table_sec + supply_sec + audio_preload_sec
        + f0_ctrl_sec + f0_pat_sec
    ) * SECTOR
    routing_raw = header[
        routing_offset : routing_offset + routing_sec * SECTOR
    ]
    routes = decode_routes(routing_raw, nfr, version)
    if routes[0] != (0, 0):
        raise AssertionError(f"frame 0 route must be (0, 0), got {routes[0]}")
    expected_header_len = (routing_offset // SECTOR + routing_sec + prebuf_sec) * SECTOR
    if len(header) != expected_header_len:
        raise AssertionError(
            f"HEADER.DAT is {len(header)} bytes, expected {expected_header_len}"
        )

    body = body_path.read_bytes()
    slots = frame_sectors(routes, version, fps, vsync_n, features)
    body_pos = 0
    control_stream = bytearray()
    for seq in range(1, nfr):
        slot_len = slots[seq] * SECTOR
        slot = body[body_pos : body_pos + slot_len]
        if len(slot) != slot_len:
            raise AssertionError(f"frame {seq}: BODY.DAT slot is truncated")
        n_ctrl = routes[seq][1]
        control_stream += slot[: n_ctrl * SECTOR]
        body_pos += slot_len
    if body_pos != len(body):
        raise AssertionError(
            f"BODY.DAT has {len(body) - body_pos} unrouted trailing bytes"
        )

    control_pos = 0
    for seq in range(1, nfr):
        if control_pos + 2 > len(control_stream):
            raise AssertionError(f"frame {seq}: missing control block length")
        block_len = struct.unpack_from(">H", control_stream, control_pos)[0]
        if block_len < 8 or block_len & 1:
            raise AssertionError(f"frame {seq}: invalid control length {block_len}")
        end = control_pos + block_len
        if end > len(control_stream):
            raise AssertionError(f"frame {seq}: control block is truncated")
        controls.append(parse_control(bytes(control_stream[control_pos:end]), seq, cells))
        control_pos = end

    return Stream(cols, rows, cells, tuple(controls))


def bitmap_cells(bitmap: bytes, cells: int) -> list[int]:
    return [
        cell
        for cell in range(cells)
        if bitmap[cell >> 3] & (1 << (cell & 7))
    ]


def update_reference(shadow: list[int], block: ControlBlock, cells: int) -> None:
    """Current assembly model: per bit, indexed cell*2 shadow write."""
    entry_pos = 0
    for cell in range(cells):
        if block.bitmap[cell >> 3] & (1 << (cell & 7)):
            shadow[cell] = block.entries[entry_pos] & NAME_ENTRY_MASK
            entry_pos += 1
    if entry_pos != len(block.entries):
        raise AssertionError(f"frame {block.seq}: reference did not consume all entries")


def update_fast(shadow: list[int], block: ControlBlock, cells: int) -> tuple[int, int, int]:
    """Proposed pointer walk with zero/full/mixed bitmap-byte paths."""
    shadow_pos = 0
    entry_pos = 0
    zero_bytes = 0
    full_bytes = 0
    mixed_bytes = 0
    for bitmap_byte in block.bitmap:
        remaining = min(8, cells - shadow_pos)
        if bitmap_byte == 0:
            zero_bytes += 1
            shadow_pos += remaining
            continue
        if bitmap_byte == 0xFF and remaining == 8:
            full_bytes += 1
            values = block.entries[entry_pos : entry_pos + 8]
            if len(values) != 8:
                raise AssertionError(f"frame {block.seq}: full path runs out of entries")
            shadow[shadow_pos : shadow_pos + 8] = [
                value & NAME_ENTRY_MASK for value in values]
            shadow_pos += 8
            entry_pos += 8
            continue

        mixed_bytes += 1
        bits = bitmap_byte
        for _ in range(remaining):
            if bits & 1:
                if entry_pos >= len(block.entries):
                    raise AssertionError(f"frame {block.seq}: mixed path runs out of entries")
                shadow[shadow_pos] = block.entries[entry_pos] & NAME_ENTRY_MASK
                entry_pos += 1
            bits >>= 1
            shadow_pos += 1

    if shadow_pos != cells or entry_pos != len(block.entries):
        raise AssertionError(
            f"frame {block.seq}: fast path ended at cell {shadow_pos}/{cells}, "
            f"entry {entry_pos}/{len(block.entries)}"
        )
    return zero_bytes, full_bytes, mixed_bytes


def scalar_nt_copy(words: list[int]) -> list[int]:
    return list(words)


def grouped_nt_copy(words: list[int]) -> list[int]:
    """Model four MOVE.L writes per group plus the assembly's word tail."""
    grouped = len(words) & ~7
    memory = struct.pack(f">{len(words)}H", *words) if words else b""
    vdp_words: list[int] = []
    for group_offset in range(0, grouped * 2, 16):
        longs = struct.unpack_from(">4L", memory, group_offset)
        for value in longs:
            vdp_words.extend((value >> 16, value & 0xFFFF))
    vdp_words.extend(words[grouped:])
    return vdp_words


def verify_decisions(stream: Stream, decisions_path: Path) -> None:
    with decisions_path.open("rb") as handle:
        decisions = pickle.load(handle)
    frames = decisions["frames"]
    if len(frames) != len(stream.controls):
        raise AssertionError(
            f"decision log has {len(frames)} frames; stream has {len(stream.controls)}"
        )
    for seq, (decision_frame, block) in enumerate(zip(frames, stream.controls)):
        ordered = sorted(decision_frame, key=lambda item: int(item[0]))
        expected_cells = [int(item[0]) for item in ordered]
        actual_cells = bitmap_cells(block.bitmap, stream.cells)
        if actual_cells != expected_cells:
            raise AssertionError(f"frame {seq}: packed bitmap differs from decisions.pkl")
        expected_pals = [int(item[1]) for item in ordered]
        actual_pals = [(entry >> 13) & 3 for entry in block.entries]
        if actual_pals != expected_pals:
            raise AssertionError(f"frame {seq}: packed entry palettes differ from decisions.pkl")


def verify_name_table_rows(stream: Stream, shadows: list[list[int]]) -> tuple[int, int]:
    h32_rows = 0
    if stream.cols == 32:
        for shadow in shadows:
            for row in range(stream.rows):
                words = shadow[row * 32 : (row + 1) * 32]
                if grouped_nt_copy(words) != scalar_nt_copy(words):
                    raise AssertionError("real H32 row grouped copy changed word order")
                h32_rows += 1

    rng = random.Random(0x68000)
    synthetic_rows = 4096
    for width in range(1, 41):
        for case in range(synthetic_rows):
            words = [
                (rng.randrange(0x10000) ^ (case << 8) ^ index) & 0xFFFF
                for index in range(width)
            ]
            if grouped_nt_copy(words) != scalar_nt_copy(words):
                raise AssertionError(f"synthetic width {width} row changed word order")
    return h32_rows, synthetic_rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify Main bitmap and eight-word name-table fast paths."
    )
    parser.add_argument(
        "--header", type=Path, default=Path("out/movieplay/HEADER.DAT")
    )
    parser.add_argument(
        "--body", type=Path, default=Path("out/movieplay/BODY.DAT")
    )
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    args = parser.parse_args()

    stream = read_stream(args.header, args.body)
    verify_decisions(stream, args.decisions)

    reference_shadow = [0x5A5A] * stream.cells
    fast_shadow = reference_shadow.copy()
    shadows: list[list[int]] = []
    path_counts = [0, 0, 0]
    entries = 0
    for block in stream.controls:
        update_reference(reference_shadow, block, stream.cells)
        counts = update_fast(fast_shadow, block, stream.cells)
        path_counts = [left + right for left, right in zip(path_counts, counts)]
        entries += len(block.entries)
        if fast_shadow != reference_shadow:
            mismatch = next(
                i
                for i, (expected, actual) in enumerate(
                    zip(reference_shadow, fast_shadow)
                )
                if expected != actual
            )
            raise AssertionError(
                f"frame {block.seq}: fast shadow differs at cell {mismatch}"
            )
        shadows.append(reference_shadow.copy())
    if not all(path_counts):
        raise AssertionError(f"real stream did not exercise all three paths: {path_counts}")

    real_h32_rows, synthetic_rows = verify_name_table_rows(stream, shadows)
    print(
        "main fast-path equivalence: OK "
        f"({len(stream.controls)} frames, {entries} entries, "
        f"bitmap bytes zero/full/mixed={path_counts[0]}/{path_counts[1]}/{path_counts[2]})"
    )
    print(
        "name-table eight-word grouping: OK "
        f"({real_h32_rows} real H32 rows, {synthetic_rows} deterministic rows each "
        "for widths 1..40; scalar tails 1..7 covered)"
    )


if __name__ == "__main__":
    main()
