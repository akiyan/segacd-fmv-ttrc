#!/usr/bin/env python3
"""Prove the DMA-command and short-run transfer transformations independently."""

from __future__ import annotations

import random


ADDRESS_MASK = 0xFFFF
VRAM_ADDRESS_MASK = 0x3FFF
VRAM_WRITE = 0x4000
CD5_DMA = 0x0080
TILE_WORDS = 16
RANDOM_SEED = 0xD4A5EED
RANDOM_CASES_PER_SIZE = 2048


def legacy_control_words(destination: int, *, dma: bool) -> tuple[int, int]:
    """Return the two control-port words emitted by the former code path."""
    destination &= ADDRESS_MASK
    first = VRAM_WRITE | (destination & VRAM_ADDRESS_MASK)
    second = (destination >> 14) & 0x0003
    if dma:
        second |= CD5_DMA
    return first, second


def ordinary_command_long(destination: int) -> int:
    """Build the reusable non-DMA VRAM-write command as one longword."""
    first, second = legacy_control_words(destination, dma=False)
    return (first << 16) | second


def dma_command_long(destination: int) -> int:
    """Set CD5 in the low word, which is the second 68000 bus write."""
    return ordinary_command_long(destination) | CD5_DMA


def longword_bus_order(value: int) -> tuple[int, int]:
    """Model a big-endian 68000 MOVE.L as high word followed by low word."""
    return (value >> 16) & 0xFFFF, value & 0xFFFF


def verify_commands() -> None:
    for destination in range(0x10000):
        legacy_repair = legacy_control_words(destination, dma=False)
        legacy_dma = legacy_control_words(destination, dma=True)
        repair_bus = longword_bus_order(ordinary_command_long(destination))
        dma_bus = longword_bus_order(dma_command_long(destination))

        if repair_bus != legacy_repair:
            raise AssertionError(
                f"destination 0x{destination:04X}: repair command "
                f"{repair_bus!r} != {legacy_repair!r}"
            )
        if dma_bus != legacy_dma:
            raise AssertionError(
                f"destination 0x{destination:04X}: DMA command "
                f"{dma_bus!r} != {legacy_dma!r}"
            )
        if dma_bus[0] != repair_bus[0]:
            raise AssertionError(
                f"destination 0x{destination:04X}: CD5 changed the first word"
            )
        if dma_bus[1] != (repair_bus[1] | CD5_DMA):
            raise AssertionError(
                f"destination 0x{destination:04X}: CD5 is not in the second word"
            )


def word_ram_dma_and_repair(
    initial_vram: list[int], destination: int, source: list[int]
) -> list[int]:
    """Model src+2/full-length Word-RAM DMA followed by destination-word-0 repair."""
    if not source:
        raise ValueError("the DMA model requires at least one source word")
    result = initial_vram.copy()

    # The programmed source is source+2.  The Word-RAM DMA quirk skips the
    # first destination word and reduces the effective transfer by one word,
    # so source[1:] lands at destination+1 in its original order.
    programmed_source = source[1:]
    effective_destination = destination + 1
    effective_length = len(source) - 1
    result[
        effective_destination : effective_destination + effective_length
    ] = programmed_source[:effective_length]

    # The Main CPU restores the one word that the DMA did not write.
    result[destination] = source[0]
    return result


def cpu_direct_longwrites(
    initial_vram: list[int], destination: int, source: list[int]
) -> list[int]:
    """Model the one/two-tile MOVE.L stream to the VDP data port."""
    if len(source) not in (TILE_WORDS, 2 * TILE_WORDS):
        raise ValueError("the CPU-direct model accepts exactly one or two tiles")
    result = initial_vram.copy()
    cursor = destination
    for index in range(0, len(source), 2):
        register = (source[index] << 16) | source[index + 1]
        high_word, low_word = longword_bus_order(register)
        result[cursor] = high_word
        result[cursor + 1] = low_word
        cursor += 2
    return result


def expected_write(
    initial_vram: list[int], destination: int, source: list[int]
) -> list[int]:
    result = initial_vram.copy()
    result[destination : destination + len(source)] = source
    return result


def verify_transfer_case(
    initial_vram: list[int], destination: int, source: list[int]
) -> None:
    expected = expected_write(initial_vram, destination, source)
    dma = word_ram_dma_and_repair(initial_vram, destination, source)
    direct = cpu_direct_longwrites(initial_vram, destination, source)
    if dma != expected:
        raise AssertionError("Word-RAM DMA plus repair changed the word sequence")
    if direct != expected:
        raise AssertionError("CPU direct long writes changed the word sequence")
    if dma != direct:
        raise AssertionError("DMA and CPU-direct paths produced different VRAM")


def verify_transfers() -> int:
    cases = 0
    for word_count in (TILE_WORDS, 2 * TILE_WORDS):
        fixed_sources = (
            [0x0000] * word_count,
            [0xFFFF] * word_count,
            [index & 0xFFFF for index in range(word_count)],
            [0xAAAA if index & 1 else 0x5555 for index in range(word_count)],
        )
        for destination in (0, 1, 7):
            for source in fixed_sources:
                size = destination + word_count + 9
                initial = [((index * 0x1F31) + 0x2468) & 0xFFFF for index in range(size)]
                verify_transfer_case(initial, destination, source)
                cases += 1

    rng = random.Random(RANDOM_SEED)
    for word_count in (TILE_WORDS, 2 * TILE_WORDS):
        for _ in range(RANDOM_CASES_PER_SIZE):
            destination = rng.randrange(0, 9)
            suffix_words = rng.randrange(1, 10)
            size = destination + word_count + suffix_words
            initial = [rng.randrange(0x10000) for _ in range(size)]
            source = [rng.randrange(0x10000) for _ in range(word_count)]
            verify_transfer_case(initial, destination, source)
            cases += 1
    return cases


def main() -> None:
    verify_commands()
    transfer_cases = verify_transfers()
    print(
        "DMA run fast-path proof: OK "
        f"(65536 destinations; {transfer_cases} one/two-tile vectors)"
    )


if __name__ == "__main__":
    main()
