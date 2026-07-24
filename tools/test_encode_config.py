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
schema_version = 3

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
"""


class EncodeProfileArtifactTests(unittest.TestCase):
    def test_removed_schema_v2_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old-schema.toml"
            path.write_text(PROFILE.replace(
                "schema_version = 3", "schema_version = 2"))
            with self.assertRaisesRegex(ValueError, "schema_version must be 3"):
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
            "CBRSIM_QUALITY_BUDGET_KB": "999",
            "CBRSIM_RING_CAP_KB": "999",
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
        self.assertNotIn("CBRSIM_QUALITY_BUDGET_KB", env)
        self.assertNotIn("CBRSIM_QUALITY_BUDGET_KB", inherited)
        self.assertNotIn("CBRSIM_RING_CAP_KB", inherited)

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

    def test_sonic_h40_encodes_only_native_truemotion_raster(self) -> None:
        root = Path(__file__).resolve().parents[1]
        profile = load_profile(root / "configs/sonic-jam-op-h40.toml")
        env = apply_profile_env(profile, {})
        self.assertTrue(env["CBRSIM_SRC"].endswith("assets/SonicJamOp.avi"))
        self.assertEqual(env["CBRSIM_FPS"], "30")
        self.assertEqual(env["CBRSIM_DURATION"], "90.466667")
        self.assertEqual(env["CBRSIM_SOURCE_SAR"], "32:35")
        self.assertEqual(env["CBRSIM_MODE"], "H40")
        self.assertEqual(env["CBRSIM_W"], "288")
        self.assertEqual(env["CBRSIM_H"], "200")
        self.assertEqual(env["CBRSIM_ACTIVE_TILES"], "900")
        self.assertEqual(env["CBRSIM_GEOMETRY_FIT"], "pad")
        self.assertEqual(env["CBRSIM_MASTER_DENOISE"], "0")
        self.assertEqual(env["CBRSIM_MASTER_VF"], "setsar=1")
        self.assertEqual(env["CBRSIM_RAW_VF"], "setsar=1")
        self.assertTrue(env["CBRSIM_OUT"].endswith(
            "videos/SonicJamOp_H40_288x200_adpcm22/tmp"))

    def test_machi_op_uses_confirmed_black_bar_crop_and_native_h40_sar(self) -> None:
        root = Path(__file__).resolve().parents[1]
        profile = load_profile(root / "configs/machi-op-h40.toml")
        env = apply_profile_env(profile, {"CBRSIM_ACTIVE_TILES": "1"})
        self.assertEqual(env["CBRSIM_H"], "152")
        self.assertEqual(env["CBRSIM_ACTIVE_TILES"], "760")
        self.assertEqual(env["CBRSIM_SOURCE_SAR"], "32:35")
        self.assertEqual(env["CBRSIM_GEOMETRY_FIT"], "crop")
        self.assertEqual(env["CBRSIM_MASTER_DENOISE"], "0")
        self.assertEqual(
            env["CBRSIM_MASTER_VF"], "setsar=1,crop=320:152:0:34")
        self.assertEqual(
            env["CBRSIM_RAW_VF"], "setsar=1,crop=320:152:0:34")
        # The hardware baseline remains 360; this qualified profile explicitly
        # raises the encoder ceiling to the cap-480 result.
        self.assertEqual(av_config.baseline_cold_cap_for_fps(15), 360)
        self.assertEqual(env["CBRSIM_COLD_CAP"], "480")

    def test_machi_ed_uses_full_h40_grid_and_profile_cap_380(self) -> None:
        root = Path(__file__).resolve().parents[1]
        profile = load_profile(root / "configs/machi-ed-h40.toml")
        env = apply_profile_env(profile, {"CBRSIM_ACTIVE_TILES": "1"})
        self.assertEqual(env["CBRSIM_ACTIVE_TILES"], "1120")
        self.assertEqual(env["CBRSIM_SOURCE_SAR"], "32:35")
        self.assertEqual(env["CBRSIM_GEOMETRY_FIT"], "crop")
        self.assertEqual(env["CBRSIM_MASTER_DENOISE"], "0")
        self.assertEqual(env["CBRSIM_COLD_CAP"], "380")

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
        self.assertEqual(env["CBRSIM_COLD_CAP"], "180")
        self.assertEqual(
            env["CBRSIM_VRAM_TILES"],
            str(av_config.VRAM_PATTERN_POOL_TILES))
        self.assertEqual(env["CBRSIM_GPU"], "1")
        self.assertEqual(env["CBRSIM_DITHER"], "1")
        self.assertEqual(env["CBRSIM_SEGPAL"], "1")
        self.assertEqual(env["CBRSIM_NEAR"], "1")
        self.assertEqual(env["CBRSIM_BOOT_VRAM_PREFETCH"], "1")

    def test_profile_cold_cap_may_raise_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raised-cold-cap.toml"
            path.write_text(PROFILE.replace(
                "[palette]", "[encoder]\ncold_cap = 180\n\n[palette]"))
            env = apply_profile_env(
                load_profile(path), {"CBRSIM_COLD_CAP": "999"})
        self.assertEqual(env["CBRSIM_COLD_CAP"], "180")

    def test_profile_cold_cap_below_baseline_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lowered-cold-cap.toml"
            path.write_text(PROFILE.replace(
                "[palette]", "[encoder]\ncold_cap = 179\n\n[palette]"))
            with self.assertRaisesRegex(
                    ValueError, "cold_cap 179 is below baseline 180"):
                load_profile(path)

    def test_profile_cold_cap_must_be_an_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid-cold-cap.toml"
            path.write_text(PROFILE.replace(
                "[palette]", "[encoder]\ncold_cap = 180.5\n\n[palette]"))
            with self.assertRaisesRegex(
                    ValueError, "cold_cap must be an integer"):
                load_profile(path)

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

    def test_vram_pool_is_fixed_and_profile_key_is_rejected(self) -> None:
        self.assertEqual(MAX_RESIDENT_VRAM_TILES, 1535)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile-vram.toml"
            path.write_text(PROFILE.replace(
                "[palette]",
                f"[encoder]\nvram_tiles = {MAX_RESIDENT_VRAM_TILES}\n\n[palette]"))
            with self.assertRaisesRegex(
                    ValueError, "unknown \\[encoder\\] keys.*vram_tiles"):
                load_profile(path)

    def test_profile_baseline_depends_only_on_fps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h40-15-900.toml"
            path.write_text(
                PROFILE.replace('fps = "30"', 'fps = "15"')
                .replace('mode = "H32"', 'mode = "H40"')
                .replace('width = 256', 'width = 320')
                .replace('fit = "pad"', 'fit = "pad"\nactive_tiles = 900'))
            env = apply_profile_env(load_profile(path), {})
        self.assertEqual(env["CBRSIM_COLD_CAP"], "360")

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
            path.write_text(
                PROFILE + '\n[pack]\noutput = "out/legacy/MOVIE.DAT"\n')
            with self.assertRaisesRegex(ValueError, "unknown sections.*pack"):
                load_profile(path)

    def test_unsafe_filename_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe name.toml"
            path.write_text(PROFILE)
            with self.assertRaisesRegex(ValueError, "filename stem"):
                load_profile(path)


if __name__ == "__main__":
    unittest.main()
