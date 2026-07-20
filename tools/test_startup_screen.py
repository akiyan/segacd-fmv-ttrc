#!/usr/bin/env python3
"""Regression tests for the generated preload status screen."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from encode_config import EncodeProfile
import startup_screen


def profile() -> EncodeProfile:
    return EncodeProfile(
        Path("/tmp/bad-apple-h32.toml"),
        {
            "metadata": {"title": "BAD APPLE!!"},
            "source": {"path": "assets/BadApple.mp4", "fps": "30",
                       "duration": "219.213021"},
            "video": {"mode": "H32", "width": 256, "height": 224,
                      "fit": "crop", "resize_filter": "area",
                      "master_denoise": False},
            "audio": {"kind": "adpcm22"},
            "output": {"directory": "videos/test/tmp", "emit_decisions": True},
            "encoder": {"rate_kib": 144, "vram_tiles": 1400, "dither": True,
                        "segment_palettes": True, "near": True, "coa": True},
            "palette": {"algorithm": "mosaic-gm"},
            "pack": {"debug": True},
        },
        "0" * 64,
    )


def constants() -> SimpleNamespace:
    return SimpleNamespace(
        tcols=32, trows=28, frames=6576, nseg=1,
        paltab_sec=1, adpcm_table_sectors=5, audio_preload_sec=19,
        wr0_patterns=880, wr1_patterns=880, main_patterns=208,
        f0_ctrl_sec=2, f0_pat_sec=1, routing_sec=4,
    )


class StartupScreenTests(unittest.TestCase):
    def test_h32_layout_stays_within_native_grid(self) -> None:
        lines = startup_screen.screen_lines(
            profile(), constants(), "20260720.E68.P59")
        self.assertTrue(any(text.startswith("PrgBuf 000/388")
                            for _, _, _, text in lines))
        self.assertTrue(any(text == "[" + "-" * 30 + "]"
                            for _, _, _, text in lines))
        for row, col, _palette, text in lines:
            self.assertLess(row, 28)
            self.assertLessEqual(col + len(text), 32)

    def test_generated_include_uses_live_progress_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            font = Path(tmp) / "font_default.png"
            Image.new("P", (128, 48), 0).save(font)
            rendered = startup_screen.render_include(
                profile(), constants(), "20260720.E68.P59", font)
        self.assertIn("STARTUP_PRG_VALUE_ADDR", rendered)
        self.assertIn("STARTUP_PRG_BAR_ADDR", rendered)
        self.assertIn("STARTUP_PREFIX_OK_N, 5", rendered)
        self.assertIn("startup_prefix_ok_addrs:", rendered)


if __name__ == "__main__":
    unittest.main()
