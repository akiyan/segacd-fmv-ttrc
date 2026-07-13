# Full-screen H40 ring collapse — investigation

Diagnostic notes and scripts for the permanent stream desync ("collapse") seen
when playing the full-screen H40 (320x224, 40x28 = 1120-cell) `machi_ed` stream
on hardware (GPGX). Kept here per the harness policy in `AGENTS.md`.

## Symptom

The player boots, plays the opening cleanly for ~15 s, then the picture turns to
multicolor garbage and never recovers (flat red/blue field with a few moving
tiles for the rest of the movie). A held/frozen frame is clean; only live
playback collapses — a real-time effect, not corrupt data.

## Root cause 1 — back-pressure ratchet (fixed)

The pack's forward-fill fills the streaming ring up to `RING_CAP_KB`. The player
(`boot/movieplay_sp.s`, `pump_poll`) stops pumping CD when ring occupancy reaches
the back-pressure threshold `RING_SIZE - 0x1000 = 416 KB`. With `RING_CAP_KB=400`
the gap to the threshold was only 16 KB, so heavy scenes pushed occupancy into
back-pressure, the pump stalled, the CDC FIFO dropped sectors, and the stream
desynced permanently.

Fix: lower `CBRSIM_RING_CAP_KB` well below 416 KB (300 KB verified safe, 66 KB
margin at 350 KB). Env `CBRSIM_RING_CAP_KB` in `tools/pack_stream.py`.

## Root cause 2 — cold-vs-RING_CAP jitter budget (empirical)

Even below back-pressure, the collapse depends on BOTH the per-frame cold count
(`CBRSIM_PACK_MAXCOLD`) and `RING_CAP_KB`. Measured on hardware:

| per-frame cold cap | RING_CAP | result   |
|--------------------|----------|----------|
| 192                | 300 KB   | complete |
| 260                | 300 KB   | COLLAPSE |
| 260                | 350 KB   | complete |

So a higher cold cap needs a larger ring (more jitter buffer). `PACK_MAXCOLD`
caps only the cold (Raw / new-CD-pattern) tiles; Same and reuse
(Near/Coa/Flbk/Buf) are unaffected. Cold is CD-bandwidth-bound at ~262/frame on
1x (9830 payload bytes / 32 B, minus control/audio).

## Key negative result — the deterministic ring model CANNOT predict it

`ring_analyze.py` / `ring_jitter.py` reconstruct the pack's occupancy trajectory
`occ[i] = delivered - popped` for each config. Findings:

- `occ` is held near `RING_CAP` for the whole movie body (forward-fill keeps it
  full); it only dips toward zero at the very end (frames ~3978-3997) as the
  stream drains out.
- The collapse frame (~218, early) shows **full deterministic occ** — the pack
  sees no problem there.
- `RING_CAP=300` (collapse) and `RING_CAP=350` (safe) at cap=260 have an
  **identical** deterministic `occ` trajectory everywhere except the unused
  higher ceiling; `ring_min`, drain rate, and stall-survival margins are all
  identical.

Conclusion: the collapse has **no signature in the deterministic occupancy
model**. It is a real-time hardware effect (CD delivery jitter / CDC FIFO / pump
timing / slip phase) that a larger ring peak absorbs but the pack cannot see.
Predicting it precisely would require a cycle/real-time hardware model (as CRAM
behavior was once added to the sim), not a finer occupancy metric.

## Practical rule (until a real-time model exists)

- Keep `RING_CAP_KB` a safe margin below 416 KB (>= ~60 KB; 350 used).
- Provision the ring for the cold cap: higher `PACK_MAXCOLD` needs higher
  `RING_CAP_KB`. Verified point: cap 260 needs RING_CAP >= 350.
- `under=0` from the pack is necessary but NOT sufficient; it does not model
  jitter. Always confirm on hardware.

> Historical note (e9): `CBRSIM_PACK_MAXCOLD` was later removed — the cold cap
> now lives in the encoder (`CBRSIM_MAX_COLD`), with the realized ceiling
> asserted at pack time via `tools/av_config.py` (`COLD_CAP_REALIZED`). The
> pack's default `RING_CAP_KB` is now derived there too (420 KB ring − 40 KB
> jitter margin = 380 KB).

## Scripts

- `ring_analyze.py` — occ trajectory, frames-below-threshold, longest low run,
  per-cap sweep at a given `RCAP` (env `RCAP`).
- `ring_jitter.py` — jitter-stall survival margins per config (shows they do not
  discriminate safe from collapse — the negative result above).

Run with the GPU venv python and `machi_ed` decisions:
`~/.config/cbrsim-gpu/venv/bin/python harness/ring-collapse/ring_analyze.py`
(expects `tmp/machi_ed_dec/decisions.pkl`).
