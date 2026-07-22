"""Single source of truth for the streaming geometry shared by the whole pipeline.

The encoder (``tools/sim.py``), the packer (``tools/pack_stream.py``) and the
on-disc player (``boot/movieplay_sp.s``) share one safe PrgBuf capacity.
Their objects are not identical: sim has a virtual quality budget, while pack
and player schedule and hold physical PrgBuf sectors. Historically each side
had its own capacity knob:

* player  ``.equ RING_SIZE``       = 428 KB   (the physical buffer)
* pack    ``CBRSIM_RING_CAP_KB``    = 404 KB   (legacy internal scheduler name)
* sim     quality budget             = 440 KB   (*larger than PrgBuf!*)

Three independent capacity values are a double-management trap: the sim can
borrow more virtual budget than the hardware can schedule, causing live
underruns even when the encode looked feasible.

Here we define the physical ring **once** and derive both safe capacity ceilings
from it. The player's ``RING_SIZE`` is asserted equal to
``RING_SIZE_KB`` at build time (``tools/check_player_ring.py``, run by the
Makefile). This module also owns the measured cold-cap qualification table used
by the encoder, packer, profile validator, and analysis renderer. The packer
refuses to re-cap an already encoded stream.
"""

import math
from dataclasses import dataclass

# Physical PRG-RAM ring in the player. MUST equal boot/movieplay_sp.s
# `.equ RING_SIZE` (0x6B000 = 428 KB). Build-time assertion enforces it.
# Routing now lives in both Word-RAM banks, so the physical PrgBuf ring can occupy the
# complete safe PRG range from 0x0C000 up to APPLY_BASE at 0x77000.
RING_SIZE_KB = 428

# Keep the physical overflow guard distinct from delivery-jitter headroom. The
# player throttles its CD pump at RING_SIZE-4KB (back-pressure), and the pack
# schedules another 20KB below that threshold. The sim may borrow no more
# quality budget than the same cap; its occupancy remains separate.
RING_PHYSICAL_GUARD_KB = 4
RING_JITTER_HEADROOM_KB = 20

# Frame 0 is staged only during boot and may span the jitter tail plus the
# otherwise-unused APPLY ring. It is not part of the timed PrgBuf occupancy.
FRAME0_PATTERN_STAGING_KB = 36

# Derived — do not set these independently anywhere else.
BACKPRESSURE_KB = RING_SIZE_KB - RING_PHYSICAL_GUARD_KB
RING_CAP_KB = BACKPRESSURE_KB - RING_JITTER_HEADROOM_KB  # internal scheduler name
PRG_BUF_CAP_KB = RING_CAP_KB                         # public physical-buffer name
QUALITY_BUDGET_KB = PRG_BUF_CAP_KB                   # virtual quality ceiling

assert BACKPRESSURE_KB - RING_CAP_KB == RING_JITTER_HEADROOM_KB

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
# SEGA-CD 1x is the codec's physical delivery source.  The encoder's fresh
# per-frame quality allowance is derived from these constants; it is not a
# profile bitrate setting.
CD_SECTOR_BYTES = 2048
CD_SECTORS_PER_SECOND = 75
CD_BYTES_PER_SECOND = CD_SECTOR_BYTES * CD_SECTORS_PER_SECOND

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

# --- Per-frame cold cap as a qualified physical draw/delivery limit ---
# A qualification applies only to the exact display mode, nominal fps, and
# active-tile count that was measured.  Never extrapolate a measurement to
# another active area or cadence.  A missing tuple is a measurement task, not
# a reason to silently fall back to a scaled/default cap.
_COLD_CAP_MODES = {"H32", "H40", "MODE4"}


@dataclass(frozen=True)
class ColdCapQualification:
    mode: str
    fps: float
    active_tiles: int
    cap: int


class ColdCapMeasurementRequired(ValueError):
    """No measured cold-cap tuple exactly matches the playback geometry."""


# Full-length pipeline qualifications.  Keep this ordered data as the single
# source of truth; sim, pack, profile validation, and analysis all call the
# selector below.
COLD_CAP_QUALIFICATIONS = (
    ColdCapQualification("H32", 24.0, 896, 219),
    ColdCapQualification("H32", 30.0, 896, 175),
    ColdCapQualification("H40", 15.0, 720, 400),
    ColdCapQualification("H40", 15.0, 1040, 400),
    ColdCapQualification("H40", 24.0, 1120, 200),
    ColdCapQualification("H40", 30.0, 1120, 175),
)


def cold_cap_qualification(fps, mode, active_tiles):
    """Return the measured tuple that exactly matches the request.

    Results are not reused across active-tile counts, display modes, or nominal
    frame rates even when another measurement looks more conservative.
    """
    mode_key = str(mode).upper()
    if mode_key not in _COLD_CAP_MODES:
        raise ValueError(f"unsupported display mode for cold cap: {mode!r}")
    fps_value = float(fps)
    if fps_value <= 0:
        raise ValueError(f"fps must be positive, got {fps!r}")
    active_tiles_value = int(active_tiles)
    if active_tiles_value <= 0:
        raise ValueError(f"active tile count must be positive: {active_tiles!r}")

    same_rate = [
        item for item in COLD_CAP_QUALIFICATIONS
        if item.mode == mode_key and math.isclose(
            item.fps, fps_value, rel_tol=0.0, abs_tol=1e-9)
    ]
    exact = [
        item for item in same_rate
        if item.active_tiles == active_tiles_value
    ]
    if exact:
        return exact[0]

    coverage = (
        ", ".join(
            f"{item.active_tiles} tiles -> cap {item.cap}"
            for item in sorted(same_rate, key=lambda item: item.active_tiles)
        )
        or "none"
    )
    raise ColdCapMeasurementRequired(
        "cold-cap measurement required for "
        f"mode={mode_key} fps={fps_value:g} active_tiles={active_tiles_value}; "
        f"measured tuples at this mode/fps: {coverage}")


def cold_cap_for_fps(fps, mode, active_tiles):
    """Per-frame cold cap from an exact measured mode/fps/active-tile tuple.

    Frame 0 is exempt because the header loads it before timed playback.
    """
    return cold_cap_qualification(fps, mode, active_tiles).cap


def cold_realized_ceiling_for_fps(fps, mode, active_tiles):
    """Pack-time realized-cold ceiling. Now == the cap: the shared two-pass allocator
    makes the pack's realized cold equal the sim's cap exactly, so the ceiling is the
    cap itself (the assert `realized <= ceiling` holds by construction)."""
    return cold_cap_for_fps(fps, mode, active_tiles)
