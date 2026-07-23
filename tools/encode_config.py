#!/usr/bin/env python3
"""Load one reproducible per-source encode profile from TOML.

The encoder still has mature ``CBRSIM_*`` internals.  This module is the only
translation layer from the public TOML profile to those internals.  Profile
values replace inherited per-source environment variables so a shell left over
from another encode cannot silently change the result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from fractions import Fraction
from dataclasses import dataclass
from pathlib import Path
from typing import Any, MutableMapping

import av_config


SCHEMA_VERSION = 1
ARTIFACT_ROOT = Path("out")
TEMP_ROOT = Path("tmp")
_ARTIFACT_STEM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# VRAM tile 0 is clear, then the resident movie pool starts at tile 1 and runs
# right up to the first movie name table at tile 1536 (0xC000).  The shared
# hexadecimal HUD font no longer sits above the pool: it is fixed in the unused
# 0xD000-0xDFFF gap between NT0 and NT1 (VRAM_HUD_FONT_TILE), so the pool ceiling
# is a full 1535 slots in both DEBUG and release builds.
VRAM_PATTERN_BASE_TILE = 1
VRAM_FIRST_MOVIE_NT_TILE = 0xC000 // 32
HUD_FONT_TILES = 16
VRAM_HUD_FONT_TILE = 0xD000 // 32  # fixed font base, tiles 1664..1679
MAX_RESIDENT_VRAM_TILES = (
    VRAM_FIRST_MOVIE_NT_TILE - VRAM_PATTERN_BASE_TILE)

# (section, key): legacy internal variable.  Keeping this table in one place is
# deliberate: TOML is the user interface; CBRSIM_* is an implementation detail.
ENV_MAP = {
    ("source", "path"): "CBRSIM_SRC",
    ("source", "fps"): "CBRSIM_FPS",
    ("source", "duration"): "CBRSIM_DURATION",
    ("source", "sar"): "CBRSIM_SOURCE_SAR",
    ("video", "mode"): "CBRSIM_MODE",
    ("video", "width"): "CBRSIM_W",
    ("video", "height"): "CBRSIM_H",
    ("video", "active_tiles"): "CBRSIM_ACTIVE_TILES",
    ("video", "fit"): "CBRSIM_GEOMETRY_FIT",
    ("video", "resize_filter"): "CBRSIM_RESIZE_FILTER",
    ("video", "master_denoise"): "CBRSIM_MASTER_DENOISE",
    ("video", "master_filter"): "CBRSIM_MASTER_VF",
    ("video", "raw_filter"): "CBRSIM_RAW_VF",
    ("audio", "kind"): "CBRSIM_AUDIO",
    ("output", "directory"): "CBRSIM_OUT",
    ("output", "reuse"): "CBRSIM_REUSE",
    ("output", "emit_decisions"): "CBRSIM_EMIT_DEC",
    ("encoder", "gpu"): "CBRSIM_GPU",
    ("encoder", "vram_tiles"): "CBRSIM_VRAM_TILES",
    ("encoder", "dither"): "CBRSIM_DITHER",
    ("encoder", "segment_palettes"): "CBRSIM_SEGPAL",
    ("encoder", "near"): "CBRSIM_NEAR",
    ("encoder", "boot_vram_prefetch"): "CBRSIM_BOOT_VRAM_PREFETCH",
    ("encoder", "raw_prefetch"): "CBRSIM_RAW_PREFETCH",
    ("palette", "algorithm"): "CBRSIM_PAL_ALGO",
    ("palette", "map_weight"): "CBRSIM_PAL_MAP_WEIGHT",
    ("palette", "seam_weight"): "CBRSIM_PAL_SEAM_WEIGHT",
    ("palette", "seam_iterations"): "CBRSIM_PAL_SEAM_ITERATIONS",
    ("palette", "sample_counts"): "CBRSIM_PAL_SAMPLE_COUNTS",
    ("palette", "validate_frames"): "CBRSIM_PAL_VALIDATE_FRAMES",
    ("palette", "segment_train_frames"): "CBRSIM_PAL_SEG_TRAIN_FRAMES",
    ("palette", "segment_validate_frames"): "CBRSIM_PAL_SEG_VALIDATE_FRAMES",
    ("palette", "segment_gain_relative"): "CBRSIM_PAL_SEG_GAIN_REL",
    ("palette", "segment_gain_per_pixel"): "CBRSIM_PAL_SEG_GAIN_ABS",
}
PROFILE_ENV_DEFAULTS = {
    "CBRSIM_PREPROCESS_ENDPOINT_SNAP_BLACK_MAX": "-1",
    "CBRSIM_PREPROCESS_ENDPOINT_SNAP_WHITE_MIN": "256",
    "CBRSIM_RESIZE_FILTER": "lanczos",
    "CBRSIM_MASTER_DENOISE": "1",
    "CBRSIM_QUALITY_BUDGET_KB": str(av_config.QUALITY_BUDGET_KB),
    "CBRSIM_BOOT_VRAM_PREFETCH": "1",
    "CBRSIM_RAW_PREFETCH": "0",
}

ALLOWED = {
    "source": ({key for section, key in ENV_MAP if section == "source"}
               | {"preprocess"}),
    "video": {key for section, key in ENV_MAP if section == "video"},
    "audio": {key for section, key in ENV_MAP if section == "audio"},
    "output": {key for section, key in ENV_MAP if section == "output"},
    "encoder": {key for section, key in ENV_MAP if section == "encoder"},
    "palette": {key for section, key in ENV_MAP if section == "palette"},
    "pack": {"fill", "startup_audio_frames", "output"},
}
REQUIRED = {
    "source": {"path", "fps", "duration"},
    "video": {"mode", "width", "height", "fit"},
    "audio": {"kind"},
    "output": {"directory", "emit_decisions"},
    "palette": {"algorithm"},
}


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, list):
        return ",".join(_toml_scalar(item) for item in value)
    return str(value)


@dataclass(frozen=True)
class EncodeProfile:
    path: Path
    data: dict[str, Any]
    sha256: str

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.data.get(name, {}))

    @property
    def output_dir(self) -> Path:
        return Path(self.data["output"]["directory"])

    @property
    def decision_log(self) -> Path:
        return self.output_dir / "decisions.pkl"

    @property
    def artifact_stem(self) -> str:
        """Stable build name derived only from the TOML filename."""
        stem = self.path.stem
        if not _ARTIFACT_STEM_RE.fullmatch(stem):
            raise ValueError(
                f"{self.path}: TOML filename stem must match "
                "[A-Za-z0-9][A-Za-z0-9._-]*")
        return stem

    @property
    def artifact_dir(self) -> Path:
        return ARTIFACT_ROOT / self.artifact_stem

    @property
    def pack_output(self) -> Path:
        return self.artifact_dir / "MOVIE.DAT"

    @property
    def temp_dir(self) -> Path:
        return TEMP_ROOT / self.artifact_stem

    @property
    def build_dir(self) -> Path:
        return self.temp_dir / "build"

    @property
    def disc_staging_dir(self) -> Path:
        return self.temp_dir / "disc"

    @property
    def disc_iso(self) -> Path:
        return ARTIFACT_ROOT / f"{self.artifact_stem}.iso"

    @property
    def disc_cue(self) -> Path:
        return ARTIFACT_ROOT / f"{self.artifact_stem}.cue"


def load_profile(path: str | os.PathLike[str]) -> EncodeProfile:
    profile_path = Path(path).expanduser().resolve()
    raw = profile_path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    version = data.pop("schema_version", None)
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{profile_path}: schema_version must be {SCHEMA_VERSION}, got {version!r}")
    unknown_sections = set(data) - set(ALLOWED)
    if unknown_sections:
        raise ValueError(
            f"{profile_path}: unknown sections: {', '.join(sorted(unknown_sections))}")
    for section, values in data.items():
        if not isinstance(values, dict):
            raise ValueError(f"{profile_path}: [{section}] must be a table")
        unknown = set(values) - ALLOWED[section]
        if unknown:
            raise ValueError(
                f"{profile_path}: unknown [{section}] keys: {', '.join(sorted(unknown))}")
    for section, keys in REQUIRED.items():
        missing = keys - set(data.get(section, {}))
        if missing:
            raise ValueError(
                f"{profile_path}: missing [{section}] keys: {', '.join(sorted(missing))}")
    mode = str(data["video"]["mode"]).upper()
    if mode not in {"H32", "H40", "MODE4"}:
        raise ValueError(f"{profile_path}: unsupported video.mode {mode!r}")
    if int(data["video"]["width"]) % 8 or int(data["video"]["height"]) % 8:
        raise ValueError(f"{profile_path}: video width and height must be multiples of 8")
    total_tiles = int(data["video"]["width"]) * int(data["video"]["height"]) // 64
    active_tiles = int(data["video"].get("active_tiles", total_tiles))
    if not 1 <= active_tiles <= total_tiles:
        raise ValueError(
            f"{profile_path}: video.active_tiles must be within 1..{total_tiles}")
    vram_tiles = int(data.get("encoder", {}).get(
        "vram_tiles", MAX_RESIDENT_VRAM_TILES))
    if not 1 <= vram_tiles <= MAX_RESIDENT_VRAM_TILES:
        raise ValueError(
            f"{profile_path}: encoder.vram_tiles must be within "
            f"1..{MAX_RESIDENT_VRAM_TILES} so the resident pool stays below "
            "the movie name table at tile 1536")
    try:
        source_fps = float(Fraction(str(data["source"]["fps"])))
        av_config.cold_cap_for_fps(source_fps, mode, active_tiles)
    except av_config.ColdCapMeasurementRequired as exc:
        raise ValueError(f"{profile_path}: {exc}") from exc
    if str(data["video"]["fit"]).lower() not in {"pad", "crop"}:
        raise ValueError(f"{profile_path}: video.fit must be 'pad' or 'crop'")
    audio_kind = str(data["audio"]["kind"]).lower()
    if audio_kind not in {"pcm13", "adpcm22"}:
        raise ValueError(
            f"{profile_path}: audio.kind must be 'pcm13' or 'adpcm22'")
    resize_filter = str(data["video"].get("resize_filter", "lanczos")).lower()
    if resize_filter not in {"area", "bicubic", "bilinear", "lanczos", "neighbor"}:
        raise ValueError(
            f"{profile_path}: video.resize_filter must be area, bicubic, "
            "bilinear, lanczos, or neighbor")
    preprocess = data["source"].get("preprocess", {})
    if not isinstance(preprocess, dict):
        raise ValueError(f"{profile_path}: [source.preprocess] must be a table")
    unknown_preprocess = set(preprocess) - {"endpoint_snap"}
    if unknown_preprocess:
        raise ValueError(
            f"{profile_path}: unknown [source.preprocess] keys: "
            f"{', '.join(sorted(unknown_preprocess))}")
    if "endpoint_snap" in preprocess:
        endpoint_snap = preprocess["endpoint_snap"]
        if not isinstance(endpoint_snap, dict):
            raise ValueError(
                f"{profile_path}: [source.preprocess.endpoint_snap] must be a table")
        unknown_snap = set(endpoint_snap) - {"black_max", "white_min"}
        if unknown_snap:
            raise ValueError(
                f"{profile_path}: unknown [source.preprocess.endpoint_snap] keys: "
                f"{', '.join(sorted(unknown_snap))}")
        missing_snap = {"black_max", "white_min"} - set(endpoint_snap)
        if missing_snap:
            raise ValueError(
                f"{profile_path}: missing [source.preprocess.endpoint_snap] keys: "
                f"{', '.join(sorted(missing_snap))}")
        black_max = int(endpoint_snap["black_max"])
        white_min = int(endpoint_snap["white_min"])
        if not 0 <= black_max <= 255 or not 0 <= white_min <= 255:
            raise ValueError(
                f"{profile_path}: endpoint snap limits must be within 0..255")
        if black_max >= white_min:
            raise ValueError(
                f"{profile_path}: endpoint snap black_max must be below white_min")
    profile = EncodeProfile(profile_path, data, hashlib.sha256(raw).hexdigest())
    # Validate the filename while loading so every consumer agrees on paths.
    profile.artifact_stem
    return profile


def apply_profile_env(
        profile: EncodeProfile,
        environ: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Apply all TOML-backed values, replacing inherited values unconditionally."""
    env = os.environ if environ is None else environ
    applied: dict[str, str] = {}
    for (section, key), name in ENV_MAP.items():
        values = profile.data.get(section, {})
        if key not in values:
            continue
        value = _toml_scalar(values[key])
        env[name] = value
        applied[name] = value
    # Profiles without confirmed black-only tiles conservatively use the full
    # output grid. Always overwrite an inherited value so one source cannot
    # silently lend its smaller active area to the next encode.
    if "CBRSIM_ACTIVE_TILES" not in applied:
        video = profile.data["video"]
        value = str(int(video["width"]) * int(video["height"]) // 64)
        env["CBRSIM_ACTIVE_TILES"] = value
        applied["CBRSIM_ACTIVE_TILES"] = value
    for name, value in PROFILE_ENV_DEFAULTS.items():
        if name not in applied:
            env[name] = value
            applied[name] = value
    endpoint_snap = (profile.data["source"].get("preprocess", {})
                     .get("endpoint_snap"))
    if endpoint_snap is not None:
        snap_env = {
            "CBRSIM_PREPROCESS_ENDPOINT_SNAP_BLACK_MAX": endpoint_snap["black_max"],
            "CBRSIM_PREPROCESS_ENDPOINT_SNAP_WHITE_MIN": endpoint_snap["white_min"],
        }
        for name, value in snap_env.items():
            scalar = _toml_scalar(value)
            env[name] = scalar
            applied[name] = scalar
    env["CBRSIM_CONFIG"] = str(profile.path)
    applied["CBRSIM_CONFIG"] = str(profile.path)
    return applied


def consume_config_arg(
    argv: list[str] | None = None,
    *,
    required: bool = False,
) -> EncodeProfile | None:
    """Consume a required first positional profile and apply it immediately.

    ``sim.py`` and ``render_analysis.py`` evaluate settings at import time, so
    this small pre-parser must run before their other local imports. Import-only
    callers pass ``required=False`` and retain the historical no-profile test
    defaults; executable entry points pass ``required=True``.
    """
    args = sys.argv if argv is None else argv
    if not required:
        return None
    if len(args) < 2:
        raise SystemExit(
            "encode profile is required as the first positional argument: "
            f"{Path(args[0]).name} PROFILE.toml")
    config = args[1]
    if config == "--config" or config.startswith("--config="):
        raise SystemExit(
            "encode profile is positional; do not use --config: "
            f"{Path(args[0]).name} PROFILE.toml")
    if config.startswith("-"):
        raise SystemExit(
            "encode profile must be the first positional argument: "
            f"{Path(args[0]).name} PROFILE.toml")
    del args[1]
    try:
        profile = load_profile(config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"invalid encode profile: {exc}") from exc
    apply_profile_env(profile)
    return profile


def profile_identity(profile: EncodeProfile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {"path": str(profile.path), "sha256": profile.sha256,
            "schema_version": SCHEMA_VERSION}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--print-env", action="store_true")
    output.add_argument("--print-stem", action="store_true")
    output.add_argument("--print-artifacts", action="store_true")
    args = parser.parse_args()
    try:
        profile = load_profile(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.print_env:
        print(json.dumps(apply_profile_env(profile, {}), indent=2, sort_keys=True))
    elif args.print_stem:
        print(profile.artifact_stem)
    elif args.print_artifacts:
        print(json.dumps({
            "stem": profile.artifact_stem,
            "directory": str(profile.artifact_dir),
            "pack": str(profile.pack_output),
            "temporary": str(profile.temp_dir),
            "build": str(profile.build_dir),
            "disc_staging": str(profile.disc_staging_dir),
            "iso": str(profile.disc_iso),
            "cue": str(profile.disc_cue),
        }, indent=2, sort_keys=True))
    else:
        print(json.dumps({"path": str(profile.path), "sha256": profile.sha256,
                          "output": str(profile.output_dir),
                          "artifacts": str(profile.artifact_dir)}, indent=2))


if __name__ == "__main__":
    main()
