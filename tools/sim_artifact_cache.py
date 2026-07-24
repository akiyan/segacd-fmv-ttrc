#!/usr/bin/env python3
"""Stable identities and validation for completed sim artifacts.

The cache identity deliberately ignores profile filenames, TOML formatting,
output paths, and individual source-file hashes. It authenticates the source
bytes, every effective ``CBRSIM_*`` input, and the public encoder version.
Output-affecting code or fixed-policy changes must bump that version.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pickle
from typing import Any, Mapping

import numpy as np
from analysis_logs import encoder_version


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_SCHEMA_VERSION = 3

# Performance controls and per-pass plumbing do not change encoded decisions.
_IGNORED_ENV_EXACT = {
    "CBRSIM_CONFIG",
    "CBRSIM_EMIT_DEC",
    "CBRSIM_FORCE_REENCODE",
    "CBRSIM_LOOP_PROFILE_INTERVAL",
    "CBRSIM_NOPANELS",
    "CBRSIM_OUT",
    "CBRSIM_PASS_CACHE",
    "CBRSIM_PASS_CACHE_INVOCATION",
    "CBRSIM_PNG_WORKERS",
    "CBRSIM_REUSE",
    "CBRSIM_SLOT_LOCALITY_MAP",
    "CBRSIM_SLOT_LOCALITY_RETRY_ALLOWED",
    "CBRSIM_SLOT_LOCALITY_RETRY_MAP",
    "CBRSIM_SLOT_LOCALITY_REUSE",
    "CBRSIM_SLOT_LOCALITY_STAGE",
    "CBRSIM_TMPFS_PREPARED",
    "CBRSIM_WORKERS",
}

class CacheValidationError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_identity(path: str | os.PathLike[str]) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    stat = source.stat()
    return {
        "name": source.stem,
        "bytes": int(stat.st_size),
        "sha256": _sha256_file(source),
    }


def effective_environment(
        environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    values = {}
    for name, value in env.items():
        if not name.startswith("CBRSIM_") or name in _IGNORED_ENV_EXACT:
            continue
        values[name] = str(value)
    # The source is represented by its byte hash, not by its filesystem path.
    values.pop("CBRSIM_SRC", None)
    return dict(sorted(values.items()))


def build_identity(
        *,
        source: str | os.PathLike[str],
        emit_decisions: bool,
        environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    identity = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "source": source_identity(source),
        "effective_environment": effective_environment(environ),
        "emit_decisions": bool(emit_decisions),
        "encoder_version": encoder_version(),
    }
    return identity


def identity_sha256(identity: Mapping[str, Any]) -> str:
    raw = json.dumps(
        identity, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def readable_key(
        identity: Mapping[str, Any],
        *,
        mode: str,
        width: int,
        height: int,
        fps: str,
        fit: str,
        cold_cap: int,
) -> str:
    source = identity["source"]
    settings = {
        "effective_environment": identity["effective_environment"],
        "emit_decisions": identity["emit_decisions"],
    }
    settings_sha = identity_sha256(settings)
    return (
        f"{source['name']}-{mode.upper()}-{width}x{height}-{fps}fps-"
        f"fit-{fit}-cold{cold_cap}-"
        f"src{source['sha256'][:8]}-cfg{settings_sha[:8]}-"
        f"enc-{identity['encoder_version']}"
    )


def validate_completed_data(
        data: Path,
        identity: Mapping[str, Any],
        *,
        marker: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Validate the artifact needed by packing and analysis rendering."""

    data = Path(data)
    required = (
        "decisions.pkl", "stats.npz", "buffer_remaining.npz",
        "miss_masks.npy", "palettes.bin", "seg_palettes.npz",
        "audio_22k05_s16_mono.wav", "audio_playback_adpcm22_rf5c.wav",
    )
    missing = [name for name in required if not (data / name).is_file()]
    if missing:
        raise CacheValidationError(
            f"completed sim artifact is missing: {', '.join(missing)}")

    if marker is not None:
        expected = identity_sha256(identity)
        if marker.get("identity_sha256") != expected:
            raise CacheValidationError("completed sim identity does not match")
        if marker.get("identity") != identity:
            raise CacheValidationError("completed sim metadata does not match")

    with (data / "decisions.pkl").open("rb") as source:
        decisions = pickle.load(source)
    frames = decisions.get("frames")
    frame_seg = np.asarray(decisions.get("frame_seg", ()))
    if not isinstance(frames, list) or not frames:
        raise CacheValidationError("decision log has no frames")
    frame_count = len(frames)
    if frame_seg.shape != (frame_count,):
        raise CacheValidationError("decision frame_seg length differs")
    if int(decisions.get("max_cold", -1)) <= 0:
        raise CacheValidationError("decision log has no valid cold cap")

    with np.load(data / "stats.npz") as stats:
        stats_lengths = {
            int(value.shape[0])
            for value in stats.values()
            if isinstance(value, np.ndarray) and value.ndim >= 1
        }
    if frame_count not in stats_lengths:
        raise CacheValidationError("stats.npz does not contain the full trace")

    for directory in ("master", "raw", "preview", "catmap"):
        count = sum(1 for _path in (data / directory).glob("*.png"))
        if count != frame_count:
            raise CacheValidationError(
                f"{directory} has {count} frames, expected {frame_count}")
    return {"frames": frame_count}
