#!/usr/bin/env python3
"""Prove that v6 -> v7 routing conversion preserves every BODY sector."""

from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path


SECTOR = 2048
FRAME_SECTORS = 5
MAX_V7_FRAMES = 16 * 1024


def header_fields(header: bytes) -> dict[str, int]:
    if len(header) < SECTOR or header[:4] != b"TTRC":
        raise ValueError("not a TTRC HEADER.DAT")
    version = struct.unpack_from(">H", header, 4)[0]
    if version != 6:
        raise ValueError(f"migration source must be v6, got v{version}")
    fields = {
        "frames": struct.unpack_from(">H", header, 6)[0],
        "routing_sec": struct.unpack_from(">L", header, 26)[0],
        "prebuf_sec": struct.unpack_from(">L", header, 30)[0],
        "f0_ctrl_sec": struct.unpack_from(">L", header, 40)[0],
        "f0_pat_sec": struct.unpack_from(">L", header, 44)[0],
        "paltab_sec": struct.unpack_from(">L", header, 48)[0],
        "fps": struct.unpack_from(">H", header, 56)[0],
        "audio_pre_sec": struct.unpack_from(">H", header, 60)[0],
    }
    if not 1 <= fields["frames"] <= MAX_V7_FRAMES or fields["fps"] <= 0:
        raise ValueError(f"invalid frame/fps fields: {fields}")
    return fields


def v6_routes(header: bytes, fields: dict[str, int]) -> list[tuple[int, int]]:
    offset = (
        1
        + fields["paltab_sec"]
        + fields["audio_pre_sec"]
        + fields["f0_ctrl_sec"]
        + fields["f0_pat_sec"]
    ) * SECTOR
    size = fields["routing_sec"] * SECTOR
    region = header[offset:offset + size]
    if len(region) != size or len(region) < fields["frames"] * 2:
        raise ValueError("truncated v6 routing region")
    routes = []
    for frame in range(fields["frames"]):
        pay, ctrl = region[frame * 2:frame * 2 + 2]
        if pay + ctrl > FRAME_SECTORS:
            raise ValueError(f"v6 frame {frame} has invalid route {pay}+{ctrl}")
        routes.append((pay, ctrl))
    if routes[0] != (0, 0):
        raise ValueError(f"v6 frame 0 route is {routes[0]}, expected (0, 0)")
    return routes


def migrate(routes: list[tuple[int, int]]) -> tuple[bytes, list[tuple[int, int]]]:
    encoded = bytearray()
    decoded = []
    for frame, (pay, ctrl) in enumerate(routes):
        total = pay + ctrl
        if not (0 <= ctrl <= total <= FRAME_SECTORS):
            raise ValueError(f"invalid source route at frame {frame}: {pay}, {ctrl}")
        entry = (total << 3) | ctrl
        if entry & 0xC0:
            raise AssertionError("encoder set a reserved routing bit")
        encoded.append(entry)
        decoded_ctrl = entry & 7
        decoded_total = (entry >> 3) & 7
        if decoded_total > FRAME_SECTORS or decoded_ctrl > decoded_total:
            raise AssertionError(f"invalid encoded route at frame {frame}: 0x{entry:02X}")
        decoded.append((decoded_total - decoded_ctrl, decoded_ctrl))
    sectors = (len(routes) + SECTOR - 1) // SECTOR
    blob = bytes(encoded).ljust(sectors * SECTOR, b"\0")
    if blob[0] or any(blob[len(routes):]):
        raise AssertionError("v7 frame 0 or sector padding is nonzero")
    return blob, decoded


def walk_body(
    body: bytes,
    routes: list[tuple[int, int]],
    fps: int,
) -> tuple[list[int], bytes, bytes, bytes]:
    offset = 0
    sec_acc = 0
    lead = 0
    boundaries = [0]
    control = bytearray()
    payload = bytearray()
    padding = bytearray()
    for frame, (pay, ctrl) in enumerate(routes[1:], 1):
        sec_acc += 75
        ratedelta, sec_acc = divmod(sec_acc, fps)
        actual = pay + ctrl
        due = ratedelta - lead
        fsec = max(actual, due)
        lead += fsec - ratedelta
        end = offset + fsec * SECTOR
        if end > len(body):
            raise ValueError(f"BODY.DAT ends inside frame {frame}")
        ctrl_end = offset + ctrl * SECTOR
        pay_end = ctrl_end + pay * SECTOR
        control += body[offset:ctrl_end]
        payload += body[ctrl_end:pay_end]
        padding += body[pay_end:end]
        offset = end
        boundaries.append(offset)
    if offset != len(body):
        raise ValueError(f"BODY walk ended at {offset}, file has {len(body)} bytes")
    return boundaries, bytes(control), bytes(payload), bytes(padding)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("header", type=Path)
    parser.add_argument("body", type=Path)
    args = parser.parse_args()

    header = args.header.read_bytes()
    body = args.body.read_bytes()
    if len(header) % SECTOR or len(body) % SECTOR:
        raise SystemExit("HEADER.DAT and BODY.DAT must be sector aligned")
    try:
        fields = header_fields(header)
        old_routes = v6_routes(header, fields)
        v7_blob, new_routes = migrate(old_routes)
        if new_routes != old_routes:
            raise AssertionError("v7 route round-trip changed a pay/control pair")
        old_walk = walk_body(body, old_routes, fields["fps"])
        new_walk = walk_body(body, new_routes, fields["fps"])
        if new_walk != old_walk:
            raise AssertionError("v7 route changed BODY boundaries or classified bytes")
    except (AssertionError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print(
        f"routing v6->v7 proof: OK  frames={fields['frames']} "
        f"route={fields['routing_sec']}sec->{len(v7_blob) // SECTOR}sec "
        f"BODY={len(body) // SECTOR}sec "
        f"sha256={hashlib.sha256(body).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
