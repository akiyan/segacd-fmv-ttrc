from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import sim_pass_cache


class SimPassCacheTests(unittest.TestCase):
    def test_round_trip_and_identity_guard(self) -> None:
        metadata = {
            "schema_version": 1,
            "invocation": "abc",
            "profile_sha256": "123",
            "geometry": {"frame_count": 2},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pass.pkl"
            sim_pass_cache.save(path, metadata, {"frames": [1, 2]})
            self.assertEqual(
                sim_pass_cache.load(path, metadata), {"frames": [1, 2]})
            changed = dict(metadata)
            changed["geometry"] = {"frame_count": 3}
            with self.assertRaisesRegex(
                    sim_pass_cache.PassCacheError, "geometry"):
                sim_pass_cache.load(path, changed)

    def test_missing_cache_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                    sim_pass_cache.PassCacheError, "does not exist"):
                sim_pass_cache.load(Path(tmp) / "missing.pkl", {})


if __name__ == "__main__":
    unittest.main()
