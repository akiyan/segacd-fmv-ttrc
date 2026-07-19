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
                tank_before=100,
                frame_supply=50,
                reserve_after=80,
                already_spent=20,
            ),
            70,
        )
        self.assertEqual(
            upgrade_planner.planned_spend_limit(
                tank_before=100,
                frame_supply=50,
                reserve_after=80,
                already_spent=90,
            ),
            90,
        )


if __name__ == "__main__":
    unittest.main()
