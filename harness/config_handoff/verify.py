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
import av_config  # noqa: E402


PER_SOURCE_ENV = {
    "CBRSIM_SRC", "CBRSIM_FPS", "CBRSIM_DURATION", "CBRSIM_SOURCE_SAR",
    "CBRSIM_MODE", "CBRSIM_W", "CBRSIM_H", "CBRSIM_GEOMETRY_FIT",
    "CBRSIM_ACTIVE_TILES",
    "CBRSIM_MASTER_VF", "CBRSIM_RAW_VF", "CBRSIM_OUT",
    "CBRSIM_VRAM_TILES", "CBRSIM_QUALITY_BUDGET_KB", "CBRSIM_RING_CAP_KB",
    "CBRSIM_MAX_COLD", "CBRSIM_COLD_CAP", "CBRSIM_COLD_CAP_DIAG",
    "CBRSIM_PACK_FILL", "CBRSIM_STARTUP_AUDIO_FRAMES",
}
POLLUTED = {
    "CBRSIM_SRC": "wrong.mp4", "CBRSIM_FPS": "15", "CBRSIM_MODE": "H40",
    "CBRSIM_W": "320", "CBRSIM_H": "144",
    "CBRSIM_VRAM_TILES": "7", "CBRSIM_QUALITY_BUDGET_KB": "1",
    "CBRSIM_RING_CAP_KB": "1", "CBRSIM_MAX_COLD": "1",
    "CBRSIM_COLD_CAP": "1",
    "CBRSIM_PACK_FILL": "0",
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
        "--dec-log", str(decision), "--output", str(output),
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
        expected_cap = av_config.baseline_cold_cap_for_fps(
            float(profile.data["source"]["fps"]), mode,
            int(profile.data["video"].get(
                "active_tiles",
                int(profile.data["video"]["width"])
                * int(profile.data["video"]["height"]) // 64)))
        assert env["CBRSIM_COLD_CAP"] == str(expected_cap)
    sonic = load_profile(ROOT / "configs" / "sonic-jam-op-h40.toml")
    sonic_env = {"CBRSIM_COLD_CAP": "1"}
    apply_profile_env(sonic, sonic_env)
    assert sonic_env["CBRSIM_COLD_CAP"] == "190"

    source = (ROOT / "configs" / "sonic-jam-op-h40.toml").read_text(
        encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="cold-cap-profile-") as td:
        temp = Path(td)
        omitted_path = temp / "sonic-h40-omitted.toml"
        omitted_path.write_text(
            source.replace("cold_cap = 190\n", ""), encoding="utf-8")
        omitted = load_profile(omitted_path)
        omitted_env = {"CBRSIM_COLD_CAP": "999"}
        apply_profile_env(omitted, omitted_env)
        assert omitted_env["CBRSIM_COLD_CAP"] == "180"

        lower_path = temp / "sonic-h40-lower.toml"
        lower_path.write_text(
            source.replace("cold_cap = 190", "cold_cap = 179"),
            encoding="utf-8")
        try:
            load_profile(lower_path)
        except ValueError as exc:
            assert "below baseline 180" in str(exc)
        else:
            raise AssertionError("below-baseline TOML cold cap was accepted")
    print("TOML mapping: OK (profile values replace polluted environment)")


def check_cold_caps() -> None:
    expected = (
        ("H32", 24, 896, 219),
        ("H32", 30, 896, 175),
        ("H40", 15, 720, 500),
        ("H40", 15, 1040, 400),
        ("H40", 24, 1120, 200),
        ("H40", 30, 1120, 180),
    )
    for mode, fps, active_tiles, cap in expected:
        assert av_config.cold_cap_for_fps(fps, mode, active_tiles) == cap
        assert av_config.cold_realized_ceiling_for_fps(
            fps, mode, active_tiles) == cap
        raised = av_config.cold_cap_qualification(
            fps, mode, active_tiles, requested_cap=cap + 10)
        assert raised.cap == cap + 10
        assert raised.baseline_cap == cap
        assert raised.source == "profile"
        try:
            av_config.cold_cap_qualification(
                fps, mode, active_tiles, requested_cap=cap - 1)
        except ValueError as exc:
            assert "below baseline" in str(exc)
        else:
            raise AssertionError(
                f"below-baseline profile cap accepted: {mode}/{fps}/{active_tiles}")
    print("Cold caps: OK (mode/fps/active-tile sim and pack limits agree)")
    for fps, mode, active_tiles in (
            (24, "H32", 500),
            (15, "H40", 900),
            (15, "H40", 1120),
            (15, "H32", 896),
            (15, "MODE4", 896)):
        try:
            av_config.cold_cap_for_fps(fps, mode, active_tiles)
        except av_config.ColdCapMeasurementRequired:
            pass
        else:
            raise AssertionError(
                f"unmeasured tuple unexpectedly accepted: {mode}/{fps}/{active_tiles}")
    print("Cold caps: OK (unmeasured tuples require qualification)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decision", type=Path,
                        help="an existing decision log, preferably a 30fps/full-height one")
    args = parser.parse_args()
    decision = args.decision.resolve()
    if not decision.exists():
        raise SystemExit(f"not found: {decision}")
    check_profiles()
    check_cold_caps()

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
