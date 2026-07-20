#!/usr/bin/env python3
"""Extract and aggregate the DEBUG HUD from a native playback recording.

The player renders values only in this fixed internal order, with an H40-only
diagnostic suffix:

    H32: xxxx xx xx xx xx xx xx xx xx xx
    H40: xxxx xx xx xx xx xx xx xx xx xx xxxx xx

The corresponding keys remain F/P/S/D/R/L/C/W/M/A/U/N in the CSV and report.

Frames are decoded sequentially through ffmpeg.  High-confidence OCR samples
with the same F value are combined before R transitions are reported.  This is
a diagnostic tool only; its HUD timing must not be used to trim a publication
recording or to place YouTube chapters.
"""

from __future__ import annotations

import argparse
import csv
import json
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
) -> Iterable[Sample]:
    # Only the top-left HUD area is sent through the pipe.  Decoding still sees
    # every source frame, while pipe traffic stays small even for an upscaled MP4.
    available_width = probe.width - crop_x
    layout = read_frameno.hud_layout_for_width(available_width)
    fields = tuple(name for name, _col, _digits in layout)
    hud_cells = (
        read_frameno.HUD_H40_CELLS
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
    h40 = f" U{v['U']:04X} N{v['N']:02X}" if "U" in v else ""
    return (
        f"loop={group.loop} t={group.time_first:8.3f}s "
        f"cap={group.capture_first:5d}-{group.capture_last:<5d} "
        f"F{v['F']:04X} P{v['P']:02X} S{v['S']:02X} D{v['D']:02X} "
        f"R{v['R']:02X} L{v['L']:02X} C{v['C']:02X} W{v['W']:02X} "
        f"M{v['M']:02X} A{v['A']:02X}{h40} n={group.sample_count} "
        f"conf={group.confidence:.3f}"
    )


def print_report(groups: list[FrameGroup], context: int) -> list[int]:
    transitions = transition_indices(groups)
    print(f"movie HUD groups: {len(groups)}")
    print(f"first: {_fmt(groups[0])}")
    print(f"last:  {_fmt(groups[-1])}")
    print(f"R transitions: {len(transitions)}")
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


def write_csv(path: Path, groups: list[FrameGroup], transitions: list[int]) -> None:
    transition_set = set(transitions)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "loop", "capture_first", "capture_last", "time_first_s", "time_last_s",
        "sample_count", "confidence", "frame", "frame_hex", "palette", "slip",
        "desync", "resync", "lead_256b", "lead_hex", "cd_wait", "sub_wait_lines",
        "main_vblank_wait", "sub_adpcm_decode_units", "main_pattern_ticks",
        "main_pattern_ms", "cold_runs_low8",
        "r_transition", "prev_frame",
        "prev_lead_256b", "next_frame", "next_lead_256b",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
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
                "r_transition": (
                    f"{previous.values['R']:02X}->{values['R']:02X}" if previous else ""
                ),
                "prev_frame": previous.values["F"] if previous else "",
                "prev_lead_256b": previous.values["L"] if previous else "",
                "next_frame": following.values["F"] if following else "",
                "next_lead_256b": following.values["L"] if following else "",
            })
    print(f"CSV: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate the DEBUG HUD and report audio R transitions."
    )
    parser.add_argument("recording", type=Path, help="native FFV1 MKV or MP4 recording")
    parser.add_argument("--csv", type=Path, help="write all aggregated movie frames to CSV")
    parser.add_argument(
        "--confidence", type=float, default=0.90,
        help="minimum confidence for every HUD field (default: 0.90)",
    )
    parser.add_argument(
        "--crop-x", type=int, default=0,
        help="left edge of the native HUD crop (default: 0; legacy centered H32 may use 32)",
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
    return args


def main() -> int:
    args = parse_args()
    probe = probe_video(args.recording)
    print(
        f"input: {args.recording} ({probe.width}x{probe.height}, "
        f"{float(probe.fps):.6f} capture fps)"
    )
    raw_groups = group_samples(
        iter_samples(args.recording, probe, args.confidence, args.crop_x),
        args.max_gap,
    )
    groups = select_movie_groups(raw_groups, args.anchor_run, args.max_frame_step)
    transitions = print_report(groups, args.context)
    if args.csv:
        write_csv(args.csv, groups, transitions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
