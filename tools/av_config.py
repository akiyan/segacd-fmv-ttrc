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
