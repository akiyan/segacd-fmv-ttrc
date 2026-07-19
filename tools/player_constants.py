#!/usr/bin/env python3
"""Generate assembler constants bound to one packed ``HEADER.DAT``.

The first 64 bytes are the complete fixed player contract.  The packer stores
their CRC-32 in the otherwise reserved first sector at offset 192.  Both Main
and Sub objects include the generated file; the Sub compares the stored value
before accepting the disc, so a player cannot silently run with another
profile's HEADER.DAT.
"""

from __future__ import annotations

import argparse
import dataclasses
import struct
import zlib
from pathlib import Path

import ttrc_routing
import ima_adpcm


SECTOR = 2048
FIXED_HEADER_BYTES = 64
SEG0_BYTES = 128
HEADER_SIGNATURE_OFFSET = FIXED_HEADER_BYTES + SEG0_BYTES
HEADER_STRUCT = struct.Struct(">4s9H4LBB3L6H")

MODE_SPECS = {
    0: ("H32", 32, 2800),
    1: ("H40", 40, 3400),
}


def header_signature(fixed_header: bytes) -> int:
    """Return the deterministic 32-bit build signature for bytes 0..63."""
    if len(fixed_header) != FIXED_HEADER_BYTES:
        raise ValueError(
            f"fixed header must be {FIXED_HEADER_BYTES} bytes, got {len(fixed_header)}")
    return zlib.crc32(fixed_header) & 0xFFFFFFFF


def stamp_header_sector(sector: bytes) -> bytes:
    """Write the fixed-header signature into a complete first sector."""
    if len(sector) != SECTOR:
        raise ValueError(f"header sector must be {SECTOR} bytes, got {len(sector)}")
    out = bytearray(sector)
    struct.pack_into(
        ">L", out, HEADER_SIGNATURE_OFFSET,
        header_signature(bytes(out[:FIXED_HEADER_BYTES])))
    return bytes(out)


@dataclasses.dataclass(frozen=True)
class PlayerConstants:
    signature: int
    version: int
    frames: int
    tcols: int
    trows: int
    cells: int
    bmbytes: int
    pool: int
    base: int
    frame_sectors: int
    nseg: int
    prebuf_pat: int
    routing_sec: int
    prebuf_sec: int
    ring_peak: int
    mode: int
    screen_cols: int
    screen_rows: int
    col0: int
    row0: int
    vbudget: int
    font_vtile: int
    font_addr: int
    f0_ctrl_sec: int
    f0_pat_sec: int
    paltab_sec: int
    vsync_n: int
    audio_bytes: int
    audio_control_bytes: int
    adpcm_table_sectors: int
    fps_int: int
    audio_fd: int
    audio_preload_sec: int
    features: int
    pump_mask: int
    wave_pump_mask: int
    sec_num: int
    sec_mod: int
    sec_base: int
    sec_rem: int


def parse_header_sector(sector: bytes) -> PlayerConstants:
    """Validate one packed first sector and derive its hot player constants."""
    if len(sector) != SECTOR:
        raise ValueError(f"header sector must be {SECTOR} bytes, got {len(sector)}")
    values = HEADER_STRUCT.unpack_from(sector)
    (
        magic, version, frames, tcols, trows, cells, pool, base,
        frame_sectors, nseg, prebuf_pat, routing_sec, prebuf_sec, ring_peak,
        mode, pad, f0_ctrl_sec, f0_pat_sec, paltab_sec, vsync_n,
        audio_bytes, fps_int, audio_fd, audio_preload_sec, features,
    ) = values

    if magic != b"TTRC":
        raise ValueError(f"bad HEADER.DAT magic: {magic!r}")
    if version != ttrc_routing.VERSION:
        raise ValueError(
            f"HEADER.DAT version {version} != player routing version {ttrc_routing.VERSION}")
    if pad != 0:
        raise ValueError(f"HEADER.DAT offset 39 must be zero, got {pad}")
    if not 0 < frames <= ttrc_routing.MAX_FRAMES:
        raise ValueError(f"invalid frame count: {frames}")
    if tcols <= 0 or trows <= 0 or cells != tcols * trows:
        raise ValueError(
            f"invalid tile geometry: {tcols}x{trows} cells={cells}")
    if mode not in MODE_SPECS:
        raise ValueError(f"player constants do not support display mode {mode}")
    _mode_name, screen_cols, vbudget = MODE_SPECS[mode]
    screen_rows = 28
    if tcols > screen_cols or trows > screen_rows:
        raise ValueError(
            f"tile grid {tcols}x{trows} exceeds {screen_cols}x{screen_rows} display")
    expected_routing_sec = ttrc_routing.routing_sector_count(frames)
    if routing_sec != expected_routing_sec:
        raise ValueError(
            f"routing_sec={routing_sec} != ceil({frames}/2048)={expected_routing_sec}")
    if frame_sectors != ttrc_routing.FRAME_SECTORS:
        raise ValueError(
            f"frame_sectors={frame_sectors} != {ttrc_routing.FRAME_SECTORS}")
    if audio_bytes <= 0 or fps_int <= 0 or vsync_n <= 0 or audio_fd <= 0:
        raise ValueError(
            f"invalid timing: vsync_n={vsync_n} audio={audio_bytes} "
            f"fps={fps_int} fd={audio_fd}")

    signature = struct.unpack_from(">L", sector, HEADER_SIGNATURE_OFFSET)[0]
    expected_signature = header_signature(sector[:FIXED_HEADER_BYTES])
    if signature != expected_signature:
        raise ValueError(
            f"HEADER.DAT signature 0x{signature:08X} != expected "
            f"0x{expected_signature:08X}")

    fixed_n2 = bool(features & ttrc_routing.FEATURE_FIXED_N2)
    adpcm22 = bool(features & ttrc_routing.FEATURE_ADPCM22)
    if adpcm22 and audio_bytes & 1:
        raise ValueError(f"ADPCM decoded audio_bytes must be even, got {audio_bytes}")
    audio_control_bytes = (
        ima_adpcm.encoded_bytes(audio_bytes) if adpcm22 else audio_bytes)
    adpcm_table_sectors = (
        (ima_adpcm.FULL_TABLE_BYTES + SECTOR - 1) // SECTOR if adpcm22 else 0)
    sec_num, sec_mod = (1001, 400) if fixed_n2 else (75, fps_int)
    sec_base, sec_rem = divmod(sec_num, sec_mod)
    fast_poll = fps_int >= 24

    return PlayerConstants(
        signature=signature,
        version=version,
        frames=frames,
        tcols=tcols,
        trows=trows,
        cells=cells,
        bmbytes=(cells + 7) // 8,
        pool=pool,
        base=base,
        frame_sectors=frame_sectors,
        nseg=nseg,
        prebuf_pat=prebuf_pat,
        routing_sec=routing_sec,
        prebuf_sec=prebuf_sec,
        ring_peak=ring_peak,
        mode=mode,
        screen_cols=screen_cols,
        screen_rows=screen_rows,
        col0=(screen_cols - tcols) // 2,
        row0=(screen_rows - trows) // 2,
        vbudget=vbudget,
        font_vtile=base + pool,
        font_addr=(base + pool) * 32,
        f0_ctrl_sec=f0_ctrl_sec,
        f0_pat_sec=f0_pat_sec,
        paltab_sec=paltab_sec,
        vsync_n=2 if fixed_n2 else vsync_n,
        audio_bytes=audio_bytes,
        audio_control_bytes=audio_control_bytes,
        adpcm_table_sectors=adpcm_table_sectors,
        fps_int=fps_int,
        audio_fd=audio_fd,
        audio_preload_sec=audio_preload_sec,
        features=features,
        pump_mask=0x03FF if fast_poll else 0x003F,
        wave_pump_mask=0x01FF if fast_poll else 0x00FF,
        sec_num=sec_num,
        sec_mod=sec_mod,
        sec_base=sec_base,
        sec_rem=sec_rem,
    )


INCLUDE_ORDER = (
    "signature", "version", "frames", "mode", "screen_cols", "screen_rows",
    "tcols", "trows", "cells", "bmbytes", "col0", "row0", "vbudget",
    "pool", "base", "font_vtile", "font_addr", "frame_sectors", "nseg",
    "prebuf_pat", "routing_sec", "prebuf_sec", "ring_peak", "f0_ctrl_sec",
    "f0_pat_sec", "paltab_sec", "vsync_n", "audio_bytes",
    "audio_control_bytes", "adpcm_table_sectors", "fps_int",
    "audio_fd", "audio_preload_sec", "features", "pump_mask",
    "wave_pump_mask", "sec_num", "sec_mod", "sec_base", "sec_rem",
)


def render_include(constants: PlayerConstants) -> str:
    """Render a stable GNU assembler include."""
    lines = [
        "/* Generated from HEADER.DAT by tools/player_constants.py. Do not edit. */",
    ]
    for name in INCLUDE_ORDER:
        value = getattr(constants, name)
        width = 8 if value > 0xFFFF or name in {"signature", "font_addr"} else 4
        lines.append(f".equ PC_{name.upper()}, 0x{value:0{width}X}")
    lines.append("")
    return "\n".join(lines)


def generate_include(header_path: Path, output_path: Path) -> PlayerConstants:
    """Generate the include, preserving mtime when its bytes are unchanged."""
    with header_path.open("rb") as src:
        sector = src.read(SECTOR)
    constants = parse_header_sector(sector)
    rendered = render_include(constants)
    if not output_path.exists() or output_path.read_text() != rendered:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered)
    return constants


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("header", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    constants = generate_include(args.header, args.output)
    print(
        f"player_constants: {args.output} signature=0x{constants.signature:08X} "
        f"{constants.tcols}x{constants.trows} {constants.fps_int}fps "
        f"audio={constants.audio_bytes} SP-rate={constants.sec_num}/{constants.sec_mod}")


if __name__ == "__main__":
    main()
