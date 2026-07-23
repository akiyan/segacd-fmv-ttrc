from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sim_artifact_cache as cache


class SimArtifactCacheTests(unittest.TestCase):
    def test_identity_ignores_paths_and_output_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "movie.mp4"
            source.write_bytes(b"same source bytes")
            common = {
                "CBRSIM_MODE": "H40",
                "CBRSIM_W": "320",
                "CBRSIM_H": "224",
                "CBRSIM_FPS": "30",
                "CBRSIM_COLD_CAP": "190",
            }
            first_env = {
                **common,
                "CBRSIM_SRC": "/one/movie.mp4",
                "CBRSIM_OUT": "videos/one/tmp",
                "CBRSIM_REUSE": "0",
                "CBRSIM_CONFIG": "/one/profile.toml",
            }
            second_env = {
                **common,
                "CBRSIM_SRC": "/two/renamed.mp4",
                "CBRSIM_OUT": "videos/two/tmp",
                "CBRSIM_REUSE": "1",
                "CBRSIM_CONFIG": "/two/renamed.toml",
            }
            with patch.object(
                    cache, "encoder_fingerprint", return_value="a" * 64):
                first = cache.build_identity(
                    source=source, pack={"fill": True},
                    emit_decisions=True, environ=first_env)
                second = cache.build_identity(
                    source=source, pack={"fill": True},
                    emit_decisions=True, environ=second_env)
            self.assertEqual(first, second)

    def test_output_setting_and_source_content_change_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "movie.mp4"
            source.write_bytes(b"revision one")
            env = {"CBRSIM_COLD_CAP": "190"}
            with patch.object(
                    cache, "encoder_fingerprint", return_value="b" * 64):
                first = cache.build_identity(
                    source=source, pack={"fill": True},
                    emit_decisions=True, environ=env)
                changed_setting = cache.build_identity(
                    source=source, pack={"fill": False},
                    emit_decisions=True, environ=env)
                source.write_bytes(b"revision two")
                changed_source = cache.build_identity(
                    source=source, pack={"fill": True},
                    emit_decisions=True, environ=env)
            self.assertNotEqual(
                cache.identity_sha256(first),
                cache.identity_sha256(changed_setting))
            self.assertNotEqual(
                cache.identity_sha256(first),
                cache.identity_sha256(changed_source))

    def test_readable_key_exposes_major_conditions(self) -> None:
        identity = {
            "source": {"name": "SonicJamOp", "sha256": "1" * 64},
            "effective_environment": {"CBRSIM_COLD_CAP": "190"},
            "pack": {"fill": True},
            "emit_decisions": True,
            "encoder_sha256": "2" * 64,
        }
        key = cache.readable_key(
            identity,
            mode="H40", width=320, height=224, fps="30",
            fit="crop", cold_cap=190)
        self.assertIn("SonicJamOp-H40-320x224-30fps-fit-crop-cold190", key)
        self.assertIn("src11111111", key)
        self.assertIn("enc22222222", key)


if __name__ == "__main__":
    unittest.main()
