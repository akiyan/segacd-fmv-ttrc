#!/usr/bin/env python3
"""Prove that packing is independent of inherited per-source CBRSIM values."""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
from encode_config import apply_profile_env, load_profile  # noqa: E402


PER_SOURCE_ENV = {
    "CBRSIM_SRC", "CBRSIM_FPS", "CBRSIM_DURATION", "CBRSIM_SOURCE_SAR",
    "CBRSIM_MODE", "CBRSIM_W", "CBRSIM_H", "CBRSIM_GEOMETRY_FIT",
    "CBRSIM_MASTER_VF", "CBRSIM_RAW_VF", "CBRSIM_AUDIO", "CBRSIM_OUT",
    "CBRSIM_VRAM_TILES", "CBRSIM_TANK_KB", "CBRSIM_RING_CAP_KB",
    "CBRSIM_MAX_COLD", "CBRSIM_RATE_KIB", "CBRSIM_PACK_DEBUG",
    "CBRSIM_PACK_FILL", "CBRSIM_STARTUP_AUDIO_FRAMES",
}
POLLUTED = {
    "CBRSIM_SRC": "wrong.mp4", "CBRSIM_FPS": "15", "CBRSIM_MODE": "H40",
    "CBRSIM_W": "320", "CBRSIM_H": "144", "CBRSIM_AUDIO": "adpcm22",
    "CBRSIM_VRAM_TILES": "7", "CBRSIM_TANK_KB": "1",
    "CBRSIM_RING_CAP_KB": "1", "CBRSIM_MAX_COLD": "1",
    "CBRSIM_PACK_DEBUG": "0", "CBRSIM_PACK_FILL": "0",
    "CBRSIM_STARTUP_AUDIO_FRAMES": "1",
}
ARTIFACTS = ("HEADER.DAT", "BODY.DAT", "MOVIE.DAT", "palettes.bin")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def run_pack(decision: Path, output: Path, env: dict[str, str]) -> None:
    command = [
        sys.executable, str(TOOLS / "pack_stream.py"),
        "--dec-log", str(decision), "--output", str(output), "--debug",
    ]
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def check_profiles() -> None:
    for name, mode, width in (
            ("bad-apple-h32.toml", "H32", "256"),
            ("bad-apple-h40.toml", "H40", "320")):
        profile = load_profile(ROOT / "configs" / name)
        env = {"CBRSIM_FPS": "999", "CBRSIM_MODE": "wrong", "CBRSIM_W": "1"}
        apply_profile_env(profile, env)
        assert env["CBRSIM_FPS"] == "30"
        assert env["CBRSIM_MODE"] == mode
        assert env["CBRSIM_W"] == width
        assert env["CBRSIM_PAL_ALGO"] == "mosaic-gm"
    print("TOML mapping: OK (profile values replace polluted environment)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decision", type=Path,
                        help="an existing decision log, preferably a 30fps/full-height one")
    args = parser.parse_args()
    decision = args.decision.resolve()
    if not decision.exists():
        raise SystemExit(f"not found: {decision}")
    check_profiles()

    clean = {key: value for key, value in os.environ.items() if key not in PER_SOURCE_ENV}
    polluted = dict(clean)
    polluted.update(POLLUTED)
    with tempfile.TemporaryDirectory(prefix="config-handoff-") as td:
        temp = Path(td)
        run_pack(decision, temp / "clean" / "MOVIE.DAT", clean)
        run_pack(decision, temp / "polluted" / "MOVIE.DAT", polluted)
        for name in ARTIFACTS:
            clean_hash = digest(temp / "clean" / name)
            polluted_hash = digest(temp / "polluted" / name)
            if clean_hash != polluted_hash:
                raise SystemExit(
                    f"FAIL: {name} differs: clean={clean_hash} polluted={polluted_hash}")
            print(f"{name}: IDENTICAL {clean_hash}")
    print("PASS: pack output is independent of inherited per-source CBRSIM values")


if __name__ == "__main__":
    main()
