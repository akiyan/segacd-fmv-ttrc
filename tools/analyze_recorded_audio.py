#!/usr/bin/env python3
"""Inspect recorded RetroArch audio for quick regression checks."""

import argparse
import hashlib
import json
import struct
import subprocess
import wave
from pathlib import Path

import numpy as np


def run(cmd):
    return subprocess.check_output(cmd, text=True)


def ffprobe(path):
    data = run(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ]
    )
    return json.loads(data)


def read_wav(path):
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise SystemExit(f"{path}: expected 16-bit WAV, got {wav.getsampwidth() * 8}-bit")
        frames = wav.getnframes()
        rate = wav.getframerate()
        channels = wav.getnchannels()
        data = wav.readframes(frames)
    samples = struct.unpack("<%dh" % (len(data) // 2), data)
    return {
        "frames": frames,
        "rate": rate,
        "channels": channels,
        "data": data,
        "samples": samples,
    }


def percentile(values, ratio):
    if not values:
        return 0
    return values[min(len(values) - 1, int(len(values) * ratio))]


def channel_samples(samples, channel, channels):
    return samples[channel::channels]


def sample_stats(samples):
    if not samples:
        return {"peak_abs": 0, "rms": 0}
    # Keep this pass out of a long Python scalar loop.  Python 3.14.4 has
    # occasionally substituted an unrelated outer local for the `abs` builtin
    # here, while NumPy also completes the 15+ million-sample capture much
    # faster.  int64 keeps the square and sum exact for signed 16-bit audio.
    values = np.asarray(samples, dtype=np.int64)
    peak_abs = int(np.abs(values).max())
    rms = float(np.sqrt(np.mean(values * values, dtype=np.float64)))
    return {"peak_abs": peak_abs, "rms": rms}


def adjacent_jumps(samples):
    """Return absolute adjacent deltas without a long Python scalar loop.

    Python 3.14.4 has occasionally substituted an unrelated outer local into
    long-running loops in this script.  NumPy performs the complete subtraction
    and absolute-value pass in compiled code.
    """
    values = np.asarray(samples, dtype=np.int32)
    return np.abs(np.diff(values)).tolist()


def candidate_indices(jumps, threshold):
    """Return one-based sample indices whose delta reaches the threshold."""
    values = np.asarray(jumps, dtype=np.int32)
    return (np.flatnonzero(values >= threshold) + 1).tolist()


def wav_stats(path, seconds, jump_threshold):
    wav = read_wav(path)
    samples = wav["samples"]
    per_channel = []
    all_jumps = []
    jump_candidates = []
    overall = sample_stats(samples)
    for channel in range(wav["channels"]):
        current = channel_samples(samples, channel, wav["channels"])
        jumps = adjacent_jumps(current)
        jumps_sorted = sorted(jumps)
        candidates = []
        for i in candidate_indices(jumps, jump_threshold):
            jump = jumps[i - 1]
            candidates.append(
                {
                    "channel": channel,
                    "frame_index": i,
                    "time_seconds": i / wav["rate"],
                    "jump": jump,
                    "from": current[i - 1],
                    "to": current[i],
                }
            )
        per_channel.append(
            {
                "channel": channel,
                **sample_stats(current),
                "max_jump": max(jumps) if jumps else 0,
                "p99_jump": percentile(jumps_sorted, 0.99),
                "p999_jump": percentile(jumps_sorted, 0.999),
                "jump_candidate_count": len(candidates),
                "first_jump_candidates": candidates[:10],
            }
        )
        all_jumps.extend(jumps)
        jump_candidates.extend(candidates)
    jumps_sorted = sorted(all_jumps)
    prefix_bytes = wav["rate"] * seconds * wav["channels"] * 2
    clip_count = 0
    for sample in samples:
        if sample in (-32768, 32767):
            clip_count += 1
    return {
        "path": str(path),
        "seconds": len(samples) / (wav["rate"] * wav["channels"]),
        "frames": wav["frames"],
        "rate": wav["rate"],
        "channels": wav["channels"],
        "sha1_prefix": hashlib.sha1(wav["data"][:prefix_bytes]).hexdigest(),
        **overall,
        "max_jump": max(all_jumps) if all_jumps else 0,
        "p99_jump": percentile(jumps_sorted, 0.99),
        "p999_jump": percentile(jumps_sorted, 0.999),
        "clip_count": clip_count,
        "jump_threshold": jump_threshold,
        "jump_candidate_count": len(jump_candidates),
        "per_channel": per_channel,
        "first_jump_candidates": jump_candidates[:10],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("recording", help="recorded MKV/MP4 file")
    parser.add_argument("--wav", help="WAV extracted from the recording")
    parser.add_argument("--compare-wav", help="second WAV to compare with --wav")
    parser.add_argument("--seconds", type=int, default=60, help="prefix length for hash comparison")
    parser.add_argument(
        "--jump-threshold",
        type=int,
        default=12000,
        help="sample-to-sample delta treated as a click candidate",
    )
    parser.add_argument(
        "--fail-on-clicks",
        action="store_true",
        help="exit non-zero if clip or jump candidates are detected",
    )
    parser.add_argument(
        "--min-rms",
        type=float,
        default=0.0,
        help="exit non-zero if WAV RMS is below this value",
    )
    args = parser.parse_args()

    result = {"recording": str(Path(args.recording)), "probe": ffprobe(Path(args.recording))}
    wav_result = None

    if args.wav:
        wav_result = wav_stats(Path(args.wav), args.seconds, args.jump_threshold)
        result["wav"] = wav_result

    if args.wav and args.compare_wav:
        a = read_wav(Path(args.wav))
        b = read_wav(Path(args.compare_wav))
        n = min(len(a["data"]), len(b["data"]), a["rate"] * args.seconds * a["channels"] * 2)
        result["compare"] = {
            "compare_wav": args.compare_wav,
            "prefix_seconds": args.seconds,
            "prefix_equal": a["data"][:n] == b["data"][:n],
            "compared_bytes": n,
        }

    print(json.dumps(result, indent=2))

    if args.fail_on_clicks and wav_result:
        if wav_result["clip_count"] > 0 or wav_result["jump_candidate_count"] > 0:
            raise SystemExit(1)
        if args.min_rms > 0 and wav_result["rms"] < args.min_rms:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
