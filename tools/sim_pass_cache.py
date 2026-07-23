#!/usr/bin/env python3
"""Invocation-local cache shared by sim seed and accounting subprocesses."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import pickle
from typing import Any

from analysis_logs import encoder_version


SCHEMA_VERSION = 1
TOOLS = Path(__file__).resolve().parent
CODE_FILES = (
    "sim.py",
    "upgrade_planner.py",
    "tile_alloc.py",
    "pattern_supply.py",
    "raw_prefetch.py",
    "palette_algorithms.py",
    "quantize_md_video.py",
    "quantize_global4_tiles.py",
    "gpu_quant.py",
    "stream_schedule.py",
    "av_config.py",
    "encode_config.py",
    "video_geometry.py",
)


class PassCacheError(RuntimeError):
    pass


def code_identity() -> str:
    digest = hashlib.sha256()
    for name in CODE_FILES:
        path = TOOLS / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def source_identity(path: str | os.PathLike[str]) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    stat = source.stat()
    return {
        "path": str(source),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def expected_metadata(
    *,
    profile,
    source: str | os.PathLike[str],
    width: int,
    height: int,
    cells: int,
    active_tiles: int,
    fps: str,
    frame_count: int,
    invocation: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "invocation": str(invocation),
        "profile_sha256": str(profile.sha256),
        "profile_path": str(Path(profile.path).resolve()),
        "source": source_identity(source),
        "encoder_version": encoder_version(),
        "code_sha256": code_identity(),
        "geometry": {
            "width": int(width),
            "height": int(height),
            "cells": int(cells),
            "active_tiles": int(active_tiles),
            "fps": str(fps),
            "frame_count": int(frame_count),
        },
    }


def save(path: Path, metadata: dict[str, Any], payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        pickle.dump({
            "schema_version": SCHEMA_VERSION,
            "metadata": metadata,
            "payload": payload,
        }, stream, protocol=pickle.HIGHEST_PROTOCOL)
    temporary.replace(path)


def load(path: Path, expected: dict[str, Any]) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise PassCacheError(f"sim pass cache does not exist: {path}")
    with path.open("rb") as stream:
        record = pickle.load(stream)
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        raise PassCacheError("sim pass cache schema differs")
    actual = record.get("metadata")
    if actual != expected:
        differing = sorted(
            key for key in set((actual or {}).keys()) | set(expected.keys())
            if (actual or {}).get(key) != expected.get(key))
        raise PassCacheError(
            "sim pass cache identity differs: " + ", ".join(differing))
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise PassCacheError("sim pass cache payload is malformed")
    return payload
