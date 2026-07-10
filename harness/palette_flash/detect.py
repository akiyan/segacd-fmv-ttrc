#!/usr/bin/env python3
"""Palette-flash detector for the SEGA-CD player.

Definitively catches the "CRAM applied a frame too early/late" flash by comparing
the REAL recording against pixel-exact GT decodes at every palette boundary,
INCLUDING the two mismatched palette x content combinations.

At boundary Fb (segment S-1 -> S) the four candidate images are:
  prev  = content(Fb-1) x CRAM(S-1)   correct previous frame
  new   = content(Fb)   x CRAM(S)     correct new frame
  early = content(Fb-1) x CRAM(S)     OLD frame with NEW palette  (CRAM too early)
  late  = content(Fb)   x CRAM(S-1)   NEW frame with OLD palette  (flip-before-CRAM)

For each real capture frame in a window around the boundary we pick the
best-matching candidate on the content band. If any frame's best match is `early`
or `late`, that is a flash. This is robust to 60fps capture of 15fps content
(a 1-game-frame flash spans ~4 capture frames; we test every one).

usage:
  python3 harness/palette_flash/detect.py REAL.mkv [MOVIE.DAT] [--fps 60] \
      [--win 0.6] [--thresh 6] [--dump DIR]
"""
import argparse
import subprocess
import io
import sys
from pathlib import Path
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "tools"))
import decode as D
from read_frameno import read_frameno


def grab(mkv, t, fps=None, n=1):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "%.4f" % t, "-i", mkv]
    if n > 1:
        cmd += ["-frames:v", str(n), "-f", "image2pipe", "-vcodec", "png", "-"]
        r = subprocess.run(cmd, capture_output=True)
        # split concatenated PNGs
        blob = r.stdout
        out = []
        sig = b"\x89PNG\r\n\x1a\n"
        idxs = [i for i in range(len(blob)) if blob[i:i + 8] == sig]
        for j, s in enumerate(idxs):
            e = idxs[j + 1] if j + 1 < len(idxs) else len(blob)
            out.append(np.asarray(Image.open(io.BytesIO(blob[s:e])).convert("RGB"), np.int16))
        return out
    cmd += ["-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
    r = subprocess.run(cmd, capture_output=True)
    return [np.asarray(Image.open(io.BytesIO(r.stdout)).convert("RGB"), np.int16)]


def find_t0(mkv, probe_ts=(108.0, 112.0, 120.0)):
    """F0000 seek time from HUD reads at a few probe points (linear)."""
    ests = []
    for t in probe_ts:
        img = grab(mkv, t)[0]
        f, conf = read_frameno(Image.fromarray(img.astype(np.uint8)))
        if conf >= 0.9 and f >= 0:
            ests.append(t - f / 15.0)
    if not ests:
        raise SystemExit("could not read HUD F to establish t0")
    ests.sort()
    return ests[len(ests) // 2]


def calibrate_offset(mkv, hdr, t0, cal_frame, gt_img):
    """Find (dy, dx) aligning GT content band into the 320x224 real frame."""
    real = grab(mkv, t0 + cal_frame / 15.0 + 0.02)[0]
    H, W = gt_img.shape[:2]
    best = (1e9, 40, 0)
    for dy in range(28, 60):
        for dx in range(-2, 3):
            y0, x0 = dy, max(0, dx)
            gx0 = max(0, -dx)
            if y0 + H > real.shape[0] or x0 + (W - gx0) > real.shape[1]:
                continue
            r = real[y0:y0 + H, x0:x0 + W - gx0]
            g = gt_img[:, gx0:W]
            dm = np.abs(r.astype(int) - g.astype(int)).mean()
            if dm < best[0]:
                best = (dm, dy, dx)
    return best[1], best[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("real")
    ap.add_argument("movie", nargs="?", default="out/movieplay/MOVIE.DAT")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--win", type=float, default=0.6, help="+-seconds around each boundary")
    ap.add_argument("--thresh", type=float, default=6.0,
                    help="min diff margin (correct-best minus flash-best) to call a flash")
    ap.add_argument("--dump", default="")
    args = ap.parse_args()

    hdr = D.load(args.movie)
    bounds = D.palette_boundaries(hdr)
    bounds = [b for b in bounds if b > 0]     # frame0 has no 'previous segment'
    print("mode=%d frames=%d cells=%d  palette boundaries: %s" %
          (hdr["d"][38], hdr["nfr"], hdr["CC"], bounds))

    # decode once, snapshotting each boundary's (Fb-1, Fb) states + a calib frame
    need = set()
    for b in bounds:
        need.add(b - 1); need.add(b)
    CAL = min(bounds[0] - 40, 300) if bounds else 200
    CAL = max(CAL, 30)
    need.add(CAL)
    dec = D.Decoder(hdr)
    snaps = {}
    for i in range(max(need) + 1):
        dec.step(i)
        if i in need:
            snaps[i] = dec.snapshot()

    # candidates per boundary
    cand = {}
    for b in bounds:
        cprev, poolprev, segprev, cramprev = snaps[b - 1]
        cnew, poolnew, segnew, cramnew = snaps[b]
        cand[b] = {
            "prev":  D.render(hdr, cprev, poolprev, cramprev),
            "new":   D.render(hdr, cnew, poolnew, cramnew),
            "early": D.render(hdr, cprev, poolprev, cramnew),   # old frame, NEW palette
            "late":  D.render(hdr, cnew, poolnew, cramprev),    # new frame, OLD palette
        }

    t0 = find_t0(args.real)
    ccells, cpool, cseg, ccram = snaps[CAL]
    gt_cal = D.render(hdr, ccells, cpool, ccram)
    dy, dx = calibrate_offset(args.real, hdr, t0, CAL, gt_cal)
    print("t0=%.3fs  content offset dy=%d dx=%d" % (t0, dy, dx))
    H, W = gt_cal.shape[:2]

    if args.dump:
        Path(args.dump).mkdir(parents=True, exist_ok=True)

    any_flash = False
    for b in bounds:
        t_center = t0 + b / 15.0
        n = int(args.win * args.fps * 2) + 1
        imgs = grab(args.real, t_center - args.win, fps=args.fps, n=n)
        rows = []
        for j, real in enumerate(imgs):
            # extract the content band at the calibrated offset
            y0, x0 = dy, max(0, dx); gx0 = max(0, -dx)
            if y0 + H > real.shape[0]:
                continue
            r = real[y0:y0 + H, x0:x0 + W - gx0].astype(int)
            hud = read_frameno(Image.fromarray(real.astype(np.uint8)))
            scores = {}
            for name, im in cand[b].items():
                g = im[:, gx0:W].astype(int)
                scores[name] = float(np.abs(r - g).mean())
            best = min(scores, key=scores.get)
            correct_best = min(scores["prev"], scores["new"])
            flash_best = min(scores["early"], scores["late"])
            margin = correct_best - flash_best
            rows.append((j, hud[0], hud[1], best, scores, margin))
        # a flash frame: best candidate is early/late AND it beats both correct by margin
        flashes = [row for row in rows if row[3] in ("early", "late") and row[5] >= args.thresh]
        # only count if the winning flash frame isn't just a mid-transition ambiguity:
        # require the flash candidate to also beat 'new' and 'prev' individually
        real_flashes = []
        for row in flashes:
            sc = row[4]
            if sc[row[3]] < sc["prev"] - 1 and sc[row[3]] < sc["new"] - 1:
                real_flashes.append(row)
        seg = bounds.index(b)
        if real_flashes:
            any_flash = True
            fr = real_flashes[len(real_flashes) // 2]
            print("  FLASH @ boundary f%d (P%d->P%d): %d capture frame(s), type=%s "
                  "margin=%.1f  (HUD F~%d)" %
                  (b, seg, seg + 1, len(real_flashes), fr[3], fr[5], fr[2]))
            if args.dump:
                # dump the worst flash frame + candidates
                tt = t_center - args.win + fr[0] / args.fps
                real = grab(args.real, tt)[0]
                y0 = dy
                Image.fromarray(real[y0:y0 + H, :W].astype(np.uint8)).save(
                    "%s/f%d_real.png" % (args.dump, b))
                for name in ("prev", "new", "early", "late"):
                    Image.fromarray(cand[b][name].astype(np.uint8)).save(
                        "%s/f%d_%s.png" % (args.dump, b, name))
        else:
            best_margin = max((row[5] for row in rows), default=0)
            print("  clean  @ boundary f%d (P%d->P%d)  max flash-margin=%.1f" %
                  (b, seg, seg + 1, best_margin))

    print("VERDICT:", "FLASH DETECTED" if any_flash else "clean (no palette flash)")
    return 1 if any_flash else 0


if __name__ == "__main__":
    sys.exit(main())
