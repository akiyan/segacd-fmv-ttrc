#!/usr/bin/env python3
"""Flip-cadence timeline model for the H40/30fps fixed-N2 player.

Mechanism being modelled (boot/movieplay_ip.s):

- `do_flip` stamps `pace_flip_tick` with the ACTUAL flip time; the next
  frame's arm point is that stamp + PACE_N2_ARM_TICKS (800 ticks).  A flip
  accepted late inside its VBlank therefore shifts the next deadline base
  late as well: heavy stretches ratchet the flip phase toward the VBlank
  tail until one frame falls past the FC..FF guard (or past the blank) and
  pays a whole extra VBlank, which resets the phase early.  This is why
  breaks land on modest frames at the END of plateaus.
- Normal frames may flip anywhere inside the target VBlank except its
  terminal 4 V-counter lines.  Palette frames instead need a FRESH blank
  start (wait_vb_start after the arm point) and then write 64 CRAM words
  before flipping — an earlier, harder deadline.

Per frame n+1 the Main-side completion is approximated from the DEBUG HUD:

    E = flip_n + K0 + K1*n_upd + W*LINE + U*TICK

where W (sub_wait_lines) and U (main_pattern_ticks, which already includes
Pass2's internal wait_vb_start spins) come from the recording's HUD OCR
series, and K0/K1 absorb parse + bitmap + blit + HUD prep.  The model then
applies the arm/guard rules on the real VBlank grid and reports every frame
whose flip lands 3+ fields after the previous one.  K0, K1, and the blank
phase offset are calibrated by sweeping until the observed break set is
reproduced (gates G2/G3 of the investigation plan).

Usage:
  tools/python.sh harness/cold_cap_model/model.py \
      --frames frames_195.tsv \
      --hud videos/SonicJamOp_H40_cold195_emu_hud.tsv \
      [--k0-us N] [--k1-ns N] [--sweep]
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

TICK_US = 30.72            # Mega-CD stopwatch tick
FIELD_US = 1_000_000 / (60_000 / 1_001)   # 16,683.35 us NTSC field
LINE_US = FIELD_US / 262.5                # ~63.55 us scanline
BLANK_LINES = 38                          # V28 NTSC: 262 - 224
BLANK_US = BLANK_LINES * LINE_US          # ~2,415 us
TAIL_GUARD_US = 4 * LINE_US               # FC..FF terminal guard
ARM_US = 800 * TICK_US                    # PACE_N2_ARM_TICKS
CRAM_US = 64 * 1.6                        # 64 CRAM word writes, ~0.1 ms


def load_joined(frames_tsv: Path, hud_tsv: Path):
    packs = {}
    with frames_tsv.open() as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            packs[int(r["frame"])] = r
    rows = []
    with hud_tsv.open() as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            if r["loop"] != "0":
                continue
            f = int(r["frame"])
            if f == 0 or f not in packs:
                continue
            p = packs[f]
            rows.append(dict(
                frame=f,
                w_lines=int(r["sub_wait_lines"]),
                u_ticks=int(r["main_pattern_ticks"]),
                samples=int(r["sample_count"]),
                n_upd=int(p["n_upd"]),
                pal=int(p["pal_switch"]),
            ))
    rows.sort(key=lambda d: d["frame"])
    return rows


def observed_breaks(rows):
    """Frames held for 3+ captures, excluding the trailing end-hold frame."""
    return {r["frame"] for r in rows[:-1] if r["samples"] > 2}


def simulate(rows, k0_us: float, k1_ns: float, phase_us: float):
    """Run the ratchet.  Blank k spans [k*FIELD + phase, ... + BLANK_US).

    flip_0 sits in blank 0 at `phase + small`.  Returns (breaks, min_margin)
    where margin is the distance from the flip to the guarded tail of its
    blank (negative margin means the frame broke).
    """
    def blank_index(t):
        return int(t // FIELD_US)

    def blank_start(k):
        return k * FIELD_US + phase_us

    flip = blank_start(0)
    breaks = set()
    margins = []
    for r in rows:
        e = flip + k0_us + r["n_upd"] * k1_ns / 1000.0 \
            + r["w_lines"] * LINE_US + r["u_ticks"] * TICK_US
        t = max(e, flip + ARM_US)
        target = blank_index(flip) + 2
        if r["pal"]:
            # wait_vb_start semantics: leave any current blank, then wait
            # for the next blank START; CRAM replace precedes the flip.
            k = blank_index(t - phase_us) + 1 \
                if t >= blank_start(blank_index(t - phase_us)) else 0
            start = blank_start(k)
            if start < t:
                start = blank_start(k + 1)
            new_flip = start + CRAM_US
        else:
            k = blank_index(t - phase_us)
            in_blank = blank_start(k) <= t < blank_start(k) + BLANK_US \
                - TAIL_GUARD_US
            new_flip = t if in_blank and t >= blank_start(target) else None
            if new_flip is None:
                # wait for the next blank start after t
                k2 = blank_index(t - phase_us) + 1
                start = blank_start(k2)
                while start < t:
                    k2 += 1
                    start = blank_start(k2)
                new_flip = start
        if blank_index(new_flip) < target:
            new_flip = blank_start(target)      # arm spin holds until here
        used = blank_index(new_flip) - blank_index(flip)
        if used >= 3:
            breaks.add(r["frame"])
        tail = blank_start(blank_index(new_flip)) + BLANK_US - TAIL_GUARD_US
        margins.append((tail - new_flip, r["frame"]))
        flip = new_flip
    return breaks, margins


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", type=Path, required=True)
    ap.add_argument("--hud", type=Path, required=True)
    ap.add_argument("--k0-us", type=float, default=9000.0,
                    help="fixed Main work per frame, us")
    ap.add_argument("--k1-ns", type=float, default=0.0,
                    help="additional Main work per update entry, ns")
    ap.add_argument("--phase-us", type=float, default=0.0,
                    help="flip offset inside blank 0, us")
    ap.add_argument("--sweep", action="store_true",
                    help="sweep K0/phase and report break-set matches")
    args = ap.parse_args()
    for name in ("frames", "hud"):
        if getattr(args, name).suffix.lower() != ".tsv":
            ap.error(f"--{name} input must use the .tsv extension")

    rows = load_joined(args.frames, args.hud)
    obs = observed_breaks(rows)
    print(f"{len(rows)} frames; observed breaks: {sorted(obs)}")

    if not args.sweep:
        breaks, margins = simulate(rows, args.k0_us, args.k1_ns,
                                   args.phase_us)
        margins.sort()
        print(f"K0={args.k0_us}us K1={args.k1_ns}ns phase={args.phase_us}us "
              f"-> breaks {sorted(breaks)}")
        print("tightest margins (us, frame):",
              [(round(m), f) for m, f in margins[:10]])
        return

    exact, near = [], []
    for k0 in range(4000, 15001, 250):
        for phase in range(0, 2200, 200):
            breaks, _ = simulate(rows, float(k0), args.k1_ns, float(phase))
            if breaks == obs:
                exact.append((k0, phase))
            elif len(breaks ^ obs) <= 2:
                near.append((k0, phase, sorted(breaks)))
    print(f"exact break-set matches (K0us, phase_us): {exact[:40]}")
    if not exact:
        for k0, phase, br in near[:15]:
            print(f"  near: K0={k0} phase={phase} -> {br}")


if __name__ == "__main__":
    main()
