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


def profile(mode: str = "H32") -> EncodeProfile:
    h40 = mode == "H40"
    return EncodeProfile(
        Path(f"/tmp/bad-apple-{mode.lower()}.toml"),
        {
            "metadata": {"title": "BAD APPLE!!"},
            "source": {"path": "assets/BadApple.mp4", "fps": "30",
                       "duration": "219.213021"},
            "video": {"mode": mode, "width": 320 if h40 else 256, "height": 224,
                      "fit": "crop", "resize_filter": "area",
                      "master_denoise": False},
            "audio": {"kind": "adpcm22"},
            "output": {"directory": "videos/test/tmp", "emit_decisions": True},
            "encoder": {"vram_tiles": 1400, "dither": True,
                        "segment_palettes": True, "near": True, "coa": True},
            "palette": {"algorithm": "mosaic-gm"},
            "pack": {"debug": True},
        },
        "0" * 64,
    )


def constants(mode: str = "H32") -> SimpleNamespace:
    return SimpleNamespace(
        tcols=40 if mode == "H40" else 32, trows=28, frames=6576, nseg=1,
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
        self.assertFalse(any("KiB/s" in text for _, _, _, text in lines))
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

    def test_h40_centers_panel_and_live_targets_by_four_columns(self) -> None:
        h32_lines = startup_screen.screen_lines(
            profile("H32"), constants("H32"), "20260721.E70.P64")
        h40_lines = startup_screen.screen_lines(
            profile("H40"), constants("H40"), "20260721.E70.P64")
        self.assertEqual(startup_screen.column_offset(profile("H32")), 0)
        self.assertEqual(startup_screen.column_offset(profile("H40")), 4)
        self.assertEqual(startup_screen._bytes(h40_lines, 4)[1], h40_lines[0][1] + 4)

        with tempfile.TemporaryDirectory() as tmp:
            font = Path(tmp) / "font_default.png"
            Image.new("P", (128, 48), 0).save(font)
            h32 = startup_screen.render_include(
                profile("H32"), constants("H32"), "20260721.E70.P64", font)
            h40 = startup_screen.render_include(
                profile("H40"), constants("H40"), "20260721.E70.P64", font)
        self.assertIn("STARTUP_COLUMN_OFFSET, 0", h32)
        self.assertIn("STARTUP_COLUMN_OFFSET, 4", h40)

        def address(rendered: str, name: str) -> int:
            prefix = f".equ {name}, 0x"
            line = next(line for line in rendered.splitlines() if line.startswith(prefix))
            return int(line.removeprefix(prefix), 16)

        for name in (
            "STARTUP_PRG_VALUE_ADDR", "STARTUP_PRG_STATUS_ADDR",
            "STARTUP_PRG_BAR_ADDR", "STARTUP_SUB_STATUS_ADDR",
        ):
            self.assertEqual(address(h40, name) - address(h32, name), 8)


if __name__ == "__main__":
    unittest.main()
