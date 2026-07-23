from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import tmpfs_workspace as workspace


class TmpfsWorkspaceTests(unittest.TestCase):
    def env(self, root: Path):
        return patch.dict(os.environ, {
            "SEGACD_TMPFS_ROOT": str(root),
            "SEGACD_TMPFS_ALLOW_NON_TMPFS": "1",
            "SEGACD_TMPFS_ALLOW_ANY_ALIAS": "1",
            "SEGACD_TMPFS_MIN_FREE_GB": "0",
        })

    def test_directory_alias_and_live_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.env(Path(tmp) / "ram"):
            alias = Path(tmp) / "videos" / "movie" / "tmp"
            lease = workspace.activate_directory(
                alias, kind="sim", key="profile-abc")
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.resolve(), lease.entry / "data")
            second = workspace.lease_managed_alias(alias.resolve())
            self.assertIsNotNone(second)
            self.assertEqual(second.entry, lease.entry)
            second.release()
            record = json.loads(lease.marker.read_text(encoding="utf-8"))
            self.assertEqual(Path(record["entry"]), lease.entry)
            lease.release()
            self.assertFalse(lease.marker.exists())

    def test_file_is_published_through_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.env(Path(tmp) / "ram"):
            alias = Path(tmp) / "videos" / "movie_analysis.mp4"
            actual, lease = workspace.allocate_file(
                alias, kind="analysis", key="profile-abc")
            actual.write_bytes(b"video")
            workspace.publish_alias(alias, actual)
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.read_bytes(), b"video")
            lease.release()

    def test_file_command_publishes_only_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.env(Path(tmp) / "ram"):
            source = Path(tmp) / "source.bin"
            source.write_bytes(b"rendered")
            alias = Path(tmp) / "videos" / "rendered.mp4"
            workspace.run_file_command(
                alias,
                kind="test-render",
                required_bytes=0,
                command=["cp", str(source), "{output}"],
            )
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias.read_bytes(), b"rendered")

    def test_stale_lease_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.env(Path(tmp) / "ram"):
            root = workspace.ensure_root()
            entry = root / "artifacts" / "old"
            entry.mkdir()
            marker = root / "leases" / "stale.json"
            marker.write_text(json.dumps({
                "pid": 99999999, "entry": str(entry),
            }), encoding="utf-8")
            self.assertEqual(workspace._active_entries(root), set())
            self.assertFalse(marker.exists())

    def test_low_space_evicts_oldest_inactive_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.env(Path(tmp) / "ram"):
            root = workspace.ensure_root()
            old = root / "artifacts" / "old"
            new = root / "artifacts" / "new"
            old.mkdir()
            new.mkdir()
            os.utime(old, ns=(1, 1))
            os.utime(new, ns=(2, 2))
            usage = shutil._ntuple_diskusage
            with patch.object(
                    workspace.shutil, "disk_usage",
                    side_effect=[
                        usage(100, 90, 10),
                        usage(100, 70, 30),
                        usage(100, 70, 30),
                    ]):
                removed = workspace.evict_old_entries(20, root=root)
            self.assertEqual(removed, [old])
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())


if __name__ == "__main__":
    unittest.main()
