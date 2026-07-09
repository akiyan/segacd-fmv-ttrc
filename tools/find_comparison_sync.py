#!/usr/bin/env python3
"""Find the real recording frame where the player's debug HUD shows F0000.

Input can be either a video file or an already extracted PNG directory. The
reported frame index is the comparison renderer's real-frame index `k` at
`CMP_FPS`, so `ideal_frame = k - f0000_k`.
"""
import argparse
import glob
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_frameno import read_frameno  # noqa: E402


def frames_from_input(path, fps, workdir):
    p = Path(path)
    if p.is_dir():
        frames = sorted(glob.glob(str(p / "*.png")))
        if not frames:
            raise SystemExit(f"no PNG frames in {p}")
        return frames, None
    out = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="cmp_sync_"))
    out.mkdir(parents=True, exist_ok=True)
    frames = sorted(glob.glob(str(out / "*.png")))
    if not frames:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(p),
                        "-vf", f"fps={fps}", str(out / "%05d.png")], check=True)
        frames = sorted(glob.glob(str(out / "*.png")))
    return frames, out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="real recording video, or directory of extracted PNG frames")
    ap.add_argument("--fps", type=int, default=15, help="extraction fps for video input")
    ap.add_argument("--workdir", default="", help="reuse/write extracted frames here")
    ap.add_argument("--min-conf", type=float, default=0.6)
    ap.add_argument("--max-frame", type=int, default=0, help="only scan the first N frames")
    ap.add_argument("--context", type=int, default=6, help="print this many frames around F0000")
    args = ap.parse_args()

    frames, out = frames_from_input(args.input, args.fps, args.workdir)
    if args.max_frame:
        frames = frames[:args.max_frame]

    reads = []
    for k, p in enumerate(frames):
        val, conf = read_frameno(Image.open(p))
        ok = conf >= args.min_conf
        reads.append((k, val, conf, ok, p))

    # Prefer a confident F0000 that is followed by early movie frames soon
    # after. Depending on the 60fps->15fps sampling phase, F0001 may be skipped.
    anchor = None
    for idx, (k, val, conf, ok, _p) in enumerate(reads):
        if not ok or val != 0:
            continue
        future = [r for r in reads[idx + 1:idx + 8] if r[3]]
        if any(1 <= r[1] <= 2 for r in future):
            anchor = idx
            break
    inferred = None
    for idx, (k, val, conf, ok, _p) in enumerate(reads):
        if ok and 1 <= val <= 4:
            inferred = (idx, k - val, val, conf)
            break
    if anchor is None:
        for idx, (_k, val, _conf, ok, _p) in enumerate(reads):
            if ok and val == 0:
                anchor = idx
                break
    if anchor is None:
        raise SystemExit("no confident F0000 found")

    k0, _val, conf0, _ok, p0 = reads[anchor]
    print(f"f0000_frame={k0}")
    print(f"f0000_path={p0}")
    print(f"confidence={conf0:.3f}")
    if out:
        print(f"frames_dir={out}")
    print(f"CMP_F0_REAL_FRAME={k0}")
    if inferred and inferred[1] != k0:
        _idx, ik, val, conf = inferred
        print(f"inferred_first_pass_f0000_frame={ik}  from F{val:04X} conf={conf:.3f}")
    print("context:")
    lo = max(0, anchor - args.context)
    hi = min(len(reads), anchor + args.context + 1)
    for k, val, conf, ok, p in reads[lo:hi]:
        mark = "<-- F0000" if k == k0 else ""
        shown = f"F{val:04X}" if ok else "----"
        print(f"  k={k:05d} {shown} conf={conf:.3f} {Path(p).name} {mark}")


if __name__ == "__main__":
    main()
