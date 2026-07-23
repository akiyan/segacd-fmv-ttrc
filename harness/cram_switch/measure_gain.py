#!/usr/bin/env python3
"""Measure the color-improvement opportunity of adding CRAM (palette) switch points.

For a candidate switch frame i and a forward window [i, i+W), compare:
  E_keep  = quantization error painting the window with the CURRENT segment palette
  E_fresh = quantization error painting the window with a palette learned on the window
  gain    = (E_keep - E_fresh) / E_keep   (relative color-error reduction)

A large gain at a mid-segment scene change = the current palette is stale for the
upcoming scene = a switch there would improve colour. We compare candidate gains
against the gains at the existing (darkness-based) boundaries as a reference.

Cheap: reuses the sim's cached master frames + palette machinery. Read-only.
"""
import os, sys, pickle, glob
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import sim  # noqa: E402
from quantize_global4_tiles import build_palettes  # noqa: E402

OUT = os.environ.get("CBRSIM_OUT", "videos/machi_ed_H40_320x224_adpcm22")
W = int(os.environ.get("WIN", "40"))            # forward window (frames)
log = pickle.load(open(f"{OUT}/decisions.pkl", "rb"))
frames = sorted(glob.glob(f"{OUT}/master/*.png"))
n = len(frames)
frame_seg = np.asarray(log["frame_seg"])
seg_pals = [np.asarray(p, np.uint8) for p in log["seg_pals"]]

_tile_cache = {}
def tiles(i):
    if i not in _tile_cache:
        m = sim.to_rgb333(np.asarray(Image.open(frames[i]).convert("RGB")))
        _tile_cache[i] = sim.tile_blocks(m).astype(np.int64)      # (C,64,3)
    return _tile_cache[i]

def win_tiles(i, w):
    return np.concatenate([tiles(j) for j in range(i, min(i + w, n))], axis=0)  # (C*w,64,3)

def quant_err(tstack, pals_arr):
    """Sum of per-cell min quantization error (assign each cell to its best of 4 palettes)."""
    C = tstack.shape[0]
    px = tstack.reshape(C, 64, 1, 3)
    per_pal = np.stack([                                          # (C,4)
        ((px - pals_arr[p].reshape(1, 1, 15, 3).astype(np.int64)) ** 2).sum(3).min(2).sum(1)
        for p in range(4)], axis=1)
    return float(per_pal.min(1).sum())                           # assign best palette per cell

def gain_at(i, cur_seg):
    ts = win_tiles(i, W)
    e_keep = quant_err(ts, seg_pals[cur_seg])
    fresh = np.stack(build_palettes(ts.astype(np.uint8), n_pal=4)).astype(np.uint8)
    e_fresh = quant_err(ts, fresh)
    return e_keep, e_fresh, (e_keep - e_fresh) / max(e_keep, 1)

bnds = [i for i in range(1, n) if frame_seg[i] != frame_seg[i - 1]]

# candidates loaded from mad_ed.npy peaks (mid-segment scene changes)
mad = np.load(os.path.join(os.path.dirname(__file__), "mad_ed.npy"))
thr = mad.mean() + 3 * mad.std()
peaks = [i for i in range(2, n - 1) if mad[i] > thr and mad[i] >= mad[i-1] and mad[i] >= mad[i+1]]
def near_bnd(i): return min([abs(i - b) for b in bnds] + [9999])
mid = [i for i in peaks if near_bnd(i) > 8 and i + W <= n]

print(f"W={W}  existing boundaries={len(bnds)}  mid-segment scene-cuts={len(mid)}")
print("\n== gain at EXISTING boundaries (reference: these DO switch) ==")
print("  frame  E_keep     E_fresh    gain%   (E_keep uses the palette of the segment BEFORE the boundary)")
for b in bnds:
    if b + W > n: continue
    cur = int(frame_seg[b - 1])
    ek, ef, g = gain_at(b, cur)
    print(f"  {b:5d}  {ek:9.0f}  {ef:9.0f}  {100*g:5.1f}")

print("\n== gain at MID-SEGMENT scene cuts (candidate new switch points) ==")
print("  frame  seg  dist2bnd  E_keep     E_fresh    gain%")
rows = []
for i in mid:
    cur = int(frame_seg[i])
    ek, ef, g = gain_at(i, cur)
    rows.append((i, cur, near_bnd(i), ek, ef, g))
    print(f"  {i:5d}  {cur:3d}  {near_bnd(i):7d}  {ek:9.0f}  {ef:9.0f}  {100*g:5.1f}")

# control: random mid-segment NON-cut frames (should show low gain)
rng = list(range(500, n - W, 400))
ctrl = [i for i in rng if near_bnd(i) > 20 and mad[i] < mad.mean()]
print("\n== control: quiet mid-segment frames (should be LOW gain) ==")
gs = []
for i in ctrl[:8]:
    cur = int(frame_seg[i])
    ek, ef, g = gain_at(i, cur); gs.append(g)
    print(f"  {i:5d}  gain% {100*g:5.1f}")

if rows:
    cg = np.array([r[5] for r in rows])
    print(f"\nSUMMARY: candidate gain% mean={100*cg.mean():.1f} max={100*cg.max():.1f} "
          f">10%={int((cg>0.10).sum())}/{len(cg)}   control gain% mean={100*np.mean(gs):.1f}")
