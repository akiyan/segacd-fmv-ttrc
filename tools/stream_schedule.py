#!/usr/bin/env python3
"""Pure payload-RING delivery scheduling shared by sim and pack.

The encoder has a virtual VBV reservoir for quality decisions.  The player has
a different, physical object: the PRG-RAM payload RING filled by whole CD
sectors.  This module models the latter from the frozen per-frame cold-pattern
counts and control-block lengths so analysis and disc packing cannot drift.
"""
from __future__ import annotations

import numpy as np

import av_config


SECTOR_BYTES = 2048
CD_SECTORS_PER_SECOND = 75
CD_BYTES_PER_SECOND = SECTOR_BYTES * CD_SECTORS_PER_SECOND
PATTERN_BYTES = 32
PATTERNS_PER_SECTOR = SECTOR_BYTES // PATTERN_BYTES
DEBUG_BLOCK_BYTES = 22
STREAM_SCHEDULE_SCHEMA_VERSION = 2


class ScheduleError(ValueError):
    """The requested stream cannot satisfy the player's delivery rules."""


def body_delivery_rate_bps(useful_bytes, physical_bytes):
    """Return useful BODY bandwidth over each slot's actual CD read time.

    A physical slot containing ``n`` sectors occupies ``n / 75`` seconds on a
    CD-1x drive.  Dividing useful bytes by that duration makes pad visible as
    unused bandwidth and guarantees that a valid slot cannot exceed CD 1x.
    """
    useful = np.asarray(useful_bytes, np.int64)
    physical = np.asarray(physical_bytes, np.int64)
    if useful.shape != physical.shape:
        raise ValueError("useful and physical BODY bytes must have equal shapes")
    if (useful < 0).any() or (physical < 0).any() or (useful > physical).any():
        raise ValueError("useful BODY bytes must be within physical BODY bytes")
    rate = np.zeros(useful.shape, np.int64)
    present = physical > 0
    rate[present] = (
        useful[present] * CD_BYTES_PER_SECOND // physical[present])
    return rate


def average_body_delivery_rate_bps(useful_bytes, physical_bytes):
    """Return useful BODY bandwidth over the complete physical read time."""
    useful = np.asarray(useful_bytes, np.int64)
    physical = np.asarray(physical_bytes, np.int64)
    # Reuse the per-slot validation even though only the totals are needed.
    body_delivery_rate_bps(useful, physical)
    physical_total = int(physical.sum())
    if not physical_total:
        return 0.0
    return float(useful.sum()) * CD_BYTES_PER_SECOND / physical_total


def control_block_lengths(
        updates, runs, *, cells, audio_frame_bytes, debug=False,
        debug_bytes=DEBUG_BLOCK_BYTES):
    """Return the exact packed control length for every frame.

    This mirrors ``pack_stream.build_control`` without constructing audio or
    bitmap bytes.  The packer later checks these frozen lengths against the
    blocks it actually built.
    """
    n_upd = np.asarray(updates, np.int64)
    n_runs = np.asarray(runs, np.int64)
    if n_upd.ndim != 1 or n_runs.ndim != 1 or n_upd.shape != n_runs.shape:
        raise ValueError("updates and runs must be equal-length vectors")
    if (n_upd < 0).any() or (n_runs < 0).any():
        raise ValueError("updates and runs must be non-negative")
    bitmap_bytes = (int(cells) + 7) // 8
    # body prefix: frame_seq, n_upd, pal and dbg = 6 bytes.  Entry words and
    # the run suffix are even-sized; only the pre-suffix body may need a byte.
    pre_suffix = (
        6 + (int(debug_bytes) if debug else 0) + bitmap_bytes
        + n_upd * 2 + int(audio_frame_bytes)
    )
    pre_suffix += pre_suffix & 1
    # total_len word + aligned body + n_runs word + four bytes per run.
    return (2 + pre_suffix + 2 + n_runs * 4).astype(np.int64)


def rate_deltas(frame_count, fps):
    """Return the CD-1x sector allowance for BODY frames 1..N-1."""
    try:
        rate_num, rate_mod = av_config.cd_sector_rate(fps)
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc
    out = np.zeros(int(frame_count), np.int64)
    acc = 0
    for i in range(1, len(out)):
        acc += rate_num
        out[i] = acc // rate_mod
        acc -= int(out[i]) * rate_mod
    return out


def rate_match_sectors(payload_sectors, control_sectors, *, fps):
    """Apply the player's bounded CD-rate accumulator to a routing table."""
    n_pay = np.asarray(payload_sectors, np.int64)
    n_ctrl = np.asarray(control_sectors, np.int64)
    if n_pay.shape != n_ctrl.shape or n_pay.ndim != 1:
        raise ValueError("payload and control sectors must be equal-length vectors")
    ratedelta = rate_deltas(len(n_pay), fps)
    fsec = np.zeros(len(n_pay), np.int64)
    lead_trace = np.zeros(len(n_pay), np.int64)
    lead = 0
    for i in range(1, len(n_pay)):
        actual = int(n_pay[i]) + int(n_ctrl[i])
        due = int(ratedelta[i]) - lead
        fsec[i] = max(actual, due)
        lead += int(fsec[i]) - int(ratedelta[i])
        if lead < 0:
            raise AssertionError("rate-match lead became negative")
        lead_trace[i] = lead
    return fsec, ratedelta, lead_trace


def _allocate_useful_bytes(delivery_sectors, total_useful_bytes, *, name):
    """Assign a continuous stream's real bytes to its physical delivery slots."""
    sectors = np.asarray(delivery_sectors, np.int64)
    useful = np.zeros(len(sectors), np.int64)
    remaining = int(total_useful_bytes)
    for i, count in enumerate(sectors):
        take = min(remaining, int(count) * SECTOR_BYTES)
        useful[i] = take
        remaining -= take
    if remaining:
        raise ScheduleError(
            f"{name} delivery omitted {remaining} useful BODY bytes")
    return useful


def useful_body_delivery_trace(
        payload_sectors, control_sectors, physical_sectors, *,
        body_payload_bytes, body_control_bytes):
    """Return per-slot useful BODY bytes and all physical padding.

    Control and payload are independent continuous streams.  A final sector can
    therefore contain alignment zeros even when another stream still has real
    data.  ``pad_bytes`` combines those stream-tail zeros with rate-match pad;
    HEADER data and frame 0 are absent by construction.
    """
    n_pay = np.asarray(payload_sectors, np.int64)
    n_ctrl = np.asarray(control_sectors, np.int64)
    fsec = np.asarray(physical_sectors, np.int64)
    if n_pay.ndim != 1 or n_pay.shape != n_ctrl.shape or n_pay.shape != fsec.shape:
        raise ValueError("BODY delivery sector vectors must have equal length")
    if (n_pay < 0).any() or (n_ctrl < 0).any() or (fsec < n_pay + n_ctrl).any():
        raise ValueError("BODY delivery sector vectors are physically inconsistent")

    useful_payload = _allocate_useful_bytes(
        n_pay, body_payload_bytes, name="payload")
    useful_control = _allocate_useful_bytes(
        n_ctrl, body_control_bytes, name="control")
    physical_bytes = fsec * SECTOR_BYTES
    rate_pad_bytes = (fsec - n_pay - n_ctrl) * SECTOR_BYTES
    stream_pad_bytes = (
        (n_pay + n_ctrl) * SECTOR_BYTES - useful_payload - useful_control)
    pad_bytes = rate_pad_bytes + stream_pad_bytes
    if not np.array_equal(
            useful_payload + useful_control + pad_bytes, physical_bytes):
        raise AssertionError("useful BODY trace does not sum to physical slots")
    return {
        "body_useful_payload_bytes": useful_payload,
        "body_useful_control_bytes": useful_control,
        "body_pad_bytes": pad_bytes,
        "body_rate_pad_bytes": rate_pad_bytes,
        "body_stream_pad_bytes": stream_pad_bytes,
        "body_physical_bytes": physical_bytes,
    }


def schedule_payload_ring(
        pattern_loads, block_lengths, *, fps, ring_capacity_patterns,
        frame_sectors, fill=True):
    """Schedule control JIT and payload prefetch, including physical RING use.

    ``ring_occupancy[i]`` is the number of 32-byte pattern slots physically in
    the payload RING at the end of frame ``i``.  It includes padding in the last
    payload sector because the player receives and advances whole sectors.
    Frame 0 is loaded from HEADER.DAT and does not consume this RING.
    """
    n_load = np.asarray(pattern_loads, np.int64)
    blk_len = np.asarray(block_lengths, np.int64)
    if n_load.ndim != 1 or blk_len.ndim != 1 or n_load.shape != blk_len.shape:
        raise ValueError("pattern loads and block lengths must be equal-length vectors")
    if not len(n_load):
        raise ValueError("the stream must contain at least one frame")
    if (n_load < 0).any() or (blk_len < 0).any():
        raise ValueError("pattern loads and block lengths must be non-negative")

    nfr = len(n_load)
    nc = np.zeros(nfr, np.int64)
    ctrl_deliv = 0
    ctrl_cur = 0
    for i in range(1, nfr):
        deficit = (ctrl_cur + int(blk_len[i])) - ctrl_deliv
        k = max(0, -(-deficit // SECTOR_BYTES)) if deficit > 0 else 0
        nc[i] = k
        ctrl_deliv += k * SECTOR_BYTES
        ctrl_cur += int(blk_len[i])

    cap_sec = np.maximum(int(frame_sectors) - nc, 0)
    n_load_body = n_load.copy()
    n_load_body[0] = 0
    consumed = np.cumsum(n_load_body)
    total_patterns = int(consumed[-1])
    consumed_sec = -(-consumed // PATTERNS_PER_SECTOR)
    total_payload_sec = int(-(-total_patterns // PATTERNS_PER_SECTOR))
    delivered = np.zeros(nfr, np.int64)

    if fill:
        ring_sec = int(ring_capacity_patterns) // PATTERNS_PER_SECTOR
        prebuffer_sec = int(min(ring_sec, total_payload_sec))

        # Back-propagate future cold deadlines through each slot's hard sector
        # cap.  This is the minimum cumulative payload that must have arrived.
        need = np.zeros(nfr, np.int64)
        need[-1] = total_payload_sec
        for i in range(nfr - 2, -1, -1):
            immediate = int(-(-int(consumed[i + 1]) // PATTERNS_PER_SECTOR))
            future = int(need[i + 1]) - int(cap_sec[i + 1])
            need[i] = max(immediate, future, 0)
        if prebuffer_sec < int(need[0]):
            raise ScheduleError(
                f"prebuffer {prebuffer_sec} sectors cannot arm the payload schedule; "
                f"at least {int(need[0])} are required")

        ratedelta = rate_deltas(nfr, fps)
        rate_lead = 0
        prev = prebuffer_sec
        delivered[0] = prebuffer_sec
        for i in range(1, nfr):
            due = int(ratedelta[i]) - rate_lead
            soft_pay = max(0, due - int(nc[i]))
            hi_ring = (
                int(consumed[i]) + int(ring_capacity_patterns)
            ) // PATTERNS_PER_SECTOR
            hi = min(prev + int(cap_sec[i]), total_payload_sec, int(hi_ring))
            lo = max(prev, int(need[i]))
            if lo > hi:
                raise ScheduleError(
                    f"rate-shaped payload schedule is impossible at frame {i}: "
                    f"minimum cumulative delivery {lo} sectors exceeds limit {hi}")
            current = max(lo, min(prev + soft_pay, hi))
            delivered[i] = current
            actual = (current - prev) + int(nc[i])
            routed = max(actual, due)
            rate_lead += routed - int(ratedelta[i])
            if rate_lead < 0:
                raise AssertionError("rate-shaped schedule lead became negative")
            prev = current
    else:
        cumcap = np.cumsum(cap_sec)
        prebuffer_sec = int(max(0, np.max(consumed_sec - cumcap)))
        delivered[-1] = total_payload_sec
        for i in range(nfr - 1, 0, -1):
            delivered[i - 1] = max(
                int(consumed_sec[i - 1]), int(delivered[i] - cap_sec[i]))

    n_pay_sec = np.empty(nfr, np.int64)
    n_pay_sec[0] = delivered[0] - prebuffer_sec
    n_pay_sec[1:] = delivered[1:] - delivered[:-1]
    occupancy = delivered * PATTERNS_PER_SECTOR - consumed
    under = int((occupancy < 0).sum())
    over = int((n_pay_sec + nc > int(frame_sectors)).sum())

    if nfr > 1:
        ready_margin = delivered[:-1] * PATTERNS_PER_SECTOR - consumed[1:]
        ready_bad = np.flatnonzero(ready_margin < 0)
        ready_min = int(ready_margin.min())
        if ready_bad.size:
            frame = int(ready_bad[0]) + 1
            raise ScheduleError(
                f"control-first invariant failed at frame {frame}: only "
                f"{int(delivered[frame - 1]) * PATTERNS_PER_SECTOR} patterns "
                f"delivered before control, but {int(consumed[frame])} are "
                f"consumed through that frame (short by "
                f"{-int(ready_margin[frame - 1])})")
    else:
        ready_min = 0

    ctrl_need_len = blk_len.copy()
    ctrl_need_len[0] = 0
    ctrl_need = np.cumsum(ctrl_need_len)
    ctrl_delivered = np.cumsum(nc) * SECTOR_BYTES
    ctrl_margin = ctrl_delivered - ctrl_need
    ctrl_bad = np.flatnonzero(ctrl_margin < 0)
    ctrl_min = int(ctrl_margin.min())
    if ctrl_bad.size:
        frame = int(ctrl_bad[0])
        raise ScheduleError(
            f"control completeness failed at frame {frame}: "
            f"{int(ctrl_delivered[frame])} bytes delivered, "
            f"{int(ctrl_need[frame])} bytes required")

    fsec, ratedelta, rate_lead_trace = rate_match_sectors(
        n_pay_sec, nc, fps=fps)
    body_payload_bytes = max(
        0, (total_patterns - prebuffer_sec * PATTERNS_PER_SECTOR)
        * PATTERN_BYTES)
    body_control_bytes = int(blk_len[1:].sum())
    delivery_trace = useful_body_delivery_trace(
        n_pay_sec,
        nc,
        fsec,
        body_payload_bytes=body_payload_bytes,
        body_control_bytes=body_control_bytes,
    )
    rate_lead_peak = int(rate_lead_trace.max())
    rate_lead_end = int(rate_lead_trace[-1])
    feasible = (
        (n_pay_sec >= 0).all() and over == 0 and under == 0
        and ready_min >= 0 and ctrl_min >= 0 and rate_lead_end == 0
    )
    result = {
        "n_pay_sec": n_pay_sec,
        "n_ctrl_sec": nc,
        "feasible": bool(feasible),
        "over": over,
        "under": under,
        "prebuf_pat": prebuffer_sec * PATTERNS_PER_SECTOR,
        "ring_peak": int(occupancy.max()),
        "ring_min": int(occupancy.min()),
        "ring_occupancy": occupancy,
        "ready_min": ready_min,
        "ctrl_min": ctrl_min,
        "blk_len": blk_len,
        "M": total_patterns,
        "fsec": fsec,
        "ratedelta": ratedelta,
        "rate_lead_peak": rate_lead_peak,
        "rate_lead_end": rate_lead_end,
        "f0_header": True,
        "f0_cold": int(n_load[0]),
        "f0_ctrl_len": int(blk_len[0]),
    }
    result.update(delivery_trace)
    return result
