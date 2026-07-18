#!/usr/bin/env python3
"""Verify strict DEBUG-frame cadence in a native playback recording.

The verifier decodes every recorded video frame in order, OCRs only the
player's ``Fxxxx`` field, finds a plausible F0000 cadence anchor, then proves
that every movie frame first appears exactly ``N`` capture frames after the
previous one.  It intentionally ignores the recording tail after the requested
final movie frame first appears.
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import BinaryIO, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import read_frameno  # noqa: E402


NATIVE_GEOMETRIES = {(256, 224), (320, 224)}


class CadenceError(RuntimeError):
    """The recording does not prove the requested strict cadence."""


@dataclass(frozen=True)
class HeaderInfo:
    version: int
    frame_count: int
    vsync_n: int


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: Fraction


@dataclass(frozen=True)
class Observation:
    capture: int
    frame: int
    confidence: float = 1.0


@dataclass(frozen=True)
class CadenceReport:
    anchor_capture: int
    final_frame: int
    first_captures: tuple[int, ...]
    accepted_observations: int

    @property
    def deltas(self) -> tuple[int, ...]:
        return tuple(
            current - previous
            for previous, current in zip(self.first_captures, self.first_captures[1:])
        )


def integer(value: str) -> int:
    """Parse either an ordinary decimal integer or a 0x-prefixed integer."""
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc


def read_header(path: Path) -> HeaderInfo:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CadenceError(f"cannot read HEADER.DAT: {path}: {exc}") from exc
    if len(data) < 58:
        raise CadenceError(f"HEADER.DAT is too short: {len(data)} bytes")
    if data[:4] != b"TTRC":
        raise CadenceError(f"not a TTRC HEADER.DAT: {path}")
    version, frame_count = struct.unpack_from(">HH", data, 4)
    vsync_n = struct.unpack_from(">H", data, 52)[0]
    if frame_count < 1:
        raise CadenceError("HEADER.DAT has no movie frames")
    if vsync_n < 1:
        raise CadenceError("HEADER.DAT has no valid VBlank cadence hint")
    return HeaderInfo(version=version, frame_count=frame_count, vsync_n=vsync_n)


def _fraction(value: str) -> Fraction:
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise CadenceError(f"invalid capture frame rate: {value!r}") from exc
    if result <= 0:
        raise CadenceError(f"non-positive capture frame rate: {value!r}")
    return result


def probe_video(path: Path) -> VideoInfo:
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise CadenceError("ffprobe was not found") from exc
    if result.returncode:
        detail = result.stderr.strip() or "ffprobe failed"
        raise CadenceError(detail)
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError as exc:
        raise CadenceError(f"ffprobe returned invalid JSON: {exc}") from exc
    if len(streams) != 1:
        raise CadenceError(f"expected one video stream, found {len(streams)}")
    stream = streams[0]
    rate = stream.get("avg_frame_rate")
    if not rate or rate == "0/0":
        rate = stream.get("r_frame_rate")
    return VideoInfo(
        width=int(stream["width"]),
        height=int(stream["height"]),
        fps=_fraction(str(rate)),
    )


def _read_exact(pipe: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = pipe.read(size - len(chunks))
        if not chunk:
            break
        chunks += chunk
    return bytes(chunks)


def decode_observations(
    path: Path,
    video: VideoInfo,
    confidence: float,
    crop_x: int,
) -> tuple[list[Observation], int]:
    """Decode native capture frames sequentially and OCR only ``Fxxxx``."""
    if (video.width, video.height) not in NATIVE_GEOMETRIES:
        allowed = ", ".join(f"{w}x{h}" for w, h in sorted(NATIVE_GEOMETRIES))
        raise CadenceError(
            f"recording is {video.width}x{video.height}; expected a native capture "
            f"({allowed}), not an upload compilation"
        )
    if not 50.0 <= float(video.fps) <= 65.0:
        raise CadenceError(
            f"capture rate is {float(video.fps):.6f} fps; VBlank counting requires "
            "the native approximately 60 fps recording"
        )
    crop_width = 5 * read_frameno.CELL
    crop_height = 24
    if crop_x < 0 or crop_x + crop_width > video.width:
        raise CadenceError(
            f"--crop-x must leave {crop_width} pixels within width {video.width}"
        )

    vf = f"crop={crop_width}:{crop_height}:{crop_x}:0,format=gray"
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-xerror", "-err_detect", "explode", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf,
        "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise CadenceError("ffmpeg was not found") from exc

    assert process.stdout is not None
    frame_size = crop_width * crop_height
    observations: list[Observation] = []
    capture = 0
    try:
        while True:
            raw = _read_exact(process.stdout, frame_size)
            if not raw:
                break
            if len(raw) != frame_size:
                raise CadenceError(
                    f"ffmpeg returned a partial raw frame: {len(raw)} / {frame_size} bytes"
                )
            image = np.frombuffer(raw, np.uint8).reshape(crop_height, crop_width)
            frame, score = read_frameno.read_frameno(image)
            if score >= confidence:
                observations.append(Observation(capture, int(frame), float(score)))
            capture += 1
    except BaseException:
        process.kill()
        process.wait()
        raise

    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    return_code = process.wait()
    if return_code:
        raise CadenceError(stderr.strip() or f"ffmpeg failed with exit code {return_code}")
    if capture == 0:
        raise CadenceError("ffmpeg decoded no video frames")
    return observations, capture


def _check_observation_order(observations: Sequence[Observation]) -> None:
    previous = -1
    for observation in observations:
        if observation.capture <= previous:
            raise ValueError("observations must have unique, increasing capture indices")
        previous = observation.capture


def _anchor_candidate(
    observations: Sequence[Observation],
    start: int,
    vblanks: int,
    anchor_frames: int,
) -> bool:
    first_capture = observations[start].capture
    current = 0
    for observation in observations[start + 1:]:
        if observation.frame == current:
            continue
        if observation.frame != current + 1:
            return False
        current += 1
        if observation.capture != first_capture + current * vblanks:
            return False
        if current + 1 >= anchor_frames:
            return True
    return anchor_frames == 1


def find_anchor(
    observations: Sequence[Observation],
    vblanks: int,
    anchor_frames: int = 4,
) -> int:
    """Return the observation index of a plausible exact F0000 cadence run."""
    if vblanks < 1:
        raise ValueError("vblanks must be positive")
    if anchor_frames < 1:
        raise ValueError("anchor_frames must be positive")
    _check_observation_order(observations)
    for index, observation in enumerate(observations):
        if observation.frame == 0 and _anchor_candidate(
            observations, index, vblanks, anchor_frames
        ):
            return index
    raise CadenceError(
        f"could not find F0000 followed by {anchor_frames - 1} frames at "
        f"exactly {vblanks} VBlanks; check the DEBUG build, crop and confidence"
    )


def validate_observations(
    observations: Sequence[Observation],
    final_frame: int,
    vblanks: int,
    anchor_frames: int = 4,
) -> CadenceReport:
    """Validate first appearances through ``final_frame`` and ignore the tail."""
    if final_frame < 0:
        raise ValueError("final_frame must not be negative")
    anchor = find_anchor(observations, vblanks, anchor_frames)
    anchor_capture = observations[anchor].capture
    first_captures = [anchor_capture]
    accepted = 1
    current = 0
    if final_frame == 0:
        return CadenceReport(anchor_capture, final_frame, tuple(first_captures), accepted)

    for observation in observations[anchor + 1:]:
        accepted += 1
        if observation.frame == current:
            continue
        if observation.frame < current:
            raise CadenceError(
                f"out-of-order F at capture {observation.capture}: "
                f"F{observation.frame:04X} after F{current:04X}"
            )
        if observation.frame > current + 1:
            raise CadenceError(
                f"missing F{current + 1:04X}: capture {observation.capture} "
                f"jumped from F{current:04X} to F{observation.frame:04X}"
            )

        delta = observation.capture - first_captures[-1]
        if delta != vblanks:
            timing = "early" if delta < vblanks else "late"
            raise CadenceError(
                f"F{observation.frame:04X} first appeared {timing} at capture "
                f"{observation.capture}: delta={delta}, expected {vblanks}"
            )
        current += 1
        first_captures.append(observation.capture)
        if current == final_frame:
            # The final frame may be held until RetroArch exits.  Nothing after
            # its first appearance belongs to this cadence proof.
            return CadenceReport(
                anchor_capture, final_frame, tuple(first_captures), accepted
            )

    raise CadenceError(
        f"recording ended before F{final_frame:04X}; last accepted movie frame "
        f"was F{current:04X}"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path, help="native DEBUG lossless MKV")
    parser.add_argument(
        "--header", type=Path, required=True,
        help="matching packed HEADER.DAT (supplies frame count and default N)",
    )
    parser.add_argument(
        "--vblanks", type=integer,
        help="required capture-frame delta (default: HEADER.DAT vsync_n)",
    )
    parser.add_argument(
        "--through-frame", type=integer,
        help="stop the proof at this movie frame (decimal or 0xHEX)",
    )
    parser.add_argument(
        "--confidence", type=float, default=0.90,
        help="minimum F-field OCR confidence (default: 0.90)",
    )
    parser.add_argument(
        "--crop-x", type=int, default=0,
        help="native x coordinate of the F glyph (default: 0)",
    )
    parser.add_argument(
        "--anchor-frames", type=int, default=4,
        help="exact initial frames required to accept F0000 (default: 4)",
    )
    args = parser.parse_args(argv)
    if not args.recording.is_file():
        parser.error(f"recording not found: {args.recording}")
    if not args.header.is_file():
        parser.error(f"HEADER.DAT not found: {args.header}")
    if args.vblanks is not None and args.vblanks < 1:
        parser.error("--vblanks must be positive")
    if args.through_frame is not None and args.through_frame < 0:
        parser.error("--through-frame must not be negative")
    if not 0.0 <= args.confidence <= 1.0:
        parser.error("--confidence must be within 0..1")
    if args.anchor_frames < 1:
        parser.error("--anchor-frames must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        header = read_header(args.header)
        vblanks = args.vblanks if args.vblanks is not None else header.vsync_n
        final_frame = (
            header.frame_count - 1
            if args.through_frame is None
            else args.through_frame
        )
        if final_frame >= header.frame_count:
            raise CadenceError(
                f"--through-frame F{final_frame:04X} is outside HEADER.DAT's "
                f"F0000..F{header.frame_count - 1:04X}"
            )
        video = probe_video(args.recording)
        observations, decoded = decode_observations(
            args.recording, video, args.confidence, args.crop_x
        )
        anchor_frames = min(args.anchor_frames, header.frame_count)
        report = validate_observations(
            observations, final_frame, vblanks, anchor_frames
        )
    except CadenceError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    delta_counts = Counter(report.deltas)
    delta_text = ", ".join(
        f"{delta}x{count}" for delta, count in sorted(delta_counts.items())
    ) or "none (F0000 only)"
    scope = "full" if final_frame == header.frame_count - 1 else "partial"
    print(
        f"input: {args.recording} ({video.width}x{video.height}, "
        f"{float(video.fps):.6f} fps, {decoded} capture frames)"
    )
    print(
        f"header: TTRC v{header.version}, {header.frame_count} movie frames, "
        f"N={header.vsync_n}; required={vblanks}"
    )
    print(
        f"PASS ({scope}): F0000..F{final_frame:04X}; "
        f"first capture={report.anchor_capture}, "
        f"last capture={report.first_captures[-1]}"
    )
    print(f"first-appearance deltas: {delta_text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
