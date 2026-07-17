#!/usr/bin/env python3
"""Regression tests for exact FFV1/FLAC recording comparison."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from compare_recordings import (
    compare_recordings,
    first_non_monotonic,
    first_sequence_mismatch,
    main,
)


FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg and ffprobe are required")
class RecordingComparisonIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temp.name)
        cls.baseline = cls.root / "baseline.mkv"
        cls.identical = cls.root / "identical.mkv"
        cls.video_changed = cls.root / "video_changed.mkv"
        cls.audio_changed = cls.root / "audio_changed.mkv"
        cls.shorter = cls.root / "shorter.mkv"
        cls._make_recording(cls.baseline, "red", 440, "0.40")
        shutil.copyfile(cls.baseline, cls.identical)
        cls._make_recording(cls.video_changed, "blue", 440, "0.40")
        cls._make_recording(cls.audio_changed, "red", 880, "0.40")
        cls._make_recording(cls.shorter, "red", 440, "0.30")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    @staticmethod
    def _make_recording(path: Path, colour: str, frequency: int, duration: str) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:s=32x24:r=30",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=44100",
                "-t",
                duration,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "ffv1",
                "-pix_fmt",
                "bgr0",
                "-threads",
                "1",
                "-c:a",
                "flac",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-shortest",
                str(path),
            ],
            check=True,
        )

    def test_identical_recordings_pass(self) -> None:
        report = compare_recordings(self.baseline, self.identical)
        self.assertTrue(report["pass"], report["failures"])
        self.assertEqual(report["failures"], [])
        self.assertTrue(report["checks"]["video"]["hashes_equal"])
        self.assertTrue(report["checks"]["audio"]["sha256_equal"])
        self.assertTrue(report["checks"]["timeline"]["video"]["equal"])
        self.assertTrue(report["checks"]["timeline"]["audio"]["equal"])

    def test_video_change_fails_only_the_decoded_video_gate(self) -> None:
        report = compare_recordings(self.baseline, self.video_changed)
        codes = {failure["code"] for failure in report["failures"]}
        self.assertFalse(report["pass"])
        self.assertIn("video.framemd5", codes)
        self.assertTrue(report["checks"]["audio"]["sha256_equal"])
        self.assertTrue(report["checks"]["timeline"]["video"]["equal"])
        self.assertTrue(report["checks"]["timeline"]["audio"]["equal"])

    def test_audio_change_fails_only_the_decoded_audio_gate(self) -> None:
        report = compare_recordings(self.baseline, self.audio_changed)
        codes = {failure["code"] for failure in report["failures"]}
        self.assertFalse(report["pass"])
        self.assertIn("audio.pcm_sha256", codes)
        self.assertTrue(report["checks"]["video"]["hashes_equal"])
        self.assertTrue(report["checks"]["timeline"]["video"]["equal"])
        self.assertTrue(report["checks"]["timeline"]["audio"]["equal"])

    def test_shorter_recording_cannot_pass_on_a_common_prefix(self) -> None:
        report = compare_recordings(self.baseline, self.shorter)
        codes = {failure["code"] for failure in report["failures"]}
        self.assertFalse(report["pass"])
        self.assertIn("video.frame_count", codes)
        self.assertIn("audio.sample_frames", codes)
        self.assertIn("timeline.format_duration", codes)

    def test_cli_exit_status_and_json(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main([str(self.baseline), str(self.identical)])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(stdout.getvalue())["pass"])

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main([str(self.baseline), str(self.video_changed)])
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(stdout.getvalue())["pass"])


class TimelineHelperTests(unittest.TestCase):
    def test_strict_monotonicity(self) -> None:
        self.assertIsNone(first_non_monotonic([]))
        self.assertIsNone(first_non_monotonic([0]))
        self.assertIsNone(first_non_monotonic([0, 1, 3]))
        self.assertEqual(first_non_monotonic([0, 1, 1]), 2)
        self.assertEqual(first_non_monotonic([0, 2, 1]), 2)

    def test_timeline_length_is_part_of_equality(self) -> None:
        self.assertIsNone(first_sequence_mismatch([(0, 1)], [(0, 1)]))
        self.assertEqual(first_sequence_mismatch([(0, 1)], [(0, 1), (1, 1)]), 1)
        self.assertEqual(first_sequence_mismatch([(0, 1), (2, 1)], [(0, 1), (3, 1)]), 1)


if __name__ == "__main__":
    unittest.main()
