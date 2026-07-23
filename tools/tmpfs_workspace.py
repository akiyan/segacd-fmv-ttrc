#!/usr/bin/env python3
"""Manage disposable codec artifacts in a project-scoped tmpfs workspace.

The public paths stay under ``videos/`` as symlinks, while their bytes live
under ``/dev/shm``.  Eviction is limited to this project's managed entries and
never removes a live lease owned by another encoder/render process.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path("/dev/shm/segacd-fmv-ttrc")
DEFAULT_MIN_FREE_BYTES = 4 * 1024 ** 3


class TmpfsWorkspaceError(RuntimeError):
    pass


def managed_root() -> Path:
    return Path(os.environ.get(
        "SEGACD_TMPFS_ROOT", str(DEFAULT_ROOT))).expanduser().resolve()


def _decode_mount_path(value: str) -> str:
    return (value.replace("\\040", " ").replace("\\011", "\t")
            .replace("\\012", "\n").replace("\\134", "\\"))


def filesystem_type(path: Path) -> str | None:
    """Return the closest Linux mount's filesystem type."""

    path = path.resolve()
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.is_file():
        return None
    best: tuple[int, str] | None = None
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        post = right.split()
        if len(fields) < 5 or not post:
            continue
        mount = Path(_decode_mount_path(fields[4]))
        try:
            path.relative_to(mount)
        except ValueError:
            continue
        candidate = (len(str(mount)), post[0])
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


def ensure_root(root: Path | None = None) -> Path:
    root = (root or managed_root()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    fs_type = filesystem_type(root)
    allow_non_tmpfs = os.environ.get(
        "SEGACD_TMPFS_ALLOW_NON_TMPFS", "0").strip().lower() in {
            "1", "true", "yes", "on",
        }
    if fs_type != "tmpfs" and not allow_non_tmpfs:
        raise TmpfsWorkspaceError(
            f"managed artifact root is not tmpfs: {root} ({fs_type or 'unknown'})")
    for name in ("artifacts", "runs", "leases"):
        (root / name).mkdir(exist_ok=True)
    return root


def _slug(value: str, limit: int = 72) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return (clean or "artifact")[:limit]


def _entry_key(kind: str, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    # Sim keys intentionally spell out geometry, fps, fit and caps. Preserve
    # enough of that human-readable identity to make tmpfs inspection useful.
    return f"{_slug(kind, 20)}-{_slug(key, 180)}-{digest}"


def _pid_alive(pid: int) -> bool:
    return pid > 0 and Path(f"/proc/{pid}").exists()


def _lease_records(root: Path) -> list[tuple[Path, dict]]:
    records = []
    for marker in (root / "leases").glob("*.json"):
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
            pid = int(record.get("pid", 0))
            entry = Path(record["entry"]).resolve()
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            marker.unlink(missing_ok=True)
            continue
        if not _pid_alive(pid):
            marker.unlink(missing_ok=True)
            continue
        record["entry"] = str(entry)
        records.append((marker, record))
    return records


def _active_entries(root: Path) -> set[Path]:
    return {Path(record["entry"]) for _marker, record in _lease_records(root)}


def _remove_alias_for_entry(entry: Path) -> None:
    metadata = entry / ".managed.json" if entry.is_dir() else None
    if metadata is None or not metadata.is_file():
        return
    try:
        alias = Path(json.loads(metadata.read_text(encoding="utf-8"))["alias"])
    except (OSError, KeyError, json.JSONDecodeError):
        return
    if not alias.is_symlink():
        return
    try:
        target = alias.resolve(strict=False)
    except OSError:
        return
    try:
        target.relative_to(entry.resolve())
    except ValueError:
        return
    alias.unlink(missing_ok=True)


def _remove_entry(entry: Path) -> None:
    if entry.is_dir():
        _remove_alias_for_entry(entry)
        shutil.rmtree(entry)
    else:
        entry.unlink(missing_ok=True)


def _minimum_free_bytes() -> int:
    raw = os.environ.get("SEGACD_TMPFS_MIN_FREE_GB")
    if raw is None:
        return DEFAULT_MIN_FREE_BYTES
    return max(0, int(float(raw) * 1024 ** 3))


def evict_old_entries(
    required_bytes: int = 0,
    *,
    root: Path | None = None,
    exclude: tuple[Path, ...] = (),
) -> list[Path]:
    """Delete oldest inactive managed entries until requested free space exists."""

    root = ensure_root(root)
    required = max(0, int(required_bytes))
    target_free = required + _minimum_free_bytes()
    usage = shutil.disk_usage(root)
    if target_free > usage.total:
        raise TmpfsWorkspaceError(
            f"tmpfs request exceeds capacity: need {target_free / 1024**3:.1f} GiB, "
            f"total {usage.total / 1024**3:.1f} GiB")
    if usage.free >= target_free:
        return []

    active = _active_entries(root)
    excluded = {path.resolve() for path in exclude}
    candidates = []
    for parent in (root / "artifacts", root / "runs"):
        for entry in parent.iterdir():
            resolved = entry.resolve()
            if resolved in active or resolved in excluded:
                continue
            try:
                stamp = entry.stat().st_mtime_ns
            except FileNotFoundError:
                continue
            candidates.append((stamp, entry))
    candidates.sort(key=lambda item: (item[0], item[1].name))

    removed = []
    for _stamp, entry in candidates:
        _remove_entry(entry)
        removed.append(entry)
        print(f"tmpfs eviction: removed {entry}", flush=True)
        if shutil.disk_usage(root).free >= target_free:
            break
    if shutil.disk_usage(root).free < target_free:
        raise TmpfsWorkspaceError(
            "tmpfs is still short after deleting every inactive project artifact; "
            "active runs were preserved")
    return removed


@dataclass
class Lease:
    entry: Path
    marker: Path
    reused: bool = False

    def release(self) -> None:
        self.marker.unlink(missing_ok=True)
        try:
            os.utime(self.entry, None)
        except FileNotFoundError:
            pass


def acquire_lease(entry: Path, *, root: Path | None = None) -> Lease:
    root = ensure_root(root)
    entry = entry.resolve()
    token = f"{os.getpid()}-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    marker = root / "leases" / f"{token}.json"
    marker.write_text(json.dumps({
        "pid": os.getpid(),
        "entry": str(entry),
        "created_ns": time.time_ns(),
    }, sort_keys=True) + "\n", encoding="utf-8")
    return Lease(entry=entry, marker=marker)


def _validate_video_alias(alias: Path) -> None:
    if os.environ.get("SEGACD_TMPFS_ALLOW_ANY_ALIAS", "0") in {
            "1", "true", "yes", "on"}:
        return
    if not is_video_alias(alias):
        raise TmpfsWorkspaceError(
            f"managed artifact alias must stay below videos/: {alias}")


def is_video_alias(path: Path) -> bool:
    videos = (PROJECT_ROOT / "videos").resolve()
    try:
        Path(path).absolute().relative_to(videos)
    except ValueError:
        return False
    return True


def _replace_alias(alias: Path, target: Path, *, directory: bool) -> None:
    _validate_video_alias(alias)
    alias.parent.mkdir(parents=True, exist_ok=True)
    if alias.is_symlink() or alias.is_file():
        alias.unlink()
    elif alias.exists():
        shutil.rmtree(alias)
    alias.symlink_to(target.resolve(), target_is_directory=directory)


def activate_directory(
    alias: Path,
    *,
    kind: str,
    key: str,
    required_bytes: int = 0,
    root: Path | None = None,
    reuse_token: str | None = None,
) -> Lease:
    """Expose a managed directory, reusing only an authenticated completion."""

    root = ensure_root(root)
    entry = root / "artifacts" / _entry_key(kind, key)
    active = _active_entries(root)
    if entry.resolve() in active:
        raise TmpfsWorkspaceError(f"artifact directory is already active: {entry}")
    complete_path = entry / ".complete.json"
    if entry.is_dir() and reuse_token is not None and complete_path.is_file():
        try:
            complete = json.loads(complete_path.read_text(encoding="utf-8"))
            metadata = json.loads(
                (entry / ".managed.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            complete = {}
            metadata = {}
        if (complete.get("reuse_token") == reuse_token
                and metadata.get("kind") == kind
                and metadata.get("key") == key
                and (entry / "data").is_dir()):
            _replace_alias(alias, entry / "data", directory=True)
            lease = acquire_lease(entry, root=root)
            lease.reused = True
            return lease
    if entry.exists():
        _remove_entry(entry)
    evict_old_entries(required_bytes, root=root)
    entry.mkdir(parents=True)
    data = entry / "data"
    data.mkdir()
    (entry / ".managed.json").write_text(json.dumps({
        "alias": str(alias.absolute()), "kind": kind, "key": key,
    }, sort_keys=True) + "\n", encoding="utf-8")
    _replace_alias(alias, data, directory=True)
    return acquire_lease(entry, root=root)


def mark_directory_complete(
    lease: Lease,
    *,
    reuse_token: str,
    details: dict | None = None,
) -> None:
    """Atomically mark a leased managed directory safe for later reuse."""

    if not lease.marker.is_file():
        raise TmpfsWorkspaceError("cannot complete an unleased artifact")
    payload = {
        "reuse_token": reuse_token,
        "completed_ns": time.time_ns(),
    }
    if details:
        payload.update(details)
    target = lease.entry / ".complete.json"
    temporary = lease.entry / f".complete.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def allocate_file(
    alias: Path,
    *,
    kind: str,
    key: str | None = None,
    required_bytes: int = 0,
    root: Path | None = None,
) -> tuple[Path, Lease]:
    """Allocate a tmpfs target for one disposable videos/ file."""

    root = ensure_root(root)
    identity = key or str(alias.absolute())
    entry = root / "artifacts" / _entry_key(kind, identity)
    active = _active_entries(root)
    if entry.resolve() in active:
        raise TmpfsWorkspaceError(f"artifact file is already active: {entry}")
    if entry.exists():
        _remove_entry(entry)
    evict_old_entries(required_bytes, root=root)
    entry.mkdir(parents=True)
    actual = entry / alias.name
    (entry / ".managed.json").write_text(json.dumps({
        "alias": str(alias.absolute()), "kind": kind, "key": identity,
    }, sort_keys=True) + "\n", encoding="utf-8")
    return actual, acquire_lease(entry, root=root)


def publish_alias(alias: Path, actual: Path) -> None:
    if not actual.is_file():
        raise TmpfsWorkspaceError(f"tmpfs artifact was not produced: {actual}")
    _replace_alias(alias, actual, directory=False)


def lease_managed_alias(alias: Path, *, root: Path | None = None) -> Lease | None:
    """Lease an existing managed directory/file reached through its alias."""

    root = ensure_root(root)
    alias = Path(alias)
    try:
        target = alias.resolve(strict=True)
    except FileNotFoundError:
        return None
    artifacts = (root / "artifacts").resolve()
    try:
        relative = target.relative_to(artifacts)
    except ValueError:
        return None
    if not relative.parts:
        return None
    entry = artifacts / relative.parts[0]
    if not (entry / ".managed.json").is_file():
        return None
    return acquire_lease(entry, root=root)


def create_run_directory(
    key: str,
    *,
    required_bytes: int = 0,
    root: Path | None = None,
) -> Lease:
    root = ensure_root(root)
    evict_old_entries(required_bytes, root=root)
    entry = root / "runs" / (
        f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-"
        f"{_slug(key, 40)}-{uuid.uuid4().hex[:8]}")
    entry.mkdir(parents=True)
    return acquire_lease(entry, root=root)


def remove_run_directory(lease: Lease) -> None:
    lease.release()
    if lease.entry.exists():
        shutil.rmtree(lease.entry)


def run_file_command(
    alias: Path,
    *,
    kind: str,
    required_bytes: int,
    command: list[str],
) -> None:
    """Run a producer against a leased target, then publish its videos/ alias."""

    if not command:
        raise TmpfsWorkspaceError("tmpfs file producer command is empty")
    if all("{output}" not in part for part in command):
        raise TmpfsWorkspaceError(
            "tmpfs file producer command must contain {output}")
    actual, lease = allocate_file(
        alias, kind=kind, required_bytes=required_bytes)
    expanded = [part.replace("{output}", str(actual)) for part in command]
    try:
        subprocess.run(expanded, check=True)
        publish_alias(alias, actual)
    finally:
        lease.release()


def _parse_cli() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    run_file = subparsers.add_parser(
        "run-file", help="run a producer and publish one videos/ file")
    run_file.add_argument("--output", required=True, type=Path)
    run_file.add_argument("--kind", required=True)
    run_file.add_argument("--required-gb", type=float, default=1.0)
    run_file.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args()


def _main() -> None:
    args = _parse_cli()
    if args.action == "run-file":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        run_file_command(
            args.output,
            kind=args.kind,
            required_bytes=max(0, int(args.required_gb * 1024 ** 3)),
            command=command,
        )
        print(args.output, flush=True)


if __name__ == "__main__":
    _main()
