#!/usr/bin/env python3
"""Prove packed cold-run descriptors preserve the legacy Sub-CPU output.

The feature-bit-0 control suffix is appended after the existing audio and an
absolute-address alignment pad:

    n_runs:u16, repeated v12 indexed four-byte descriptors

This checker does not import the packer.  It independently reads the real split
TTRC v16 files and reconstructs every current control and payload byte. The
display entries remain in cell order, while the p39 suffix and physical pattern
payload independently follow ascending VRAM-slot order.  The checker proves both
views against the same decisions and proves the suffix consumes each physical
source in that authoritative order.

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
FEATURE_COLD_RUNS = 0x0001
FEATURE_FIXED_N2 = 0x0002
FEATURE_PATTERN_SUPPLY = 0x0008
FEATURE_SHADOW_UPDATE_LISTS = 0x0010
FEATURE_VRAM_RAW_PREFETCH = 0x0020
FEATURE_DICBUF_INDEXED_RUNS = 0x0040
FEATURE_BOOT_VRAM_SIDECAR = 0x0080
ADPCM_TABLE_SECTORS = 5
ROUTING_TOTAL_MAX = 5
PATTERN_SUPPLY_OFFSET = 196
SOURCE_SHIFT = 11
SOURCE_MASK = 0x1800
SOURCE_PRG = 0
SOURCE_WR = 1
SOURCE_DIC = 2
NAME_ENTRY_MASK = 0x67FF
RUN_SOURCE_SHIFT = 14
RUN_COUNT_MASK = 0x07FF
SHADOW_UPDATE_LIST_TAG = 0x8000
SHADOW_UPDATE_COUNT_MASK = 0x7FFF
DEFAULT_DECISIONS = Path(
    "videos/sonic_H32_256x224_adpcm22_geometry_pad_4by3/decisions.pkl"
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
    use_list: bool


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
    source_payloads: tuple[bytes, ...] | None


def ceil_sectors(byte_count: int) -> int:
    return (byte_count + SECTOR - 1) // SECTOR


def take_pattern_region(
    header: bytes, offset: int, sectors: int, patterns: int, label: str,
) -> tuple[bytes, int]:
    """Read one sector-aligned boot pattern region and require zero padding."""
    region = header[offset:offset + sectors * SECTOR]
    if len(region) != sectors * SECTOR:
        raise AssertionError(f"{label} region is truncated")
    used = patterns * PATTERN_BYTES
    if used > len(region):
        raise AssertionError(f"{label} patterns exceed their sectors")
    if any(region[used:]):
        raise AssertionError(f"{label} sector padding is nonzero")
    return region[:used], offset + len(region)


def frame_sectors(
    routes: list[tuple[int, int]], version: int, fps: int, vsync_n: int,
    features: int,
) -> list[int]:
    """Reproduce the v6+ bounded CD-1x BODY slot accumulator."""
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
    """Decode routing independently from the production packer."""
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


def parse_control(
    raw: bytes, seq: int, cells: int, audio_bytes: int, features: int
) -> Control:
    if len(raw) < 8:
        raise AssertionError(f"frame {seq}: control is shorter than 8 bytes")
    total_len, packed_seq, raw_count = struct.unpack_from(">HHH", raw)
    n_upd = raw_count & SHADOW_UPDATE_COUNT_MASK
    use_list = bool(raw_count & SHADOW_UPDATE_LIST_TAG)
    if total_len != len(raw):
        raise AssertionError(f"frame {seq}: total_len {total_len} != {len(raw)}")
    if packed_seq != seq:
        raise AssertionError(f"frame {seq}: packed sequence is {packed_seq}")
    if n_upd > cells:
        raise AssertionError(f"frame {seq}: n_upd {n_upd} exceeds {cells}")

    bitmap_start = 8
    bitmap_bytes = (cells + 7) // 8
    bitmap_end = bitmap_start + bitmap_bytes
    entries_start = (bitmap_end + 1) & ~1
    entries_end = entries_start + 2 * n_upd
    if use_list:
        entries_start = bitmap_start
        entries_end = entries_start + 4 * n_upd
    audio_end = entries_end + audio_bytes
    if audio_end > len(raw):
        raise AssertionError(f"frame {seq}: audio extends beyond the control")
    if use_list:
        bitmap_mut = bytearray(bitmap_bytes)
        entries_mut = []
        previous_cell = -1
        for index in range(n_upd):
            offset, entry = struct.unpack_from(">HH", raw, entries_start + index * 4)
            if offset & 1 or offset >= cells * 2:
                raise AssertionError(f"frame {seq}: invalid shadow offset {offset}")
            cell = offset // 2
            if cell <= previous_cell:
                raise AssertionError(f"frame {seq}: list cells are not ascending")
            bitmap_mut[cell >> 3] |= 1 << (cell & 7)
            entries_mut.append(entry)
            previous_cell = cell
        bitmap = bytes(bitmap_mut)
        entries = tuple(entries_mut)
    else:
        bitmap = raw[bitmap_start:bitmap_end]
        if any(raw[bitmap_end:entries_start]):
            raise AssertionError(f"frame {seq}: bitmap alignment pad is nonzero")
        entries = (
            tuple(struct.unpack_from(f">{n_upd}H", raw, entries_start))
            if n_upd else ()
        )
    if sum(value.bit_count() for value in bitmap) != n_upd:
        raise AssertionError(f"frame {seq}: update population differs from n_upd")
    for cell in range(cells, bitmap_bytes * 8):
        if bitmap[cell >> 3] & (1 << (cell & 7)):
            raise AssertionError(f"frame {seq}: bitmap padding bit {cell} is set")
    if features & FEATURE_COLD_RUNS:
        suffix_start = (audio_end + 1) & ~1
        pad = raw[audio_end:suffix_start]
        descriptor_suffix = raw[suffix_start:]
        if pad != (b"\0" if audio_end & 1 else b""):
            raise AssertionError(f"frame {seq}: invalid descriptor alignment pad")
        # Decode here as a structural check. The main proof below also compares
        # these actual on-disc bytes with the independently rebuilt descriptors.
        decode_descriptors(
            descriptor_suffix, bool(features & FEATURE_PATTERN_SUPPLY))
    else:
        pad = raw[audio_end:]
        descriptor_suffix = None
        if len(pad) > 1 or any(pad):
            raise AssertionError(
                f"frame {seq}: expected zero or one zero even-pad byte, got {pad!r}"
            )
    return Control(
        seq, raw, bitmap, entries, audio_end, pad, descriptor_suffix, use_list)


def read_stream(header_path: Path, body_path: Path) -> Stream:
    header = header_path.read_bytes()
    magic, version, nfr, cols, rows, cells, pool, base = struct.unpack_from(
        ">4sHHHHHHH", header
    )
    if magic != b"TTRC" or version != 16:
        raise AssertionError(
            f"expected split TTRC v16, got {magic!r} v{version}")
    if cols * rows != cells:
        raise AssertionError(f"grid {cols}x{rows} does not equal {cells} cells")

    prebuf_patterns = struct.unpack_from(">L", header, 22)[0]
    routing_sectors = struct.unpack_from(">L", header, 26)[0]
    prebuf_sectors = struct.unpack_from(">L", header, 30)[0]
    f0_ctrl_sectors, f0_pattern_sectors, palette_sectors = struct.unpack_from(
        ">LLL", header, 40
    )
    decoded_audio_bytes = struct.unpack_from(">H", header, 54)[0]
    vsync_n = struct.unpack_from(">H", header, 52)[0]
    fps = struct.unpack_from(">H", header, 56)[0] or 15
    audio_preload_sectors = struct.unpack_from(">H", header, 60)[0]
    features = struct.unpack_from(">H", header, 62)[0]
    unknown_features = features & ~(
        FEATURE_COLD_RUNS | FEATURE_FIXED_N2
        | FEATURE_PATTERN_SUPPLY | FEATURE_SHADOW_UPDATE_LISTS
        | FEATURE_VRAM_RAW_PREFETCH | FEATURE_DICBUF_INDEXED_RUNS
        | FEATURE_BOOT_VRAM_SIDECAR)
    if unknown_features:
        raise AssertionError(f"unsupported header feature bits 0x{unknown_features:04X}")
    if features & FEATURE_SHADOW_UPDATE_LISTS and not features & FEATURE_PATTERN_SUPPLY:
        raise AssertionError("shadow update lists require pattern supply")
    audio_bytes = 4 + decoded_audio_bytes // 2

    table_sectors = ADPCM_TABLE_SECTORS
    wr0_patterns = wr1_patterns = dic_patterns = 0
    wr0_sectors = wr1_sectors = dic_sectors = 0
    if features & FEATURE_PATTERN_SUPPLY:
        supply = struct.unpack_from(">4s8H", header, PATTERN_SUPPLY_OFFSET)
        supply_magic, supply_version, supply_reserved = supply[:3]
        if supply_magic != b"PSUP" or supply_version != 2 or supply_reserved:
            raise AssertionError(f"invalid pattern-supply extension: {supply!r}")
        (
            wr0_patterns, wr1_patterns, dic_patterns,
            wr0_sectors, wr1_sectors, dic_sectors,
        ) = supply[3:]

    cursor = (1 + palette_sectors + table_sectors) * SECTOR
    wr0_payload = wr1_payload = dic_payload = b""
    if features & FEATURE_PATTERN_SUPPLY:
        wr0_payload, cursor = take_pattern_region(
            header, cursor, wr0_sectors, wr0_patterns, "Wr0")
        wr1_payload, cursor = take_pattern_region(
            header, cursor, wr1_sectors, wr1_patterns, "Wr1")
        dic_payload, cursor = take_pattern_region(
            header, cursor, dic_sectors, dic_patterns, "Dic")
    cursor += audio_preload_sectors * SECTOR
    frame0_offset = cursor
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
    frame0_runs = decode_descriptors(
        controls[0].descriptor_suffix or b"",
        bool(features & FEATURE_PATTERN_SUPPLY),
    )
    frame0_cold = sum(
        count for _slot, count, source, _dic_index in frame0_runs
        if source == SOURCE_PRG)
    frame0_payload_start = frame0_offset + f0_ctrl_sectors * SECTOR
    frame0_payload = header[
        frame0_payload_start : frame0_payload_start + frame0_cold * PATTERN_BYTES
    ]
    if len(frame0_payload) != frame0_cold * PATTERN_BYTES:
        raise AssertionError("frame 0 pattern payload is truncated")
    if frame0_cold * PATTERN_BYTES > f0_pattern_sectors * SECTOR:
        raise AssertionError("frame 0 pattern count exceeds its header sectors")

    routing_offset = frame0_offset + (
        f0_ctrl_sectors + f0_pattern_sectors) * SECTOR
    routing_raw = header[
        routing_offset : routing_offset + routing_sectors * SECTOR
    ]
    routes = decode_routes(routing_raw, nfr, version)
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
    slots = frame_sectors(routes, version, fps, vsync_n, features)
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

    source_aware = bool(features & FEATURE_PATTERN_SUPPLY)
    def control_runs(control: Control) -> tuple[tuple[int, int, int, int], ...]:
        if control.use_list:
            if control.descriptor_suffix is None:
                raise AssertionError(
                    f"frame {control.seq}: list control has no run suffix")
            return decode_descriptors(control.descriptor_suffix, source_aware)
        return old_sub_runs(control.entries, base, source_aware)

    total_cold = sum(
        count for control in controls
        for _slot, count, _source, _dic in control_runs(control))
    timed_prg_cold = sum(
        count for control in controls[1:]
        for _slot, count, source, _dic in control_runs(control)
        if not source_aware or source == SOURCE_PRG)
    streamed_pattern_bytes = timed_prg_cold * PATTERN_BYTES
    streamed_payload = bytes(prebuffer_payload + body_payload)
    if len(streamed_payload) < streamed_pattern_bytes:
        raise AssertionError("streamed cold payload is truncated")
    payload_tail = streamed_payload[streamed_pattern_bytes:]
    if any(payload_tail):
        raise AssertionError("nonzero bytes follow the final cold payload")
    prg_payload = streamed_payload[:streamed_pattern_bytes]
    if source_aware:
        source_payloads = (
            frame0_payload, prg_payload, wr0_payload, wr1_payload, dic_payload)
        payload = b"".join(source_payloads)
    else:
        source_payloads = None
        payload = frame0_payload + prg_payload
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
        source_payloads,
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


def old_sub_runs(
    entries: tuple[int, ...], base: int, source_aware: bool,
) -> tuple[tuple[int, int, int, int], ...]:
    """Model the entry scan and source-aware open-run accumulator."""
    result: list[tuple[int, int, int, int]] = []
    for entry in entries:
        if not entry & 0x8000:
            continue
        slot = (entry & 0x07FF) - base
        source = (entry & SOURCE_MASK) >> SOURCE_SHIFT if source_aware else 0
        if (result and result[-1][0] + result[-1][1] == slot
                and result[-1][2] == source
                and source != SOURCE_DIC):
            start, count, _source, _dic = result[-1]
            result[-1] = start, count + 1, source, 0
        else:
            # Entry metadata has no DicBuf index. Dic runs cannot be rebuilt
            # from entries alone in v12, so keep each Dic update separate.
            result.append((slot, 1, source, 0))
    return tuple(result)


def per_run_descriptors(
    entries: tuple[int, ...], colds: tuple[bool, ...], sources: tuple[int, ...],
    dic_indices: tuple[int, ...], base: int,
) -> tuple[tuple[int, int, int, int], ...]:
    """Build the proposed descriptor list from packer's logical per tuple."""
    result: list[tuple[int, int, int, int]] = []
    for entry, cold, source, dic_index in zip(
            entries, colds, sources, dic_indices, strict=True):
        if not cold:
            continue
        slot = (entry & 0x07FF) - base
        if (result and result[-1][0] + result[-1][1] == slot
                and result[-1][2] == source
                and (source != SOURCE_DIC
                     or result[-1][3] + result[-1][1] == dic_index)):
            start, count, _source, start_dic = result[-1]
            result[-1] = start, count + 1, source, start_dic
        else:
            result.append((slot, 1, source, dic_index if source == SOURCE_DIC else 0))
    return tuple(result)


def encode_descriptors(
    runs: tuple[tuple[int, int, int, int], ...], source_aware: bool,
) -> bytes:
    if len(runs) > 0xFFFF:
        raise AssertionError("descriptor count does not fit u16")
    raw = bytearray(struct.pack(">H", len(runs)))
    for slot_start, count, source, dic_index in runs:
        count_limit = RUN_COUNT_MASK if source_aware else 0xFFFF
        if not 0 <= slot_start <= 0xFFFF or not 1 <= count <= count_limit:
            raise AssertionError(f"invalid run ({slot_start}, {count}, {source})")
        if source_aware and source not in (SOURCE_PRG, SOURCE_WR, SOURCE_DIC):
            raise AssertionError(f"invalid run source {source}")
        if source != SOURCE_DIC and dic_index:
            raise AssertionError("non-Dic run carries a dictionary index")
        word0 = slot_start | ((dic_index >> 3) << 11)
        source_count = (
            count | (source << RUN_SOURCE_SHIFT) | ((dic_index & 7) << 11)
            if source_aware else count)
        raw += struct.pack(">HH", word0, source_count)
    return bytes(raw)


def decode_descriptors(
    raw: bytes, source_aware: bool,
) -> tuple[tuple[int, int, int, int], ...]:
    if len(raw) < 2:
        raise AssertionError("descriptor suffix is truncated")
    n_runs = struct.unpack_from(">H", raw)[0]
    if len(raw) != 2 + 4 * n_runs:
        raise AssertionError(
            f"descriptor suffix is {len(raw)} bytes for {n_runs} runs"
        )
    result = []
    for index in range(n_runs):
        word0, source_count = struct.unpack_from(">HH", raw, 2 + 4 * index)
        slot = word0 & 0x07FF
        if source_aware:
            count = source_count & RUN_COUNT_MASK
            source = source_count >> RUN_SOURCE_SHIFT
            dic_index = ((word0 >> 11) << 3) | ((source_count >> 11) & 7)
            if source not in (SOURCE_PRG, SOURCE_WR, SOURCE_DIC) or not count:
                raise AssertionError(
                    f"invalid source-aware run ({slot}, 0x{source_count:04X})")
        else:
            count, source, dic_index = source_count, SOURCE_PRG, 0
        result.append((slot, count, source, dic_index))
    return tuple(result)


def expanded_slots(
    runs: tuple[tuple[int, int, int, int], ...],
) -> tuple[tuple[int, int], ...]:
    return tuple(
        (slot, source)
        for slot_start, count, source, _dic in runs
        for slot in range(slot_start, slot_start + count)
    )


def verify_descriptor_alignment() -> None:
    """Prove the assembly's absolute alignment for even and odd audio starts."""
    for bitmap_bytes in range(1, 141):
        for audio_bytes in (372, 443, 887, 444, 888):
            audio_start = 8 + ((bitmap_bytes + 1) & ~1) + 2 * 17
            packed_suffix = (audio_start + audio_bytes + 1) & ~1
            player_suffix = audio_start + audio_bytes
            if player_suffix & 1:
                player_suffix += 1
            if player_suffix != packed_suffix or player_suffix & 1:
                raise AssertionError(
                    "descriptor alignment differs for "
                    f"bitmap={bitmap_bytes}, audio={audio_bytes}"
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
    source_aware = bool(stream.features & FEATURE_PATTERN_SUPPLY)
    source_positions = [0] * 5
    dic_payload = stream.source_payloads[4] if stream.source_payloads else b""
    dic_index_by_pattern = {
        dic_payload[pos:pos + PATTERN_BYTES]: pos // PATTERN_BYTES
        for pos in range(0, len(dic_payload), PATTERN_BYTES)
    }

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

        packed_suffix = control.descriptor_suffix
        decoded_packed_runs = (
            decode_descriptors(packed_suffix, source_aware)
            if packed_suffix is not None else ())
        ordered_patterns = tuple(
            pack_pattern(bytes(item[2])) for item in ordered)

        # Frame 0 is a boot construction, not a timed BODY frame.  Its name
        # entries remain in cell order, while the boot loader consumes the
        # descriptor suffix and pattern payload in physical-slot order.  The
        # ordinary timed-frame comparison below intentionally requires those
        # orders to match, so prove the boot representation separately.
        if seq == 0:
            if control.use_list:
                raise AssertionError("frame 0 must use the boot cold-entry form")
            packed_slots = expanded_slots(decoded_packed_runs)
            if any(source != SOURCE_PRG for _slot, source in packed_slots):
                raise AssertionError("frame 0 boot runs use a non-boot source")
            slot_patterns = {}
            for entry, pattern in zip(
                    control.entries, ordered_patterns, strict=True):
                slot = (entry & 0x07FF) - stream.base
                previous = slot_patterns.setdefault(slot, pattern)
                if previous != pattern:
                    raise AssertionError(
                        f"frame 0 slot {slot} maps to conflicting patterns")
            physical_payload = stream.source_payloads[0]
            descriptor_slots = tuple(slot for slot, _source in packed_slots)
            useful_payload = len(descriptor_slots) * 32
            if len(physical_payload) < useful_payload or any(
                    physical_payload[useful_payload:]):
                raise AssertionError(
                    "frame 0 payload length differs from descriptor demand")
            physical_payload = physical_payload[:useful_payload]
            physical_by_slot = {
                slot: physical_payload[index * 32:(index + 1) * 32]
                for index, slot in enumerate(descriptor_slots)
            }
            if not set(slot_patterns).issubset(physical_by_slot):
                raise AssertionError(
                    "frame 0 descriptors do not cover every displayed slot")
            for slot, pattern in slot_patterns.items():
                if physical_by_slot[slot] != pattern:
                    raise AssertionError(
                        "frame 0 physical payload differs at a displayed slot")
            source_positions[0] = len(physical_payload)
            total_updates += len(control.entries)
            total_cold += len(descriptor_slots)
            total_runs += len(decoded_packed_runs)
            run_counts.append(len(decoded_packed_runs))
            descriptor_bytes_by_frame.append(len(packed_suffix or b""))
            continue

        if control.use_list:
            source_by_slot = {}
            for slot, source in expanded_slots(decoded_packed_runs):
                if slot in source_by_slot:
                    raise AssertionError(
                        f"frame {seq}: duplicate physical run slot {slot}")
                source_by_slot[slot] = source
            inferred_colds = []
            inferred_sources = []
            for entry in control.entries:
                slot = (entry & 0x07FF) - stream.base
                source = source_by_slot.pop(slot, None)
                is_cold = source is not None
                inferred_colds.append(is_cold)
                inferred_sources.append(source if is_cold else 0)
            if source_by_slot and not stream.features & FEATURE_VRAM_RAW_PREFETCH:
                raise AssertionError(
                    f"frame {seq}: run suffix contains non-update slots")
            colds = tuple(inferred_colds)
            sources = tuple(inferred_sources)
        else:
            colds = tuple(bool(entry & 0x8000) for entry in control.entries)
            sources = tuple(
                (entry & SOURCE_MASK) >> SOURCE_SHIFT if cold and source_aware else 0
                for entry, cold in zip(control.entries, colds, strict=True))
        dic_indices = tuple(
            dic_index_by_pattern[pattern]
            if cold and source == SOURCE_DIC else 0
            for pattern, cold, source in zip(
                ordered_patterns, colds, sources, strict=True))
        entry_mask = NAME_ENTRY_MASK if source_aware else 0x7FFF
        logical_entries = tuple(entry & entry_mask for entry in control.entries)
        cold_records = [
            (entry, source, dic_index, pattern)
            for entry, cold, source, dic_index, pattern in zip(
                logical_entries, colds, sources, dic_indices, ordered_patterns,
                strict=True)
            if cold
        ]
        if packed_suffix is not None:
            # Bitmap/list entries intentionally remain in cell order.  The
            # packed suffix and source payload are independently ordered by
            # physical VRAM slot so the player can issue longer contiguous
            # transfers.  Rebuild that authoritative order before comparing
            # descriptor bytes or payload.
            cold_records.sort(
                key=lambda record: (
                    (record[0] & 0x07FF) - stream.base,
                )
            )
        physical_entries = tuple(record[0] for record in cold_records)
        physical_sources = tuple(record[1] for record in cold_records)
        physical_dic_indices = tuple(record[2] for record in cold_records)
        expected_runs = per_run_descriptors(
            physical_entries,
            (True,) * len(cold_records),
            physical_sources,
            physical_dic_indices,
            stream.base,
        )
        expected_suffix = encode_descriptors(expected_runs, source_aware)
        suffix = packed_suffix or expected_suffix
        if packed_suffix is not None and suffix != expected_suffix:
            raise AssertionError(
                f"frame {seq}: packed descriptor bytes differ from physical runs "
                f"(packed={suffix.hex()} expected={expected_suffix.hex()})"
            )
        decoded_runs = decode_descriptors(suffix, source_aware)
        if decoded_runs != expected_runs:
            raise AssertionError(
                f"frame {seq}: decoded descriptors differ from physical runs")

        cold_count = sum(colds)
        decision_patterns = [record[3] for record in cold_records]
        expected_frame_payload = b"".join(decision_patterns)
        expected_payload += expected_frame_payload
        if stream.source_payloads is None:
            frame_payload_bytes = cold_count * PATTERN_BYTES
            frame_payload = stream.payload[
                payload_pos : payload_pos + frame_payload_bytes]
            if len(frame_payload) != frame_payload_bytes:
                raise AssertionError(f"frame {seq}: cold payload is truncated")
            payload_pos += frame_payload_bytes
            if frame_payload != expected_frame_payload:
                raise AssertionError(
                    f"frame {seq}: physical payload order differs from decisions.pkl")
        else:
            physical_patterns = []
            for _slot, count, source, run_dic_index in decoded_runs:
                if source not in (SOURCE_PRG, SOURCE_WR, SOURCE_DIC):
                    raise AssertionError(f"frame {seq}: invalid source {source}")
                if seq == 0:
                    payload_index = 0
                elif source == SOURCE_PRG:
                    payload_index = 1
                elif source == SOURCE_WR:
                    payload_index = 2 if seq % 2 == 0 else 3
                else:
                    payload_index = 4
                physical = stream.source_payloads[payload_index]
                for offset in range(count):
                    pos = ((run_dic_index + offset) * PATTERN_BYTES
                           if source == SOURCE_DIC
                           else source_positions[payload_index])
                    pattern = physical[pos:pos + PATTERN_BYTES]
                    if len(pattern) != PATTERN_BYTES:
                        raise AssertionError(
                            f"frame {seq}: source {payload_index} is exhausted")
                    physical_patterns.append(pattern)
                    if source != SOURCE_DIC:
                        source_positions[payload_index] += PATTERN_BYTES
            if physical_patterns != decision_patterns:
                raise AssertionError(
                    f"frame {seq}: physical source patterns differ from decisions")

        expected_slots = tuple(
            ((entry & 0x07FF) - stream.base, source)
            for entry, source in zip(
                physical_entries, physical_sources, strict=True)
        )
        descriptor_slots = expanded_slots(decoded_runs)
        if descriptor_slots != expected_slots:
            raise AssertionError(
                f"frame {seq}: descriptor slot order differs from physical order")
        descriptor_output = tuple(
            zip(descriptor_slots, decision_patterns, strict=True))
        expected_output = tuple(
            zip(expected_slots, decision_patterns, strict=True))
        if descriptor_output != expected_output:
            raise AssertionError(f"frame {seq}: descriptor output changed")
        for slot, _source in descriptor_slots:
            if not 0 <= slot < stream.pool:
                raise AssertionError(f"frame {seq}: slot {slot} is outside the pool")

        total_updates += len(control.entries)
        total_cold += cold_count
        total_runs += len(decoded_runs)
        run_counts.append(len(decoded_runs))
        descriptor_bytes_by_frame.append(len(suffix))

    if stream.source_payloads is None:
        if payload_pos != len(stream.payload) or bytes(expected_payload) != stream.payload:
            raise AssertionError("whole-stream payload comparison did not consume exactly")
    else:
        for index, (position, payload) in enumerate(
                zip(source_positions, stream.source_payloads, strict=True)):
            if index == 4:
                continue
            if position != len(payload):
                raise AssertionError(
                    f"source {index}: consumed {position}/{len(payload)} bytes")

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
    if stream.frames > 2019:
        frame = 2019
        print_range_stats(
            "frame F2019(decimal)", [frame], stream.controls, run_counts
        )
    else:
        print(f"frame F2019(decimal): skipped for {stream.frames}-frame smoke stream")


if __name__ == "__main__":
    main()
