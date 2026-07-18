from __future__ import annotations

import argparse
import struct
import tempfile
import unittest
from pathlib import Path

import verify


def observations(values: list[int]) -> list[verify.Observation]:
    return [verify.Observation(index, value) for index, value in enumerate(values)]


class IntegerTest(unittest.TestCase):
    def test_decimal_and_hex(self) -> None:
        self.assertEqual(902, verify.integer("902"))
        self.assertEqual(0x0386, verify.integer("0x0386"))

    def test_invalid(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            verify.integer("0386")


class HeaderTest(unittest.TestCase):
    def test_reads_frame_count_and_vsync(self) -> None:
        data = bytearray(2048)
        data[:4] = b"TTRC"
        struct.pack_into(">HH", data, 4, 8, 6576)
        struct.pack_into(">H", data, 52, 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "HEADER.DAT"
            path.write_bytes(data)
            self.assertEqual(
                verify.HeaderInfo(version=8, frame_count=6576, vsync_n=2),
                verify.read_header(path),
            )

    def test_rejects_wrong_magic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "HEADER.DAT"
            path.write_bytes(bytes(2048))
            with self.assertRaises(verify.CadenceError):
                verify.read_header(path)


class CadenceTest(unittest.TestCase):
    def test_exact_two_vblank_cadence(self) -> None:
        source = observations([0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
        report = verify.validate_observations(source, 4, 2)
        self.assertEqual((0, 2, 4, 6, 8), report.first_captures)
        self.assertEqual((2, 2, 2, 2), report.deltas)

    def test_anchor_skips_false_startup_zero(self) -> None:
        source = [verify.Observation(3, 0), verify.Observation(8, 0)]
        source += [
            verify.Observation(capture, frame)
            for capture, frame in (
                (9, 0), (10, 1), (11, 1), (12, 2), (13, 2),
                (14, 3), (15, 3), (16, 4),
            )
        ]
        report = verify.validate_observations(source, 4, 2)
        self.assertEqual(8, report.anchor_capture)

    def test_missing_frame_is_rejected(self) -> None:
        source = observations([0, 0, 1, 1, 2, 2, 3, 3, 5])
        with self.assertRaisesRegex(verify.CadenceError, "missing F0004"):
            verify.validate_observations(source, 5, 2)

    def test_out_of_order_frame_is_rejected(self) -> None:
        source = observations([0, 0, 1, 1, 2, 2, 1, 3, 4])
        with self.assertRaisesRegex(verify.CadenceError, "out-of-order"):
            verify.validate_observations(source, 4, 2, anchor_frames=3)

    def test_late_first_appearance_is_rejected(self) -> None:
        source = observations([0, 0, 1, 1, 2, 2, 3, 3, 3, 4])
        with self.assertRaisesRegex(verify.CadenceError, "late"):
            verify.validate_observations(source, 4, 2)

    def test_early_first_appearance_is_rejected(self) -> None:
        source = observations([0, 0, 1, 1, 2, 2, 3, 4])
        with self.assertRaisesRegex(verify.CadenceError, "early"):
            verify.validate_observations(source, 4, 2)

    def test_partial_scope_ignores_held_and_bad_tail(self) -> None:
        source = observations([0, 0, 1, 1, 2, 2, 3, 3, 3, 2, 9])
        report = verify.validate_observations(source, 3, 2)
        self.assertEqual((0, 2, 4, 6), report.first_captures)

    def test_incomplete_recording_is_rejected(self) -> None:
        source = observations([0, 0, 1, 1, 2, 2, 3, 3])
        with self.assertRaisesRegex(verify.CadenceError, "ended before F0004"):
            verify.validate_observations(source, 4, 2)

    def test_observation_indices_must_increase(self) -> None:
        source = [verify.Observation(0, 0), verify.Observation(0, 1)]
        with self.assertRaises(ValueError):
            verify.find_anchor(source, 2)


if __name__ == "__main__":
    unittest.main()
