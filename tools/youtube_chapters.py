#!/usr/bin/env python3
"""Generate YouTube chapter markers at CRAM (palette-segment) switch points.

Each palette segment in the sim's decision log (`frame_seg`) is one CRAM swap, so
one chapter is emitted per segment start. This makes the CRAM switches navigable
on the uploaded video and is the *permanent* way this project chapters its codec
videos (see AGENTS.md "YouTube Upload Style"): every analysis / real-playback
upload prepends the output of this tool to its description.

YouTube's chapter rules are enforced so the list is actually rendered:
  * the first chapter is at 00:00,
  * chapters are >= 10 s apart (a switch closer than 10 s to the previous chapter
    is merged away — YouTube would reject a shorter one anyway; merges are logged
    to stderr so nothing is dropped silently),
  * timestamps ascend, and there are at least 3 chapters (else nothing is emitted,
    since YouTube ignores a shorter list).

Timestamps use the *content* fps (the segment frame index / fps). Analysis videos
normally begin at content frame 0. Playback recordings may retain the Mega-CD
startup sequence; pass ``--content-offset`` for those so only the chapter markers
move while the recording remains intact. Determine that offset by ordinary visual
playback, not DEBUG HUD OCR.

Usage:
    python tools/youtube_chapters.py <sim_out_dir> [fps]
    python tools/youtube_chapters.py <sim_out_dir> [fps] \
        --content-offset SECONDS --intro-label "Mega-CD startup"
Prints the chapter block to stdout; prepend it (with a blank line after) to the
video description before uploading.
"""
import argparse
import sys
import pickle
from pathlib import Path

import numpy as np

MIN_GAP_S = 10.0            # YouTube requires each chapter to be at least 10 s long
MIN_CHAPTERS = 3           # YouTube renders chapters only if there are at least 3


def fmt(t):
    t = int(round(t))
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_fps(value):
    if value is None or isinstance(value, (int, float)):
        return value
    if "/" in value:
        num, den = value.split("/", 1)
        return float(num) / float(den)
    return float(value)


def chapters(out_dir, fps=None, content_offset=0.0):
    if content_offset < 0:
        raise ValueError("content_offset must be zero or greater")
    log = pickle.load(open(Path(out_dir) / "decisions.pkl", "rb"))
    fps = parse_fps(fps) if fps else float(log.get("fps", 15))
    fseg = np.asarray(log["frame_seg"])
    n = len(fseg)
    # one boundary per segment start (frame 0 is the first segment's start)
    bnds = [0] + [i for i in range(1, n) if fseg[i] != fseg[i - 1]]
    out = []
    if content_offset > 0:
        # YouTube requires its first chapter at 00:00. Keep the startup as a
        # real chapter rather than pretending that movie frame 0 begins here.
        out.append((0.0, None, False))
        last_t = 0.0
    else:
        last_t = -MIN_GAP_S
    for b in bnds:
        t = content_offset + b / fps
        if t - last_t >= MIN_GAP_S or not out:
            out.append((t, int(fseg[b]), b > 0))
            last_t = t
        else:
            print(f"[youtube_chapters] merged segment {int(fseg[b])} at {fmt(t)} "
                  f"(<{MIN_GAP_S:.0f}s after previous chapter)", file=sys.stderr)
    return out, fps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sim_out_dir")
    parser.add_argument("fps", nargs="?", type=parse_fps)
    parser.add_argument(
        "--content-offset",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="time from recording start to movie frame 0; shifts chapters only",
    )
    parser.add_argument(
        "--intro-label",
        default="Mega-CD startup",
        help="00:00 chapter label when --content-offset is greater than zero",
    )
    args = parser.parse_args()
    if args.content_offset < 0:
        parser.error("--content-offset must be zero or greater")
    out, fps = chapters(args.sim_out_dir, args.fps, args.content_offset)
    if len(out) < MIN_CHAPTERS:
        print(f"[youtube_chapters] only {len(out)} chapters (< {MIN_CHAPTERS}); "
              f"YouTube would ignore them, emitting nothing.", file=sys.stderr)
        return
    lines = []
    for t, seg, is_switch in out:
        if seg is None:
            lines.append(f"{fmt(t)} {args.intro_label}")
        elif is_switch:
            lines.append(f"{fmt(t)} Segment {seg + 1} (CRAM switch)")
        else:
            lines.append(f"{fmt(t)} Segment {seg + 1}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
