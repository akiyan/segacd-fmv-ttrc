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
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, MutableMapping


SCHEMA_VERSION = 1

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
    ("video", "fit"): "CBRSIM_GEOMETRY_FIT",
    ("video", "master_filter"): "CBRSIM_MASTER_VF",
    ("video", "raw_filter"): "CBRSIM_RAW_VF",
    ("audio", "kind"): "CBRSIM_AUDIO",
    ("output", "directory"): "CBRSIM_OUT",
    ("output", "reuse"): "CBRSIM_REUSE",
    ("output", "emit_decisions"): "CBRSIM_EMIT_DEC",
    ("encoder", "gpu"): "CBRSIM_GPU",
    ("encoder", "rate_kib"): "CBRSIM_RATE_KIB",
    ("encoder", "vram_tiles"): "CBRSIM_VRAM_TILES",
    ("encoder", "dither"): "CBRSIM_DITHER",
    ("encoder", "segment_palettes"): "CBRSIM_SEGPAL",
    ("encoder", "near"): "CBRSIM_NEAR",
    ("encoder", "coa"): "CBRSIM_COA",
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

ALLOWED = {
    "source": {key for section, key in ENV_MAP if section == "source"},
    "video": {key for section, key in ENV_MAP if section == "video"},
    "audio": {key for section, key in ENV_MAP if section == "audio"},
    "output": {key for section, key in ENV_MAP if section == "output"},
    "encoder": {key for section, key in ENV_MAP if section == "encoder"},
    "palette": {key for section, key in ENV_MAP if section == "palette"},
    "pack": {"debug", "fill", "startup_audio_frames", "output"},
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
    if str(data["video"]["fit"]).lower() not in {"pad", "crop"}:
        raise ValueError(f"{profile_path}: video.fit must be 'pad' or 'crop'")
    return EncodeProfile(profile_path, data, hashlib.sha256(raw).hexdigest())


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
    env["CBRSIM_CONFIG"] = str(profile.path)
    applied["CBRSIM_CONFIG"] = str(profile.path)
    return applied


def consume_config_arg(argv: list[str] | None = None) -> EncodeProfile | None:
    """Remove ``--config`` from argv and apply the selected profile immediately.

    sim.py and render_analysis.py evaluate settings at import time, so this small
    pre-parser must run before their other local imports.
    """
    args = sys.argv if argv is None else argv
    config: str | None = None
    clean = [args[0]]
    i = 1
    while i < len(args):
        arg = args[i]
        if arg == "--config":
            if i + 1 >= len(args):
                raise SystemExit("--config requires a TOML path")
            config = args[i + 1]
            i += 2
            continue
        if arg.startswith("--config="):
            config = arg.split("=", 1)[1]
            i += 1
            continue
        clean.append(arg)
        i += 1
    args[:] = clean
    if not config:
        return None
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
    parser.add_argument("--print-env", action="store_true")
    args = parser.parse_args()
    try:
        profile = load_profile(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.print_env:
        print(json.dumps(apply_profile_env(profile, {}), indent=2, sort_keys=True))
    else:
        print(json.dumps({"path": str(profile.path), "sha256": profile.sha256,
                          "output": str(profile.output_dir)}, indent=2))


if __name__ == "__main__":
    main()
