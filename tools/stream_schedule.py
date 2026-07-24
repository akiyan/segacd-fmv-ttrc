#!/usr/bin/env python3
"""Pure PrgBuf delivery scheduling shared by sim and pack.

The encoder has an offline whole-movie quality budget.  The player has a
different, physical object: the PRG-RAM PrgBuf circular buffer filled by whole
CD sectors.  This module models the latter from the frozen per-frame Prg loads
and control-block lengths so analysis and disc packing cannot drift.
"""
from __future__ import annotations

import numpy as np

import av_config
import shadow_updates


SECTOR_BYTES = av_config.CD_SECTOR_BYTES
CD_SECTORS_PER_SECOND = av_config.CD_SECTORS_PER_SECOND
CD_BYTES_PER_SECOND = av_config.CD_BYTES_PER_SECOND
PATTERN_BYTES = 32
PATTERNS_PER_SECTOR = SECTOR_BYTES // PATTERN_BYTES
RUN_DESCRIPTOR_BYTES = 4
STREAM_SCHEDULE_SCHEMA_VERSION = 3


class ScheduleError(ValueError):
    """The requested stream cannot satisfy the player's delivery rules."""

    def __init__(self, message, *, kind="", details=None):
        super().__init__(message)
        self.kind = str(kind)
        self.details = dict(details or {})


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
        updates, runs, *, cells, audio_frame_bytes, update_lists=None):
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
    if update_lists is None:
        use_lists = np.zeros(n_upd.shape, np.bool_)
    else:
        use_lists = np.asarray(update_lists, np.bool_)
        if use_lists.shape != n_upd.shape:
            raise ValueError("update-list flags must match update counts")
    update_bytes = np.where(
        use_lists,
        n_upd * shadow_updates.LIST_ITEM_BYTES,
        shadow_updates.aligned_bitmap_bytes(cells)
        + n_upd * shadow_updates.SHADOW_ENTRY_BYTES,
    )
    # body prefix: frame_seq, n_upd and pal:u16 = 6 bytes. Entry words and the
    # run suffix are even-sized; only the pre-suffix body may need a byte.
    pre_suffix = 6 + update_bytes + int(audio_frame_bytes)
    pre_suffix += pre_suffix & 1
    # total_len word + aligned body + n_runs word + four bytes per run.
    return (2 + pre_suffix + 2 + n_runs * RUN_DESCRIPTOR_BYTES).astype(np.int64)


def body_fresh_byte_supply(
        frame_count, fps, *, cells, audio_frame_bytes):
    """Return BODY bytes left after reserving fixed control data first.

    The gross allowance follows the player's exact integer sector cadence, not
    an averaged bytes-per-frame rate.  Variable control bytes (two bytes per
    update and four bytes per run) and pattern payload are charged by the
    encoder from ``variable``.  Frame 0 lives outside BODY and is all zero.
    """
    count = int(frame_count)
    if count < 0:
        raise ValueError("frame_count must not be negative")
    gross = rate_deltas(count, fps) * SECTOR_BYTES
    zeros = np.zeros(count, np.int64)
    fixed = control_block_lengths(
        zeros, zeros,
        cells=cells,
        audio_frame_bytes=audio_frame_bytes,
    )
    if count:
        fixed[0] = 0
    variable = gross - fixed
    if np.any(variable < 0):
        frame = int(np.flatnonzero(variable < 0)[0])
        raise ScheduleError(
            f"BODY fixed control exceeds the CD-1x allowance at frame {frame}: "
            f"control={int(fixed[frame])}B gross={int(gross[frame])}B")
    return {
        "gross": gross,
        "fixed_control": fixed,
        "variable": variable,
    }


def max_run_control_reservation(max_cold, active_tiles):
    """Return the temporary worst-case run-descriptor reservation.

    A source-aware run always contains at least one cold tile, so its count
    cannot exceed the per-frame cold cap.  A zero cap means uncapped and falls
    back to the active cell count.  The encoder refunds the difference between
    this reservation and the exact run count as soon as allocation finishes.
    """
    cold = int(max_cold)
    active = int(active_tiles)
    if cold < 0 or active < 0:
        raise ValueError("max_cold and active_tiles must be non-negative")
    return (cold if cold else active) * RUN_DESCRIPTOR_BYTES


def body_funded_work_bytes(
        pattern_loads, updates, runs, *, cells, audio_frame_bytes,
        update_lists=None):
    """Return exact control plus Prg-pattern work attributed to each frame.

    This is encoder funding demand, not the physical BODY delivery trace: the
    packer may satisfy an initial prefix of the Prg patterns from the boot
    prebuffer before the timed BODY read begins.
    """
    n_load = np.asarray(pattern_loads, np.int64)
    n_upd = np.asarray(updates, np.int64)
    n_runs = np.asarray(runs, np.int64)
    if (n_load.ndim != 1 or n_upd.shape != n_load.shape
            or n_runs.shape != n_load.shape):
        raise ValueError("pattern, update, and run vectors must have equal length")
    if np.any(n_load < 0):
        raise ValueError("pattern loads must be non-negative")
    control = control_block_lengths(
        n_upd, n_runs,
        cells=cells,
        audio_frame_bytes=audio_frame_bytes,
        update_lists=update_lists,
    )
    useful = control + n_load * PATTERN_BYTES
    if len(useful):
        useful[0] = 0
    return useful


def select_shadow_update_lists(
        cell_lists, runs, pattern_loads, *, cells, fps, ring_capacity_patterns,
        frame_sectors, audio_frame_bytes, fill=True,
        prebuffer_capacity_patterns=None,
        max_control_bytes=0x2000, allow_control_growth=False):
    """Select faster lists without reducing physical delivery margins.

    Positive control growth is disabled in the qualified path. Full Bad Apple
    playback proved that unchanged PrgBuf/readiness minima do not by themselves
    protect the Sub-CPU completion phase from extra APPLY copies.
    """
    frames = tuple(tuple(int(cell) for cell in frame) for frame in cell_lists)
    n_upd = np.asarray([len(frame) for frame in frames], np.int64)
    n_runs = np.asarray(runs, np.int64)
    loads = np.asarray(pattern_loads, np.int64)
    if n_runs.shape != n_upd.shape or loads.shape != n_upd.shape:
        raise ValueError("shadow cells, runs and pattern loads must have equal lengths")

    costs = tuple(shadow_updates.frame_cost(frame, cells) for frame in frames)
    legacy_lengths = control_block_lengths(
        n_upd, n_runs, cells=cells, audio_frame_bytes=audio_frame_bytes)
    all_list_lengths = control_block_lengths(
        n_upd, n_runs, cells=cells, audio_frame_bytes=audio_frame_bytes,
        update_lists=np.ones(n_upd.shape, np.bool_))
    eligible = np.asarray([
        index > 0 and cost.saved_cycles > 0
        and int(all_list_lengths[index]) <= int(max_control_bytes)
        for index, cost in enumerate(costs)
    ], np.bool_)

    def run_schedule(flags):
        lengths = control_block_lengths(
            n_upd, n_runs, cells=cells, audio_frame_bytes=audio_frame_bytes,
            update_lists=flags)
        scheduled = schedule_payload_ring(
            loads, lengths, fps=fps,
            ring_capacity_patterns=ring_capacity_patterns,
            frame_sectors=frame_sectors, fill=fill,
            prebuffer_capacity_patterns=prebuffer_capacity_patterns)
        return lengths, scheduled

    baseline_lengths, baseline = run_schedule(np.zeros(n_upd.shape, np.bool_))
    if not baseline["feasible"]:
        raise ScheduleError("baseline schedule is infeasible before shadow-list selection")
    target_ring = int(baseline["ring_min"])
    target_ready = int(baseline["ready_min"])

    selected = np.asarray([
        bool(eligible[index] and cost.added_bytes <= 0)
        for index, cost in enumerate(costs)
    ], np.bool_)
    lengths, chosen_schedule = run_schedule(selected)
    if (not chosen_schedule["feasible"]
            or int(chosen_schedule["ring_min"]) < target_ring
            or int(chosen_schedule["ready_min"]) < target_ready):
        raise AssertionError("zero-cost shadow lists unexpectedly reduced schedule margins")

    from fractions import Fraction
    groups = {}
    for index, cost in enumerate(costs):
        if eligible[index] and cost.added_bytes > 0:
            ratio = Fraction(cost.saved_cycles, cost.added_bytes)
            groups.setdefault(ratio, []).append(index)

    cutoff = None
    rejected_ratio = max(groups, default=None)
    if allow_control_growth:
        rejected_ratio = None
        for ratio in sorted(groups, reverse=True):
            trial = selected.copy()
            trial[groups[ratio]] = True
            trial_lengths, trial_schedule = run_schedule(trial)
            if (trial_schedule["feasible"]
                    and int(trial_schedule["ring_min"]) >= target_ring
                    and int(trial_schedule["ready_min"]) >= target_ready):
                selected = trial
                lengths = trial_lengths
                chosen_schedule = trial_schedule
                cutoff = ratio
            else:
                rejected_ratio = ratio
                break

    return {
        "schema_version": 1,
        "selected": selected,
        "costs": costs,
        "block_lengths": lengths,
        "legacy_block_lengths": baseline_lengths,
        "baseline_schedule": baseline,
        "schedule": chosen_schedule,
        "cutoff_numerator": int(cutoff.numerator) if cutoff is not None else 0,
        "cutoff_denominator": int(cutoff.denominator) if cutoff is not None else 1,
        "rejected_numerator": (
            int(rejected_ratio.numerator) if rejected_ratio is not None else 0),
        "rejected_denominator": (
            int(rejected_ratio.denominator) if rejected_ratio is not None else 1),
        "control_growth_enabled": bool(allow_control_growth),
    }


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


def split_body_payload_classes(
        raw_pattern_flags, body_payload_bytes, *, prebuffer_patterns):
    """Split timed BODY payload into Raw and Prg-charge byte traces.

    ``raw_pattern_flags`` follows PrgBuf payload order after frame 0. True
    marks a same-frame Raw-funded pattern; false marks a quality-budget or
    prefetch Prg pattern. HEADER prebuffer patterns precede timed BODY data.
    """
    flags = np.asarray(raw_pattern_flags, np.bool_)
    delivered = np.asarray(body_payload_bytes, np.int64)
    prebuffer = int(prebuffer_patterns)
    if flags.ndim != 1 or delivered.ndim != 1:
        raise ValueError("payload class flags and delivery bytes must be 1-D")
    if prebuffer < 0 or prebuffer > len(flags):
        raise ValueError("payload prebuffer is outside the class stream")
    if np.any(delivered < 0) or np.any(delivered % PATTERN_BYTES):
        raise ValueError("payload delivery bytes must contain whole patterns")
    body_flags = flags[prebuffer:]
    delivered_patterns = delivered // PATTERN_BYTES
    if int(delivered_patterns.sum()) != len(body_flags):
        raise ValueError(
            "payload class stream does not match delivered BODY patterns")
    raw_bytes = np.zeros(delivered.shape, np.int64)
    cursor = 0
    for frame, count in enumerate(delivered_patterns):
        next_cursor = cursor + int(count)
        raw_bytes[frame] = (
            int(np.count_nonzero(body_flags[cursor:next_cursor]))
            * PATTERN_BYTES
        )
        cursor = next_cursor
    return raw_bytes, delivered - raw_bytes


def schedule_payload_ring(
        pattern_loads, block_lengths, *, fps, ring_capacity_patterns,
        frame_sectors, fill=True, prebuffer_capacity_patterns=None):
    """Schedule control JIT and payload prefetch, including physical RING use.

    ``ring_occupancy[i]`` is the number of 32-byte pattern slots physically in
    PrgBuf at the end of frame ``i``.  It includes padding in the last
    payload sector because the player receives and advances whole sectors.
    Frame 0 is loaded from HEADER.DAT and does not consume this RING.

    ``prebuffer_capacity_patterns`` is the normal fps-specific PrgBuf ceiling.
    ``ring_capacity_patterns`` is the larger hard physical-delivery ceiling
    below player back-pressure. Keeping them separate lets continuous BODY
    delivery absorb cadence-scaled jitter without pretending that the encoder
    owns the reserved interval as ordinary time-shifting capacity.
    """
    n_load = np.asarray(pattern_loads, np.int64)
    blk_len = np.asarray(block_lengths, np.int64)
    if n_load.ndim != 1 or blk_len.ndim != 1 or n_load.shape != blk_len.shape:
        raise ValueError("pattern loads and block lengths must be equal-length vectors")
    if not len(n_load):
        raise ValueError("the stream must contain at least one frame")
    if (n_load < 0).any() or (blk_len < 0).any():
        raise ValueError("pattern loads and block lengths must be non-negative")
    physical_capacity = int(ring_capacity_patterns)
    prebuffer_capacity = (
        physical_capacity
        if prebuffer_capacity_patterns is None
        else int(prebuffer_capacity_patterns)
    )
    if physical_capacity <= 0:
        raise ValueError("physical ring capacity must be positive")
    if not 0 < prebuffer_capacity <= physical_capacity:
        raise ValueError(
            "prebuffer capacity must be positive and no larger than the "
            "physical ring capacity")

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
        prebuffer_sec = int(min(
            prebuffer_capacity // PATTERNS_PER_SECTOR,
            total_payload_sec,
        ))

        # Back-propagate future cold deadlines through each slot's hard sector
        # cap.  This is the minimum cumulative payload that must have arrived.
        need = np.zeros(nfr, np.int64)
        need_origin = np.zeros(nfr, np.int64)
        need[-1] = total_payload_sec
        need_origin[-1] = nfr - 1
        for i in range(nfr - 2, -1, -1):
            immediate = int(-(-int(consumed[i + 1]) // PATTERNS_PER_SECTOR))
            future = int(need[i + 1]) - int(cap_sec[i + 1])
            if immediate >= future and immediate > 0:
                need[i] = immediate
                need_origin[i] = i + 1
            elif future > 0:
                need[i] = future
                need_origin[i] = need_origin[i + 1]
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
                int(consumed[i]) + physical_capacity
            ) // PATTERNS_PER_SECTOR
            hi = min(prev + int(cap_sec[i]), total_payload_sec, int(hi_ring))
            lo = max(prev, int(need[i]))
            if lo > hi:
                deficit_sectors = lo - hi
                origin_frame = int(need_origin[i])
                origin_patterns = int(consumed[origin_frame])
                origin_sectors = int(
                    -(-origin_patterns // PATTERNS_PER_SECTOR))
                target_sectors = max(0, origin_sectors - deficit_sectors)
                patterns_to_remove = max(
                    1,
                    origin_patterns
                    - target_sectors * PATTERNS_PER_SECTOR,
                )
                raise ScheduleError(
                    f"rate-shaped payload schedule is impossible at frame {i}: "
                    f"minimum cumulative delivery {lo} sectors exceeds limit {hi}; "
                    f"deadline origin frame {origin_frame} needs "
                    f"{patterns_to_remove} fewer Prg patterns",
                    kind="payload_capacity",
                    details={
                        "failure_frame": int(i),
                        "origin_frame": origin_frame,
                        "deficit_sectors": int(deficit_sectors),
                        "patterns_to_remove": int(patterns_to_remove),
                        "minimum_delivery_sectors": int(lo),
                        "delivery_limit_sectors": int(hi),
                    })
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
    # Independent of the rate-shaped choice above, this is the most Prg
    # payload that could physically have arrived before each frame after
    # reserving its control sectors. It is the safe cumulative catch-up
    # ceiling for a stateful accounting pass: unlike the projected demand
    # curve, it does not permanently suppress work that was merely deferred.
    max_prebuffer_sec = (
        prebuffer_capacity // PATTERNS_PER_SECTOR)
    timed_cap_sec = cap_sec.copy()
    timed_cap_sec[0] = 0
    max_delivered_sec = (
        max_prebuffer_sec + np.cumsum(timed_cap_sec, dtype=np.int64))
    max_cumulative_prg_consumption = np.zeros(nfr, np.int64)
    if nfr > 1:
        max_cumulative_prg_consumption[1:] = (
            max_delivered_sec[:-1] * PATTERNS_PER_SECTOR)
    payload_frames = np.flatnonzero(n_pay_sec > 0)
    # Once the final payload sector has arrived, the terminal suffix can only
    # consume what remains.  Keep that suffix in the feasibility proof, but do
    # not let the intentional end-of-stream drain define comparison minima.
    evaluation_end_frame = (
        min(nfr, int(payload_frames[-1]) + 1) if payload_frames.size else nfr
    )
    evaluation_occupancy = occupancy[
        1:evaluation_end_frame] if evaluation_end_frame > 1 else occupancy[:1]
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
        "prebuffer_capacity_patterns": prebuffer_capacity,
        "ring_capacity_patterns": physical_capacity,
        "jitter_headroom_patterns": (
            physical_capacity - prebuffer_capacity),
        "ring_peak": int(occupancy.max()),
        "ring_jitter_peak": max(
            0, int(occupancy.max()) - prebuffer_capacity),
        "ring_min": int(occupancy.min()),
        "ring_min_evaluation": int(evaluation_occupancy.min()),
        "evaluation_end_frame": int(evaluation_end_frame),
        "ring_occupancy": occupancy,
        "max_cumulative_prg_consumption": (
            max_cumulative_prg_consumption),
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
