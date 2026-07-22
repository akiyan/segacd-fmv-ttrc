#!/usr/bin/env python3
"""Regression tests for the recording HUD upload gate."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "startup_resync_analyze", ROOT / "harness/startup_resync/analyze.py"
)
assert SPEC and SPEC.loader
analyze = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analyze
SPEC.loader.exec_module(analyze)


def groups(count: int, **peaks: int):
    result = []
    for frame in range(count):
        values = {field: 0 for field in "SDRCMJ"}
        if frame == count - 1:
            values.update(peaks)
        values["F"] = frame
        result.append(analyze.FrameGroup(
            loop=0, capture_first=frame * 2, capture_last=frame * 2 + 1,
            time_first=frame / 30, time_last=(frame + 0.5) / 30,
            sample_count=2, confidence=1.0, values=values,
        ))
    return result


class HudUploadGateTests(unittest.TestCase):
    def evaluate(self, rows, expected):
        with tempfile.NamedTemporaryFile() as recording:
            return analyze.evaluate_upload_gate(rows, expected, Path(recording.name))

    def test_clean_complete_loop_passes(self):
        result = self.evaluate(groups(4, M=1, J=23), 4)
        self.assertTrue(result["pass"], result["failures"])
        self.assertTrue(result["requires_explicit_upload_approval"])

    def test_each_unsafe_metric_blocks_upload(self):
        for field, value in {"S": 1, "D": 1, "R": 1, "C": 1,
                             "M": 2, "J": 24}.items():
            with self.subTest(field=field):
                result = self.evaluate(groups(4, **{field: value}), 4)
                self.assertFalse(result["pass"])
                self.assertTrue(any(text.startswith(field) for text in result["failures"]))

    def test_missing_movie_frame_blocks_upload(self):
        rows = groups(4)
        rows.pop(2)
        result = self.evaluate(rows, 4)
        self.assertFalse(result["pass"])
        self.assertTrue(any("incomplete" in text for text in result["failures"]))


if __name__ == "__main__":
    unittest.main()
