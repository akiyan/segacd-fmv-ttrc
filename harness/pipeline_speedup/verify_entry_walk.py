#!/usr/bin/env python3
"""Prove that the Sub CPU can consume update entries without re-walking bitmap.

The Main CPU still needs the bitmap to map entries to cells.  The Sub CPU only
needs cold entries in stream order to pop patterns and build DMA runs.  This
checker walks every real control block in MOVIE.DAT both ways and verifies that
the entry stream, cold-slot order and run grouping are identical.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path


SECTOR = 2048


def frame_sectors(routes: list[tuple[int, int]], fps: int) -> list[int]:
    """Return the v4/v5 bounded-accumulator sector schedule for frames 1+."""
    acc = 0
    lead = 0
    out = [0]
    for n_pay, n_ctrl in routes[1:]:
        acc += 75
        ratedelta, acc = divmod(acc, fps)
        actual = n_pay + n_ctrl
        fsec = max(actual, ratedelta - lead)
        lead += fsec - ratedelta
        out.append(fsec)
    return out


def runs(entries: list[int]) -> list[tuple[int, int]]:
    """Model the Sub CPU's consecutive cold-slot run builder."""
    out: list[tuple[int, int]] = []
    for entry in entries:
        if not entry & 0x8000:
            continue
        slot = (entry & 0x07FF) - 1
        if out and out[-1][0] + out[-1][1] == slot:
            start, count = out[-1]
            out[-1] = start, count + 1
        else:
            out.append((slot, 1))
    return out


def verify_block(block: bytes, seq: int, cells: int, pool: int) -> tuple[int, int]:
    if len(block) < 8:
        raise AssertionError(f"frame {seq}: short control block")
    total_len, frame_seq, n_upd = struct.unpack_from(">HHH", block)
    if total_len != len(block):
        raise AssertionError(f"frame {seq}: total_len {total_len} != {len(block)}")
    if frame_seq != seq:
        raise AssertionError(f"frame {seq}: frame_seq is {frame_seq}")
    if n_upd > cells:
        raise AssertionError(f"frame {seq}: n_upd {n_upd} exceeds {cells} cells")

    dbg = block[7]
    bitmap_off = 8 + (22 if dbg else 0)
    bitmap_len = (cells + 7) // 8
    entries_off = bitmap_off + bitmap_len
    entries_end = entries_off + 2 * n_upd
    if entries_end > len(block):
        raise AssertionError(f"frame {seq}: entries exceed control block")
    bitmap = block[bitmap_off:entries_off]
    direct = list(struct.unpack_from(f">{n_upd}H", block, entries_off)) if n_upd else []

    old: list[int] = []
    entry_i = 0
    for cell in range(cells):
        if bitmap[cell >> 3] & (1 << (cell & 7)):
            if entry_i >= len(direct):
                raise AssertionError(f"frame {seq}: bitmap has more updates than n_upd")
            old.append(direct[entry_i])
            entry_i += 1
    if entry_i != n_upd:
        raise AssertionError(
            f"frame {seq}: bitmap consumed {entry_i} entries, header says {n_upd}"
        )
    for cell in range(cells, bitmap_len * 8):
        if bitmap[cell >> 3] & (1 << (cell & 7)):
            raise AssertionError(f"frame {seq}: padding bitmap bit {cell} is set")

    if old != direct or runs(old) != runs(direct):
        raise AssertionError(f"frame {seq}: direct entry walk differs from bitmap walk")
    for entry in direct:
        if entry & 0x8000:
            slot_plus_one = entry & 0x07FF
            if not 1 <= slot_plus_one <= pool:
                raise AssertionError(
                    f"frame {seq}: cold slot+1 {slot_plus_one} outside pool {pool}"
                )
    return n_upd, sum(bool(entry & 0x8000) for entry in direct)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("movie", nargs="?", default="out/movieplay/MOVIE.DAT")
    args = parser.parse_args()
    data = Path(args.movie).read_bytes()

    magic, version, nfr, _cols, _rows, cells, pool = struct.unpack_from(
        ">4sHHHHHH", data, 0
    )
    if magic != b"TTRC" or version < 4:
        raise SystemExit(f"expected TTRC v4+, got {magic!r} v{version}")
    routing_sec = struct.unpack_from(">L", data, 26)[0]
    prebuf_sec = struct.unpack_from(">L", data, 30)[0]
    f0_ctrl_sec, f0_pat_sec, paltab_sec = struct.unpack_from(">LLL", data, 40)
    fps = struct.unpack_from(">H", data, 56)[0] or 15
    audio_preload_sec = struct.unpack_from(">H", data, 60)[0]

    f0_off = (1 + paltab_sec + audio_preload_sec) * SECTOR
    f0_len = struct.unpack_from(">H", data, f0_off)[0]
    controls = [data[f0_off : f0_off + f0_len]]

    routing_off = (1 + paltab_sec + audio_preload_sec + f0_ctrl_sec + f0_pat_sec) * SECTOR
    routing_raw = data[routing_off : routing_off + 2 * nfr]
    if len(routing_raw) != 2 * nfr:
        raise AssertionError("truncated routing table")
    routes = [(routing_raw[2 * i], routing_raw[2 * i + 1]) for i in range(nfr)]
    fsecs = frame_sectors(routes, fps)

    frames_off = (routing_off // SECTOR + routing_sec + prebuf_sec) * SECTOR
    cursor = frames_off
    control_stream = bytearray()
    for i in range(1, nfr):
        n_pay, n_ctrl = routes[i]
        frame_len = fsecs[i] * SECTOR
        frame = data[cursor : cursor + frame_len]
        if len(frame) != frame_len:
            raise AssertionError(f"frame {i}: truncated sector slot")
        control_stream += frame[n_pay * SECTOR : (n_pay + n_ctrl) * SECTOR]
        cursor += frame_len

    control_pos = 0
    for seq in range(1, nfr):
        if control_pos + 2 > len(control_stream):
            raise AssertionError(f"frame {seq}: missing control length")
        total_len = struct.unpack_from(">H", control_stream, control_pos)[0]
        if total_len == 0 or total_len & 1:
            raise AssertionError(f"frame {seq}: invalid total_len {total_len}")
        controls.append(bytes(control_stream[control_pos : control_pos + total_len]))
        control_pos += total_len

    updates = 0
    cold = 0
    for seq, block in enumerate(controls):
        frame_updates, frame_cold = verify_block(block, seq, cells, pool)
        updates += frame_updates
        cold += frame_cold
    print(
        f"entry-walk equivalence: OK ({nfr} frames, {updates} entries, "
        f"{cold} cold, {cells} cells)"
    )


if __name__ == "__main__":
    main()
