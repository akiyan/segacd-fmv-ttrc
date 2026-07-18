#!/usr/bin/env python3
"""Regression tests for the TTRC v7 routing byte."""

from __future__ import annotations

import unittest

import ttrc_routing as routing


class RoutingEntryTests(unittest.TestCase):
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

    def test_bad_header_entry_and_padding_are_rejected(self) -> None:
        for table in (
            bytes([8]).ljust(routing.SECTOR_BYTES, b"\0"),
            bytes([0, 0x40]).ljust(routing.SECTOR_BYTES, b"\0"),
            bytes([0]).ljust(routing.SECTOR_BYTES - 1, b"\0") + b"\1",
        ):
            with self.subTest(table=table[:2]), self.assertRaises(ValueError):
                routing.validate_route_table(table, 1, 1)

    def test_wrong_sector_count_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            routing.validate_route_table(bytes(routing.SECTOR_BYTES), 2049, 1)


if __name__ == "__main__":
    unittest.main()
