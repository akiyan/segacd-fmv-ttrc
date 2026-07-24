#!/usr/bin/env python3
"""Regression tests for the shared physical payload-RING schedule."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

import numpy as np

import pack_stream as pack
import shadow_updates
import stream_schedule as schedule
import ttrc_routing


class ControlLengthTests(unittest.TestCase):
    def test_player_cold_run_path_matches_rate_and_pattern_supply(self) -> None:
        self.assertFalse(ttrc_routing.player_uses_packed_cold_runs(15, 0))
        self.assertFalse(ttrc_routing.player_uses_packed_cold_runs(20, 0))
        self.assertTrue(ttrc_routing.player_uses_packed_cold_runs(24, 0))
        self.assertTrue(ttrc_routing.player_uses_packed_cold_runs(30, 0))
        self.assertTrue(ttrc_routing.player_uses_packed_cold_runs(
            15, ttrc_routing.FEATURE_PATTERN_SUPPLY))

    def test_lengths_mix_bitmap_and_shadow_lists(self) -> None:
        lengths = schedule.control_block_lengths(
            np.array([3, 3]), np.array([0, 0]), cells=1120,
            audio_frame_bytes=372,
            update_lists=np.array([False, True]))
        # The list replaces a 140-byte bitmap plus six entry bytes with 12B.
        self.assertEqual(int(lengths[0] - lengths[1]), 134)

    def test_shadow_update_count_tag_roundtrip(self) -> None:
        raw = shadow_updates.encode_count(1120, True)
        self.assertEqual(shadow_updates.decode_count(raw), (1120, True))
        self.assertEqual(
            shadow_updates.decode_count(shadow_updates.encode_count(1120, False)),
            (1120, False))

    def test_shadow_list_selection_preserves_schedule_margins(self) -> None:
        cells = [[], [0], list(range(64)), [1119], list(range(256))]
        plan = schedule.select_shadow_update_lists(
            cells, np.zeros(5, np.int64), np.zeros(5, np.int64),
            cells=1120, fps=30, ring_capacity_patterns=64,
            frame_sectors=3, audio_frame_bytes=16, fill=True)
        self.assertFalse(bool(plan["selected"][0]))
        self.assertGreaterEqual(
            int(plan["schedule"]["ring_min"]),
            int(plan["baseline_schedule"]["ring_min"]))
        self.assertGreaterEqual(
            int(plan["schedule"]["ready_min"]),
            int(plan["baseline_schedule"]["ready_min"]))
        self.assertFalse(plan["control_growth_enabled"])
        self.assertTrue(all(
            cost.added_bytes <= 0
            for cost, selected in zip(plan["costs"], plan["selected"], strict=True)
            if selected))

    def test_lengths_match_the_packed_layout_formula(self) -> None:
        lengths = schedule.control_block_lengths(
            [0, 17], [0, 3], cells=1120, audio_frame_bytes=888)
        self.assertEqual(lengths.tolist(), [1038, 1084])

    def test_body_supply_reserves_fixed_control_before_variable_work(self) -> None:
        supply = schedule.body_fresh_byte_supply(
            5, 30,
            cells=1120,
            audio_frame_bytes=736,
        )
        self.assertEqual(supply["gross"].tolist(), [0, 4096, 6144, 4096, 6144])
        self.assertEqual(supply["fixed_control"].tolist(), [0, 886, 886, 886, 886])
        self.assertEqual(supply["variable"].tolist(), [0, 3210, 5258, 3210, 5258])

    def test_funded_work_includes_all_control_and_prg_patterns(self) -> None:
        useful = schedule.body_funded_work_bytes(
            [0, 10], [0, 4], [0, 2],
            cells=1120,
            audio_frame_bytes=736,
        )
        self.assertEqual(useful.tolist(), [0, 1222])

    def test_run_control_reservation_uses_cold_cap_or_active_tiles(self) -> None:
        self.assertEqual(schedule.max_run_control_reservation(178, 1120), 712)
        self.assertEqual(schedule.max_run_control_reservation(0, 1120), 4480)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            schedule.max_run_control_reservation(-1, 1120)


class BodyDeliveryRateTests(unittest.TestCase):
    def test_15fps_fixed_n4_includes_one_six_sector_slot_per_cycle(self) -> None:
        deltas = schedule.rate_deltas(201, 15)[1:]
        self.assertEqual(int(deltas.sum()), 1001)
        self.assertEqual(int(np.count_nonzero(deltas == 5)), 199)
        self.assertEqual(int(np.count_nonzero(deltas == 6)), 1)

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

    def test_payload_class_split_skips_prebuffer_and_preserves_delivery(self) -> None:
        raw, prg = schedule.split_body_payload_classes(
            [True, False, True, False, True],
            [64, 64],
            prebuffer_patterns=1,
        )
        self.assertEqual(raw.tolist(), [32, 32])
        self.assertEqual(prg.tolist(), [32, 32])
        np.testing.assert_array_equal(raw + prg, [64, 64])


class PayloadRingScheduleTests(unittest.TestCase):
    def test_useful_body_trace_excludes_header_and_all_padding(self) -> None:
        result = schedule.schedule_payload_ring(
            [64, 64, 64, 1],
            [100, 2050, 10, 0],
            fps=15,
            ring_capacity_patterns=128,
            prebuffer_capacity_patterns=64,
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
        loads = np.asarray([64, 64, 64, 1], np.int64)
        np.testing.assert_array_equal(
            result["ring_occupancy_before_consume"],
            result["ring_occupancy"]
            + np.asarray([0, *loads[1:]], np.int64),
        )
        self.assertEqual(
            result["ring_peak"],
            int(result["ring_occupancy_before_consume"].max()),
        )

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

    def test_terminal_drain_is_excluded_only_from_comparison_minimum(self) -> None:
        result = schedule.schedule_payload_ring(
            [0, 96, 96, 96, 96, 96, 96, 96, 96, 0, 0],
            [0] * 11,
            fps=30,
            ring_capacity_patterns=256,
            frame_sectors=3,
            fill=True,
        )
        end = int(result["evaluation_end_frame"])
        self.assertLess(end, len(result["ring_occupancy"]))
        self.assertEqual(
            int(result["ring_min_evaluation"]),
            int(result["ring_occupancy"][1:end].min()))
        self.assertLessEqual(
            int(result["ring_min"]), int(result["ring_min_evaluation"]))

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

    def test_runtime_delivery_may_use_reserved_jitter_above_prebuffer(self) -> None:
        result = schedule.schedule_payload_ring(
            [0, 32, 64, 64, 64, 64, 64, 64, 0],
            [0] * 9,
            fps=15,
            ring_capacity_patterns=160,
            prebuffer_capacity_patterns=128,
            frame_sectors=5,
            fill=True,
        )
        self.assertTrue(result["feasible"])
        self.assertEqual(result["prebuf_pat"], 128)
        self.assertEqual(result["ring_peak"], 160)
        self.assertEqual(result["ring_jitter_peak"], 32)
        self.assertEqual(result["prebuffer_capacity_patterns"], 128)
        self.assertEqual(result["ring_capacity_patterns"], 160)

    def test_payload_failure_identifies_the_causal_deadline(self) -> None:
        with self.assertRaises(schedule.ScheduleError) as caught:
            schedule.schedule_payload_ring(
                [0, 112, 112, 112],
                [0, 0, 0, 0],
                fps=15,
                ring_capacity_patterns=128,
                frame_sectors=5,
                fill=True,
            )
        self.assertEqual(caught.exception.kind, "payload_capacity")
        self.assertEqual(caught.exception.details["failure_frame"], 1)
        self.assertEqual(caught.exception.details["origin_frame"], 2)
        self.assertEqual(caught.exception.details["patterns_to_remove"], 96)

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
                ring_capacity_patterns=pack.RING_DELIVERY_CAP_PAT,
                prebuffer_capacity_patterns=pack.RING_CAP_PAT,
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
            "ring_occupancy_before_consume": np.array([64, 64]),
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
                "ring_occupancy_before_consume": np.array([64, 64]),
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
            "ring_occupancy_before_consume": np.array([64, 64]),
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
                "ring_occupancy_before_consume": np.array([64, 64]),
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
