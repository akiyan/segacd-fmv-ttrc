#!/usr/bin/env python3
"""Independently replay the v10 Prg/Wr0/Wr1/Main pattern supply format.

This verifier deliberately does not import the production packer, scheduler,
or pattern-supply planner.  It walks the real HEADER.DAT and BODY.DAT, consumes
every source in player order, and compares every resulting VRAM tile with the
authenticated decision log.
"""

from __future__ import annotations

import argparse
import pickle
import struct
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path


SECTOR = 2048
PATTERN_BYTES = 32
DEBUG_BYTES = 22
VERSION = 10
FEATURE_COLD_RUNS = 0x0001
FEATURE_FIXED_N2 = 0x0002
FEATURE_ADPCM22 = 0x0004
FEATURE_PATTERN_SUPPLY = 0x0008
SOURCE_PRG = 0
SOURCE_WR = 1
SOURCE_MAIN = 2
SOURCE_MASK = 0x1800
SOURCE_SHIFT = 11
RUN_COUNT_MASK = 0x3FFF
RUN_SOURCE_SHIFT = 14
ENTRY_DISPLAY_MASK = 0x67FF


@dataclass(frozen=True)
class Control:
    seq: int
    bitmap: bytes
    entries: tuple[int, ...]
    runs: tuple[tuple[int, int, int], ...]


def packed_pattern(key: bytes) -> bytes:
    if len(key) != 64:
        raise AssertionError(f"decision pattern has {len(key)} pixels, expected 64")
    out = bytearray()
    for pos in range(0, 64, 2):
        high, low = key[pos], key[pos + 1]
        if high > 15 or low > 15:
            raise AssertionError("decision pattern contains a palette index above 15")
        out.append((high << 4) | low)
    return bytes(out)


def parse_control(raw: bytes, seq: int, cells: int, audio_bytes: int) -> Control:
    if len(raw) < 8:
        raise AssertionError(f"frame {seq}: control is truncated")
    total, packed_seq, n_upd = struct.unpack_from(">HHH", raw)
    if total != len(raw) or total & 1:
        raise AssertionError(f"frame {seq}: invalid total_len {total}/{len(raw)}")
    if packed_seq != seq or n_upd > cells:
        raise AssertionError(
            f"frame {seq}: packed seq/count is {packed_seq}/{n_upd}, cells={cells}")

    bitmap_start = 8 + (DEBUG_BYTES if raw[7] else 0)
    bitmap_bytes = (cells + 7) // 8
    entries_start = bitmap_start + bitmap_bytes
    entries_end = entries_start + n_upd * 2
    audio_end = entries_end + audio_bytes
    suffix_start = (audio_end + 1) & ~1
    if suffix_start + 2 > len(raw):
        raise AssertionError(f"frame {seq}: descriptor suffix is truncated")
    if raw[audio_end:suffix_start] != (b"\0" if audio_end & 1 else b""):
        raise AssertionError(f"frame {seq}: invalid audio alignment byte")

    bitmap = raw[bitmap_start:entries_start]
    if sum(value.bit_count() for value in bitmap) != n_upd:
        raise AssertionError(f"frame {seq}: bitmap population differs from n_upd")
    entries = (
        tuple(struct.unpack_from(f">{n_upd}H", raw, entries_start))
        if n_upd else ()
    )
    n_runs = struct.unpack_from(">H", raw, suffix_start)[0]
    suffix_end = suffix_start + 2 + n_runs * 4
    if suffix_end != len(raw):
        raise AssertionError(
            f"frame {seq}: descriptor suffix ends at {suffix_end}, total={len(raw)}")
    runs = []
    for index in range(n_runs):
        slot, encoded = struct.unpack_from(">HH", raw, suffix_start + 2 + index * 4)
        count = encoded & RUN_COUNT_MASK
        source = encoded >> RUN_SOURCE_SHIFT
        if not count or source > SOURCE_MAIN:
            raise AssertionError(
                f"frame {seq}: invalid run {index}: slot={slot} count={count} source={source}")
        runs.append((slot, count, source))
    return Control(seq, bitmap, entries, tuple(runs))


def expected_runs(entries: tuple[int, ...], base: int) -> tuple[tuple[int, int, int], ...]:
    runs: list[list[int]] = []
    previous_slot = -2
    previous_source = -1
    for entry in entries:
        if not entry & 0x8000:
            if entry & SOURCE_MASK:
                raise AssertionError("reuse entry carries a pattern source")
            continue
        source = (entry & SOURCE_MASK) >> SOURCE_SHIFT
        slot = (entry & 0x07FF) - base
        if runs and source == previous_source and slot == previous_slot + 1:
            runs[-1][1] += 1
        else:
            runs.append([slot, 1, source])
        previous_slot = slot
        previous_source = source
    # Keep this hot full-movie path as an explicit loop. Managed CPython 3.14.4
    # has crashed in executor invalidation after repeatedly specializing the
    # nested generator expression here on a 6,576-frame stream.
    frozen = []
    for slot, count, source in runs:
        frozen.append((slot, count, source))
    return tuple(frozen)


def take_region(
    header: bytes, cursor: int, sectors: int, useful_bytes: int, label: str,
) -> tuple[bytes, int]:
    region = header[cursor:cursor + sectors * SECTOR]
    if len(region) != sectors * SECTOR:
        raise AssertionError(f"{label} is truncated")
    if useful_bytes > len(region):
        raise AssertionError(f"{label} useful bytes exceed its sectors")
    if any(region[useful_bytes:]):
        raise AssertionError(f"{label} sector padding is nonzero")
    return region[:useful_bytes], cursor + len(region)


def body_streams(
    body: bytes, routes: list[tuple[int, int]], fps: int, features: int,
) -> tuple[bytes, bytes]:
    numerator, modulus = ((1001, 400) if features & FEATURE_FIXED_N2 else (75, fps))
    accumulator = 0
    lead = 0
    cursor = 0
    controls = bytearray()
    payload = bytearray()
    for frame, (n_pay, n_ctrl) in enumerate(routes[1:], start=1):
        accumulator += numerator
        rated, accumulator = divmod(accumulator, modulus)
        actual = n_pay + n_ctrl
        sectors = max(actual, rated - lead)
        lead += sectors - rated
        slot = body[cursor:cursor + sectors * SECTOR]
        if len(slot) != sectors * SECTOR:
            raise AssertionError(f"frame {frame}: BODY slot is truncated")
        controls += slot[:n_ctrl * SECTOR]
        payload += slot[n_ctrl * SECTOR:actual * SECTOR]
        if any(slot[actual * SECTOR:]):
            raise AssertionError(f"frame {frame}: rate padding is nonzero")
        cursor += len(slot)
    if cursor != len(body):
        raise AssertionError(f"BODY has {len(body) - cursor} unrouted bytes")
    return bytes(controls), bytes(payload)


def bitmap_cells(bitmap: bytes, cells: int) -> list[int]:
    return [
        cell for cell in range(cells)
        if bitmap[cell >> 3] & (1 << (cell & 7))
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--header", type=Path, required=True)
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    args = parser.parse_args()

    header = args.header.read_bytes()
    body = args.body.read_bytes()
    if len(header) < SECTOR:
        raise SystemExit("HEADER.DAT is shorter than one sector")
    (
        magic, version, frames, cols, rows, cells, pool, base, _frame_sectors,
        nseg,
    ) = struct.unpack_from(">4s9H", header)
    if magic != b"TTRC" or version != VERSION or cols * rows != cells:
        raise SystemExit(
            f"expected TTRC v{VERSION}, got {magic!r} v{version} {cols}x{rows}/{cells}")
    prebuf_patterns, routing_sectors, prebuf_sectors, _ring_peak = struct.unpack_from(
        ">4L", header, 22)
    f0_ctrl_sectors, f0_pattern_sectors, paltab_sectors = struct.unpack_from(
        ">3L", header, 40)
    vsync_n, decoded_audio, fps, _audio_fd, audio_preload, features = struct.unpack_from(
        ">6H", header, 52)
    required_supply_features = (
        FEATURE_COLD_RUNS | FEATURE_FIXED_N2 | FEATURE_PATTERN_SUPPLY)
    if features & required_supply_features != required_supply_features:
        raise SystemExit(
            f"expected v10 cold-run/fixed-N2/pattern-supply features, "
            f"got 0x{features:04X}")
    if vsync_n <= 0 or fps < 24:
        raise SystemExit(f"invalid supply timing N={vsync_n} fps={fps}")
    audio_bytes = 4 + decoded_audio // 2 if features & FEATURE_ADPCM22 else decoded_audio
    signature = struct.unpack_from(">L", header, 192)[0]
    expected_signature = zlib.crc32(header[:64]) & 0xFFFFFFFF
    if signature != expected_signature:
        raise AssertionError(
            f"header signature 0x{signature:08X} != 0x{expected_signature:08X}")

    supply = struct.unpack_from(">4s8H", header, 196)
    magic_supply, supply_version, reserved = supply[:3]
    wr0_count, wr1_count, main_count, wr0_sec, wr1_sec, main_sec = supply[3:]
    if magic_supply != b"PSUP" or supply_version != 1 or reserved:
        raise AssertionError(f"invalid pattern-supply extension: {supply!r}")
    for label, count, sectors, capacity in (
        ("Wr0", wr0_count, wr0_sec, 880),
        ("Wr1", wr1_count, wr1_sec, 880),
        ("Main", main_count, main_sec, 208),
    ):
        if count > capacity or sectors != (count + 63) // 64:
            raise AssertionError(
                f"{label}: count/sectors {count}/{sectors}, capacity={capacity}")

    cursor = SECTOR
    _paltab, cursor = take_region(
        header, cursor, paltab_sectors, nseg * 128, "PALTAB")
    adpcm_sectors = 5 if features & FEATURE_ADPCM22 else 0
    cursor += adpcm_sectors * SECTOR
    wr0, cursor = take_region(header, cursor, wr0_sec, wr0_count * 32, "Wr0")
    wr1, cursor = take_region(header, cursor, wr1_sec, wr1_count * 32, "Wr1")
    main_blob, cursor = take_region(
        header, cursor, main_sec, main_count * 32, "Main")
    cursor += audio_preload * SECTOR

    f0_region = header[cursor:cursor + f0_ctrl_sectors * SECTOR]
    if len(f0_region) != f0_ctrl_sectors * SECTOR:
        raise AssertionError("frame 0 control region is truncated")
    f0_len = struct.unpack_from(">H", f0_region)[0]
    f0_control = parse_control(f0_region[:f0_len], 0, cells, audio_bytes)
    if any(f0_region[f0_len:]):
        raise AssertionError("frame 0 control sector padding is nonzero")
    cursor += len(f0_region)
    f0_cold = sum(bool(entry & 0x8000) for entry in f0_control.entries)
    f0_patterns, cursor = take_region(
        header, cursor, f0_pattern_sectors, f0_cold * 32, "frame 0 patterns")

    routing_region = header[cursor:cursor + routing_sectors * SECTOR]
    if len(routing_region) != routing_sectors * SECTOR:
        raise AssertionError("routing is truncated")
    if routing_region[0] or any(routing_region[frames:]):
        raise AssertionError("routing frame 0 or sector padding is nonzero")
    routes = []
    for frame, encoded in enumerate(routing_region[:frames]):
        if encoded & 0xC0:
            raise AssertionError(f"frame {frame}: routing reserved bits set")
        n_ctrl = encoded & 7
        total = encoded >> 3
        if n_ctrl > total or total > 5:
            raise AssertionError(f"frame {frame}: invalid route 0x{encoded:02X}")
        routes.append((total - n_ctrl, n_ctrl))
    cursor += len(routing_region)
    prebuffer, cursor = take_region(
        header, cursor, prebuf_sectors, prebuf_patterns * 32, "Prg prebuffer")
    if cursor != len(header):
        raise AssertionError(f"HEADER has {len(header) - cursor} unparsed bytes")

    control_stream, body_payload = body_streams(body, routes, fps, features)
    controls = [f0_control]
    control_cursor = 0
    for frame in range(1, frames):
        if control_cursor + 2 > len(control_stream):
            raise AssertionError(f"frame {frame}: missing control length")
        length = struct.unpack_from(">H", control_stream, control_cursor)[0]
        raw = control_stream[control_cursor:control_cursor + length]
        if len(raw) != length:
            raise AssertionError(f"frame {frame}: control is truncated")
        controls.append(parse_control(raw, frame, cells, audio_bytes))
        control_cursor += length
    if any(control_stream[control_cursor:]):
        raise AssertionError("nonzero bytes follow the final control")

    with args.decisions.open("rb") as source:
        decisions = pickle.load(source)
    decision_frames = decisions["frames"]
    if len(decision_frames) != frames:
        raise AssertionError(
            f"decision log has {len(decision_frames)} frames, stream has {frames}")

    prg_count = sum(
        1 for frame, control in enumerate(controls) for entry in control.entries
        if frame and entry & 0x8000 and ((entry & SOURCE_MASK) >> SOURCE_SHIFT) == SOURCE_PRG
    )
    streamed_prg = prebuffer + body_payload
    useful_prg_bytes = prg_count * 32
    if len(streamed_prg) < useful_prg_bytes or any(streamed_prg[useful_prg_bytes:]):
        raise AssertionError("Prg payload length/padding does not match source-coded entries")

    sources = {
        "F0": deque(
            f0_patterns[pos:pos + 32] for pos in range(0, len(f0_patterns), 32)),
        "Prg": deque(
            streamed_prg[pos:pos + 32] for pos in range(0, useful_prg_bytes, 32)),
        "Wr0": deque(wr0[pos:pos + 32] for pos in range(0, len(wr0), 32)),
        "Wr1": deque(wr1[pos:pos + 32] for pos in range(0, len(wr1), 32)),
        "Main": deque(
            main_blob[pos:pos + 32] for pos in range(0, len(main_blob), 32)),
    }
    consumed = {name: 0 for name in sources}
    vram: dict[int, bytes] = {}
    total_updates = total_cold = 0

    for frame, (decision_frame, control) in enumerate(
            zip(decision_frames, controls, strict=True)):
        ordered = sorted(decision_frame, key=lambda item: int(item[0]))
        cells_expected = [int(item[0]) for item in ordered]
        if bitmap_cells(control.bitmap, cells) != cells_expected:
            raise AssertionError(f"frame {frame}: bitmap cells differ from decisions")
        if len(ordered) != len(control.entries):
            raise AssertionError(f"frame {frame}: decision/update count differs")
        if control.runs != expected_runs(control.entries, base):
            raise AssertionError(f"frame {frame}: source-coded cold runs differ from entries")

        for item, entry in zip(ordered, control.entries, strict=True):
            expected = packed_pattern(bytes(item[2]))
            palette = int(item[1])
            if ((entry & ENTRY_DISPLAY_MASK) >> 13) & 3 != palette:
                raise AssertionError(f"frame {frame}: palette entry differs from decisions")
            slot = (entry & 0x07FF) - base
            if not 0 <= slot < pool:
                raise AssertionError(f"frame {frame}: VRAM slot {slot} is outside the pool")
            if entry & 0x8000:
                source_id = (entry & SOURCE_MASK) >> SOURCE_SHIFT
                source_name = (
                    "F0" if frame == 0 else
                    "Prg" if source_id == SOURCE_PRG else
                    ("Wr1" if frame & 1 else "Wr0") if source_id == SOURCE_WR else
                    "Main" if source_id == SOURCE_MAIN else "reserved"
                )
                if source_name not in sources or not sources[source_name]:
                    raise AssertionError(
                        f"frame {frame}: {source_name} is empty before slot {slot}")
                actual = sources[source_name].popleft()
                consumed[source_name] += 1
                if actual != expected:
                    raise AssertionError(
                        f"frame {frame}: {source_name} pattern differs at slot {slot}")
                vram[slot] = actual
                total_cold += 1
            if vram.get(slot) != expected:
                raise AssertionError(
                    f"frame {frame}: resident/reused pattern differs at slot {slot}")
            total_updates += 1

    leftovers = {name: len(queue) for name, queue in sources.items() if queue}
    if leftovers:
        raise AssertionError(f"unconsumed pattern supplies: {leftovers}")
    print(
        "pattern supply replay: OK "
        f"({frames} frames, {total_updates} updates, {total_cold} cold; "
        f"F0={consumed['F0']} Prg={consumed['Prg']} Wr0={consumed['Wr0']} "
        f"Wr1={consumed['Wr1']} Main={consumed['Main']})")
    print("VRAM resident/reuse equivalence: OK (every updated cell, every frame)")


if __name__ == "__main__":
    main()
