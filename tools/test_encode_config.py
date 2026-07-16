#!/usr/bin/env python3
"""Regression tests for TOML-derived artifact names."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from encode_config import load_profile


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
