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

Timestamps use the *content* fps (the segment frame index / fps): the analysis and
real-playback videos share the movie's wall-clock timeline, so a segment that
starts at content frame F is at F/fps seconds in the upload. Pass an explicit fps
if a particular render retimes the content.

Usage:
    python tools/youtube_chapters.py <sim_out_dir> [fps]
Prints the chapter block to stdout; prepend it (with a blank line after) to the
video description before uploading.
"""
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


def chapters(out_dir, fps=None):
    log = pickle.load(open(Path(out_dir) / "decisions.pkl", "rb"))
    fps = float(fps) if fps else float(log.get("fps", 15))
    fseg = np.asarray(log["frame_seg"])
    n = len(fseg)
    # one boundary per segment start (frame 0 is the first segment's start)
    bnds = [0] + [i for i in range(1, n) if fseg[i] != fseg[i - 1]]
    out, last_t = [], -MIN_GAP_S
    for b in bnds:
        t = b / fps
        if t - last_t >= MIN_GAP_S or not out:
            out.append((t, int(fseg[b])))
            last_t = t
        else:
            print(f"[youtube_chapters] merged segment {int(fseg[b])} at {fmt(t)} "
                  f"(<{MIN_GAP_S:.0f}s after previous chapter)", file=sys.stderr)
    if out and out[0][0] > 0:                          # guarantee a 00:00 chapter
        out.insert(0, (0.0, int(fseg[0])))
    return out, fps


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out, fps = chapters(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    if len(out) < MIN_CHAPTERS:
        print(f"[youtube_chapters] only {len(out)} chapters (< {MIN_CHAPTERS}); "
              f"YouTube would ignore them, emitting nothing.", file=sys.stderr)
        return
    lines = [f"{fmt(t)} Segment {seg + 1} (CRAM switch)" if t > 0
             else f"{fmt(t)} Segment {seg + 1}" for t, seg in out]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
