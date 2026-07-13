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

# --- Clean-playback realized per-frame cold ceiling ---
# The pack ASSERTS each streaming frame's ACTUAL new-tile loads stay <= this, so a
# too-high sim cap fails loudly here instead of silently shipping a disc that glitches.
# Subtlety: the encoder (sim) models residency with LRU, but the packer allocates slots
# contiguously (for efficient DMA runs), so the pack RE-LOADS a few tiles the sim kept
# resident -> the pack's realized cold runs a few above the sim's model cap. So the sim
# cap (CBRSIM_MAX_COLD) must sit below this ceiling. frame0 (the full-load header) is
# exempt. Same "single source of truth + pack-time verification" pattern as the ring.
#
# This ceiling ROSE with the pump-optimized player (p5). The heavy scene-cut montage is
# Sub-CPU bound: the OLD player called pump_poll every bitmap byte (~140x/frame, each a
# 15-reg save + BIOS_CDC_STAT), and that fixed overhead made heavy frames overrun the CD
# (a sector slip) above realized ~200. Calling pump_poll every 8 bytes (~18x/frame, still
# ample: the CD delivers 1 sector per ~166k cycles) freed the Sub, and measured on the
# full-screen (H40 320x224, 1120-cell) machi_ed the slip disappeared: realized 219/258/303
# /362/429 all play S=0. The next limit is AUDIO, not slips: at realized 429 the audio
# lead dips below SYNC_MIN a couple times (R 1->3); at realized 362 it stays clean (R=1).
# Quality (mean cold) saturates ~realized 362 anyway (429 adds +0.5). So the clean ceiling
# with the p5 player is ~380 (shipped machi_ed = sim cap 350 -> realized 362, S=0 R=1).
#
# PER-SOURCE: lighter sources tolerate more — H40-letterbox machi_op (320x144, 720-cell)
# plays S=0 fully *uncapped* (realized cold up to 714). The pack accepts
# `CBRSIM_COLD_CAP_REALIZED` to raise this per source (machi_op ships uncapped).
COLD_CAP_REALIZED = 380
