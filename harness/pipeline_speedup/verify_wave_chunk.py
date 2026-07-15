#!/usr/bin/env python3
"""Prove the chunked RF5C164 writer matches the old byte loop.

This models the observable write order, pump positions, PCM bank changes and
final write pointer.  It intentionally exercises odd source addresses because
the 68000 MOVE.L fast path must scalar-align those before using MOVEP.L.
"""

from __future__ import annotations

import random


RING_END = 0x8000


def reference(start: int, data: bytes):
    ptr = start
    writes = []
    pumps = []
    banks = [0x80 | (ptr >> 12)] if data else []
    for value in data:
        if ptr & 0xFF == 0:
            pumps.append(ptr)
        writes.append((ptr, value))
        ptr += 1
        if ptr >= RING_END:
            ptr = 0
            banks.append(0x80)
        elif ptr & 0x0FFF == 0:
            banks.append(0x80 | (ptr >> 12))
    return writes, pumps, banks, ptr


def chunked(start: int, data: bytes, source_odd: bool):
    ptr = start
    pos = 0
    source_addr = int(source_odd)
    writes = []
    pumps = []
    banks = [0x80 | (ptr >> 12)] if data else []
    bank = ptr >> 12
    wave_addr = (ptr & 0x0FFF) * 2

    def physical_write(address: int, value: int):
        assert address & 1 == 0, address
        assert 0 <= address < 0x2000, address
        logical = bank * 0x1000 + address // 2
        writes.append((logical, value))

    def scalar():
        nonlocal pos, source_addr, wave_addr
        physical_write(wave_addr, data[pos])
        pos += 1
        source_addr += 1
        wave_addr += 2

    def movep4(displacement: int):
        nonlocal pos, source_addr
        assert source_addr & 1 == 0, source_addr
        for index, value in enumerate(data[pos : pos + 4]):
            physical_write(wave_addr + displacement + index * 2, value)
        pos += 4
        source_addr += 4

    while pos < len(data):
        if ptr & 0xFF == 0:
            pumps.append(ptr)
        count = min(len(data) - pos, 0x100 - (ptr & 0xFF))
        remaining = count
        if source_addr & 1:
            scalar()
            remaining -= 1
        groups16, remaining = divmod(remaining, 16)
        for _ in range(groups16):
            movep4(0)
            movep4(8)
            movep4(16)
            movep4(24)
            wave_addr += 32
        groups4, remaining = divmod(remaining, 4)
        for _ in range(groups4):
            movep4(0)
            wave_addr += 8
        for _ in range(remaining):
            scalar()

        ptr += count
        if ptr & 0x0FFF == 0:
            if ptr >= RING_END:
                ptr = 0
                bank = 0
                banks.append(0x80)
            else:
                bank = ptr >> 12
                banks.append(0x80 | (ptr >> 12))
            wave_addr = 0

        assert source_addr == int(source_odd) + pos
        assert wave_addr == (ptr & 0x0FFF) * 2

    return writes, pumps, banks, ptr


def check(start: int, length: int, source_odd: bool):
    data = bytes((i * 73 + start) & 0xFF for i in range(length))
    expected = reference(start, data)
    actual = chunked(start, data, source_odd)
    assert actual == expected, (start, length, source_odd, expected, actual)


def main():
    starts = [
        0x0000, 0x0001, 0x00FE, 0x00FF, 0x0100,
        0x0FFE, 0x0FFF, 0x1000, 0x7EFE, 0x7EFF, 0x7FFF,
    ]
    lengths = [0, 1, 2, 3, 4, 15, 16, 17, 255, 256, 257, 443, 444, 887]
    for start in starts:
        for length in lengths:
            for source_odd in (False, True):
                check(start, length, source_odd)

    rng = random.Random(0x68000)
    for _ in range(5000):
        check(rng.randrange(RING_END), rng.randrange(1, 1000), bool(rng.randrange(2)))
    print("verify_wave_chunk: OK (boundary matrix + 5000 deterministic random cases)")


if __name__ == "__main__":
    main()
