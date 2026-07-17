#!/usr/bin/env python3
"""Compare two native RetroArch FFV1/FLAC recordings exactly.

The comparison is deliberately stricter than comparing the two container files:
Matroska metadata and FLAC/FFV1 packetisation may change without changing the
decoded recording.  This tool verifies the recording contract, then compares
every decoded video frame, every decoded PCM sample frame, and both packet PTS
timelines.

The default comparison covers the complete recordings, including the Mega-CD
startup.  It performs no automatic trimming or alignment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence, TypeVar


SCHEMA_VERSION = 1
_FRAME_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_FRAME_TB_RE = re.compile(r"^#tb\s+\d+:\s*(\S+)\s*$")
T = TypeVar("T")


class RecordingError(RuntimeError):
    """An input could not be probed or decoded completely."""


@dataclass(frozen=True)
class FrameHash:
    stream: int
    dts: int
    pts: int
    duration: int
    size: int
    digest: str


@dataclass(frozen=True)
class PcmDigest:
    sha256: str
    byte_count: int
    sample_frames: int


def _run_json(command: Sequence[str], label: str) -> dict[str, Any]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RecordingError(f"{command[0]} was not found") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RecordingError(
            f"{label} failed with exit code {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RecordingError(f"{label} returned invalid JSON: {exc}") from exc


def _integer(value: Any, field: str) -> int:
    if value is None or value == "N/A":
        raise RecordingError(f"missing {field}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RecordingError(f"invalid {field}: {value!r}") from exc


def _optional_integer(value: Any, field: str) -> int | None:
    """Parse an optional ffprobe integer such as Matroska packet duration."""
    if value is None or value == "N/A":
        return None
    return _integer(value, field)


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or value == "N/A":
        raise RecordingError(f"missing {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RecordingError(f"invalid {field}: {value!r}") from exc
    if not result.is_finite():
        raise RecordingError(f"non-finite {field}: {value!r}")
    return result


def probe_recording(path: Path) -> dict[str, Any]:
    """Read stream metadata and the original packet timestamp timelines."""
    data = _run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-show_packets",
            "-show_entries",
            (
                "format=format_name,start_time,duration:"
                "stream=index,codec_type,codec_name,width,height,pix_fmt,"
                "sample_aspect_ratio,sample_fmt,sample_rate,channels,"
                "channel_layout,time_base,start_time,r_frame_rate,avg_frame_rate:"
                "packet=stream_index,pts,dts,duration"
            ),
            "-of",
            "json",
            str(path),
        ],
        f"ffprobe {path}",
    )

    streams = data.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise RecordingError(
            f"{path}: expected exactly one video and one audio stream, "
            f"got video={len(videos)} audio={len(audios)}"
        )

    video = videos[0]
    audio = audios[0]
    video_index = _integer(video.get("index"), "video stream index")
    audio_index = _integer(audio.get("index"), "audio stream index")
    packets: dict[str, list[dict[str, int | None]]] = {"video": [], "audio": []}
    for number, packet in enumerate(data.get("packets", [])):
        stream_index = _integer(packet.get("stream_index"), f"packet {number} stream_index")
        if stream_index == video_index:
            target = packets["video"]
            name = "video"
        elif stream_index == audio_index:
            target = packets["audio"]
            name = "audio"
        else:
            continue
        target.append(
            {
                "pts": _integer(packet.get("pts"), f"{name} packet {len(target)} pts"),
                "dts": _integer(packet.get("dts"), f"{name} packet {len(target)} dts"),
                "duration": _optional_integer(
                    packet.get("duration"), f"{name} packet {len(target)} duration"
                ),
            }
        )
    if not packets["video"] or not packets["audio"]:
        raise RecordingError(f"{path}: an audio or video stream has no packets")

    fmt = data.get("format", {})
    duration = _decimal(fmt.get("duration"), "format duration")
    if duration <= 0:
        raise RecordingError(f"{path}: format duration is not positive")

    return {
        "format": {
            "format_name": str(fmt.get("format_name", "")),
            "start_time": str(fmt.get("start_time", "")),
            "duration": str(duration),
        },
        "video": video,
        "audio": audio,
        "packets": packets,
    }


def decode_framemd5(path: Path) -> tuple[str, list[FrameHash]]:
    """Decode all video frames into canonical bgr0 framemd5 rows."""
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-err_detect",
        "explode",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-pix_fmt",
        "bgr0",
        "-fps_mode",
        "passthrough",
        "-f",
        "framemd5",
        "pipe:1",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RecordingError("ffmpeg was not found") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RecordingError(
            f"video decode failed for {path} with exit code {result.returncode}"
            + (f": {detail}" if detail else "")
        )

    time_base = ""
    frames: list[FrameHash] = []
    for line_number, raw in enumerate(result.stdout.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = _FRAME_TB_RE.match(line)
        if match:
            time_base = match.group(1)
            continue
        if line.startswith("#"):
            continue
        columns = [column.strip() for column in line.split(",")]
        if len(columns) != 6:
            raise RecordingError(
                f"{path}: malformed framemd5 row {line_number}: {raw!r}"
            )
        try:
            stream, dts, pts, duration, size = map(int, columns[:5])
        except ValueError as exc:
            raise RecordingError(
                f"{path}: non-integer framemd5 row {line_number}: {raw!r}"
            ) from exc
        digest = columns[5].lower()
        if not _FRAME_HASH_RE.fullmatch(digest):
            raise RecordingError(
                f"{path}: invalid framemd5 digest on row {line_number}: {digest!r}"
            )
        frames.append(FrameHash(stream, dts, pts, duration, size, digest))
    if not time_base:
        raise RecordingError(f"{path}: framemd5 output did not declare a time base")
    if not frames:
        raise RecordingError(f"{path}: video decode produced no frames")
    return time_base, frames


def decode_pcm(path: Path, channels: int) -> PcmDigest:
    """Decode and hash canonical signed 16-bit PCM in one bounded-memory pass."""
    if channels <= 0:
        raise RecordingError(f"{path}: invalid audio channel count {channels}")
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-xerror",
        "-err_detect",
        "explode",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "pipe:1",
    ]
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_file)
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                byte_count += len(chunk)
            process.stdout.close()
            return_code = process.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read().decode("utf-8", "replace").strip()
    except FileNotFoundError as exc:
        raise RecordingError("ffmpeg was not found") from exc
    if return_code:
        raise RecordingError(
            f"audio decode failed for {path} with exit code {return_code}"
            + (f": {stderr}" if stderr else "")
        )
    block_align = channels * 2
    if byte_count == 0:
        raise RecordingError(f"{path}: audio decode produced no PCM")
    if byte_count % block_align:
        raise RecordingError(
            f"{path}: decoded PCM byte count {byte_count} is not aligned to "
            f"{channels} signed 16-bit channels"
        )
    return PcmDigest(digest.hexdigest(), byte_count, byte_count // block_align)


def first_non_monotonic(values: Sequence[int]) -> int | None:
    """Return the first index that is not strictly greater than its predecessor."""
    for index in range(1, len(values)):
        if values[index] <= values[index - 1]:
            return index
    return None


def first_decreasing(values: Sequence[int]) -> int | None:
    """Return the first decrease; equal adjacent derived timestamps are valid."""
    for index in range(1, len(values)):
        if values[index] < values[index - 1]:
            return index
    return None


def first_sequence_mismatch(a: Sequence[T], b: Sequence[T]) -> int | None:
    """Return the first differing index, including a missing tail element."""
    for index, (left, right) in enumerate(zip(a, b)):
        if left != right:
            return index
    if len(a) != len(b):
        return min(len(a), len(b))
    return None


def _sha256_sequence(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _stream_metadata(stream: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == "video":
        fields = (
            "codec_name",
            "width",
            "height",
            "pix_fmt",
            "sample_aspect_ratio",
            "time_base",
            "start_time",
            "r_frame_rate",
            "avg_frame_rate",
        )
    else:
        fields = (
            "codec_name",
            "sample_fmt",
            "sample_rate",
            "channels",
            "channel_layout",
            "time_base",
            "start_time",
        )
    return {field: stream.get(field) for field in fields}


def _packet_summary(packets: Sequence[dict[str, int | None]]) -> dict[str, Any]:
    pts = [packet["pts"] for packet in packets]
    dts = [packet["dts"] for packet in packets]
    assert all(value is not None for value in pts)
    assert all(value is not None for value in dts)
    typed_pts = [int(value) for value in pts]
    typed_dts = [int(value) for value in dts]
    last_duration = packets[-1]["duration"]
    if last_duration is not None:
        end_pts = typed_pts[-1] + last_duration
        end_pts_source = "packet_duration"
    elif len(typed_pts) >= 2 and typed_pts[-1] > typed_pts[-2]:
        end_pts = typed_pts[-1] + (typed_pts[-1] - typed_pts[-2])
        end_pts_source = "last_pts_delta"
    else:
        end_pts = None
        end_pts_source = None
    return {
        "packet_count": len(packets),
        "first_pts": typed_pts[0],
        "last_pts": typed_pts[-1],
        "end_pts": end_pts,
        "end_pts_source": end_pts_source,
        "pts_monotonic": first_non_monotonic(typed_pts) is None,
        "dts_monotonic": first_non_monotonic(typed_dts) is None,
    }


def _failure(
    report: dict[str, Any],
    code: str,
    message: str,
    *,
    baseline: Any = None,
    candidate: Any = None,
) -> None:
    item: dict[str, Any] = {"code": code, "message": message}
    if baseline is not None:
        item["baseline"] = baseline
    if candidate is not None:
        item["candidate"] = candidate
    report["failures"].append(item)


def _validate_contract(report: dict[str, Any], name: str, probe: dict[str, Any]) -> None:
    fmt = probe["format"]
    video = probe["video"]
    audio = probe["audio"]
    if "matroska" not in fmt["format_name"].split(","):
        _failure(
            report,
            f"{name}.format",
            f"{name} is not a Matroska recording",
            baseline=fmt["format_name"] if name == "baseline" else None,
            candidate=fmt["format_name"] if name == "candidate" else None,
        )
    expected = (
        ("video.codec", video.get("codec_name"), "ffv1"),
        ("video.pix_fmt", video.get("pix_fmt"), "bgr0"),
        ("audio.codec", audio.get("codec_name"), "flac"),
        ("audio.sample_fmt", audio.get("sample_fmt"), "s16"),
        ("audio.sample_rate", str(audio.get("sample_rate")), "44100"),
        ("audio.channels", audio.get("channels"), 2),
    )
    for field, actual, wanted in expected:
        if actual != wanted:
            _failure(
                report,
                f"{name}.{field}",
                f"{name} {field} is {actual!r}, expected {wanted!r}",
            )
    try:
        width = _integer(video.get("width"), "video width")
        height = _integer(video.get("height"), "video height")
    except RecordingError as exc:
        _failure(report, f"{name}.video.geometry", f"{name}: {exc}")
    else:
        if width <= 0 or height <= 0:
            _failure(
                report,
                f"{name}.video.geometry",
                f"{name} has invalid {width}x{height}",
            )


def _compare_metadata(
    report: dict[str, Any], baseline: dict[str, Any], candidate: dict[str, Any]
) -> None:
    base_meta = {
        "format_name": baseline["format"]["format_name"],
        "video": _stream_metadata(baseline["video"], "video"),
        "audio": _stream_metadata(baseline["audio"], "audio"),
    }
    candidate_meta = {
        "format_name": candidate["format"]["format_name"],
        "video": _stream_metadata(candidate["video"], "video"),
        "audio": _stream_metadata(candidate["audio"], "audio"),
    }
    report["checks"]["metadata"] = {
        "baseline": base_meta,
        "candidate": candidate_meta,
        "equal": base_meta == candidate_meta,
    }
    if base_meta != candidate_meta:
        for section in ("format_name", "video", "audio"):
            left = base_meta[section]
            right = candidate_meta[section]
            if left != right:
                _failure(
                    report,
                    f"metadata.{section}",
                    f"recording {section} metadata differs",
                    baseline=left,
                    candidate=right,
                )


def _compare_packet_timelines(
    report: dict[str, Any], baseline: dict[str, Any], candidate: dict[str, Any]
) -> None:
    output: dict[str, Any] = {
        "format_duration": {
            "baseline": baseline["format"]["duration"],
            "candidate": candidate["format"]["duration"],
            "equal": _decimal(baseline["format"]["duration"], "baseline duration")
            == _decimal(candidate["format"]["duration"], "candidate duration"),
        }
    }
    if not output["format_duration"]["equal"]:
        _failure(
            report,
            "timeline.format_duration",
            "container durations differ",
            baseline=baseline["format"]["duration"],
            candidate=candidate["format"]["duration"],
        )

    for kind in ("video", "audio"):
        left = baseline["packets"][kind]
        right = candidate["packets"][kind]
        left_summary = _packet_summary(left)
        right_summary = _packet_summary(right)
        mismatch = first_sequence_mismatch(left, right)
        output[kind] = {
            "baseline": left_summary,
            "candidate": right_summary,
            "equal": mismatch is None,
            "first_mismatch_packet": mismatch,
        }
        for side, packets, summary in (
            ("baseline", left, left_summary),
            ("candidate", right, right_summary),
        ):
            for field in ("pts", "dts"):
                if not summary[f"{field}_monotonic"]:
                    values = [packet[field] for packet in packets]
                    index = first_non_monotonic(values)
                    assert index is not None
                    _failure(
                        report,
                        f"timeline.{side}.{kind}.{field}_monotonic",
                        f"{side} {kind} {field.upper()} is not strictly monotonic at packet {index}",
                        baseline=values[index - 1],
                        candidate=values[index],
                    )
        if mismatch is not None:
            left_item = left[mismatch] if mismatch < len(left) else None
            right_item = right[mismatch] if mismatch < len(right) else None
            _failure(
                report,
                f"timeline.{kind}",
                f"{kind} packet timeline differs at packet {mismatch}",
                baseline=left_item,
                candidate=right_item,
            )
    report["checks"]["timeline"] = output


def _frame_report(time_base: str, frames: Sequence[FrameHash]) -> dict[str, Any]:
    return {
        "time_base": time_base,
        "frame_count": len(frames),
        "first_pts": frames[0].pts,
        "last_pts": frames[-1].pts,
        # framemd5 rescales Matroska's original packet timestamps into its output
        # time base.  That rounding can map two strictly increasing packet PTS
        # values to the same derived value; only an actual decrease is invalid.
        "pts_monotonic": first_decreasing([frame.pts for frame in frames]) is None,
        "dts_monotonic": first_decreasing([frame.dts for frame in frames]) is None,
        "hash_sequence_sha256": _sha256_sequence(frame.digest for frame in frames),
    }


def _compare_video(
    report: dict[str, Any],
    baseline_probe: dict[str, Any],
    candidate_probe: dict[str, Any],
    baseline: tuple[str, list[FrameHash]],
    candidate: tuple[str, list[FrameHash]],
) -> None:
    base_tb, base_frames = baseline
    candidate_tb, candidate_frames = candidate
    base_report = _frame_report(base_tb, base_frames)
    candidate_report = _frame_report(candidate_tb, candidate_frames)
    hash_mismatch = first_sequence_mismatch(
        [frame.digest for frame in base_frames],
        [frame.digest for frame in candidate_frames],
    )
    timing_mismatch = first_sequence_mismatch(
        [(frame.dts, frame.pts, frame.duration, frame.size) for frame in base_frames],
        [(frame.dts, frame.pts, frame.duration, frame.size) for frame in candidate_frames],
    )
    output = {
        "baseline": base_report,
        "candidate": candidate_report,
        "hashes_equal": hash_mismatch is None,
        "framemd5_timeline_equal": base_tb == candidate_tb and timing_mismatch is None,
        "first_hash_mismatch_frame": hash_mismatch,
        "first_timeline_mismatch_frame": timing_mismatch,
    }
    report["checks"]["video"] = output

    for side, probe, frames, summary in (
        ("baseline", baseline_probe, base_frames, base_report),
        ("candidate", candidate_probe, candidate_frames, candidate_report),
    ):
        expected_size = _integer(probe["video"].get("width"), "video width") * _integer(
            probe["video"].get("height"), "video height"
        ) * 4
        bad_size = next(
            (index for index, frame in enumerate(frames) if frame.size != expected_size),
            None,
        )
        if bad_size is not None:
            _failure(
                report,
                f"video.{side}.raster_size",
                f"{side} decoded frame {bad_size} has {frames[bad_size].size} bytes, "
                f"expected {expected_size} for bgr0",
            )
        for field in ("pts", "dts"):
            if not summary[f"{field}_monotonic"]:
                values = [getattr(frame, field) for frame in frames]
                index = first_decreasing(values)
                assert index is not None
                _failure(
                    report,
                    f"video.{side}.framemd5_{field}_monotonic",
                    f"{side} framemd5 {field.upper()} decreases at frame {index}",
                )
    if len(base_frames) != len(candidate_frames):
        _failure(
            report,
            "video.frame_count",
            "decoded video frame counts differ",
            baseline=len(base_frames),
            candidate=len(candidate_frames),
        )
    if hash_mismatch is not None:
        left = base_frames[hash_mismatch].digest if hash_mismatch < len(base_frames) else None
        right = (
            candidate_frames[hash_mismatch].digest
            if hash_mismatch < len(candidate_frames)
            else None
        )
        _failure(
            report,
            "video.framemd5",
            f"decoded video differs at frame {hash_mismatch}",
            baseline=left,
            candidate=right,
        )
    if base_tb != candidate_tb or timing_mismatch is not None:
        _failure(
            report,
            "video.framemd5_timeline",
            "framemd5 time base or frame timing differs",
            baseline={"time_base": base_tb, "frame": timing_mismatch},
            candidate={"time_base": candidate_tb, "frame": timing_mismatch},
        )


def _compare_audio(
    report: dict[str, Any], baseline: PcmDigest, candidate: PcmDigest
) -> None:
    output = {
        "baseline": {
            "pcm_sha256": baseline.sha256,
            "pcm_bytes": baseline.byte_count,
            "sample_frames": baseline.sample_frames,
        },
        "candidate": {
            "pcm_sha256": candidate.sha256,
            "pcm_bytes": candidate.byte_count,
            "sample_frames": candidate.sample_frames,
        },
        "sha256_equal": baseline.sha256 == candidate.sha256,
        "sample_frames_equal": baseline.sample_frames == candidate.sample_frames,
    }
    report["checks"]["audio"] = output
    if baseline.sample_frames != candidate.sample_frames:
        _failure(
            report,
            "audio.sample_frames",
            "decoded PCM sample-frame counts differ",
            baseline=baseline.sample_frames,
            candidate=candidate.sample_frames,
        )
    if baseline.sha256 != candidate.sha256:
        _failure(
            report,
            "audio.pcm_sha256",
            "decoded PCM SHA-256 differs",
            baseline=baseline.sha256,
            candidate=candidate.sha256,
        )


def compare_recordings(baseline_path: Path, candidate_path: Path) -> dict[str, Any]:
    """Return a JSON-serialisable exact-comparison report."""
    baseline_path = Path(baseline_path)
    candidate_path = Path(candidate_path)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "inputs": {
            "baseline": str(baseline_path),
            "candidate": str(candidate_path),
        },
        "checks": {},
        "failures": [],
        "pass": False,
    }
    for name, path in (("baseline", baseline_path), ("candidate", candidate_path)):
        if not path.is_file():
            _failure(report, f"{name}.missing", f"{name} recording not found: {path}")
    if report["failures"]:
        return report

    probes: dict[str, dict[str, Any]] = {}
    for name, path in (("baseline", baseline_path), ("candidate", candidate_path)):
        try:
            probes[name] = probe_recording(path)
        except RecordingError as exc:
            _failure(report, f"{name}.probe", str(exc))
    if len(probes) != 2:
        return report

    _validate_contract(report, "baseline", probes["baseline"])
    _validate_contract(report, "candidate", probes["candidate"])
    _compare_metadata(report, probes["baseline"], probes["candidate"])
    _compare_packet_timelines(report, probes["baseline"], probes["candidate"])

    frame_hashes: dict[str, tuple[str, list[FrameHash]]] = {}
    for name, path in (("baseline", baseline_path), ("candidate", candidate_path)):
        try:
            frame_hashes[name] = decode_framemd5(path)
        except RecordingError as exc:
            _failure(report, f"{name}.video_decode", str(exc))
    if len(frame_hashes) == 2:
        _compare_video(
            report,
            probes["baseline"],
            probes["candidate"],
            frame_hashes["baseline"],
            frame_hashes["candidate"],
        )

    pcm: dict[str, PcmDigest] = {}
    for name, path in (("baseline", baseline_path), ("candidate", candidate_path)):
        try:
            channels = _integer(probes[name]["audio"].get("channels"), "audio channels")
            pcm[name] = decode_pcm(path, channels)
        except RecordingError as exc:
            _failure(report, f"{name}.audio_decode", str(exc))
    if len(pcm) == 2:
        _compare_audio(report, pcm["baseline"], pcm["candidate"])

    report["pass"] = not report["failures"]
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare complete native FFV1/FLAC recordings after decode. "
            "No trimming or automatic alignment is performed."
        )
    )
    parser.add_argument("baseline", type=Path, help="known-good FFV1/FLAC MKV")
    parser.add_argument("candidate", type=Path, help="candidate FFV1/FLAC MKV")
    parser.add_argument("--json", type=Path, help="also write the JSON report to this path")
    args = parser.parse_args(argv)

    report = compare_recordings(args.baseline, args.candidate)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json:
        try:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(payload, encoding="utf-8")
        except OSError as exc:
            report["pass"] = False
            _failure(report, "report.write", f"could not write {args.json}: {exc}")
            payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(payload)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
