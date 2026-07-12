#!/usr/bin/env python3
"""Build-time guard: the player's `.equ RING_SIZE` must equal av_config.RING_SIZE_KB.

Run by the Makefile before assembling boot/movieplay_sp.s. Fails the build if the
player's physical ring and the pipeline's single-source-of-truth ring drift apart
(which would let the sim/pack schedule for a ring the hardware does not have).
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import av_config

SP = Path(__file__).resolve().parent.parent / "boot" / "movieplay_sp.s"
text = SP.read_text()
m = re.search(r"^\.equ\s+RING_SIZE,\s*(0x[0-9A-Fa-f]+|\d+)", text, re.M)
if not m:
    sys.exit(f"check_player_ring: could not find `.equ RING_SIZE` in {SP}")
ring_bytes = int(m.group(1), 0)
want_bytes = av_config.RING_SIZE_KB * 1024
if ring_bytes != want_bytes:
    sys.exit(
        f"check_player_ring: player RING_SIZE={ring_bytes} (0x{ring_bytes:X}) "
        f"!= av_config.RING_SIZE_KB={av_config.RING_SIZE_KB} "
        f"({want_bytes} / 0x{want_bytes:X}). Update one so they agree "
        f"(single source of truth = tools/av_config.py).")
print(f"check_player_ring: OK  RING_SIZE={ring_bytes//1024}KB "
      f"== av_config.RING_SIZE_KB (cap {av_config.RING_CAP_KB}KB, tank {av_config.TANK_KB}KB)")
