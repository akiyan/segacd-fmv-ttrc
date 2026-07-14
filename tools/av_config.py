"""Single source of truth for the streaming geometry shared by the whole pipeline.

The encoder (``tools/sim.py``), the packer (``tools/pack_stream.py``) and the
on-disc player (``boot/movieplay_sp.s``) all describe the *same* PRG-RAM ring.
Historically each side had its own knob:

* player  ``.equ RING_SIZE``       = 420 KB   (the physical buffer)
* pack    ``CBRSIM_RING_CAP_KB``    = 380 KB   (schedule / prebuffer cap)
* sim     ``CBRSIM_TANK_KB``        = 440 KB   (VBV tank — *larger than the ring!*)

Three independent values for one buffer is a double-management trap: the sim can
model more buffer than the hardware has, so a schedule it calls feasible
underruns live, and the analysis (rendered from the sim) stops matching the disc.

Here we define the ring **once** and derive everything from it, so sim, pack and
player cannot drift apart. The player's ``RING_SIZE`` is asserted equal to
``RING_SIZE_KB`` at build time (``tools/check_player_ring.py``, run by the
Makefile). The per-source *cold cap* is deliberately NOT here: it belongs to the
encoder alone (``CBRSIM_MAX_COLD`` in the sim). The packer refuses to re-cap.
"""

# Physical PRG-RAM ring in the player. MUST equal boot/movieplay_sp.s
# `.equ RING_SIZE` (0x69000 = 420 KB). Build-time assertion enforces it.
RING_SIZE_KB = 420

# The player throttles its CD pump at RING_SIZE-4KB (back-pressure); real
# CD-delivery jitter shrinks the usable ring further. The pack schedules within,
# and the sim's VBV tank models, the SAME jitter-adjusted usable ring, so all
# three agree by construction. 40 KB keeps the scheduled peak (~RING_CAP-32)
# comfortably below the 416 KB back-pressure threshold (measured: no ratchet).
RING_JITTER_MARGIN_KB = 40

# Derived — do not set these independently anywhere else.
RING_CAP_KB = RING_SIZE_KB - RING_JITTER_MARGIN_KB   # 380: pack schedule cap
TANK_KB = RING_CAP_KB                                # sim VBV tank == usable ring

# The player's pump_poll back-pressure threshold (RING_SIZE - 4KB). RING_CAP must
# stay below this or the pump stalls (back-pressure ratchet -> CDC drops).
BACKPRESSURE_KB = RING_SIZE_KB - 4

assert RING_CAP_KB <= BACKPRESSURE_KB, (
    f"RING_CAP_KB={RING_CAP_KB} must be <= back-pressure {BACKPRESSURE_KB}")

# --- CRAM pre-load table (PALTAB) capacity ---
# All segment palettes ship once in a PALTAB region right after the header and
# are copied at boot into a Main-RAM table (player `.equ PALTAB_MAX_SEG` in
# boot/movieplay_ip.s, asserted equal at build time by
# tools/check_player_ring.py). The per-frame stream then carries only a 1-byte
# segment reference (pal = seg+1, 0 = no switch) instead of a 128-byte CRAM
# payload, so palettes are independent of stream timing (slip/recovery safe)
# and the switch frame's budget is freed.
# Capacity = Main-RAM table size = PALTAB_MAX_SEG * 128 bytes (64 -> 8 KB at
# PALTAB_RAM 0xFFB000..0xFFD000). Raising it needs only this constant and the
# player equ (build-checked), within these hard limits:
#   * Word-RAM staging (O_PALTAB 0xB000..0x10000 = 20 KB) -> 160 segments max
#   * pal byte = seg+1 in one byte                        -> 255 segments max
PALTAB_MAX_SEG = 64

assert PALTAB_MAX_SEG <= 160, "PALTAB staging (Word-RAM O_PALTAB) holds 160 segments max"
assert PALTAB_MAX_SEG <= 255, "pal byte = seg+1 addresses at most 255 segments"

# --- Realized cold == cap, by construction ---
# The sim (tools/sim.py) and the pack (tools/pack_stream.py) now share ONE tile-slot
# allocator (tools/tile_alloc.py, two-pass contiguous). So the pack's REALIZED per-frame
# cold EQUALS the sim's cap — the historical +overhead (the sim modelled LRU residency
# while the pack allocated contiguously and re-loaded a few tiles) is gone: the two-pass
# protects every reuse tile shown this frame before allocating cold slots, so nothing is
# re-loaded. There is therefore no separate realized ceiling and no per-source
# `CBRSIM_COLD_CAP_REALIZED` env override — the ceiling IS the cap. The pack still asserts
# realized <= cap as a guard (it must hold by construction). frame0 (the full-load header)
# is exempt.

# --- Per-frame cold cap as a PHYSICAL DRAW LIMIT (scales with fps) ---
# This project ships to real hardware, so the sim MUST model what the player can
# actually DRAW per frame, not just what the CD/tank can deliver. The player renders
# at most a fixed number of fresh (cold) 8x8 tiles per VBLANK (raw-share DMA + buffer
# drain). The ONE confirmed data point is 15fps: cap 350 plays clean on hardware with
# the p5 player (realized ~362, S=0). 15fps content spans 4 VBLANks/frame, and the cap
# scales inversely with fps (higher fps = fewer VBLANks/frame = fewer drawable cold):
#   cap(fps) = COLD_CAP_15FPS x 15 / fps   ->   15->350, 30->175, 24->219.
# Uncapped is no longer allowed — an uncapped sim shows impossible bursts (Sonic H32
# 30fps wanted 600-738 cold on the opening frames, far above what the hardware draws)
# that would collapse live.
#
# COLD_CAP_15FPS is the single knob. The pipeline-speedup work (issue #15: MOVEM block
# copies, padding, table lookups) will raise it; everything below derives from it so
# the whole pipeline (per-fps caps, the drop-safe realized ceiling) tracks the change.
COLD_CAP_15FPS = 350        # confirmed drop-safe at 15fps (p5 player). Raised by issue #15.
_CAP_REF_FPS = 15


def cold_cap_for_fps(fps):
    """Per-frame cold cap, scaled inversely with fps from the confirmed 15fps value.
    frame0 is exempt (loaded from the header at boot, no VBLANK limit)."""
    return int(round(COLD_CAP_15FPS * _CAP_REF_FPS / float(fps)))


def cold_realized_ceiling_for_fps(fps):
    """Pack-time realized-cold ceiling. Now == the cap: the shared two-pass allocator
    makes the pack's realized cold equal the sim's cap exactly, so the ceiling is the
    cap itself (the assert `realized <= ceiling` holds by construction)."""
    return cold_cap_for_fps(fps)
