#!/usr/bin/env python3
"""Regression tests for TOML-derived artifact names."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import os
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import av_config
from encode_config import (
    MAX_RESIDENT_VRAM_TILES,
    apply_profile_env,
    consume_config_arg,
    load_profile,
)


PROFILE = """\
schema_version = 2

[source]
path = "assets/source.mp4"
fps = "30"
duration = "1"

[video]
mode = "H32"
width = 256
height = 224
fit = "pad"

[output]
directory = "videos/test/tmp"
emit_decisions = true

[palette]
algorithm = "mosaic-gm"

[pack]
fill = true
"""


class EncodeProfileArtifactTests(unittest.TestCase):
    def test_removed_schema_v1_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old-schema.toml"
            path.write_text(PROFILE.replace(
                "schema_version = 2", "schema_version = 1"))
            with self.assertRaisesRegex(ValueError, "schema_version must be 2"):
                load_profile(path)

    def test_required_profile_is_consumed_as_first_positional_argument(self) -> None:
        root = Path(__file__).resolve().parents[1]
        profile_path = root / "configs" / "bad-apple-h32.toml"
        argv = ["sim.py", str(profile_path)]
        with patch.dict(os.environ, {}, clear=False):
            profile = consume_config_arg(argv, required=True)
        self.assertEqual(profile.path, profile_path.resolve())
        self.assertEqual(argv, ["sim.py"])

    def test_required_profile_preserves_following_frame_range(self) -> None:
        root = Path(__file__).resolve().parents[1]
        profile_path = root / "configs" / "bad-apple-h32.toml"
        argv = ["render_analysis.py", str(profile_path), "10", "20"]
        with patch.dict(os.environ, {}, clear=False):
            consume_config_arg(argv, required=True)
        self.assertEqual(argv, ["render_analysis.py", "10", "20"])

    def test_missing_required_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "profile is required.*positional"):
            consume_config_arg(["sim.py"], required=True)

    def test_legacy_config_option_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "positional; do not use --config"):
            consume_config_arg(
                ["sim.py", "--config", "configs/bad-apple-h32.toml"],
                required=True,
            )

    def test_all_repository_profiles_have_measured_cold_cap_coverage(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for path in sorted((root / "configs").glob("*.toml")):
            with self.subTest(profile=path.name):
                load_profile(path)

    def test_bad_apple_h40_uses_source_endpoint_snap(self) -> None:
        root = Path(__file__).resolve().parents[1]
        h40 = load_profile(root / "configs/bad-apple-h40.toml")
        inherited = {
            "CBRSIM_PREPROCESS_ENDPOINT_SNAP_BLACK_MAX": "9",
            "CBRSIM_PREPROCESS_ENDPOINT_SNAP_WHITE_MIN": "246",
        }
        env = apply_profile_env(h40, inherited)
        self.assertTrue(env["CBRSIM_SRC"].endswith("BadApple.mp4"))
        self.assertEqual(
            env["CBRSIM_PREPROCESS_ENDPOINT_SNAP_BLACK_MAX"], "2")
        self.assertEqual(
            env["CBRSIM_PREPROCESS_ENDPOINT_SNAP_WHITE_MIN"], "253")
        self.assertEqual(env["CBRSIM_RESIZE_FILTER"], "area")
        self.assertEqual(env["CBRSIM_MASTER_DENOISE"], "0")
        self.assertEqual(env["CBRSIM_ACTIVE_TILES"], "1120")
        self.assertEqual(env["CBRSIM_RAW_PREFETCH"], "1")
        self.assertTrue(
            env["CBRSIM_OUT"].endswith(
                "videos/BadApple_H40_320x224_adpcm22/tmp"))
        self.assertEqual(
            env["CBRSIM_QUALITY_BUDGET_KB"],
            str(av_config.QUALITY_BUDGET_KB))

    def test_bad_apple_h32_is_full_cover_adpcm22(self) -> None:
        root = Path(__file__).resolve().parents[1]
        h32 = load_profile(root / "configs/bad-apple-h32.toml")
        env = apply_profile_env(h32, {})
        self.assertEqual(env["CBRSIM_GEOMETRY_FIT"], "crop")
        self.assertEqual(env["CBRSIM_ACTIVE_TILES"], "896")
        self.assertEqual(env["CBRSIM_RESIZE_FILTER"], "area")
        self.assertEqual(env["CBRSIM_MASTER_DENOISE"], "0")
        self.assertEqual(
            env["CBRSIM_PREPROCESS_ENDPOINT_SNAP_BLACK_MAX"], "2")
        self.assertEqual(
            env["CBRSIM_PREPROCESS_ENDPOINT_SNAP_WHITE_MIN"], "253")
        self.assertTrue(env["CBRSIM_OUT"].endswith(
            "videos/BadApple_H32_256x224_adpcm22/tmp"))

    def test_machi_op_declares_its_confirmed_active_tile_area(self) -> None:
        root = Path(__file__).resolve().parents[1]
        profile = load_profile(root / "configs/machi-op-h40.toml")
        env = apply_profile_env(profile, {"CBRSIM_ACTIVE_TILES": "1"})
        self.assertEqual(env["CBRSIM_ACTIVE_TILES"], "720")

    def test_machi_ed_declares_its_confirmed_active_tile_area(self) -> None:
        root = Path(__file__).resolve().parents[1]
        profile = load_profile(root / "configs/machi-ed-h40.toml")
        env = apply_profile_env(profile, {"CBRSIM_ACTIVE_TILES": "1"})
        self.assertEqual(env["CBRSIM_ACTIVE_TILES"], "1040")

    def test_profile_without_preprocess_clears_inherited_snap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "no-preprocess.toml"
            path.write_text(PROFILE)
            env = apply_profile_env(load_profile(path), {
                "CBRSIM_PREPROCESS_ENDPOINT_SNAP_BLACK_MAX": "2",
                "CBRSIM_PREPROCESS_ENDPOINT_SNAP_WHITE_MIN": "253",
            })
        self.assertEqual(
            env["CBRSIM_PREPROCESS_ENDPOINT_SNAP_BLACK_MAX"], "-1")
        self.assertEqual(
            env["CBRSIM_PREPROCESS_ENDPOINT_SNAP_WHITE_MIN"], "256")
        self.assertEqual(env["CBRSIM_RESIZE_FILTER"], "lanczos")
        self.assertEqual(env["CBRSIM_MASTER_DENOISE"], "1")
        self.assertEqual(env["CBRSIM_RAW_PREFETCH"], "0")

    def test_endpoint_snap_limits_must_be_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "endpoint-snap.toml"
            path.write_text(PROFILE.replace(
                "[video]",
                "[source.preprocess.endpoint_snap]\nblack_max = 253\n"
                "white_min = 2\n\n[video]"))
            with self.assertRaisesRegex(ValueError, "black_max must be below"):
                load_profile(path)

    def test_endpoint_snap_requires_both_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "endpoint-snap.toml"
            path.write_text(PROFILE.replace(
                "[video]",
                "[source.preprocess.endpoint_snap]\nblack_max = 2\n\n[video]"))
            with self.assertRaisesRegex(ValueError, "missing.*white_min"):
                load_profile(path)

    def test_unknown_resize_filter_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resize-filter.toml"
            path.write_text(PROFILE.replace(
                "fit = \"pad\"", "fit = \"pad\"\nresize_filter = \"magic\""))
            with self.assertRaisesRegex(ValueError, "video.resize_filter"):
                load_profile(path)

    def test_active_tiles_must_fit_the_output_grid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-active-tiles.toml"
            path.write_text(PROFILE.replace(
                'fit = "pad"', 'fit = "pad"\nactive_tiles = 897'))
            with self.assertRaisesRegex(ValueError, "video.active_tiles"):
                load_profile(path)

    def test_vram_pool_must_stay_below_movie_name_table(self) -> None:
        self.assertEqual(MAX_RESIDENT_VRAM_TILES, 1535)
        with tempfile.TemporaryDirectory() as tmp:
            valid = Path(tmp) / "valid-vram.toml"
            valid.write_text(PROFILE.replace(
                "[palette]",
                f"[encoder]\nvram_tiles = {MAX_RESIDENT_VRAM_TILES}\n\n[palette]"))
            load_profile(valid)

            invalid = Path(tmp) / "invalid-vram.toml"
            invalid.write_text(PROFILE.replace(
                "[palette]",
                f"[encoder]\nvram_tiles = {MAX_RESIDENT_VRAM_TILES + 1}\n\n[palette]"))
            with self.assertRaisesRegex(ValueError, "vram_tiles must be within"):
                load_profile(invalid)

    def test_profile_without_measured_cold_cap_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unmeasured-h32-15.toml"
            path.write_text(PROFILE.replace('fps = "30"', 'fps = "15"'))
            with self.assertRaisesRegex(
                    ValueError, "cold-cap measurement required.*H32.*15"):
                load_profile(path)

    def test_profile_without_exact_active_tile_measurement_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unmeasured-h40-15-900.toml"
            path.write_text(
                PROFILE.replace('fps = "30"', 'fps = "15"')
                .replace('mode = "H32"', 'mode = "H40"')
                .replace('width = 256', 'width = 320')
                .replace('fit = "pad"', 'fit = "pad"\nactive_tiles = 900'))
            with self.assertRaisesRegex(
                    ValueError, "cold-cap measurement required.*H40.*900"):
                load_profile(path)

    def test_removed_audio_section_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-audio.toml"
            path.write_text(PROFILE + '\n[audio]\nkind = "adpcm22"\n')
            with self.assertRaisesRegex(ValueError, "unknown sections.*audio"):
                load_profile(path)

    def test_artifacts_follow_toml_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sakura-h32.toml"
            path.write_text(PROFILE)
            profile = load_profile(path)

        self.assertEqual(profile.artifact_stem, "sakura-h32")
        self.assertEqual(profile.artifact_dir, Path("out/sakura-h32"))
        self.assertEqual(profile.pack_output, Path("out/sakura-h32/MOVIE.DAT"))
        self.assertEqual(profile.temp_dir, Path("tmp/sakura-h32"))
        self.assertEqual(profile.build_dir, Path("tmp/sakura-h32/build"))
        self.assertEqual(profile.disc_staging_dir, Path("tmp/sakura-h32/disc"))
        self.assertEqual(profile.disc_iso, Path("out/sakura-h32.iso"))
        self.assertEqual(profile.disc_cue, Path("out/sakura-h32.cue"))

    def test_removed_pack_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-apple-h40.toml"
            path.write_text(PROFILE.replace(
                "fill = true", 'fill = true\noutput = "out/legacy/MOVIE.DAT"'))
            with self.assertRaisesRegex(ValueError, "unknown \\[pack\\] keys.*output"):
                load_profile(path)

    def test_unsafe_filename_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe name.toml"
            path.write_text(PROFILE)
            with self.assertRaisesRegex(ValueError, "filename stem"):
                load_profile(path)


if __name__ == "__main__":
    unittest.main()
