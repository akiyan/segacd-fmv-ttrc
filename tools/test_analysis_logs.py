from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import analysis_logs


class Profile:
    path = Path("/repo/configs/sonic-jam-op-h40.toml")
    sha256 = "4dd3ae5754c01c1d4f3948a8eddc8a5f19a5739b03c26eeb32c385502fecffdb"


class AnalysisLogTests(unittest.TestCase):
    def test_unique_name_contains_required_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                os.environ, {"ANALYSIS_LOG_DIR": tmp}):
            path = analysis_logs.unique_tsv_path(
                Profile(), now=datetime(
                    2026, 7, 23, 12, 34, 56, 123456, tzinfo=timezone.utc))
            self.assertRegex(
                path.name,
                r"^20260723-123456-123456_sonic-jam-op-h40_"
                r"4dd3ae5754_e\d+\.tsv$")

    def test_alias_points_to_persistent_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "logs" / "run.tsv"
            log.parent.mkdir()
            log.write_text("frame\n", encoding="utf-8")
            alias = root / "videos" / "movie_analysis.tsv"
            analysis_logs.publish_alias(alias, log)
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.read_text(encoding="utf-8"), "frame\n")


if __name__ == "__main__":
    unittest.main()
