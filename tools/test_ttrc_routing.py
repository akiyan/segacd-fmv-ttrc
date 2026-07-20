#!/usr/bin/env python3
"""Regression tests for the TTRC v7+ routing byte retained by v10."""

from __future__ import annotations

import unittest

import ttrc_routing as routing


class RoutingEntryTests(unittest.TestCase):
    def test_v10_feature_bits_are_stable(self) -> None:
        self.assertEqual(routing.FEATURE_COLD_RUNS, 0x0001)
        self.assertEqual(routing.FEATURE_FIXED_N2, 0x0002)
        self.assertEqual(routing.FEATURE_ADPCM22, 0x0004)
        self.assertEqual(routing.FEATURE_PATTERN_SUPPLY, 0x0008)

    def test_all_valid_pairs_round_trip(self) -> None:
        seen = set()
        for total in range(routing.FRAME_SECTORS + 1):
            for ctrl in range(total + 1):
                pay = total - ctrl
                entry = routing.encode_route(pay, ctrl)
                self.assertEqual(routing.decode_route(entry), (pay, ctrl, total))
                seen.add(entry)
        self.assertEqual(len(seen), 21)

    def test_invalid_pairs_are_rejected(self) -> None:
        for pair in ((-1, 0), (0, -1), (6, 0), (3, 3), (1.5, 0)):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                routing.encode_route(*pair)

    def test_invalid_bytes_are_rejected(self) -> None:
        for entry in (0x06, 0x07, 0x30, 0x38, 0x40, 0x80, 0x100, -1):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                routing.decode_route(entry)

    def test_all_byte_values_match_the_v7_plus_contract(self) -> None:
        expected = {
            routing.encode_route(total - ctrl, ctrl): (total - ctrl, ctrl, total)
            for total in range(routing.FRAME_SECTORS + 1)
            for ctrl in range(total + 1)
        }
        for entry in range(256):
            with self.subTest(entry=f"0x{entry:02X}"):
                if entry in expected:
                    self.assertEqual(routing.decode_route(entry), expected[entry])
                else:
                    with self.assertRaises(ValueError):
                        routing.decode_route(entry)


class RoutingTableTests(unittest.TestCase):
    def test_frame_and_sector_boundaries(self) -> None:
        self.assertEqual(routing.routing_sector_count(1), 1)
        self.assertEqual(routing.routing_sector_count(2048), 1)
        self.assertEqual(routing.routing_sector_count(2049), 2)
        self.assertEqual(routing.routing_sector_count(16384), 8)
        for count in (0, 16385):
            with self.subTest(count=count), self.assertRaises(ValueError):
                routing.routing_sector_count(count)

    def test_valid_padded_table(self) -> None:
        entries = bytes([0, routing.encode_route(2, 1), routing.encode_route(0, 2)])
        table = entries.ljust(routing.SECTOR_BYTES, b"\0")
        routing.validate_route_table(table, len(entries), 1)

    def test_boundary_last_entry_and_padding(self) -> None:
        sentinel = routing.encode_route(4, 1)
        for nframes, expected_sectors in ((2048, 1), (2049, 2), (16384, 8)):
            with self.subTest(nframes=nframes):
                table = bytearray(expected_sectors * routing.SECTOR_BYTES)
                table[nframes - 1] = sentinel
                routing.validate_route_table(table, nframes, expected_sectors)
                self.assertEqual(table[nframes - 1], sentinel)
                self.assertFalse(any(table[nframes:]))
                if nframes < len(table):
                    table[nframes] = sentinel
                    with self.assertRaises(ValueError):
                        routing.validate_route_table(table, nframes, expected_sectors)

    def test_bad_header_entry_and_padding_are_rejected(self) -> None:
        for table, nframes in (
            (bytes([8]).ljust(routing.SECTOR_BYTES, b"\0"), 1),
            (bytes([0, 0x40]).ljust(routing.SECTOR_BYTES, b"\0"), 2),
            (bytes([0]).ljust(routing.SECTOR_BYTES - 1, b"\0") + b"\1", 1),
        ):
            with self.subTest(table=table[:2], nframes=nframes), self.assertRaises(ValueError):
                routing.validate_route_table(table, nframes, 1)

    def test_wrong_sector_count_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            routing.validate_route_table(bytes(routing.SECTOR_BYTES), 2049, 1)


if __name__ == "__main__":
    unittest.main()
