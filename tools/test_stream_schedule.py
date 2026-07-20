#!/usr/bin/env python3
"""Regression tests for the shared physical payload-RING schedule."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

import numpy as np

import pack_stream as pack
import stream_schedule as schedule


class ControlLengthTests(unittest.TestCase):
    def test_lengths_match_the_packed_layout_formula(self) -> None:
        lengths = schedule.control_block_lengths(
            [0, 17], [0, 3], cells=1120, audio_frame_bytes=888, debug=False)
        self.assertEqual(lengths.tolist(), [1038, 1084])

    def test_debug_block_is_included(self) -> None:
        normal = schedule.control_block_lengths(
            [4], [2], cells=576, audio_frame_bytes=444, debug=False)
        debug = schedule.control_block_lengths(
            [4], [2], cells=576, audio_frame_bytes=444, debug=True)
        self.assertEqual(int(debug[0] - normal[0]), pack.DBG_LEN)

    def test_body_supply_reserves_fixed_control_before_variable_work(self) -> None:
        supply = schedule.body_fresh_byte_supply(
            5, 30,
            cells=1120,
            audio_frame_bytes=736,
            debug=True,
        )
        self.assertEqual(supply["gross"].tolist(), [0, 4096, 6144, 4096, 6144])
        self.assertEqual(supply["fixed_control"].tolist(), [0, 908, 908, 908, 908])
        self.assertEqual(supply["variable"].tolist(), [0, 3188, 5236, 3188, 5236])

    def test_funded_work_includes_all_control_and_prg_patterns(self) -> None:
        useful = schedule.body_funded_work_bytes(
            [0, 10], [0, 4], [0, 2],
            cells=1120,
            audio_frame_bytes=736,
            debug=True,
        )
        self.assertEqual(useful.tolist(), [0, 1244])

    def test_run_control_reservation_uses_cold_cap_or_active_tiles(self) -> None:
        self.assertEqual(schedule.max_run_control_reservation(178, 1120), 712)
        self.assertEqual(schedule.max_run_control_reservation(0, 1120), 4480)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            schedule.max_run_control_reservation(-1, 1120)


class BodyDeliveryRateTests(unittest.TestCase):
    def test_rate_uses_each_slots_physical_cd_time(self) -> None:
        rate = schedule.body_delivery_rate_bps(
            [0, 2048, 1024, 3072], [0, 2048, 2048, 4096])
        self.assertEqual(rate.tolist(), [0, 153600, 76800, 115200])
        self.assertLessEqual(int(rate.max()), schedule.CD_BYTES_PER_SECOND)

    def test_average_is_weighted_by_total_physical_read_time(self) -> None:
        average = schedule.average_body_delivery_rate_bps(
            [2048, 2048], [2048, 6144])
        self.assertEqual(average, 76800.0)

    def test_useful_bytes_cannot_exceed_physical_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "within physical"):
            schedule.body_delivery_rate_bps([2049], [2048])


class PayloadRingScheduleTests(unittest.TestCase):
    def test_useful_body_trace_excludes_header_and_all_padding(self) -> None:
        result = schedule.schedule_payload_ring(
            [64, 64, 64, 1],
            [100, 2050, 10, 0],
            fps=15,
            ring_capacity_patterns=64,
            frame_sectors=5,
            fill=True,
        )
        useful_control = result["body_useful_control_bytes"]
        useful_payload = result["body_useful_payload_bytes"]
        pad = result["body_pad_bytes"]
        physical = result["body_physical_bytes"]
        self.assertEqual(int(useful_control[0]), 0)
        self.assertEqual(int(useful_payload[0]), 0)
        self.assertEqual(int(useful_control.sum()), 2060)
        self.assertEqual(int(useful_payload.sum()), 65 * 32)
        np.testing.assert_array_equal(
            useful_control + useful_payload + pad, physical)

    def test_continuous_control_bytes_are_counted_in_delivery_slot(self) -> None:
        trace = schedule.useful_body_delivery_trace(
            [0, 0, 0], [0, 1, 1], [0, 5, 5],
            body_payload_bytes=0,
            body_control_bytes=2050,
        )
        self.assertEqual(
            trace["body_useful_control_bytes"].tolist(), [0, 2048, 2])
        with self.assertRaisesRegex(schedule.ScheduleError, "omitted 2"):
            schedule.useful_body_delivery_trace(
                [0, 0, 0], [0, 1, 0], [0, 5, 5],
                body_payload_bytes=0,
                body_control_bytes=2050,
            )

    def test_terminal_frames_do_not_refill_after_payload_exhaustion(self) -> None:
        result = schedule.schedule_payload_ring(
            [0, 64, 64, 0, 0],
            [0, 0, 0, 0, 0],
            fps=15,
            ring_capacity_patterns=128,
            frame_sectors=5,
            fill=True,
        )
        self.assertEqual(result["ring_occupancy"].tolist(), [128, 64, 0, 0, 0])
        self.assertEqual(result["n_pay_sec"].tolist(), [0, 0, 0, 0, 0])
        self.assertTrue(result["feasible"])

    def test_last_sector_padding_is_real_ring_occupancy(self) -> None:
        result = schedule.schedule_payload_ring(
            [0, 10, 0],
            [0, 0, 0],
            fps=15,
            ring_capacity_patterns=128,
            frame_sectors=5,
            fill=True,
        )
        self.assertEqual(result["ring_occupancy"].tolist(), [64, 54, 54])

    def test_pack_wrapper_uses_the_shared_schedule_exactly(self) -> None:
        old_fps, old_fill = pack.FPS, pack.PACK_FILL
        try:
            pack.FPS = 15
            pack.PACK_FILL = True
            loads = np.array([0, 64, 64, 0, 0], np.int64)
            blocks = [b""] * len(loads)
            wrapped = pack.schedule([None] * len(loads), loads, blocks)
            direct = schedule.schedule_payload_ring(
                loads,
                np.zeros(len(loads), np.int64),
                fps=pack.FPS,
                ring_capacity_patterns=pack.RING_CAP_PAT,
                frame_sectors=pack.FRAME_SECTORS,
                fill=pack.PACK_FILL,
            )
            for key in ("n_pay_sec", "n_ctrl_sec", "ring_occupancy", "fsec"):
                np.testing.assert_array_equal(wrapped[key], direct[key])
        finally:
            pack.FPS, pack.PACK_FILL = old_fps, old_fill

    def test_pack_rejects_a_frozen_ring_trace_mismatch(self) -> None:
        packed = {
            "blk_len": np.array([10, 12]),
            "ring_occupancy": np.array([64, 32]),
            "n_pay_sec": np.array([0, 1]),
            "n_ctrl_sec": np.array([0, 1]),
            "body_useful_payload_bytes": np.array([0, 32]),
            "body_useful_control_bytes": np.array([0, 12]),
            "body_pad_bytes": np.array([0, 4052]),
            "body_physical_bytes": np.array([0, 4096]),
        }
        frozen = {
            "stream_schedule": {
                "schema_version": schedule.STREAM_SCHEDULE_SCHEMA_VERSION,
                "block_lengths": np.array([10, 12]),
                "ring_occupancy": np.array([64, 31]),
                "payload_sectors": np.array([0, 1]),
                "control_sectors": np.array([0, 1]),
                "body_useful_payload_bytes": np.array([0, 32]),
                "body_useful_control_bytes": np.array([0, 12]),
                "body_pad_bytes": np.array([0, 4052]),
                "body_physical_bytes": np.array([0, 4096]),
            }
        }
        with self.assertRaisesRegex(SystemExit, "ring_occupancy mismatch at frame 1"):
            pack.verify_sim_stream_schedule(frozen, packed)

    def test_pack_accepts_an_exact_frozen_ring_trace(self) -> None:
        packed = {
            "blk_len": np.array([10, 12]),
            "ring_occupancy": np.array([64, 32]),
            "n_pay_sec": np.array([0, 1]),
            "n_ctrl_sec": np.array([0, 1]),
            "body_useful_payload_bytes": np.array([0, 32]),
            "body_useful_control_bytes": np.array([0, 12]),
            "body_pad_bytes": np.array([0, 4052]),
            "body_physical_bytes": np.array([0, 4096]),
        }
        frozen = {
            "stream_schedule": {
                "schema_version": schedule.STREAM_SCHEDULE_SCHEMA_VERSION,
                "block_lengths": np.array([10, 12]),
                "ring_occupancy": np.array([64, 32]),
                "payload_sectors": np.array([0, 1]),
                "control_sectors": np.array([0, 1]),
                "body_useful_payload_bytes": np.array([0, 32]),
                "body_useful_control_bytes": np.array([0, 12]),
                "body_pad_bytes": np.array([0, 4052]),
                "body_physical_bytes": np.array([0, 4096]),
            }
        }
        self.assertTrue(pack.verify_sim_stream_schedule(frozen, packed))

    def test_pack_requires_the_current_body_trace_schema(self) -> None:
        with self.assertRaisesRegex(SystemExit, "re-run sim"):
            pack.verify_sim_stream_schedule(
                {"stream_schedule": {"schema_version": 1}}, {})

    def test_packer_verifies_written_body_slots_and_padding(self) -> None:
        packed = {
            "n_pay_sec": np.array([0, 1]),
            "n_ctrl_sec": np.array([0, 1]),
            "fsec": np.array([0, 3]),
            "body_useful_payload_bytes": np.array([0, 32]),
            "body_useful_control_bytes": np.array([0, 3]),
            "body_pad_bytes": np.array([0, 3 * 2048 - 35]),
        }
        control = b"abc"
        payload = b"p" * 32
        slot = (
            control.ljust(2048, b"\0")
            + payload.ljust(2048, b"\0")
            + bytes(2048)
        )
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "BODY.DAT"
            body.write_bytes(slot)
            pack.verify_body_delivery_file(
                body, control, payload, packed, prebuf_patterns=0)
            body.write_bytes(slot[:-1] + b"x")
            with self.assertRaisesRegex(AssertionError, "rate-match pad"):
                pack.verify_body_delivery_file(
                    body, control, payload, packed, prebuf_patterns=0)


if __name__ == "__main__":
    unittest.main()
