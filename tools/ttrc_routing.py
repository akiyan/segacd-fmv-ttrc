"""TTRC v7+ one-byte routing entry codec and current stream version."""

from __future__ import annotations

import operator


VERSION = 14
FEATURE_COLD_RUNS = 0x0001
FEATURE_FIXED_N2 = 0x0002
FEATURE_ADPCM22 = 0x0004
FEATURE_PATTERN_SUPPLY = 0x0008
FEATURE_SHADOW_UPDATE_LISTS = 0x0010
FEATURE_VRAM_RAW_PREFETCH = 0x0020
FEATURE_DICBUF_INDEXED_RUNS = 0x0040
FEATURE_BOOT_VRAM_SIDECAR = 0x0080
SECTOR_BYTES = 2048
ROUTE_BYTES = 16 * 1024
# Compatibility name for callers that describe the allocation as a table.
TABLE_BYTES = ROUTE_BYTES
ENTRY_BYTES = 1
MAX_FRAMES = ROUTE_BYTES // ENTRY_BYTES
MAX_TABLE_SECTORS = ROUTE_BYTES // SECTOR_BYTES
FRAME_SECTORS = 5
CTRL_MASK = 0x07
TOTAL_SHIFT = 3
MAX_ENTRY = (FRAME_SECTORS << TOTAL_SHIFT) | FRAME_SECTORS


def player_uses_packed_cold_runs(fps: float, features: int) -> bool:
    """Return whether the Sub player consumes the packed cold-run suffix.

    Dense 24/30fps streams use the suffix directly. Multi-source pattern
    supply also requires it at every rate. A lower-rate plain-Prg stream keeps
    the legacy entry-order walker so its frequent CDC polling remains intact.
    """
    return (
        float(fps) >= 24.0
        or bool(operator.index(features) & FEATURE_PATTERN_SUPPLY)
    )


def _index(value: object, name: str) -> int:
    try:
        return operator.index(value)
    except TypeError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def encode_route(n_pay: object, n_ctrl: object) -> int:
    """Encode one valid ``(payload sectors, control sectors)`` pair."""
    pay = _index(n_pay, "n_pay")
    ctrl = _index(n_ctrl, "n_ctrl")
    total = pay + ctrl
    if pay < 0 or ctrl < 0:
        raise ValueError(f"routing counts must be non-negative: pay={pay}, ctrl={ctrl}")
    if total > FRAME_SECTORS:
        raise ValueError(
            f"routing total {total} exceeds FRAME_SECTORS={FRAME_SECTORS}: "
            f"pay={pay}, ctrl={ctrl}")
    return (total << TOTAL_SHIFT) | ctrl


def decode_route(entry: object) -> tuple[int, int, int]:
    """Decode and validate one packed entry as ``(pay, ctrl, total)``."""
    value = _index(entry, "routing entry")
    if not 0 <= value <= 0xFF:
        raise ValueError(f"routing entry is outside one byte: {value}")
    if value & 0xC0:
        raise ValueError(f"routing entry uses reserved bits: 0x{value:02X}")
    ctrl = value & CTRL_MASK
    total = (value >> TOTAL_SHIFT) & CTRL_MASK
    if total > FRAME_SECTORS:
        raise ValueError(
            f"routing total {total} exceeds FRAME_SECTORS={FRAME_SECTORS}: "
            f"0x{value:02X}")
    if ctrl > total:
        raise ValueError(f"routing control {ctrl} exceeds total {total}: 0x{value:02X}")
    return total - ctrl, ctrl, total


def routing_sector_count(nframes: object) -> int:
    """Return the exact packed table sector count for a valid frame count."""
    count = _index(nframes, "nframes")
    if not 1 <= count <= MAX_FRAMES:
        raise ValueError(f"nframes must be 1..{MAX_FRAMES}, got {count}")
    return (count + SECTOR_BYTES - 1) // SECTOR_BYTES


def validate_route_table(
    table: bytes | bytearray | memoryview,
    nframes: object,
    routing_sec: object,
) -> None:
    """Validate the complete sector-padded packed routing region."""
    count = _index(nframes, "nframes")
    expected_sec = routing_sector_count(count)
    sectors = _index(routing_sec, "routing_sec")
    if sectors != expected_sec:
        raise ValueError(
            f"routing_sec={sectors} does not match {count} frames ({expected_sec})")
    raw = bytes(table)
    expected_bytes = sectors * SECTOR_BYTES
    if len(raw) != expected_bytes:
        raise ValueError(
            f"routing region has {len(raw)} bytes, expected {expected_bytes}")
    if raw[0] != 0:
        raise ValueError(f"frame 0 routing entry must be zero, got 0x{raw[0]:02X}")
    for frame, entry in enumerate(raw[:count]):
        try:
            decode_route(entry)
        except ValueError as exc:
            raise ValueError(f"invalid routing entry at frame {frame}: {exc}") from exc
    if any(raw[count:]):
        raise ValueError("routing sector padding must be zero")
