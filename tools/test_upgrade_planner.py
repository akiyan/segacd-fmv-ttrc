"""Tests for whole-movie optional-upgrade reserve planning."""

from __future__ import annotations

import unittest

import numpy as np

import upgrade_planner


class DemandPredictionTests(unittest.TestCase):
    def test_header_seeds_residency_and_reuse_costs_only_name_bytes(self) -> None:
        a = np.array([0, 1, 2, 3], np.uint8)
        b = np.array([4, 5, 6, 7], np.uint8)
        patterns = [
            np.stack([a, b]),
            np.stack([a, a]),
        ]
        palettes = [np.array([0, 0]), np.array([0, 0])]

        demand, _risk = upgrade_planner.predict_update_demands(
            patterns, palettes, vram_tiles=4)

        np.testing.assert_array_equal(demand, [0, 2])

    def test_boot_prefetch_removes_future_pattern_payload_demand(self) -> None:
        a = np.array([0, 1, 2, 3], np.uint8)
        b = np.array([4, 5, 6, 7], np.uint8)
        patterns = [np.stack([a]), np.stack([b])]
        palettes = [np.array([0]), np.array([0])]

        baseline, _risk = upgrade_planner.predict_update_demands(
            patterns, palettes, vram_tiles=2)
        demand, _risk = upgrade_planner.predict_update_demands(
            patterns,
            palettes,
            vram_tiles=2,
            boot_prefetch_requests=((bytes(b), 1),),
        )

        np.testing.assert_array_equal(baseline, [0, 34])
        np.testing.assert_array_equal(demand, [0, 2])

    def test_cold_demand_is_clipped_to_hardware_limit(self) -> None:
        zero = np.zeros(4, np.uint8)
        patterns = [
            np.stack([zero, zero, zero]),
            np.stack([
                np.array([1, 0, 0, 0], np.uint8),
                np.array([2, 0, 0, 0], np.uint8),
                np.array([3, 0, 0, 0], np.uint8),
            ]),
        ]
        palettes = [np.zeros(3, np.int16), np.zeros(3, np.int16)]

        demand, _risk = upgrade_planner.predict_update_demands(
            patterns, palettes, vram_tiles=8, max_cold=2)

        self.assertEqual(int(demand[1]), 3 * 2 + 2 * 32)

    def test_unprotected_changes_do_not_add_risk_demand(self) -> None:
        a = np.array([0, 1, 2, 3], np.uint8)
        b = np.array([4, 5, 6, 7], np.uint8)
        _exact, demand = upgrade_planner.predict_update_demands(
            [np.stack([a]), np.stack([b])],
            [np.array([0]), np.array([0])],
            vram_tiles=4,
            protected_frames=[np.array([True]), np.array([False])],
        )

        np.testing.assert_array_equal(demand, [0, 0])

    def test_detail_exposes_the_cold_counts_behind_byte_demand(self) -> None:
        zero = np.zeros(4, np.uint8)
        one = np.array([1, 0, 0, 0], np.uint8)
        two = np.array([2, 0, 0, 0], np.uint8)
        details = upgrade_planner.predict_update_demand_details(
            [np.stack([zero, zero]), np.stack([one, two])],
            [np.zeros(2, np.int16), np.zeros(2, np.int16)],
            vram_tiles=4,
            max_cold=1,
            protected_frames=[np.ones(2, bool), np.array([True, False])],
        )

        np.testing.assert_array_equal(details.exact_cold, [0, 1])
        np.testing.assert_array_equal(details.protected_cold, [0, 1])
        np.testing.assert_array_equal(details.exact_bytes, [0, 2 * 2 + 32])
        np.testing.assert_array_equal(details.protected_bytes, [0, 2 + 32])


class ReserveCurveTests(unittest.TestCase):
    def test_future_burst_builds_only_the_needed_reserve(self) -> None:
        reserve = upgrade_planner.build_reserve_curve(
            demand=[0, 20, 20, 140, 20],
            supply=50,
            capacity=100,
        )

        np.testing.assert_array_equal(reserve, [30, 60, 90, 0, 0])

    def test_terminal_reserve_is_zero_without_an_end_ramp(self) -> None:
        reserve = upgrade_planner.build_reserve_curve(
            demand=[0, 120, 120],
            supply=50,
            capacity=100,
        )

        np.testing.assert_array_equal(reserve, [100, 70, 0])
        self.assertEqual(int(reserve[-1]), 0)

    def test_light_tail_releases_the_reserve_before_the_end(self) -> None:
        reserve = upgrade_planner.build_reserve_curve(
            demand=[0, 140, 20, 20, 20],
            supply=50,
            capacity=100,
        )

        np.testing.assert_array_equal(reserve, [90, 0, 0, 0, 0])

    def test_spend_limit_preserves_reserve_unless_base_work_already_used_it(self) -> None:
        self.assertEqual(
            upgrade_planner.planned_spend_limit(
                budget_before=100,
                frame_supply=50,
                reserve_after=80,
                already_spent=20,
            ),
            70,
        )
        self.assertEqual(
            upgrade_planner.planned_spend_limit(
                budget_before=100,
                frame_supply=50,
                reserve_after=80,
                already_spent=90,
            ),
            90,
        )

    def test_balanced_plan_spreads_an_unavoidable_two_frame_shortfall(self) -> None:
        plan = upgrade_planner.build_balanced_reserve_plan(
            demand=[0, 120, 120],
            supply=50,
            capacity=100,
        )

        np.testing.assert_array_equal(plan.planned_demand, [0, 100, 100])
        np.testing.assert_array_equal(plan.shortfall, [0, 20, 20])
        np.testing.assert_array_equal(plan.reserve, [100, 50, 0])

    def test_balanced_plan_leaves_a_feasible_burst_unchanged(self) -> None:
        demand = np.array([0, 20, 20, 140, 20], np.int64)
        plan = upgrade_planner.build_balanced_reserve_plan(
            demand=demand,
            supply=50,
            capacity=100,
        )

        np.testing.assert_array_equal(plan.planned_demand, demand)
        np.testing.assert_array_equal(plan.shortfall, np.zeros_like(demand))
        np.testing.assert_array_equal(plan.reserve, [30, 60, 90, 0, 0])

    def test_balanced_plan_handles_separate_overloaded_bursts(self) -> None:
        plan = upgrade_planner.build_balanced_reserve_plan(
            demand=[0, 120, 120, 0, 0, 120, 120],
            supply=50,
            capacity=100,
        )

        self.assertLessEqual(
            upgrade_planner._peak_buffer_draw(
                plan.planned_demand,
                np.full(7, 50, np.int64),
            ),
            100,
        )
        self.assertGreater(int(plan.shortfall[1:3].sum()), 0)
        self.assertGreater(int(plan.shortfall[5:7].sum()), 0)


if __name__ == "__main__":
    unittest.main()
