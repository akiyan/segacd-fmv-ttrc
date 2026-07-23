#!/usr/bin/env python3
"""Stable identities and validation for completed sim artifacts.

The cache identity deliberately ignores profile filenames, TOML formatting,
and output paths.  It authenticates the source bytes, every effective
``CBRSIM_*`` input that can affect the encode, pack settings, and the encoder
implementation instead.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import pickle
from typing import Any, Mapping

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_SCHEMA_VERSION = 1

# Performance controls and per-pass plumbing do not change encoded decisions.
_IGNORED_ENV_EXACT = {
    "CBRSIM_CONFIG",
    "CBRSIM_DELIVERY_COLD_CAPS",
    "CBRSIM_DELIVERY_REPAIR_REQUEST",
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

# Keep this list focused on files that can change sim decisions or their
# physical accounting.  Render-only layout/style changes must not force a sim.
ENCODER_FILES = (
    "tools/sim.py",
    "tools/av_config.py",
    "tools/gpu_quant.py",
    "tools/ima_adpcm.py",
    "tools/palette_algorithms.py",
    "tools/pattern_supply.py",
    "tools/quantize_global4_tiles.py",
    "tools/quantize_md_video.py",
    "tools/raw_prefetch.py",
    "tools/shadow_updates.py",
    "tools/sim_pass_cache.py",
    "tools/stream_schedule.py",
    "tools/tile_alloc.py",
    "tools/ttrc_routing.py",
    "tools/upgrade_planner.py",
    "tools/video_geometry.py",
)


class CacheValidationError(RuntimeError):
    pass


class _DocstringStripper(ast.NodeTransformer):
    def _strip(self, node: ast.AST) -> ast.AST:
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:]
        return node

    def visit_Module(self, node: ast.Module) -> ast.AST:
        return self.generic_visit(self._strip(node))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        return self.generic_visit(self._strip(node))

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        return self.generic_visit(self._strip(node))

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        return self.generic_visit(self._strip(node))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _python_semantic_bytes(path: Path) -> bytes:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tree = _DocstringStripper().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(
        tree, annotate_fields=True, include_attributes=False).encode("utf-8")


def encoder_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative in ENCODER_FILES:
        path = PROJECT_ROOT / relative
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(_python_semantic_bytes(path))
        digest.update(b"\0")
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
        pack: Mapping[str, Any],
        emit_decisions: bool,
        environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    identity = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "source": source_identity(source),
        "effective_environment": effective_environment(environ),
        "pack": dict(sorted(pack.items())),
        "emit_decisions": bool(emit_decisions),
        "encoder_sha256": encoder_fingerprint(),
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
        "pack": identity["pack"],
        "emit_decisions": identity["emit_decisions"],
    }
    settings_sha = identity_sha256(settings)
    return (
        f"{source['name']}-{mode.upper()}-{width}x{height}-{fps}fps-"
        f"fit-{fit}-cold{cold_cap}-"
        f"src{source['sha256'][:8]}-cfg{settings_sha[:8]}-"
        f"enc{identity['encoder_sha256'][:8]}"
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
