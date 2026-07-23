#!/usr/bin/env python3
"""Persistent, uniquely named analysis TSV paths and compatibility aliases."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AV_VERSION_PATH = Path(__file__).resolve().parent / "av_version.txt"


def encoder_version(path: Path = AV_VERSION_PATH) -> str:
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    if not values.get("e", "").isdigit():
        raise ValueError(f"encoder version is missing from {path}")
    return f"e{values['e']}"


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return text or "profile"


def log_root() -> Path:
    configured = os.environ.get("ANALYSIS_LOG_DIR")
    return (Path(configured).expanduser() if configured
            else PROJECT_ROOT / "logs").resolve()


def unique_tsv_path(profile, *, now: datetime | None = None) -> Path:
    """Allocate a persistent filename with time, profile, hash, and encoder."""

    root = log_root()
    root.mkdir(parents=True, exist_ok=True)
    moment = (now or datetime.now().astimezone())
    stamp = moment.strftime("%Y%m%d-%H%M%S-%f")
    profile_name = _slug(Path(profile.path).stem)
    checksum = str(profile.sha256)[:10]
    version = encoder_version()
    base = f"{stamp}_{profile_name}_{checksum}_{version}"
    candidate = root / f"{base}.tsv"
    sequence = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = root / f"{base}_{sequence:02d}.tsv"
        sequence += 1
    return candidate


def publish_alias(alias: Path, log_path: Path) -> None:
    """Point the traditional analysis TSV path at its persistent log."""

    alias = alias.absolute()
    log_path = log_path.resolve()
    if alias == log_path:
        return
    alias.parent.mkdir(parents=True, exist_ok=True)
    if alias.is_symlink() or alias.is_file():
        alias.unlink()
    elif alias.exists():
        raise IsADirectoryError(f"analysis TSV alias is a directory: {alias}")
    alias.symlink_to(log_path)
