#!/usr/bin/env python3

from __future__ import annotations

import unittest

import numpy as np

import pattern_supply as supply
from upgrade_planner import DemandPrediction


class PatternSupplyEncodingTests(unittest.TestCase):
    def test_entry_source_round_trip_and_name_mask(self):
        base = (3 << 13) | 0x321
        for source in (supply.SOURCE_PRG, supply.SOURCE_WR, supply.SOURCE_DIC):
            entry = supply.encode_entry_source(base, source)
            self.assertEqual(supply.decode_entry_source(entry), source)
            self.assertEqual(entry & supply.NAME_ENTRY_MASK, base)

    def test_run_count_round_trip(self):
        for source in (supply.SOURCE_PRG, supply.SOURCE_WR, supply.SOURCE_DIC):
            encoded = supply.encode_run_count(175, source)
            self.assertEqual(supply.decode_run_count(encoded), (175, source))

    def test_indexed_dic_run_descriptor_round_trip(self):
        for index, count in ((0, 1), (17, 3), (250, 6)):
            words = supply.encode_run_descriptor(
                1119, count, supply.SOURCE_DIC, index)
            self.assertEqual(
                supply.decode_run_descriptor(*words),
                (1119, count, supply.SOURCE_DIC, index),
            )
        words = supply.encode_run_descriptor(12, 175, supply.SOURCE_PRG)
        self.assertEqual(
            supply.decode_run_descriptor(*words),
            (12, 175, supply.SOURCE_PRG, 0),
        )


class PatternSupplyPlannerTests(unittest.TestCase):
    def test_dicbuf_is_selected_first_and_hits_persist(self):
        key_a = bytes([1] * 64)
        key_b = bytes([2] * 64)
        prediction = DemandPrediction(
            exact_bytes=np.array([0, 64, 32, 32]),
            protected_bytes=np.array([0, 32, 32, 0]),
            exact_cold=np.array([0, 2, 1, 1]),
            protected_cold=np.array([0, 1, 1, 0]),
            cold_keys=((), (key_a, key_b), (key_a,), (key_a,)),
            protected_keys=((), (key_b,), (key_a,), ()),
        )
        budget = supply.plan_frame_budgets(
            prediction, wr_patterns=0, dic_patterns=1)
        self.assertEqual(budget.dic_dictionary, (key_a,))
        np.testing.assert_array_equal(budget.dic, [0, 1, 1, 1])
        self.assertEqual(budget.dic_patterns, 1)

    def test_frame_budget_uses_parity_banks_then_global_dic(self):
        prediction = DemandPrediction(
            exact_bytes=np.array([0, 200, 100, 50]),
            protected_bytes=np.array([0, 200, 100, 50]),
            exact_cold=np.array([0, 4, 4, 4]),
            protected_cold=np.array([0, 4, 4, 4]),
        )
        budget = supply.plan_frame_budgets(
            prediction, wr_patterns=2, dic_patterns=2)

        # Wr0 can serve only frame 2.  Wr1 water-fills the much larger frame 1
        # before frame 3, then flexible Main continues reducing frame 1.
        np.testing.assert_array_equal(budget.wr, [0, 2, 2, 0])
        np.testing.assert_array_equal(budget.dic, [0, 2, 0, 0])
        self.assertEqual(budget.wr0_patterns, 2)
        self.assertEqual(budget.wr1_patterns, 2)
        self.assertEqual(budget.dic_patterns, 2)

    def test_frame_budget_never_exceeds_predicted_cold(self):
        prediction = DemandPrediction(
            exact_bytes=np.array([0, 34, 68]),
            protected_bytes=np.array([0, 34, 0]),
            exact_cold=np.array([0, 1, 2]),
            protected_cold=np.array([0, 1, 0]),
        )
        budget = supply.plan_frame_budgets(
            prediction, wr_patterns=99, dic_patterns=99)

        np.testing.assert_array_less(
            budget.total, prediction.exact_cold + 1)
        self.assertEqual(int(budget.total[0]), 0)

    def test_hardship_prefers_miss_risk_below_cold_cap(self):
        prediction = DemandPrediction(
            exact_bytes=np.array([0, 0, 128, 0, 96]),
            protected_bytes=np.array([0, 0, 128, 0, 96]),
            exact_cold=np.array([0, 0, 4, 0, 3]),
            protected_cold=np.array([0, 0, 4, 0, 3]),
        )
        budget = supply.plan_frame_budgets(
            prediction,
            wr_patterns=1,
            dic_patterns=0,
            cold_cap=4,
            prg_supply_patterns=np.array([0, 0, 0, 4, 0]),
            prg_capacity_patterns=100,
        )

        # Frame 2 has the larger raw burst, but it already reaches the cap.
        # The one credit can remove risk from below-cap frame 4 instead.
        np.testing.assert_array_equal(budget.wr, [0, 0, 0, 0, 1])
        np.testing.assert_array_equal(budget.cold_headroom, [4, 4, 0, 4, 1])

    def test_hardship_prefers_low_seed_prgbuf(self):
        prediction = DemandPrediction(
            exact_bytes=np.array([0, 128, 64, 0, 64]),
            protected_bytes=np.array([0, 128, 64, 0, 64]),
            exact_cold=np.array([0, 4, 2, 0, 2]),
            protected_cold=np.array([0, 4, 2, 0, 2]),
        )
        budget = supply.plan_frame_budgets(
            prediction,
            wr_patterns=1,
            dic_patterns=0,
            cold_cap=2,
            prg_supply_patterns=np.array([1, 1, 1, 5, 1]),
            prg_capacity_patterns=8,
        )

        self.assertEqual(int(budget.wr[2]), 1)
        self.assertEqual(int(budget.wr[4]), 0)
        self.assertLess(
            int(budget.seed_prg_without_wr[2]),
            int(budget.seed_prg_without_wr[4]),
        )

    def test_hardship_still_water_fills_multiple_frames(self):
        prediction = DemandPrediction(
            exact_bytes=np.array([0, 0, 64, 0, 64, 0, 64]),
            protected_bytes=np.array([0, 0, 64, 0, 64, 0, 64]),
            exact_cold=np.array([0, 0, 2, 0, 2, 0, 2]),
            protected_cold=np.array([0, 0, 2, 0, 2, 0, 2]),
        )
        budget = supply.plan_frame_budgets(
            prediction,
            wr_patterns=2,
            dic_patterns=0,
            cold_cap=2,
            prg_supply_patterns=np.full(7, 2),
            prg_capacity_patterns=8,
        )

        self.assertEqual(int(np.count_nonzero(budget.wr[::2])), 2)
        self.assertEqual(int(budget.wr[::2].sum()), 2)

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
            log, per, patterns, enabled=True, wr_patterns=1, dic_patterns=2)

        self.assertEqual(plan.sources[0], (supply.SOURCE_PRG,))
        self.assertEqual(plan.sources[1], (supply.SOURCE_DIC, supply.SOURCE_DIC))
        # The first even run fits Wr0; the second remains Prg.
        self.assertEqual(plan.sources[2], (supply.SOURCE_WR, supply.SOURCE_PRG))
        self.assertEqual(plan.prg_patterns, (patterns[0], patterns[4]))
        self.assertEqual(plan.wr0_patterns, (patterns[3],))
        self.assertEqual(plan.wr1_patterns, ())
        self.assertEqual(plan.dic_patterns, (patterns[1], patterns[2]))
        np.testing.assert_array_equal(plan.prg_loads, [1, 0, 1])
        np.testing.assert_array_equal(plan.wr0_loads, [0, 0, 1])
        np.testing.assert_array_equal(plan.wr1_loads, [0, 0, 0])
        np.testing.assert_array_equal(plan.dic_loads, [0, 2, 0])

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
                        supply.SOURCE_DIC,
                        supply.SOURCE_PRG,
                    ], np.uint8),
                ],
            },
        }
        plan = supply.plan_supply(
            log, per, patterns, enabled=True, wr_patterns=2, dic_patterns=2)

        self.assertEqual(
            plan.sources[1],
            (supply.SOURCE_WR, supply.SOURCE_DIC, supply.SOURCE_PRG),
        )
        self.assertEqual(plan.prg_patterns, (patterns[0], patterns[3]))
        self.assertEqual(plan.wr1_patterns, (patterns[1],))
        self.assertEqual(plan.dic_patterns, (patterns[2],))

    def test_disabled_plan_keeps_every_pattern_in_prg(self):
        per = [([0], [1], [True]), ([0], [2], [True])]
        patterns = [b"a" * 32, b"b" * 32]
        plan = supply.plan_supply({}, per, patterns, enabled=False)
        self.assertFalse(plan.enabled)
        self.assertEqual(plan.prg_patterns, tuple(patterns))
        np.testing.assert_array_equal(plan.prg_loads, [1, 1])

    def test_frozen_dictionary_materializes_indices_without_consumption(self):
        pattern_a = bytes([0x11]) * 32
        pattern_b = bytes([0x22]) * 32
        per = [
            ([0], [1], [True]),
            ([0, 1], [1, 2], [True, True]),
            ([0], [3], [True]),
        ]
        patterns = [pattern_b, pattern_a, pattern_b, pattern_a]
        log = {
            "pattern_supply": {
                "schema_version": 2,
                "dic_dictionary": [pattern_a, pattern_b],
                "sources": [
                    [supply.SOURCE_PRG],
                    [supply.SOURCE_DIC, supply.SOURCE_DIC],
                    [supply.SOURCE_DIC],
                ],
            },
        }
        plan = supply.plan_supply(
            log, per, patterns, enabled=True, wr_patterns=0, dic_patterns=2)
        self.assertEqual(plan.dic_patterns, (pattern_a, pattern_b))
        self.assertEqual(plan.dic_indices, ((-1,), (0, 1), (0,)))
        np.testing.assert_array_equal(plan.dic_loads, [0, 2, 1])

    def test_transfer_order_maps_payload_back_to_update_indices(self):
        pattern_a = bytes([0x11]) * 32
        pattern_b = bytes([0x22]) * 32
        pattern_c = bytes([0x33]) * 32
        per = [
            ([], [], []),
            ([0, 1, 2], [1, 2, 3], [True, True, True]),
        ]
        log = {
            "pattern_supply": {
                "schema_version": 2,
                "dic_dictionary": [pattern_a, pattern_b, pattern_c],
                "sources": [[], [
                    supply.SOURCE_DIC,
                    supply.SOURCE_DIC,
                    supply.SOURCE_DIC,
                ]],
            },
        }
        plan = supply.plan_supply(
            log,
            per,
            [pattern_c, pattern_a, pattern_b],
            transfer_orders=[(), (2, 0, 1)],
            enabled=True,
            wr_patterns=0,
            dic_patterns=3,
        )
        self.assertEqual(plan.dic_indices, ((), (0, 1, 2)))
        np.testing.assert_array_equal(plan.dic_loads, [0, 3])


if __name__ == "__main__":
    unittest.main()
