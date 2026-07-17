#!/usr/bin/env python3
"""Regression tests for TOML-derived artifact names."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from encode_config import apply_profile_env, load_profile


PROFILE = """\
schema_version = 1

[source]
path = "assets/source.mp4"
fps = "30"
duration = "1"

[video]
mode = "H32"
width = 256
height = 224
fit = "pad"

[audio]
kind = "pcm13"

[output]
directory = "videos/test/tmp"
emit_decisions = true

[palette]
algorithm = "mosaic-gm"

[pack]
debug = true
output = "out/movieplay/MOVIE.DAT"
"""


class EncodeProfileArtifactTests(unittest.TestCase):
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

    def test_profile_without_preprocess_clears_inherited_snap(self) -> None:
        root = Path(__file__).resolve().parents[1]
        h32 = load_profile(root / "configs/bad-apple-h32.toml")
        env = apply_profile_env(h32, {
            "CBRSIM_PREPROCESS_ENDPOINT_SNAP_BLACK_MAX": "2",
            "CBRSIM_PREPROCESS_ENDPOINT_SNAP_WHITE_MIN": "253",
        })
        self.assertEqual(
            env["CBRSIM_PREPROCESS_ENDPOINT_SNAP_BLACK_MAX"], "-1")
        self.assertEqual(
            env["CBRSIM_PREPROCESS_ENDPOINT_SNAP_WHITE_MIN"], "256")

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

    def test_legacy_pack_output_cannot_override_profile_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-apple-h40.toml"
            path.write_text(PROFILE)
            profile = load_profile(path)

        self.assertNotEqual(
            Path(profile.section("pack")["output"]), profile.pack_output)
        self.assertEqual(profile.pack_output, Path("out/bad-apple-h40/MOVIE.DAT"))

    def test_unsafe_filename_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsafe name.toml"
            path.write_text(PROFILE)
            with self.assertRaisesRegex(ValueError, "filename stem"):
                load_profile(path)


if __name__ == "__main__":
    unittest.main()
