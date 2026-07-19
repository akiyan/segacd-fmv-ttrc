"""Single source of truth for the streaming geometry shared by the whole pipeline.

The encoder (``tools/sim.py``), the packer (``tools/pack_stream.py``) and the
on-disc player (``boot/movieplay_sp.s``) share one safe payload-buffer capacity.
Their objects are not identical: sim has a virtual VBV budget, while pack and
player schedule and hold physical payload-RING sectors. Historically each side
had its own capacity knob:

* player  ``.equ RING_SIZE``       = 428 KB   (the physical buffer)
* pack    ``CBRSIM_RING_CAP_KB``    = 388 KB   (schedule / prebuffer cap)
* sim     ``CBRSIM_TANK_KB``        = 440 KB   (VBV tank — *larger than the ring!*)

Three independent capacity values are a double-management trap: the sim can
borrow more virtual budget than the hardware can schedule, causing live
underruns even when the encode looked feasible.

Here we define the physical ring **once** and derive both safe capacity ceilings
from it. The player's ``RING_SIZE`` is asserted equal to
``RING_SIZE_KB`` at build time (``tools/check_player_ring.py``, run by the
Makefile). The per-source *cold cap* is deliberately NOT here: it belongs to the
encoder alone (``CBRSIM_MAX_COLD`` in the sim). The packer refuses to re-cap.
"""

import math

# Physical PRG-RAM ring in the player. MUST equal boot/movieplay_sp.s
# `.equ RING_SIZE` (0x6B000 = 428 KB). Build-time assertion enforces it.
# Routing now lives in both Word-RAM banks, so the payload ring can occupy the
# complete safe PRG range from 0x0C000 up to APPLY_BASE at 0x77000.
RING_SIZE_KB = 428

# The player throttles its CD pump at RING_SIZE-4KB (back-pressure); real
# CD-delivery jitter shrinks the usable ring further. The pack schedules within
# this cap, and the sim may borrow no more virtual VBV budget than the same cap.
# Their occupancies remain separate. 40 KB keeps the scheduled peak
# comfortably below the 424 KB back-pressure threshold.
RING_JITTER_MARGIN_KB = 40

# Derived — do not set these independently anywhere else.
RING_CAP_KB = RING_SIZE_KB - RING_JITTER_MARGIN_KB   # 388: pack schedule cap
TANK_KB = RING_CAP_KB                                # virtual VBV capacity ceiling

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

# --- Content timing shared by sim and pack ---
# The player is ultimately synchronized by the CD-1x rate-matched BODY stream.
# Content close to an integer NTSC VBlank divisor (15/30/60 fps) settles on
# that exact display cadence. Other rates such as 24 fps remain delivery-paced
# at their nominal rate and naturally alternate the number of VBlanks between
# frames. Keep this decision in one place so the sim's audio budget and the
# packer's fixed PCM chunk cannot disagree.
NTSC_VSYNC = 60_000 / 1001
_INTEGER_VBLANK_TOLERANCE = 0.01

# RF5C164 phase-step conversion.  One output sample advances the 11-bit
# frequency delta once per 384 clocks of the 12.5 MHz PCM clock.
RF5C164_CLOCK_HZ = 12_500_000
RF5C164_DIVIDER = 384
RF5C164_FD_SCALE = 0x800


def vsync_n_for_fps(fps):
    """Nearest integer VBlank interval used as the player's cadence hint."""
    value = float(fps)
    if value <= 0:
        raise ValueError(f"fps must be positive, got {fps!r}")
    return max(1, int(round(NTSC_VSYNC / value)))


def playback_fps_for_content(fps):
    """Effective long-term playback rate for audio chunk sizing.

    Integer-VBlank rates use the exact NTSC-derived cadence. Delivery-paced
    rates such as 24 fps use the content rate; they are not rounded to 29.97.
    """
    value = float(fps)
    n = vsync_n_for_fps(value)
    ratio = NTSC_VSYNC / value
    if abs(ratio - n) <= _INTEGER_VBLANK_TOLERANCE:
        return NTSC_VSYNC / n
    return value


def rf5c164_fd(samples_per_frame, playback_fps):
    """Return the RF5C164 frequency delta matching one fixed audio chunk.

    Matching the player's actual fixed chunk rate matters more than matching
    the nominal source rate after the packer has evenly retimed the source.
    Otherwise the wave-RAM lead slowly walks into a re-sync threshold.
    """
    rate = int(samples_per_frame) * float(playback_fps)
    if rate <= 0:
        raise ValueError(
            f"audio chunk rate must be positive, got {samples_per_frame!r} * "
            f"{playback_fps!r}")
    fd = round(rate / (RF5C164_CLOCK_HZ / RF5C164_DIVIDER) * RF5C164_FD_SCALE)
    if not 0 < fd <= 0xFFFF:
        raise ValueError(f"RF5C164 frequency delta is out of range: {fd}")
    return fd


def uses_fixed_n2_cadence(fps):
    """Whether this source uses the exact two-VBlank NTSC display cadence.

    The packer serializes this decision as ``FEATURE_FIXED_N2``. That header
    bit, not the nominal fps or nearest-N hint, is authoritative to the player.
    This deliberately excludes 24 fps even though its nearest cadence hint is
    also N=2.  Delivery-paced 24 fps must retain its alternating two/three
    VBlank behavior; only rates already classified as NTSC N=2 are fixed.
    """
    value = float(fps)
    n = vsync_n_for_fps(value)
    return (
        n == 2
        and abs((NTSC_VSYNC / value) - n) <= _INTEGER_VBLANK_TOLERANCE
    )


def cd_sector_rate(fps):
    """Return the integer accumulator numerator/modulus for CD-1x sectors.

    A fixed N=2 frame lasts exactly two 60000/1001 Hz VBlanks, so CD-1x
    supplies 1001/400 sectors per frame.  Other rates retain the legacy
    delivery-paced ``75 / round(fps)`` schedule.
    """
    value = float(fps)
    if value <= 0:
        raise ValueError(f"fps must be positive, got {fps!r}")
    if uses_fixed_n2_cadence(value):
        return 1001, 400
    nominal = int(round(value))
    if nominal <= 0:
        raise ValueError(f"fps must round to a positive integer, got {fps!r}")
    return 75, nominal


def pcm_frame_bytes(fps, audio_rate=13_300):
    """Fixed mono u8 PCM bytes per frame, rounded up to avoid underrun."""
    return int(math.ceil(int(audio_rate) / playback_fps_for_content(fps)))


IMA_CHECKPOINT_BYTES = 4


def adpcm_frame_samples(fps, audio_rate=22_050):
    """Fixed decoded samples per IMA chunk, rounded up to an even count."""
    count = pcm_frame_bytes(fps, audio_rate)
    return count + (count & 1)


def audio_frame_layout(kind, fps):
    """Return ``(rate, decoded_samples, control_bytes)`` for one audio chunk."""
    name = str(kind).strip().lower()
    if name == "pcm13":
        samples = pcm_frame_bytes(fps, 13_300)
        return 13_300, samples, samples
    if name == "adpcm22":
        samples = adpcm_frame_samples(fps, 22_050)
        return 22_050, samples, IMA_CHECKPOINT_BYTES + samples // 2
    raise ValueError(f"unsupported audio kind: {kind!r}")

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
# drain). The confirmed common 15fps point is 350, scaled inversely with fps:
#   15->350, 24->219, 30->175
# H40 keeps cadence-specific limits explicit. At 15fps, servicing the CDC
# during the long ADPCM decode makes 400 qualified for Machi OP's 720 active
# tiles. Machi ED's 1,040-active-tile raster uses the separately measured
# candidate below. Any other active-tile count keeps the common 350 limit.
# At exactly 24fps, Lunar repeated S=2 at 219 and stayed at S=0 at 200. Unlike
# 30fps's steady two VBLANKs per frame, 24fps alternates between two and three
# VBLANKs, so keep both H40 limits explicit instead of extrapolating them.
# Uncapped is no longer allowed — an uncapped sim shows impossible bursts (Sonic H32
# 30fps wanted 600-738 cold on the opening frames, far above what the hardware draws)
# that would collapse live.
#
# Keep the measured exception explicit instead of deriving unmeasured H40 rates
# from it. MODE4 retains the common reference until it has its own measurement.
COLD_CAP_15FPS = 350
H40_15FPS_COLD_CAP = 400
H40_15FPS_QUALIFIED_ACTIVE_TILES = 720
H40_15FPS_1040_ACTIVE_COLD_CAP = 375
H40_24FPS_COLD_CAP = 200
_CAP_REF_FPS = 15
_COLD_CAP_MODES = {"H32", "H40", "MODE4"}


def cold_cap_for_fps(fps, mode, active_tiles):
    """Per-frame cold cap from cadence, mode, and active picture tiles.

    Frame 0 is exempt because the header loads it before timed playback.
    """
    mode_key = str(mode).upper()
    if mode_key not in _COLD_CAP_MODES:
        raise ValueError(f"unsupported display mode for cold cap: {mode!r}")
    fps_value = float(fps)
    active_tiles_value = int(active_tiles)
    if active_tiles_value <= 0:
        raise ValueError(f"active tile count must be positive: {active_tiles!r}")
    if (mode_key == "H40" and fps_value == 15.0
            and active_tiles_value == H40_15FPS_QUALIFIED_ACTIVE_TILES):
        return H40_15FPS_COLD_CAP
    if mode_key == "H40" and fps_value == 15.0 and active_tiles_value == 1040:
        return H40_15FPS_1040_ACTIVE_COLD_CAP
    if mode_key == "H40" and fps_value == 24.0:
        return H40_24FPS_COLD_CAP
    return int(round(COLD_CAP_15FPS * _CAP_REF_FPS / fps_value))


def cold_realized_ceiling_for_fps(fps, mode, active_tiles):
    """Pack-time realized-cold ceiling. Now == the cap: the shared two-pass allocator
    makes the pack's realized cold equal the sim's cap exactly, so the ceiling is the
    cap itself (the assert `realized <= ceiling` holds by construction)."""
    return cold_cap_for_fps(fps, mode, active_tiles)
