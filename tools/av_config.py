"""Single source of truth for the streaming geometry shared by the whole pipeline.

The encoder (``tools/sim.py``), the packer (``tools/pack_stream.py``) and the
on-disc player (``boot/movieplay_sp.s``) share one physical PrgBuf geometry.
Their objects are not identical: sim has a virtual quality budget, the packer
has an fps-derived normal prebuffer ceiling plus a physical delivery ceiling,
and the player holds the real sectors. Historically each side had its own
capacity knob:

* player  ``.equ RING_SIZE``       = 428 KB   (the physical buffer)
* pack    normal prebuffer ceiling  = fps-derived
* sim     quality budget             = 440 KB   (*larger than PrgBuf!*)

Three independent capacity values are a double-management trap: the sim can
borrow more virtual budget than the hardware can schedule, causing live
underruns even when the encode looked feasible.

Here we define the physical ring **once** and derive every capacity from it.
Delivery jitter scales with the time represented by one content frame: 20 KiB
at 30 fps, 40 KiB at 15 fps, and 25 KiB at 24 fps. The
player's ``RING_SIZE`` is asserted equal to
``RING_SIZE_KB`` at build time (``tools/check_player_ring.py``, run by the
Makefile). This module also owns the fps-derived cold-cap baseline used by the
encoder, packer, profile validator, and analysis renderer. The packer refuses
to re-cap an already encoded stream.
"""

import math
import os
import sys
from dataclasses import dataclass

# Physical PRG-RAM ring in the player. MUST equal boot/movieplay_sp.s
# `.equ RING_SIZE` (0x6B000 = 428 KB). Build-time assertion enforces it.
# Routing now lives in both Word-RAM banks, so the physical PrgBuf ring can occupy the
# complete safe PRG range from 0x0C000 up to APPLY_BASE at 0x77000.
RING_SIZE_KB = 428

# Keep the physical overflow guard distinct from delivery-jitter headroom. The
# player throttles its CD pump at RING_SIZE-4KB (back-pressure). The normal
# PrgBuf/prebuffer ceiling stays an fps-derived jitter interval below that
# threshold, while the exact physical schedule may use the reserved interval.
RING_PHYSICAL_GUARD_KB = 4
RING_JITTER_REFERENCE_FPS = 30.0
RING_JITTER_REFERENCE_KB = 20

# Frame 0 is staged only during boot and may span the jitter tail plus the
# otherwise-unused APPLY ring. It is not part of the timed PrgBuf occupancy.
FRAME0_PATTERN_STAGING_KB = 36

# Derived fixed physical limits — do not set these independently anywhere else.
BACKPRESSURE_KB = RING_SIZE_KB - RING_PHYSICAL_GUARD_KB


def _nominal_content_fps(fps):
    """Normalize NTSC-like profile rates to their named content cadence."""
    value = float(fps)
    if value <= 0:
        raise ValueError(f"fps must be positive, got {fps!r}")
    nearest = round(value)
    if nearest > 0 and math.isclose(
            value, nearest, rel_tol=0.0, abs_tol=0.1):
        return float(nearest)
    return value


def ring_jitter_headroom_kb(fps):
    """Return fps-scaled jitter reserve rounded up to a whole KiB."""
    nominal_fps = _nominal_content_fps(fps)
    nominal_kb = (
        RING_JITTER_REFERENCE_KB
        * RING_JITTER_REFERENCE_FPS
        / nominal_fps
    )
    return int(math.ceil(nominal_kb))


def prg_buf_cap_kb(fps):
    """Return the normal PrgBuf/prebuffer ceiling below physical jitter."""
    cap = BACKPRESSURE_KB - ring_jitter_headroom_kb(fps)
    if cap <= 0:
        raise ValueError(
            f"fps {fps!r} requires all {BACKPRESSURE_KB} KiB of PrgBuf "
            "for delivery jitter")
    return cap


def quality_budget_kb(fps):
    """Keep offline time-shifting within the normal fps-specific PrgBuf."""
    return prg_buf_cap_kb(fps)


def physical_delivery_cap_kb(_fps):
    """Return the hard scheduled occupancy limit before pump back-pressure."""
    return BACKPRESSURE_KB


# Compatibility aliases are the 30 fps reference values. Runtime encode, pack,
# player constants, analysis, and HUD gates must call the fps-aware functions.
RING_JITTER_HEADROOM_KB = ring_jitter_headroom_kb(30)
RING_CAP_KB = prg_buf_cap_kb(30)
PRG_BUF_CAP_KB = RING_CAP_KB
QUALITY_BUDGET_KB = quality_budget_kb(30)

assert BACKPRESSURE_KB - RING_CAP_KB == RING_JITTER_HEADROOM_KB

# --- Fixed encoder/player resources ---
# The resident movie-pattern pool starts at tile 1 and ends immediately before
# the first movie name table at VRAM 0xC000.  The HUD font lives in the gap at
# 0xD000, so DEBUG and release builds share the same full pool.
VRAM_PATTERN_BASE_TILE = 1
VRAM_FIRST_MOVIE_NT_TILE = 0xC000 // 32
VRAM_HUD_FONT_TILE = 0xD000 // 32
VRAM_PATTERN_POOL_TILES = (
    VRAM_FIRST_MOVIE_NT_TILE - VRAM_PATTERN_BASE_TILE)

# These are pipeline policy, not per-source choices.  Forward fill uses safe
# physical-slot padding for future Prg payload, while startup audio is clamped
# later to the decoded chunk size and wave-RAM capacity.
PACK_FORWARD_FILL = True
STARTUP_AUDIO_PREFETCH_FRAMES = 30

# Palette algorithm parameters are fixed across sources.  Only the algorithm
# name remains a TOML choice.
PALETTE_MAP_WEIGHT = 1.0
PALETTE_SEAM_WEIGHT = 8.0
PALETTE_SEAM_ITERATIONS = 2
PALETTE_SAMPLE_COUNTS = (120, 240, 480)
PALETTE_VALIDATE_FRAMES = 120
PALETTE_SEGMENT_TRAIN_FRAMES = 240
PALETTE_SEGMENT_VALIDATE_FRAMES = 60
PALETTE_SEGMENT_GAIN_RELATIVE = 0.005
PALETTE_SEGMENT_GAIN_PER_PIXEL = 0.002

# --- v13 boot stage / CRAM pre-load table (PALTAB) capacity ---
# A fixed 24 KiB boot stage ships right after the header.  All segment palettes
# occupy its +0x1000..+0x3000 middle region and are copied at boot into a
# Main-RAM table (player `.equ PALTAB_MAX_SEG` in
# boot/movieplay_ip.s, asserted equal at build time by
# tools/check_player_ring.py). The per-frame stream then carries only a 1-byte
# segment reference (pal = seg+1, 0 = no switch) instead of a 128-byte CRAM
# payload, so palettes are independent of stream timing (slip/recovery safe)
# and the switch frame's budget is freed.
# Capacity = Main-RAM table size = PALTAB_MAX_SEG * 128 bytes (64 -> 8 KB at
# PALTAB_RAM 0xFFB000..0xFFD000). Keep this constant and the player equ equal
# (build-checked). Raising it beyond 64 also requires a new boot-stage layout:
#   * palette part of the Word-RAM boot stage             -> 64 segments max
#   * pal byte = seg+1 in one byte                        -> 255 segments max
PALTAB_MAX_SEG = 64
PALTAB_STAGE_KB = 24
BOOT_VRAM_SIDECAR_ENTRY_BYTES = 34  # slot.u16 + packed 32-byte pattern
BOOT_VRAM_REGION_A_BYTES = 0x0F00   # bank +0xA000..+0xAF00
BOOT_VRAM_REGION_B_BYTES = 0x2000   # palette tail through bank +0xD000
BOOT_VRAM_REGION_C_BYTES = 0x1000   # bank +0xF000..+0x10000


def boot_vram_sidecar_capacity(palette_segments):
    """Records preserved around frame-0 diagnostics, O_HDR, and Dic staging."""
    palette_bytes = int(palette_segments) * 128
    if not 0 <= palette_bytes <= BOOT_VRAM_REGION_B_BYTES:
        raise ValueError("palette table exceeds the boot-sidecar middle region")
    entry = BOOT_VRAM_SIDECAR_ENTRY_BYTES
    return (
        BOOT_VRAM_REGION_A_BYTES // entry
        + (BOOT_VRAM_REGION_B_BYTES - palette_bytes) // entry
        + BOOT_VRAM_REGION_C_BYTES // entry
    )

assert PALTAB_MAX_SEG * 128 <= BOOT_VRAM_REGION_B_BYTES, (
    "PALTAB exceeds the middle region of the v13 Word-RAM boot stage")
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


def audio_frame_samples(fps, audio_rate):
    """Fixed mono samples per frame, rounded up to avoid underrun."""
    return int(math.ceil(int(audio_rate) / playback_fps_for_content(fps)))


IMA_CHECKPOINT_BYTES = 4


def adpcm_frame_samples(fps, audio_rate=22_050):
    """Fixed decoded samples per IMA chunk, rounded up to an even count."""
    count = audio_frame_samples(fps, audio_rate)
    return count + (count & 1)


def audio_frame_layout(fps):
    """Return the ADPCM ``(rate, decoded_samples, control_bytes)`` layout."""
    samples = adpcm_frame_samples(fps, 22_050)
    return 22_050, samples, IMA_CHECKPOINT_BYTES + samples // 2

# --- Realized cold matches the sim and never exceeds the cap ---
# The sim (tools/sim.py) and the pack (tools/pack_stream.py) now share ONE tile-slot
# allocator (tools/tile_alloc.py, two-pass contiguous). So the pack's realized
# per-frame cold equals the sim's selected cold, not necessarily the cap itself.
# The historical +overhead (the sim modelled LRU residency while the pack
# allocated contiguously and re-loaded a few tiles) is gone: the two-pass
# protects every reuse tile shown this frame before allocating cold slots, so
# nothing is re-loaded. There is therefore no separate realized ceiling and no
# per-source `CBRSIM_COLD_CAP_REALIZED` env override. The pack still asserts
# realized <= cap as a guard. frame0 (the full-load header) is exempt.

# --- Per-frame cold cap as an fps-derived physical draw/delivery limit ---
# The common baseline is 360 patterns at 15 fps and scales with the duration of
# one content frame: round(360 * 15 / fps) == round(5400 / fps). Display mode
# and active-tile count do not participate in this limit.
COLD_CAP_PATTERNS_PER_SECOND = 5400


@dataclass(frozen=True)
class ColdCapQualification:
    fps: float
    cap: int
    baseline_cap: int | None = None
    source: str = "baseline"


def _normalize_cold_cap_fps(fps):
    """Validate and normalize one cold-cap frame rate."""
    fps_value = float(fps)
    if fps_value <= 0:
        raise ValueError(f"fps must be positive, got {fps!r}")
    return fps_value


def baseline_cold_cap_qualification(fps):
    """Return the fps-derived baseline without a profile override."""
    fps_value = _normalize_cold_cap_fps(fps)
    cap = max(1, round(COLD_CAP_PATTERNS_PER_SECOND / fps_value))
    return ColdCapQualification(
        fps_value, cap, baseline_cap=cap, source="baseline")


def baseline_cold_cap_for_fps(fps):
    """Return only the fps-derived baseline cap."""
    return baseline_cold_cap_qualification(fps).cap


def cold_cap_qualification(fps, *, requested_cap=None):
    """Return the effective cap for one content frame rate.

    ``requested_cap`` is the optional per-profile cap. It may raise the
    baseline after a source-specific qualification, but may never lower it.
    When omitted, ``CBRSIM_COLD_CAP`` is the internal TOML handoff. If neither
    is present, the fps-derived baseline is returned.

    CBRSIM_COLD_CAP_DIAG replaces the baseline with an UNQUALIFIED value
    for cap-raise measurement streams only (harness/cold_cap_model).  It is
    deliberately loud, never a production fallback, and the resulting stream
    must not be published without a full-length hardware qualification of its
    own.
    """
    fps_value = _normalize_cold_cap_fps(fps)

    diag = os.environ.get("CBRSIM_COLD_CAP_DIAG", "").strip()
    if diag:
        diag_cap = int(diag)
        if diag_cap <= 0:
            raise ValueError(f"CBRSIM_COLD_CAP_DIAG must be positive: {diag!r}")
        print(
            f"[cold-cap] DIAGNOSTIC OVERRIDE: cap={diag_cap} for "
            f"fps={fps_value:g} (UNQUALIFIED - measurement stream only)",
            file=sys.stderr)
        return ColdCapQualification(
            fps_value, diag_cap,
            baseline_cap=None, source="diagnostic")

    baseline = baseline_cold_cap_qualification(fps_value)
    if requested_cap is None:
        raw_cap = os.environ.get("CBRSIM_COLD_CAP", "").strip()
    else:
        raw_cap = requested_cap
    if raw_cap in (None, ""):
        return baseline
    try:
        effective_cap = int(raw_cap)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"profile cold cap must be an integer: {raw_cap!r}") from exc
    if effective_cap < baseline.cap:
        raise ValueError(
            f"profile cold cap {effective_cap} is below baseline "
            f"{baseline.cap} for fps={fps_value:g}")
    return ColdCapQualification(
        fps_value, effective_cap,
        baseline_cap=baseline.cap, source="profile")


def cold_cap_for_fps(fps):
    """Per-frame cold cap derived only from content fps.

    Frame 0 is exempt because the header loads it before timed playback.
    """
    return cold_cap_qualification(fps).cap


def cold_realized_ceiling_for_fps(fps):
    """Pack-time realized-cold ceiling. Now == the cap: the shared two-pass allocator
    makes the pack's realized cold equal the sim's cap exactly, so the ceiling is the
    cap itself (the assert `realized <= ceiling` holds by construction)."""
    return cold_cap_for_fps(fps)
