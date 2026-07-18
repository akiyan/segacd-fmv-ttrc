#!/usr/bin/env python3
"""Regression tests for the shared physical payload-RING schedule."""
from __future__ import annotations

import unittest

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


class PayloadRingScheduleTests(unittest.TestCase):
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
        }
        frozen = {
            "stream_schedule": {
                "schema_version": 1,
                "block_lengths": np.array([10, 12]),
                "ring_occupancy": np.array([64, 31]),
                "payload_sectors": np.array([0, 1]),
                "control_sectors": np.array([0, 1]),
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
        }
        frozen = {
            "stream_schedule": {
                "schema_version": 1,
                "block_lengths": np.array([10, 12]),
                "ring_occupancy": np.array([64, 32]),
                "payload_sectors": np.array([0, 1]),
                "control_sectors": np.array([0, 1]),
            }
        }
        self.assertTrue(pack.verify_sim_stream_schedule(frozen, packed))


if __name__ == "__main__":
    unittest.main()
