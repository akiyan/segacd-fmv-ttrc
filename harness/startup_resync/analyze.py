#!/usr/bin/env python3
"""Extract and aggregate the DEBUG HUD from a native playback recording.

The player renders values only in one fixed 30-cell order in both modes:

    H32/H40: xxxx xx xx xx xx xx xx xx xx xx xxxx xx xx

The corresponding keys are F/P/S/D/R/L/C/W/M/A/U/N/J in the TSV and report.

Frames are decoded sequentially through ffmpeg.  High-confidence OCR samples
with the same F value are combined before R transitions are reported.  This is
a diagnostic tool only; its HUD timing must not be used to trim a publication
recording or to place YouTube chapters.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, Iterable

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import read_frameno  # noqa: E402
import av_config  # noqa: E402
import encode_config  # noqa: E402


@dataclass(frozen=True)
class Probe:
    width: int
    height: int
    fps: Fraction
    start_time: float


@dataclass(frozen=True)
class Sample:
    capture: int
    time_s: float
    values: dict[str, int]
    confidence: dict[str, float]


@dataclass(frozen=True)
class FrameGroup:
    loop: int
    capture_first: int
    capture_last: int
    time_first: float
    time_last: float
    sample_count: int
    confidence: float
    values: dict[str, int]


def _fraction(value: str) -> Fraction:
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise SystemExit(f"invalid frame rate from ffprobe: {value!r}") from exc
    if result <= 0:
        raise SystemExit(f"non-positive frame rate from ffprobe: {value!r}")
    return result


def probe_video(path: Path) -> Probe:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,start_time",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise SystemExit("ffprobe was not found") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr.strip() or "ffprobe failed") from exc
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise SystemExit(f"no video stream: {path}")
    stream = streams[0]
    rate = stream.get("avg_frame_rate")
    if not rate or rate == "0/0":
        rate = stream.get("r_frame_rate")
    return Probe(
        width=int(stream["width"]),
        height=int(stream["height"]),
        fps=_fraction(rate),
        start_time=float(stream.get("start_time", 0.0) or 0.0),
    )


def _read_exact(pipe: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = pipe.read(size - len(chunks))
        if not chunk:
            break
        chunks += chunk
    return bytes(chunks)


def iter_samples(
    path: Path,
    probe: Probe,
    confidence: float,
    crop_x: int,
    flip_fields: bool = False,
) -> Iterable[Sample]:
    # Only the top-left HUD area is sent through the pipe.  Decoding still sees
    # every source frame, while pipe traffic stays small even for an upscaled MP4.
    available_width = probe.width - crop_x
    layout = read_frameno.hud_layout_for_width(available_width)
    if flip_fields:
        if layout is not read_frameno.HUD_H40_LAYOUT:
            raise SystemExit("--flip-fields requires a native H40 recording")
        layout = read_frameno.HUD_H40_FLIP_LAYOUT
    fields = tuple(name for name, _col, _digits in layout)
    hud_cells = (
        read_frameno.HUD_H40_FLIP_CELLS
        if layout is read_frameno.HUD_H40_FLIP_LAYOUT
        else read_frameno.HUD_H40_CELLS
        if layout is read_frameno.HUD_H40_LAYOUT
        else read_frameno.HUD_CELLS
    )
    crop_w = min(hud_cells * read_frameno.CELL, available_width)
    crop_h = min(32, probe.height)
    if crop_x < 0 or crop_x >= probe.width:
        raise SystemExit(f"--crop-x must be within 0..{probe.width - 1}")
    if crop_w < hud_cells * read_frameno.CELL or crop_h < 8:
        raise SystemExit(
            f"HUD crop is too small ({crop_w}x{crop_h}); "
            f"need at least {hud_cells * read_frameno.CELL}x8"
        )

    vf = f"crop={crop_w}:{crop_h}:{crop_x}:0,format=gray"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-map", "0:v:0", "-vf", vf, "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise SystemExit("ffmpeg was not found") from exc
    assert process.stdout is not None
    frame_size = crop_w * crop_h
    capture = 0
    while True:
        raw = _read_exact(process.stdout, frame_size)
        if not raw:
            break
        if len(raw) != frame_size:
            process.kill()
            raise SystemExit(
                f"ffmpeg returned a partial raw frame: {len(raw)} / {frame_size} bytes"
            )
        image = np.frombuffer(raw, np.uint8).reshape(crop_h, crop_w)
        hud = read_frameno.read_hud(image, layout=layout)
        field_conf = {name: float(hud[name][1]) for name in fields}
        if min(field_conf.values()) >= confidence:
            yield Sample(
                capture=capture,
                time_s=probe.start_time + capture / float(probe.fps),
                values={name: int(hud[name][0]) for name in fields},
                confidence=field_conf,
            )
        capture += 1

    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    return_code = process.wait()
    if return_code:
        raise SystemExit(stderr.strip() or f"ffmpeg failed with exit code {return_code}")


def aggregate(samples: list[Sample], loop: int = -1) -> FrameGroup:
    if not samples:
        raise ValueError("cannot aggregate an empty sample group")
    values: dict[str, int] = {}
    for field in samples[0].values:
        counts = Counter(sample.values[field] for sample in samples)
        # Prefer the common value, then the value with the highest summed OCR
        # confidence.  The final tie-break is deterministic.
        values[field] = max(
            counts,
            key=lambda value: (
                counts[value],
                sum(
                    sample.confidence[field]
                    for sample in samples
                    if sample.values[field] == value
                ),
                -value,
            ),
        )
    return FrameGroup(
        loop=loop,
        capture_first=samples[0].capture,
        capture_last=samples[-1].capture,
        time_first=samples[0].time_s,
        time_last=samples[-1].time_s,
        sample_count=len(samples),
        confidence=float(statistics.median(
            min(sample.confidence.values()) for sample in samples
        )),
        values=values,
    )


def group_samples(samples: Iterable[Sample], max_gap: int) -> list[FrameGroup]:
    groups: list[FrameGroup] = []
    pending: list[Sample] = []
    for sample in samples:
        if pending and (
            sample.values["F"] != pending[-1].values["F"]
            or sample.capture - pending[-1].capture > max_gap
        ):
            groups.append(aggregate(pending))
            pending = []
        pending.append(sample)
    if pending:
        groups.append(aggregate(pending))
    return groups


def _has_anchor_run(groups: list[FrameGroup], start: int, length: int, max_step: int) -> bool:
    previous = 0
    accepted = 1
    for group in groups[start + 1:start + length * 3]:
        frame = group.values["F"]
        if frame == previous:
            continue
        if 1 <= frame - previous <= max_step:
            accepted += 1
            previous = frame
            if accepted >= length:
                return True
        else:
            return False
    return accepted >= length


def select_movie_groups(
    groups: list[FrameGroup], anchor_run: int, max_step: int
) -> list[FrameGroup]:
    anchor = None
    for index, group in enumerate(groups):
        if group.values["F"] == 0 and _has_anchor_run(groups, index, anchor_run, max_step):
            anchor = index
            break
    if anchor is None:
        raise SystemExit(
            f"could not find F0000 followed by {anchor_run - 1} plausible HUD frames; "
            "check --confidence and --crop-x"
        )

    selected: list[FrameGroup] = []
    loop = 0
    previous = -1
    for group in groups[anchor:]:
        frame = group.values["F"]
        if not selected:
            pass
        elif frame == previous:
            # A low-confidence gap can split one displayed movie frame into two
            # raw groups.  Keep the stronger aggregate rather than emitting a
            # duplicate F row.
            if group.sample_count > selected[-1].sample_count or (
                group.sample_count == selected[-1].sample_count
                and group.confidence > selected[-1].confidence
            ):
                selected[-1] = replace(group, loop=loop)
            continue
        elif 1 <= frame - previous <= max_step:
            pass
        elif frame <= max_step and previous >= max_step * 4:
            loop += 1
        else:
            # Isolated high-confidence OCR mistakes are still possible on old
            # transparent HUD captures.  Ignore a non-contiguous outlier and
            # let the next plausible group reconnect to `previous`.
            continue
        selected.append(replace(group, loop=loop))
        previous = frame
    return selected


def transition_indices(groups: list[FrameGroup]) -> list[int]:
    return [
        index for index in range(1, len(groups))
        if groups[index].values["R"] != groups[index - 1].values["R"]
    ]


def _fmt(group: FrameGroup) -> str:
    v = group.values
    transfer = f" U{v['U']:04X} N{v['N']:02X}" if "U" in v else ""
    jitter = f" J{v['J']:02X}" if "J" in v else ""
    return (
        f"loop={group.loop} t={group.time_first:8.3f}s "
        f"cap={group.capture_first:5d}-{group.capture_last:<5d} "
        f"F{v['F']:04X} P{v['P']:02X} S{v['S']:02X} D{v['D']:02X} "
        f"R{v['R']:02X} L{v['L']:02X} C{v['C']:02X} W{v['W']:02X} "
        f"M{v['M']:02X} A{v['A']:02X}{transfer}{jitter} n={group.sample_count} "
        f"conf={group.confidence:.3f}"
    )


def print_report(groups: list[FrameGroup], context: int) -> list[int]:
    transitions = transition_indices(groups)
    print(f"movie HUD groups: {len(groups)}")
    print(f"first: {_fmt(groups[0])}")
    print(f"last:  {_fmt(groups[-1])}")
    print(f"R transitions: {len(transitions)}")
    if "J" in groups[0].values:
        peak = max(group.values["J"] for group in groups)
        peak_group = next(group for group in groups if group.values["J"] == peak)
        updates = sum(
            groups[index].values["J"] > groups[index - 1].values["J"]
            for index in range(1, len(groups))
        )
        print(
            f"J high-water: {peak:02X} ({peak} KiB ceil) first at "
            f"F{peak_group.values['F']:04X} ({peak_group.values['F']}), "
            f"updates={updates}"
        )
    for number, index in enumerate(transitions, 1):
        previous = groups[index - 1]
        current = groups[index]
        following = groups[index + 1] if index + 1 < len(groups) else None
        after_lead = f"{following.values['L']:02X}" if following else "--"
        print(
            f"\n[{number}] R{previous.values['R']:02X}->R{current.values['R']:02X} "
            f"at F{current.values['F']:04X} ({current.values['F']}) "
            f"t={current.time_first:.3f}s; "
            f"L256 before/current/after={previous.values['L']:02X}/"
            f"{current.values['L']:02X}/{after_lead}"
        )
        for row in groups[max(0, index - context):min(len(groups), index + context + 1)]:
            marker = ">" if row is current else " "
            print(f" {marker} {_fmt(row)}")
    return transitions


def write_tsv(path: Path, groups: list[FrameGroup], transitions: list[int]) -> None:
    transition_set = set(transitions)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "loop", "capture_first", "capture_last", "time_first_s", "time_last_s",
        "sample_count", "confidence", "frame", "frame_hex", "palette", "slip",
        "desync", "resync", "lead_256b", "lead_hex", "cd_wait", "sub_wait_lines",
        "main_vblank_wait", "sub_adpcm_decode_units", "main_pattern_ticks",
        "main_pattern_ms", "cold_runs_low8", "prgbuf_jitter_peak_kib",
        "flip_vcounter", "flip_interval_excess_ticks", "pass2_entry_q4",
        "r_transition", "prev_frame",
        "prev_lead_256b", "next_frame", "next_lead_256b",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for index, group in enumerate(groups):
            values = group.values
            changed = index in transition_set
            previous = groups[index - 1] if changed else None
            following = groups[index + 1] if changed and index + 1 < len(groups) else None
            writer.writerow({
                "loop": group.loop,
                "capture_first": group.capture_first,
                "capture_last": group.capture_last,
                "time_first_s": f"{group.time_first:.6f}",
                "time_last_s": f"{group.time_last:.6f}",
                "sample_count": group.sample_count,
                "confidence": f"{group.confidence:.3f}",
                "frame": values["F"],
                "frame_hex": f"{values['F']:04X}",
                "palette": values["P"],
                "slip": values["S"],
                "desync": values["D"],
                "resync": values["R"],
                "lead_256b": values["L"],
                "lead_hex": f"{values['L']:02X}",
                "cd_wait": values["C"],
                "sub_wait_lines": values["W"],
                "main_vblank_wait": values["M"],
                "sub_adpcm_decode_units": values["A"],
                "main_pattern_ticks": values.get("U", ""),
                "main_pattern_ms": (
                    f"{values['U'] * 0.03072:.5f}" if "U" in values else ""
                ),
                "cold_runs_low8": values.get("N", ""),
                "prgbuf_jitter_peak_kib": values.get("J", ""),
                "flip_vcounter": (
                    f"{values['V']:02X}" if "V" in values else ""
                ),
                "flip_interval_excess_ticks": values.get("O", ""),
                "pass2_entry_q4": values.get("E", ""),
                "r_transition": (
                    f"{previous.values['R']:02X}->{values['R']:02X}" if previous else ""
                ),
                "prev_frame": previous.values["F"] if previous else "",
                "prev_lead_256b": previous.values["L"] if previous else "",
                "next_frame": following.values["F"] if following else "",
                "next_lead_256b": following.values["L"] if following else "",
            })
    print(f"TSV: {path}")


def upload_gate_limits(content_fps: float) -> tuple[dict[str, int], str]:
    """Return cadence-aware HUD limits for the exact encoded content rate."""
    fps = float(content_fps)
    if fps <= 0:
        raise ValueError(f"content fps must be positive, got {content_fps!r}")
    if av_config.uses_fixed_n2_cadence(fps):
        cadence = "fixed_n2"
        c_limit = 0
        m_limit = 1
    else:
        cadence = "delivery_paced"
        sector_num, sector_mod = av_config.cd_sector_rate(fps)
        # A delivery-paced slot may finish all but its already-armed control
        # sector on the current Sub path without slipping. The Main path may
        # use the complete number of display fields available to one content
        # frame; exceeding it proves an additional spill.
        c_limit = max(0, math.ceil(sector_num / sector_mod) - 1)
        m_limit = math.ceil(av_config.NTSC_VSYNC / fps)
    return {
        "S": 0,
        "D": 0,
        "R": 0,
        "C": c_limit,
        "M": m_limit,
        # J is ceil-KiB. Leave one complete KiB below the physical ring end so
        # an accepted value proves head and tail never became equal at full.
        # Values beyond jitter headroom remain visible for mandatory review.
        "J": (
            av_config.RING_SIZE_KB
            - av_config.prg_buf_cap_kb(fps)
            - 1
        ),
    }, cadence


def evaluate_upload_gate(
    groups: list[FrameGroup],
    expected_frames: int,
    recording: Path,
    content_fps: float = 30.0,
    profile: encode_config.EncodeProfile | None = None,
) -> dict:
    """Classify a complete first loop as PASS, WARNING, or FAIL."""
    first_loop = [group for group in groups if group.loop == 0]
    fields = ("S", "D", "R", "C", "M", "J")
    failures: list[str] = []
    warnings: list[str] = []
    missing = [field for field in fields if field not in first_loop[0].values]
    maxima = {
        field: max(group.values.get(field, 0) for group in first_loop)
        for field in fields
    }
    if missing:
        failures.append(f"HUD fields missing: {','.join(missing)}")

    frames = [group.values["F"] for group in first_loop]
    wanted = list(range(expected_frames))
    if frames != wanted:
        first_bad = next(
            (index for index, (actual, expected) in enumerate(zip(frames, wanted))
             if actual != expected),
            min(len(frames), len(wanted)),
        )
        actual = frames[first_bad] if first_bad < len(frames) else None
        expected = wanted[first_bad] if first_bad < len(wanted) else None
        failures.append(
            f"first loop is incomplete: got {len(frames)} frames, expected "
            f"{expected_frames}; first mismatch index={first_bad} "
            f"actual={actual} expected={expected}"
        )

    limits, cadence = upload_gate_limits(content_fps)
    for field, limit in limits.items():
        if maxima[field] > limit:
            target = warnings if field == "C" else failures
            target.append(
                f"{field} peak {maxima[field]:02X} exceeds upload limit {limit:02X}"
            )

    stat = recording.stat()
    status = "FAIL" if failures else "WARNING" if warnings else "PASS"
    result = {
        "schema_version": 2,
        # WARNING remains upload-capable. Keep the compatibility boolean so
        # older consumers only stop for a real FAIL.
        "pass": status != "FAIL",
        "status": status,
        "recording": str(recording.resolve()),
        "recording_size": stat.st_size,
        "recording_mtime_ns": stat.st_mtime_ns,
        "expected_frames": expected_frames,
        "observed_first_loop_frames": len(first_loop),
        "content_fps": float(content_fps),
        "cadence": cadence,
        "maxima": maxima,
        "limits": limits,
        "prg_buf_cap_kib": av_config.prg_buf_cap_kb(content_fps),
        "jitter_headroom_kib": (
            av_config.ring_jitter_headroom_kb(content_fps)),
        "delivery_limit_kib": (
            av_config.physical_delivery_cap_kb(content_fps)),
        "backpressure_kib": av_config.BACKPRESSURE_KB,
        "physical_ring_kib": av_config.RING_SIZE_KB,
        "requires_explicit_upload_approval": False,
        "warnings": warnings,
        "failures": failures,
    }
    if profile is not None:
        result["profile"] = str(profile.path.resolve())
        result["profile_sha256"] = profile.sha256
    return result


def write_gate_json(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    state = result["status"]
    maxima = result["maxima"]
    print(
        f"HUD record gate: {state}  "
        + " ".join(f"{field}{maxima[field]:02X}" for field in "SDRCMJ")
        + f"  frames={result['observed_first_loop_frames']}/"
        f"{result['expected_frames']}  cadence={result['cadence']} "
        f"fps={result['content_fps']:g}"
    )
    for failure in result["failures"]:
        print(f"  gate failure: {failure}")
    for warning in result["warnings"]:
        print(f"  gate warning: {warning}")
    print(f"HUD gate JSON: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate the DEBUG HUD and report audio R transitions."
    )
    parser.add_argument("recording", type=Path, help="native FFV1 MKV or MP4 recording")
    parser.add_argument(
        "profile", nargs="?", type=Path,
        help="encode profile; required positionally with --gate-json",
    )
    parser.add_argument(
        "--tsv",
        type=Path,
        help="write all aggregated movie frames as tab-separated values",
    )
    parser.add_argument(
        "--gate-json", type=Path,
        help="write the mandatory PASS/WARNING/FAIL pre-upload review",
    )
    parser.add_argument(
        "--expected-frames", type=int,
        help="complete first-loop frame count required by --gate-json",
    )
    parser.add_argument(
        "--confidence", type=float, default=0.90,
        help="minimum confidence for every HUD field (default: 0.90)",
    )
    parser.add_argument(
        "--crop-x", type=int, default=0,
        help="left edge of the native HUD crop (default: 0; legacy centered H32 may use 32)",
    )
    parser.add_argument(
        "--flip-fields", action="store_true",
        help="parse the 34-cell H40 layout with the V/O flip-phase fields "
             "(HUD_FLIP_FIELDS DEBUG builds only)",
    )
    parser.add_argument(
        "--max-gap", type=int, default=3,
        help="maximum capture-frame gap inside one F group (default: 3)",
    )
    parser.add_argument(
        "--max-frame-step", type=int, default=4,
        help="largest accepted F increment after a missed OCR group (default: 4)",
    )
    parser.add_argument(
        "--anchor-run", type=int, default=4,
        help="plausible groups required to accept an F0000 anchor (default: 4)",
    )
    parser.add_argument(
        "--context", type=int, default=2,
        help="aggregated frames printed on each side of an R transition (default: 2)",
    )
    args = parser.parse_args()
    if not args.recording.is_file():
        parser.error(f"recording not found: {args.recording}")
    if not 0.0 <= args.confidence <= 1.0:
        parser.error("--confidence must be within 0..1")
    for name in ("max_gap", "max_frame_step", "anchor_run"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be at least 1")
    if args.context < 0:
        parser.error("--context must not be negative")
    if args.gate_json and not args.expected_frames:
        parser.error("--gate-json requires --expected-frames")
    if args.gate_json and args.profile is None:
        parser.error("--gate-json requires the encode profile as the second positional argument")
    if args.profile is not None and not args.profile.is_file():
        parser.error(f"profile not found: {args.profile}")
    if args.expected_frames is not None and args.expected_frames < 1:
        parser.error("--expected-frames must be at least 1")
    if args.tsv is not None and args.tsv.suffix.lower() != ".tsv":
        parser.error("--tsv output must use the .tsv extension")
    return args


def main() -> int:
    args = parse_args()
    probe = probe_video(args.recording)
    print(
        f"input: {args.recording} ({probe.width}x{probe.height}, "
        f"{float(probe.fps):.6f} capture fps)"
    )
    raw_groups = group_samples(
        iter_samples(args.recording, probe, args.confidence, args.crop_x,
                     args.flip_fields),
        args.max_gap,
    )
    groups = select_movie_groups(raw_groups, args.anchor_run, args.max_frame_step)
    transitions = print_report(groups, args.context)
    if args.tsv:
        write_tsv(args.tsv, groups, transitions)
    if args.gate_json:
        profile = encode_config.load_profile(args.profile)
        content_fps = float(Fraction(str(profile.data["source"]["fps"])))
        result = evaluate_upload_gate(
            groups, args.expected_frames, args.recording, content_fps, profile)
        write_gate_json(args.gate_json, result)
        if not result["pass"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
