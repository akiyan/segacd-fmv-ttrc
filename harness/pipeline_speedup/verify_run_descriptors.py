#!/usr/bin/env python3
"""Prove packed cold-run descriptors preserve the legacy Sub-CPU output.

The feature-bit-0 control suffix is appended after the existing audio and an
absolute-address alignment pad:

    n_runs:u16, repeated (slot_start:u16, count:u16)

This checker does not import the packer.  It independently reads the real split
TTRC v6 files, reconstructs every current control and payload byte, and compares
two consumers:

* the legacy/fallback Sub path scans all update entries, selects cold entries, builds
  consecutive slot runs and consumes one 32-byte payload per cold entry;
* the p39 path decodes the packed run suffix and consumes the same payload
  directly by ``slot_start + index``.

The packed bitmap/cell/palette order and every cold pattern are also checked
against decisions.pkl, so equality is not merely two views of the same entry
list.  No production encoder or player code is imported by this proof.
"""

from __future__ import annotations

import argparse
import pickle
import struct
from dataclasses import dataclass
from pathlib import Path


SECTOR = 2048
PATTERN_BYTES = 32
DEBUG_BYTES = 22
FEATURE_COLD_RUNS = 0x0001
DEFAULT_DECISIONS = Path(
    "videos/sonic_H32_256x224_pcm13_geometry_pad_4by3/decisions.pkl"
)


@dataclass(frozen=True)
class Control:
    seq: int
    raw: bytes
    bitmap: bytes
    entries: tuple[int, ...]
    audio_end: int
    pad: bytes
    descriptor_suffix: bytes | None


@dataclass(frozen=True)
class Stream:
    frames: int
    cells: int
    pool: int
    base: int
    audio_bytes: int
    features: int
    f0_ctrl_sectors: int
    controls: tuple[Control, ...]
    payload: bytes


def ceil_sectors(byte_count: int) -> int:
    return (byte_count + SECTOR - 1) // SECTOR


def frame_sectors(routes: list[tuple[int, int]], fps: int) -> list[int]:
    """Reproduce the v6 bounded CD-1x BODY slot accumulator."""
    accumulator = 0
    lead = 0
    out = [0]
    for n_pay, n_ctrl in routes[1:]:
        accumulator += 75
        rated, accumulator = divmod(accumulator, fps)
        actual = n_pay + n_ctrl
        sectors = max(actual, rated - lead)
        lead += sectors - rated
        out.append(sectors)
    return out


def parse_control(
    raw: bytes, seq: int, cells: int, audio_bytes: int, features: int
) -> Control:
    if len(raw) < 8:
        raise AssertionError(f"frame {seq}: control is shorter than 8 bytes")
    total_len, packed_seq, n_upd = struct.unpack_from(">HHH", raw)
    if total_len != len(raw):
        raise AssertionError(f"frame {seq}: total_len {total_len} != {len(raw)}")
    if packed_seq != seq:
        raise AssertionError(f"frame {seq}: packed sequence is {packed_seq}")
    if n_upd > cells:
        raise AssertionError(f"frame {seq}: n_upd {n_upd} exceeds {cells}")

    bitmap_start = 8 + (DEBUG_BYTES if raw[7] else 0)
    bitmap_bytes = (cells + 7) // 8
    entries_start = bitmap_start + bitmap_bytes
    entries_end = entries_start + 2 * n_upd
    audio_end = entries_end + audio_bytes
    if audio_end > len(raw):
        raise AssertionError(f"frame {seq}: audio extends beyond the control")
    bitmap = raw[bitmap_start:entries_start]
    if sum(value.bit_count() for value in bitmap) != n_upd:
        raise AssertionError(f"frame {seq}: bitmap population differs from n_upd")
    for cell in range(cells, bitmap_bytes * 8):
        if bitmap[cell >> 3] & (1 << (cell & 7)):
            raise AssertionError(f"frame {seq}: bitmap padding bit {cell} is set")
    entries = (
        tuple(struct.unpack_from(f">{n_upd}H", raw, entries_start))
        if n_upd
        else ()
    )
    if features & FEATURE_COLD_RUNS:
        suffix_start = (audio_end + 1) & ~1
        pad = raw[audio_end:suffix_start]
        descriptor_suffix = raw[suffix_start:]
        if pad != (b"\0" if audio_end & 1 else b""):
            raise AssertionError(f"frame {seq}: invalid descriptor alignment pad")
        # Decode here as a structural check. The main proof below also compares
        # these actual on-disc bytes with the independently rebuilt descriptors.
        decode_descriptors(descriptor_suffix)
    else:
        pad = raw[audio_end:]
        descriptor_suffix = None
        if len(pad) > 1 or any(pad):
            raise AssertionError(
                f"frame {seq}: expected zero or one zero even-pad byte, got {pad!r}"
            )
    return Control(seq, raw, bitmap, entries, audio_end, pad, descriptor_suffix)


def read_stream(header_path: Path, body_path: Path) -> Stream:
    header = header_path.read_bytes()
    magic, version, nfr, cols, rows, cells, pool, base = struct.unpack_from(
        ">4sHHHHHHH", header
    )
    if magic != b"TTRC" or version != 6:
        raise AssertionError(f"expected split TTRC v6, got {magic!r} v{version}")
    if cols * rows != cells:
        raise AssertionError(f"grid {cols}x{rows} does not equal {cells} cells")

    prebuf_patterns = struct.unpack_from(">L", header, 22)[0]
    routing_sectors = struct.unpack_from(">L", header, 26)[0]
    prebuf_sectors = struct.unpack_from(">L", header, 30)[0]
    f0_ctrl_sectors, f0_pattern_sectors, palette_sectors = struct.unpack_from(
        ">LLL", header, 40
    )
    audio_bytes = struct.unpack_from(">H", header, 54)[0]
    fps = struct.unpack_from(">H", header, 56)[0] or 15
    audio_preload_sectors = struct.unpack_from(">H", header, 60)[0]
    features = struct.unpack_from(">H", header, 62)[0]
    unknown_features = features & ~FEATURE_COLD_RUNS
    if unknown_features:
        raise AssertionError(f"unsupported header feature bits 0x{unknown_features:04X}")

    frame0_offset = (1 + palette_sectors + audio_preload_sectors) * SECTOR
    frame0_len = struct.unpack_from(">H", header, frame0_offset)[0]
    controls = [
        parse_control(
            header[frame0_offset : frame0_offset + frame0_len],
            0,
            cells,
            audio_bytes,
            features,
        )
    ]
    frame0_cold = sum(bool(entry & 0x8000) for entry in controls[0].entries)
    frame0_payload_start = frame0_offset + f0_ctrl_sectors * SECTOR
    frame0_payload = header[
        frame0_payload_start : frame0_payload_start + frame0_cold * PATTERN_BYTES
    ]
    if len(frame0_payload) != frame0_cold * PATTERN_BYTES:
        raise AssertionError("frame 0 pattern payload is truncated")
    if frame0_cold * PATTERN_BYTES > f0_pattern_sectors * SECTOR:
        raise AssertionError("frame 0 pattern count exceeds its header sectors")

    routing_offset = (
        1
        + palette_sectors
        + audio_preload_sectors
        + f0_ctrl_sectors
        + f0_pattern_sectors
    ) * SECTOR
    routing_raw = header[routing_offset : routing_offset + 2 * nfr]
    if len(routing_raw) != 2 * nfr:
        raise AssertionError("routing table is truncated")
    routes = [(routing_raw[2 * i], routing_raw[2 * i + 1]) for i in range(nfr)]
    if routes[0] != (0, 0):
        raise AssertionError(f"frame 0 route must be (0, 0), got {routes[0]}")

    prebuffer_offset = routing_offset + routing_sectors * SECTOR
    prebuffer_bytes = prebuf_patterns * PATTERN_BYTES
    if prebuffer_bytes > prebuf_sectors * SECTOR:
        raise AssertionError("prebuffer pattern count exceeds its header sectors")
    prebuffer_payload = header[
        prebuffer_offset : prebuffer_offset + prebuffer_bytes
    ]
    expected_header_len = prebuffer_offset + prebuf_sectors * SECTOR
    if len(header) != expected_header_len:
        raise AssertionError(
            f"HEADER.DAT is {len(header)} bytes, expected {expected_header_len}"
        )

    body = body_path.read_bytes()
    slots = frame_sectors(routes, fps)
    body_pos = 0
    control_stream = bytearray()
    body_payload = bytearray()
    for seq in range(1, nfr):
        slot_bytes = slots[seq] * SECTOR
        slot = body[body_pos : body_pos + slot_bytes]
        if len(slot) != slot_bytes:
            raise AssertionError(f"frame {seq}: BODY.DAT slot is truncated")
        n_pay, n_ctrl = routes[seq]
        useful_bytes = (n_ctrl + n_pay) * SECTOR
        if useful_bytes > slot_bytes:
            raise AssertionError(f"frame {seq}: route exceeds its BODY slot")
        control_stream += slot[: n_ctrl * SECTOR]
        body_payload += slot[n_ctrl * SECTOR : useful_bytes]
        body_pos += slot_bytes
    if body_pos != len(body):
        raise AssertionError(
            f"BODY.DAT has {len(body) - body_pos} unrouted trailing bytes"
        )

    control_pos = 0
    for seq in range(1, nfr):
        if control_pos + 2 > len(control_stream):
            raise AssertionError(f"frame {seq}: missing control length")
        block_len = struct.unpack_from(">H", control_stream, control_pos)[0]
        if block_len < 8 or block_len & 1:
            raise AssertionError(f"frame {seq}: invalid control length {block_len}")
        block_end = control_pos + block_len
        if block_end > len(control_stream):
            raise AssertionError(f"frame {seq}: control is truncated")
        controls.append(
            parse_control(
                bytes(control_stream[control_pos:block_end]),
                seq,
                cells,
                audio_bytes,
                features,
            )
        )
        control_pos = block_end

    total_cold = sum(
        bool(entry & 0x8000) for control in controls for entry in control.entries
    )
    streamed_pattern_bytes = (total_cold - frame0_cold) * PATTERN_BYTES
    streamed_payload = bytes(prebuffer_payload + body_payload)
    if len(streamed_payload) < streamed_pattern_bytes:
        raise AssertionError("streamed cold payload is truncated")
    payload_tail = streamed_payload[streamed_pattern_bytes:]
    if any(payload_tail):
        raise AssertionError("nonzero bytes follow the final cold payload")
    payload = frame0_payload + streamed_payload[:streamed_pattern_bytes]
    return Stream(
        nfr,
        cells,
        pool,
        base,
        audio_bytes,
        features,
        f0_ctrl_sectors,
        tuple(controls),
        payload,
    )


def bitmap_cells(bitmap: bytes, cells: int) -> list[int]:
    return [
        cell
        for cell in range(cells)
        if bitmap[cell >> 3] & (1 << (cell & 7))
    ]


def pack_pattern(key: bytes) -> bytes:
    """Independently reproduce the 8x8 4-bit packed MD pattern."""
    if len(key) != 64:
        raise AssertionError(f"decision key has {len(key)} bytes, expected 64")
    out = bytearray()
    for row in range(8):
        for column in range(0, 8, 2):
            high = key[row * 8 + column]
            low = key[row * 8 + column + 1]
            if high > 15 or low > 15:
                raise AssertionError("decision key contains a palette index above 15")
            out.append((high << 4) | low)
    return bytes(out)


def old_sub_runs(entries: tuple[int, ...], base: int) -> tuple[tuple[int, int], ...]:
    """Model the current entry scan and open-run accumulator."""
    result: list[tuple[int, int]] = []
    for entry in entries:
        if not entry & 0x8000:
            continue
        slot = (entry & 0x07FF) - base
        if result and result[-1][0] + result[-1][1] == slot:
            start, count = result[-1]
            result[-1] = start, count + 1
        else:
            result.append((slot, 1))
    return tuple(result)


def per_run_descriptors(
    entries: tuple[int, ...], colds: tuple[bool, ...], base: int
) -> tuple[tuple[int, int], ...]:
    """Build the proposed descriptor list from packer's logical per tuple."""
    result: list[tuple[int, int]] = []
    for entry, cold in zip(entries, colds, strict=True):
        if not cold:
            continue
        slot = (entry & 0x07FF) - base
        if result and result[-1][0] + result[-1][1] == slot:
            start, count = result[-1]
            result[-1] = start, count + 1
        else:
            result.append((slot, 1))
    return tuple(result)


def encode_descriptors(runs: tuple[tuple[int, int], ...]) -> bytes:
    if len(runs) > 0xFFFF:
        raise AssertionError("descriptor count does not fit u16")
    raw = bytearray(struct.pack(">H", len(runs)))
    for slot_start, count in runs:
        if not 0 <= slot_start <= 0xFFFF or not 1 <= count <= 0xFFFF:
            raise AssertionError(f"invalid run ({slot_start}, {count})")
        raw += struct.pack(">HH", slot_start, count)
    return bytes(raw)


def decode_descriptors(raw: bytes) -> tuple[tuple[int, int], ...]:
    if len(raw) < 2:
        raise AssertionError("descriptor suffix is truncated")
    n_runs = struct.unpack_from(">H", raw)[0]
    if len(raw) != 2 + 4 * n_runs:
        raise AssertionError(
            f"descriptor suffix is {len(raw)} bytes for {n_runs} runs"
        )
    return tuple(
        struct.unpack_from(">HH", raw, 2 + 4 * index)
        for index in range(n_runs)
    )


def expanded_slots(runs: tuple[tuple[int, int], ...]) -> tuple[int, ...]:
    return tuple(
        slot
        for slot_start, count in runs
        for slot in range(slot_start, slot_start + count)
    )


def verify_descriptor_alignment() -> None:
    """Prove the assembly's absolute alignment for even and odd audio starts."""
    for bitmap_bytes in range(1, 141):
        for debug_bytes in (0, DEBUG_BYTES):
            for audio_bytes in (443, 887, 444, 888):
                audio_start = 8 + debug_bytes + bitmap_bytes + 2 * 17
                packed_suffix = (audio_start + audio_bytes + 1) & ~1
                player_suffix = audio_start + audio_bytes
                if player_suffix & 1:
                    player_suffix += 1
                if player_suffix != packed_suffix or player_suffix & 1:
                    raise AssertionError(
                        "descriptor alignment differs for "
                        f"bitmap={bitmap_bytes}, debug={debug_bytes}, audio={audio_bytes}"
                    )


def print_range_stats(
    label: str,
    frame_indices: list[int],
    controls: tuple[Control, ...],
    run_counts: list[int],
) -> None:
    cold_counts = [
        sum(bool(entry & 0x8000) for entry in controls[index].entries)
        for index in frame_indices
    ]
    runs = [run_counts[index] for index in frame_indices]
    cold_total = sum(cold_counts)
    run_total = sum(runs)
    descriptor_bytes = sum(2 + 4 * count for count in runs)
    average = cold_total / run_total if run_total else 0.0
    print(
        f"{label}: frames={len(frame_indices)} cold={cold_total} runs={run_total} "
        f"avg_run={average:.2f} descriptor={descriptor_bytes}B "
        f"runs/frame min={min(runs, default=0)} max={max(runs, default=0)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify packed cold-run descriptors against the real stream."
    )
    parser.add_argument(
        "--header", type=Path, default=Path("out/movieplay/HEADER.DAT")
    )
    parser.add_argument(
        "--body", type=Path, default=Path("out/movieplay/BODY.DAT")
    )
    parser.add_argument(
        "--decisions", type=Path, default=DEFAULT_DECISIONS
    )
    args = parser.parse_args()

    stream = read_stream(args.header, args.body)
    verify_descriptor_alignment()
    with args.decisions.open("rb") as handle:
        decisions = pickle.load(handle)
    decision_frames = decisions["frames"]
    if len(decision_frames) != stream.frames:
        raise AssertionError(
            f"decisions.pkl has {len(decision_frames)} frames; stream has {stream.frames}"
        )

    expected_payload = bytearray()
    payload_pos = 0
    total_updates = 0
    total_cold = 0
    total_runs = 0
    descriptor_bytes_by_frame: list[int] = []
    run_counts: list[int] = []

    for seq, (decision_frame, control) in enumerate(
        zip(decision_frames, stream.controls, strict=True)
    ):
        ordered = sorted(decision_frame, key=lambda item: int(item[0]))
        expected_cells = [int(item[0]) for item in ordered]
        if bitmap_cells(control.bitmap, stream.cells) != expected_cells:
            raise AssertionError(f"frame {seq}: bitmap cells differ from decisions.pkl")
        expected_palettes = [int(item[1]) for item in ordered]
        actual_palettes = [(entry >> 13) & 3 for entry in control.entries]
        if actual_palettes != expected_palettes:
            raise AssertionError(
                f"frame {seq}: entry palettes differ from decisions.pkl"
            )

        colds = tuple(bool(entry & 0x8000) for entry in control.entries)
        old_runs = old_sub_runs(control.entries, stream.base)
        logical_entries = tuple(entry & 0x7FFF for entry in control.entries)
        proposed_runs = per_run_descriptors(logical_entries, colds, stream.base)
        expected_suffix = encode_descriptors(proposed_runs)
        suffix = control.descriptor_suffix or expected_suffix
        if control.descriptor_suffix is not None and suffix != expected_suffix:
            raise AssertionError(
                f"frame {seq}: packed descriptor bytes differ from logical per runs"
            )
        decoded_runs = decode_descriptors(suffix)
        if decoded_runs != old_runs:
            raise AssertionError(f"frame {seq}: run descriptors changed old Sub runs")

        cold_count = sum(colds)
        frame_payload_bytes = cold_count * PATTERN_BYTES
        frame_payload = stream.payload[
            payload_pos : payload_pos + frame_payload_bytes
        ]
        if len(frame_payload) != frame_payload_bytes:
            raise AssertionError(f"frame {seq}: cold payload is truncated")
        payload_pos += frame_payload_bytes

        decision_patterns = [
            pack_pattern(bytes(item[2]))
            for item, cold in zip(ordered, colds, strict=True)
            if cold
        ]
        expected_frame_payload = b"".join(decision_patterns)
        expected_payload += expected_frame_payload
        if frame_payload != expected_frame_payload:
            raise AssertionError(
                f"frame {seq}: physical payload order differs from decisions.pkl"
            )

        old_slots = tuple(
            (entry & 0x07FF) - stream.base
            for entry in control.entries
            if entry & 0x8000
        )
        proposed_slots = expanded_slots(decoded_runs)
        if proposed_slots != old_slots:
            raise AssertionError(f"frame {seq}: descriptor slot order changed")
        old_output = tuple(zip(old_slots, decision_patterns, strict=True))
        proposed_output = tuple(zip(proposed_slots, decision_patterns, strict=True))
        if proposed_output != old_output:
            raise AssertionError(f"frame {seq}: descriptor output changed")
        for slot in proposed_slots:
            if not 0 <= slot < stream.pool:
                raise AssertionError(f"frame {seq}: slot {slot} is outside the pool")

        total_updates += len(control.entries)
        total_cold += cold_count
        total_runs += len(decoded_runs)
        run_counts.append(len(decoded_runs))
        descriptor_bytes_by_frame.append(len(suffix))

    if payload_pos != len(stream.payload) or bytes(expected_payload) != stream.payload:
        raise AssertionError("whole-stream payload comparison did not consume exactly")

    added_control_bytes = sum(descriptor_bytes_by_frame)
    actual_control_bytes = sum(len(control.raw) for control in stream.controls)
    if stream.features & FEATURE_COLD_RUNS:
        legacy_control_bytes = actual_control_bytes - added_control_bytes
        legacy_body_bytes = sum(
            len(control.raw) - descriptor_bytes_by_frame[index]
            for index, control in enumerate(stream.controls[1:], start=1)
        )
        actual_body_bytes = sum(len(control.raw) for control in stream.controls[1:])
        baseline_f0_sectors = ceil_sectors(
            len(stream.controls[0].raw) - descriptor_bytes_by_frame[0]
        )
        result_f0_sectors = stream.f0_ctrl_sectors
        feature_label = "packed feature bit0"
    else:
        legacy_control_bytes = actual_control_bytes
        legacy_body_bytes = sum(len(control.raw) for control in stream.controls[1:])
        actual_body_bytes = legacy_body_bytes + sum(descriptor_bytes_by_frame[1:])
        baseline_f0_sectors = stream.f0_ctrl_sectors
        result_f0_sectors = ceil_sectors(
            len(stream.controls[0].raw) + descriptor_bytes_by_frame[0]
        )
        feature_label = "legacy stream; hypothetical suffix"
    legacy_body_sectors = ceil_sectors(legacy_body_bytes)
    actual_body_sectors = ceil_sectors(actual_body_bytes)

    print(
        "cold-run descriptor equivalence: OK "
        f"({stream.frames} frames, {total_updates} entries, {total_cold} cold, "
        f"{total_runs} runs, {len(stream.payload)} payload bytes; {feature_label})"
    )
    print("descriptor absolute alignment: OK (bitmap bytes 1..140; odd/even audio starts)")
    print(
        "control overhead: "
        f"+{added_control_bytes}B "
        f"({legacy_control_bytes}B -> {legacy_control_bytes + added_control_bytes}B), "
        f"BODY control {legacy_body_sectors} -> {actual_body_sectors} sectors "
        f"(+{actual_body_sectors - legacy_body_sectors}), "
        f"frame0 control {baseline_f0_sectors} -> {result_f0_sectors} sectors, "
        f"per-frame max +{max(descriptor_bytes_by_frame)}B"
    )
    print_range_stats(
        "startup F1..F42",
        list(range(1, min(43, stream.frames))),
        stream.controls,
        run_counts,
    )
    if stream.frames <= 2019:
        raise AssertionError(f"stream ends before decimal frame 2019 ({stream.frames} frames)")
    frame = 2019
    print_range_stats(
        "frame F2019(decimal)", [frame], stream.controls, run_counts
    )


if __name__ == "__main__":
    main()
