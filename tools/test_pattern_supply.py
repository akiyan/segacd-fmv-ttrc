#!/usr/bin/env python3

from __future__ import annotations

import unittest

import numpy as np

import pattern_supply as supply
from upgrade_planner import DemandPrediction


class PatternSupplyEncodingTests(unittest.TestCase):
    def test_entry_source_round_trip_and_name_mask(self):
        base = (3 << 13) | 0x321
        for source in (supply.SOURCE_PRG, supply.SOURCE_WR, supply.SOURCE_MAIN):
            entry = supply.encode_entry_source(base, source)
            self.assertEqual(supply.decode_entry_source(entry), source)
            self.assertEqual(entry & supply.NAME_ENTRY_MASK, base)

    def test_run_count_round_trip(self):
        for source in (supply.SOURCE_PRG, supply.SOURCE_WR, supply.SOURCE_MAIN):
            encoded = supply.encode_run_count(175, source)
            self.assertEqual(supply.decode_run_count(encoded), (175, source))


class PatternSupplyPlannerTests(unittest.TestCase):
    def test_frame_budget_uses_parity_banks_then_global_main(self):
        prediction = DemandPrediction(
            exact_bytes=np.array([0, 200, 100, 50]),
            protected_bytes=np.array([0, 200, 100, 50]),
            exact_cold=np.array([0, 4, 4, 4]),
            protected_cold=np.array([0, 4, 4, 4]),
        )
        budget = supply.plan_frame_budgets(
            prediction, wr_patterns=2, main_patterns=2)

        # Wr0 can serve only frame 2.  Wr1 water-fills the much larger frame 1
        # before frame 3, then flexible Main continues reducing frame 1.
        np.testing.assert_array_equal(budget.wr, [0, 2, 2, 0])
        np.testing.assert_array_equal(budget.main, [0, 2, 0, 0])
        self.assertEqual(budget.wr0_patterns, 2)
        self.assertEqual(budget.wr1_patterns, 2)
        self.assertEqual(budget.main_patterns, 2)

    def test_frame_budget_never_exceeds_predicted_cold(self):
        prediction = DemandPrediction(
            exact_bytes=np.array([0, 34, 68]),
            protected_bytes=np.array([0, 34, 0]),
            exact_cold=np.array([0, 1, 2]),
            protected_cold=np.array([0, 1, 0]),
        )
        budget = supply.plan_frame_budgets(
            prediction, wr_patterns=99, main_patterns=99)

        np.testing.assert_array_less(
            budget.total, prediction.exact_cold + 1)
        self.assertEqual(int(budget.total[0]), 0)

    def test_whole_runs_are_assigned_and_pattern_order_is_preserved(self):
        # Frame 1 has one two-pattern run; frame 2 has two one-pattern runs.
        per = [
            ([0], [1], [True]),
            ([0, 1], [1, 2], [True, True]),
            ([0, 1], [1, 3], [True, True]),
        ]
        patterns = [bytes([value]) * 32 for value in range(5)]
        log = {
            "miss": np.array([0, 9, 4]),
            "stream_schedule": {"ring_occupancy": np.array([99, 1, 2])},
        }
        plan = supply.plan_supply(
            log, per, patterns, enabled=True, wr_patterns=1, main_patterns=2)

        self.assertEqual(plan.sources[0], (supply.SOURCE_PRG,))
        self.assertEqual(plan.sources[1], (supply.SOURCE_MAIN, supply.SOURCE_MAIN))
        # The first even run fits Wr0; the second remains Prg.
        self.assertEqual(plan.sources[2], (supply.SOURCE_WR, supply.SOURCE_PRG))
        self.assertEqual(plan.prg_patterns, (patterns[0], patterns[4]))
        self.assertEqual(plan.wr0_patterns, (patterns[3],))
        self.assertEqual(plan.wr1_patterns, ())
        self.assertEqual(plan.main_patterns, (patterns[1], patterns[2]))
        np.testing.assert_array_equal(plan.prg_loads, [1, 0, 1])
        np.testing.assert_array_equal(plan.wr0_loads, [0, 0, 1])
        np.testing.assert_array_equal(plan.wr1_loads, [0, 0, 0])
        np.testing.assert_array_equal(plan.main_loads, [0, 2, 0])

    def test_frozen_sources_are_authoritative_and_may_split_a_run(self):
        per = [
            ([0], [1], [True]),
            ([0, 1, 2], [1, 2, 3], [True, True, True]),
        ]
        patterns = [bytes([value]) * 32 for value in range(4)]
        log = {
            "pattern_supply": {
                "schema_version": 1,
                "sources": [
                    np.array([supply.SOURCE_PRG], np.uint8),
                    np.array([
                        supply.SOURCE_WR,
                        supply.SOURCE_MAIN,
                        supply.SOURCE_PRG,
                    ], np.uint8),
                ],
            },
        }
        plan = supply.plan_supply(
            log, per, patterns, enabled=True, wr_patterns=2, main_patterns=2)

        self.assertEqual(
            plan.sources[1],
            (supply.SOURCE_WR, supply.SOURCE_MAIN, supply.SOURCE_PRG),
        )
        self.assertEqual(plan.prg_patterns, (patterns[0], patterns[3]))
        self.assertEqual(plan.wr1_patterns, (patterns[1],))
        self.assertEqual(plan.main_patterns, (patterns[2],))

    def test_disabled_plan_keeps_every_pattern_in_prg(self):
        per = [([0], [1], [True]), ([0], [2], [True])]
        patterns = [b"a" * 32, b"b" * 32]
        plan = supply.plan_supply({}, per, patterns, enabled=False)
        self.assertFalse(plan.enabled)
        self.assertEqual(plan.prg_patterns, tuple(patterns))
        np.testing.assert_array_equal(plan.prg_loads, [1, 1])


if __name__ == "__main__":
    unittest.main()
