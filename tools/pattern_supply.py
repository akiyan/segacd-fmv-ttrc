#!/usr/bin/env python3
"""Plan and materialize the four physical pattern-supply sources.

``Prg`` is the streamed PRG-RAM circular buffer.  ``Wr0`` and ``Wr1`` are
physical 1M Word-RAM banks, selected by frame parity, and ``Dic`` is a
persistent Main-RAM dictionary.  The encoder selects the DicBuf dictionary from
the whole cold trace, then assigns the finite Word-RAM credits to residual
Prg-risk bursts.  The final decision log freezes every source and dictionary
index so simulation, packing, and the player consume the same plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


PATTERN_BYTES = 32
HARDSHIP_SCALE = 1024
MISS_HEADROOM_WEIGHT = 2
PRG_DEPLETION_WEIGHT = 1
FEEDBACK_REALLOCATION_DIVISOR = 16
FEEDBACK_MAX_DELTA_PER_FRAME = 1
FEEDBACK_MODES = frozenset({"miss", "prg", "combined"})

SOURCE_PRG = 0
SOURCE_WR = 1
SOURCE_DIC = 2
SOURCE_RESERVED = 3

SOURCE_SHIFT = 11
SOURCE_MASK = 0x1800
NAME_ENTRY_MASK = 0x67FF

RUN_SOURCE_SHIFT = 14
RUN_SOURCE_MASK = 0xC000
RUN_COUNT_MASK = 0x3FFF

# v12 indexed DicBuf run descriptor, still four bytes total:
#   word0: Dic index high 5 bits | zero-based VRAM slot (11 bits)
#   word1: source (2 bits) | Dic index low 3 bits | count (11 bits)
# Non-Dic sources keep all index bits zero.
RUN_V12_SLOT_MASK = 0x07FF
RUN_V12_INDEX_HIGH_SHIFT = 11
RUN_V12_INDEX_LOW_SHIFT = 11
RUN_V12_INDEX_LOW_MASK = 0x3800
RUN_V12_COUNT_MASK = 0x07FF

# Initial hardware-proof layout.  These are mirrored by movieplay_ip.s,
# movieplay_sp.s, and check_player_ring.py.
WORD_BUF_OFFSET = 0x15200
WORD_BUF_END = 0x1C000
WORD_BUF_PATTERNS = (WORD_BUF_END - WORD_BUF_OFFSET) // PATTERN_BYTES

DIC_STAGE_OFFSET = 0x0D000
DIC_BUF_BASE = 0x00FF6600
DIC_BUF_END = 0x00FF8600
DIC_BUF_PATTERNS = (DIC_BUF_END - DIC_BUF_BASE) // PATTERN_BYTES


def pack_pattern_key(key: bytes) -> bytes:
    """Pack one 8x8 nibble-per-byte simulator key into a 32-byte VDP tile."""
    raw = bytes(key)
    if len(raw) != 64 or any(value > 0x0F for value in raw):
        raise ValueError("pattern key must contain 64 palette nibbles")
    return bytes(
        (raw[offset] << 4) | raw[offset + 1]
        for offset in range(0, 64, 2)
    )


def encode_entry_source(entry: int, source: int) -> int:
    """Put a cold-pattern source in the two unused VDP-entry bits."""
    if source not in (SOURCE_PRG, SOURCE_WR, SOURCE_DIC):
        raise ValueError(f"invalid pattern source: {source}")
    if entry & SOURCE_MASK:
        raise ValueError(f"entry already uses source/flip bits: 0x{entry:04X}")
    return int(entry) | (int(source) << SOURCE_SHIFT)


def decode_entry_source(entry: int) -> int:
    return (int(entry) & SOURCE_MASK) >> SOURCE_SHIFT


def encode_run_count(count: int, source: int) -> int:
    if not 0 < int(count) <= RUN_COUNT_MASK:
        raise ValueError(f"invalid run count: {count}")
    if source not in (SOURCE_PRG, SOURCE_WR, SOURCE_DIC):
        raise ValueError(f"invalid pattern source: {source}")
    return int(count) | (int(source) << RUN_SOURCE_SHIFT)


def decode_run_count(value: int) -> tuple[int, int]:
    return int(value) & RUN_COUNT_MASK, (int(value) & RUN_SOURCE_MASK) >> RUN_SOURCE_SHIFT


def encode_run_descriptor(
    slot: int,
    count: int,
    source: int,
    dic_index: int = 0,
) -> tuple[int, int]:
    """Pack one v12 source run, including its DicBuf starting index."""
    slot = int(slot)
    count = int(count)
    source = int(source)
    dic_index = int(dic_index)
    if not 0 <= slot <= RUN_V12_SLOT_MASK:
        raise ValueError(f"invalid run slot: {slot}")
    if not 0 < count <= RUN_V12_COUNT_MASK:
        raise ValueError(f"invalid run count: {count}")
    if source not in (SOURCE_PRG, SOURCE_WR, SOURCE_DIC):
        raise ValueError(f"invalid pattern source: {source}")
    if source == SOURCE_DIC:
        if not 0 <= dic_index < DIC_BUF_PATTERNS:
            raise ValueError(f"invalid DicBuf index: {dic_index}")
        if dic_index + count > DIC_BUF_PATTERNS:
            raise ValueError("DicBuf run exceeds dictionary capacity")
    elif dic_index:
        raise ValueError("non-Dic run cannot carry a dictionary index")
    word0 = slot | ((dic_index >> 3) << RUN_V12_INDEX_HIGH_SHIFT)
    word1 = (
        (source << RUN_SOURCE_SHIFT)
        | ((dic_index & 7) << RUN_V12_INDEX_LOW_SHIFT)
        | count
    )
    return word0, word1


def decode_run_descriptor(word0: int, word1: int) -> tuple[int, int, int, int]:
    """Decode one v12 run as ``(slot, count, source, dic_index)``."""
    word0 = int(word0)
    word1 = int(word1)
    slot = word0 & RUN_V12_SLOT_MASK
    count = word1 & RUN_V12_COUNT_MASK
    source = (word1 & RUN_SOURCE_MASK) >> RUN_SOURCE_SHIFT
    dic_index = (
        ((word0 >> RUN_V12_INDEX_HIGH_SHIFT) << 3)
        | ((word1 & RUN_V12_INDEX_LOW_MASK) >> RUN_V12_INDEX_LOW_SHIFT)
    )
    if not count:
        raise ValueError("run count is zero")
    if source not in (SOURCE_PRG, SOURCE_WR, SOURCE_DIC):
        raise ValueError(f"invalid pattern source: {source}")
    if source == SOURCE_DIC:
        if dic_index + count > DIC_BUF_PATTERNS:
            raise ValueError("DicBuf run exceeds dictionary capacity")
    elif dic_index:
        raise ValueError("non-Dic run carries dictionary index bits")
    return slot, count, source, dic_index


def count_source_runs(
    slots: Sequence[int],
    sources: Sequence[int],
    dic_indices: Sequence[int] | None = None,
) -> int:
    """Count runs split by slot, physical source, or DicBuf index gap."""
    if len(slots) != len(sources):
        raise ValueError("slot and source counts differ")
    if dic_indices is None:
        dic_indices = (-1,) * len(slots)
    if len(dic_indices) != len(slots):
        raise ValueError("slot and DicBuf index counts differ")
    runs = 0
    previous_slot: int | None = None
    previous_source: int | None = None
    previous_dic = -1
    for raw_slot, raw_source, raw_dic in zip(slots, sources, dic_indices):
        slot = int(raw_slot)
        source = int(raw_source)
        dic_index = int(raw_dic)
        if source not in (SOURCE_PRG, SOURCE_WR, SOURCE_DIC):
            raise ValueError(f"invalid pattern source: {source}")
        if (previous_slot is None or slot != previous_slot + 1
                or source != previous_source
                or (source == SOURCE_DIC and dic_index != previous_dic + 1)):
            runs += 1
        previous_slot = slot
        previous_source = source
        previous_dic = dic_index
    return runs


@dataclass(frozen=True)
class SupplyPlan:
    """Per-update sources and source-local pattern streams in use order."""

    sources: tuple[tuple[int, ...], ...]
    prg_patterns: tuple[bytes, ...]
    wr0_patterns: tuple[bytes, ...]
    wr1_patterns: tuple[bytes, ...]
    dic_patterns: tuple[bytes, ...]
    prg_loads: np.ndarray
    wr0_loads: np.ndarray
    wr1_loads: np.ndarray
    dic_loads: np.ndarray
    # Per-update DicBuf indices. Non-Dic entries use -1.
    dic_indices: tuple[tuple[int, ...], ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.wr0_patterns or self.wr1_patterns or self.dic_patterns)


@dataclass(frozen=True)
class FrameSupplyBudget:
    """Maximum boot-preloaded cold patterns available to each frame."""

    wr: np.ndarray
    dic: np.ndarray
    dic_dictionary: tuple[bytes, ...] = ()
    feedback_miss: np.ndarray | None = None
    feedback_cold: np.ndarray | None = None
    feedback_prg_remaining: np.ndarray | None = None
    reallocated_wr_patterns: int = 0

    @property
    def total(self) -> np.ndarray:
        return self.wr + self.dic

    @property
    def wr0_patterns(self) -> int:
        return int(self.wr[::2].sum())

    @property
    def wr1_patterns(self) -> int:
        return int(self.wr[1::2].sum())

    @property
    def dic_patterns(self) -> int:
        return (len(self.dic_dictionary) if self.dic_dictionary
                else int(self.dic.sum()))

    @property
    def dic_dictionary_packed(self) -> tuple[bytes, ...]:
        return tuple(pack_pattern_key(key) for key in self.dic_dictionary)


def select_dic_dictionary(prediction, capacity: int) -> tuple[bytes, ...]:
    """Select persistent DicBuf entries from the complete predicted cold trace.

    Reuse count is the primary value. Protected/Miss-risk occurrences break
    ties, followed by first occurrence for deterministic output. Word-RAM is
    deliberately not consumed here; its finite credits are allocated after
    dictionary hits have been removed from provisional Prg demand.
    """
    cold_frames = tuple(getattr(prediction, "cold_keys", ()) or ())
    protected_frames = tuple(
        getattr(prediction, "protected_keys", ()) or ())
    if capacity <= 0 or not cold_frames:
        return ()
    from collections import Counter
    uses: Counter[bytes] = Counter()
    protected: Counter[bytes] = Counter()
    first: dict[bytes, int] = {}
    ordinal = 0
    if protected_frames and len(protected_frames) != len(cold_frames):
        raise ValueError("protected-key frame count differs")
    for frame, frame_keys in enumerate(cold_frames):
        risk_keys = protected_frames[frame] if protected_frames else ()
        risk = set(risk_keys)
        for key in frame_keys:
            key = bytes(key)
            uses[key] += 1
            protected[key] += int(key in risk)
            first.setdefault(key, ordinal)
            ordinal += 1
    ranked = sorted(
        uses,
        key=lambda key: (-uses[key], -protected[key], first[key], key),
    )
    return tuple(ranked[:int(capacity)])


def _credit_priority(
    frame: int,
    allocated: np.ndarray,
    exact_bytes: np.ndarray,
    protected_bytes: np.ndarray,
    exact_cold: np.ndarray,
    protected_cold: np.ndarray,
) -> tuple[int, int, int, int]:
    """Return the marginal priority of one more preload credit.

    Credits first flatten frames whose protected changes are predicted to need
    a new pattern.  Once those are covered, remaining capacity flattens the
    complete exact demand.  Recomputing the residual after every 32-byte credit
    avoids concentrating a whole buffer in one frame merely because it started
    as the largest burst.
    """
    used = int(allocated[frame])
    exact_left = max(0, int(exact_bytes[frame]) - used * PATTERN_BYTES)
    cold_left = max(0, int(exact_cold[frame]) - used)
    if used < int(protected_cold[frame]):
        protected_left = max(
            0, int(protected_bytes[frame]) - used * PATTERN_BYTES)
        return (2, protected_left, exact_left, cold_left)
    return (1, exact_left, cold_left, 0)


def _feedback_priority(
    frame: int,
    allocated: np.ndarray,
    exact_bytes: np.ndarray,
    protected_bytes: np.ndarray,
    exact_cold: np.ndarray,
    protected_cold: np.ndarray,
    feedback_miss: np.ndarray,
    feedback_cold: np.ndarray,
    feedback_prg_remaining: np.ndarray,
    feedback_mode: str,
    *,
    active_tiles: int,
    cold_cap: int,
    prg_capacity: int,
) -> tuple[int, int, int, int, int]:
    """Weight one marginal credit by measured seed-pass hardship."""
    tier, primary, exact_left, cold_left = _credit_priority(
        frame,
        allocated,
        exact_bytes,
        protected_bytes,
        exact_cold,
        protected_cold,
    )
    weighted = primary * HARDSHIP_SCALE
    headroom = min(
        int(cold_cap),
        max(0, int(cold_cap) - int(feedback_cold[frame])),
    )
    miss = min(int(active_tiles), max(0, int(feedback_miss[frame])))
    if feedback_mode in {"miss", "combined"} and miss and headroom:
        weighted += (
            primary
            * MISS_HEADROOM_WEIGHT
            * miss
            * headroom
            * HARDSHIP_SCALE
            // (int(active_tiles) * int(cold_cap))
        )
    if feedback_mode in {"prg", "combined"}:
        depletion = min(
            int(prg_capacity),
            max(0, int(prg_capacity)
                - int(feedback_prg_remaining[frame])),
        )
        weighted += (
            primary
            * PRG_DEPLETION_WEIGHT
            * depletion
            * HARDSHIP_SCALE
            // int(prg_capacity)
        )
    return (tier, weighted, primary, exact_left, cold_left)


def _allocate_credits(
    target: np.ndarray,
    available: np.ndarray,
    allocated: np.ndarray,
    frames: Sequence[int],
    capacity: int,
    exact_bytes: np.ndarray,
    protected_bytes: np.ndarray,
    exact_cold: np.ndarray,
    protected_cold: np.ndarray,
) -> None:
    """Water-fill one physical source over its eligible frames."""
    import heapq

    heap: list[tuple[int, int, int, int, int, int]] = []

    def push(frame: int) -> None:
        if int(available[frame]) <= 0:
            return
        priority = _credit_priority(
            frame, allocated, exact_bytes, protected_bytes,
            exact_cold, protected_cold)
        # heapq is a min-heap.  Negate the priority fields and retain the frame
        # plus allocation generation for deterministic ordering/stale checks.
        heapq.heappush(
            heap,
            tuple(-value for value in priority)
            + (int(frame), int(allocated[frame])),
        )

    for frame in frames:
        push(int(frame))

    remaining = max(0, int(capacity))
    while remaining and heap:
        *_priority, frame, generation = heapq.heappop(heap)
        if generation != int(allocated[frame]):
            continue
        if int(available[frame]) <= 0:
            continue
        target[frame] += 1
        available[frame] -= 1
        allocated[frame] += 1
        remaining -= 1
        push(frame)


def _reallocate_feedback_word_credits(
    target: np.ndarray,
    available: np.ndarray,
    allocated: np.ndarray,
    frames: Sequence[int],
    move_limit: int,
    exact_bytes: np.ndarray,
    protected_bytes: np.ndarray,
    exact_cold: np.ndarray,
    protected_cold: np.ndarray,
    feedback_miss: np.ndarray,
    feedback_cold: np.ndarray,
    feedback_prg_remaining: np.ndarray,
    feedback_mode: str,
    *,
    active_tiles: int,
    cold_cap: int,
    prg_capacity: int,
) -> int:
    """Move a bounded number of baseline credits toward measured hardship."""
    eligible = tuple(int(frame) for frame in frames)
    baseline = target.copy()
    moved = 0
    while moved < max(0, int(move_limit)):
        receivers = [
            frame for frame in eligible
            if int(available[frame]) > 0
            and int(target[frame] - baseline[frame])
            < FEEDBACK_MAX_DELTA_PER_FRAME
        ]
        if not receivers:
            break
        receiver = max(
            receivers,
            key=lambda frame: (
                *_feedback_priority(
                    frame,
                    allocated,
                    exact_bytes,
                    protected_bytes,
                    exact_cold,
                    protected_cold,
                    feedback_miss,
                    feedback_cold,
                    feedback_prg_remaining,
                    feedback_mode,
                    active_tiles=active_tiles,
                    cold_cap=cold_cap,
                    prg_capacity=prg_capacity,
                ),
                -frame,
            ),
        )
        donors = [
            frame for frame in eligible
            if frame != receiver and int(target[frame]) > 0
            and int(baseline[frame] - target[frame])
            < FEEDBACK_MAX_DELTA_PER_FRAME
        ]
        if not donors:
            break
        donor_priorities = []
        for frame in donors:
            allocated[frame] -= 1
            priority = _feedback_priority(
                frame,
                allocated,
                exact_bytes,
                protected_bytes,
                exact_cold,
                protected_cold,
                feedback_miss,
                feedback_cold,
                feedback_prg_remaining,
                feedback_mode,
                active_tiles=active_tiles,
                cold_cap=cold_cap,
                prg_capacity=prg_capacity,
            )
            allocated[frame] += 1
            donor_priorities.append((priority, frame))
        donor_priority, donor = min(
            donor_priorities,
            key=lambda item: (*item[0], item[1]),
        )
        receiver_priority = _feedback_priority(
            receiver,
            allocated,
            exact_bytes,
            protected_bytes,
            exact_cold,
            protected_cold,
            feedback_miss,
            feedback_cold,
            feedback_prg_remaining,
            feedback_mode,
            active_tiles=active_tiles,
            cold_cap=cold_cap,
            prg_capacity=prg_capacity,
        )
        if receiver_priority <= donor_priority:
            break
        target[receiver] += 1
        available[receiver] -= 1
        allocated[receiver] += 1
        target[donor] -= 1
        available[donor] += 1
        allocated[donor] -= 1
        moved += 1
    return moved


def plan_frame_budgets(
    prediction,
    *,
    enabled: bool = True,
    wr_patterns: int = WORD_BUF_PATTERNS,
    dic_patterns: int = DIC_BUF_PATTERNS,
    cold_cap: int = 0,
    feedback_miss: np.ndarray | None = None,
    feedback_cold: np.ndarray | None = None,
    feedback_prg_remaining: np.ndarray | None = None,
    feedback_mode: str = "combined",
    active_tiles: int = 0,
    prg_capacity_patterns: int = 0,
) -> FrameSupplyBudget:
    """Select DicBuf hits, then allocate residual Wr credits.

    Current predictions expose the cold keys for the complete movie. DicBuf is
    selected first as a persistent dictionary. Its hits are removed from the
    provisional Prg demand, after which parity-constrained Wr0/Wr1 credits
    water-fill the remaining protected/Miss-risk bursts. Older count-only test
    fixtures retain the former finite-credit behavior.
    """
    exact_bytes = np.asarray(prediction.exact_bytes, dtype=np.int64)
    protected_bytes = np.asarray(prediction.protected_bytes, dtype=np.int64)
    exact_cold = np.asarray(prediction.exact_cold, dtype=np.int64)
    protected_cold = np.asarray(prediction.protected_cold, dtype=np.int64)
    shape = exact_bytes.shape
    if exact_bytes.ndim != 1 or any(
            values.shape != shape for values in (
                protected_bytes, exact_cold, protected_cold)):
        raise ValueError("pattern-supply demand arrays must be matching vectors")
    if any(np.any(values < 0) for values in (
            exact_bytes, protected_bytes, exact_cold, protected_cold)):
        raise ValueError("pattern-supply demand must be non-negative")
    if np.any(protected_cold > exact_cold):
        raise ValueError("protected cold demand exceeds complete exact demand")
    feedback_values = (
        feedback_miss,
        feedback_cold,
        feedback_prg_remaining,
    )
    feedback_enabled = any(value is not None for value in feedback_values)
    if feedback_enabled:
        feedback_mode = str(feedback_mode).strip().lower()
        if feedback_mode not in FEEDBACK_MODES:
            raise ValueError(
                f"invalid pattern-supply feedback mode: {feedback_mode}")
        if any(value is None for value in feedback_values):
            raise ValueError("pattern-supply feedback arrays are incomplete")
        feedback_miss = np.asarray(feedback_miss, dtype=np.int64)
        feedback_cold = np.asarray(feedback_cold, dtype=np.int64)
        feedback_prg_remaining = np.asarray(
            feedback_prg_remaining, dtype=np.int64)
        if any(value.shape != shape for value in (
                feedback_miss, feedback_cold, feedback_prg_remaining)):
            raise ValueError(
                "pattern-supply feedback must match demand")
        if any(np.any(value < 0) for value in (
                feedback_miss, feedback_cold, feedback_prg_remaining)):
            raise ValueError("pattern-supply feedback must be non-negative")
        if (int(cold_cap) <= 0 or int(active_tiles) <= 0
                or int(prg_capacity_patterns) <= 0):
            raise ValueError(
                "feedback allocation requires positive cold, active, and Prg caps")

    wr = np.zeros(shape, np.int64)
    dic = np.zeros(shape, np.int64)
    if not enabled or not len(exact_bytes):
        return FrameSupplyBudget(wr=wr, dic=dic)

    cold_frames = tuple(getattr(prediction, "cold_keys", ()) or ())
    protected_frames = tuple(
        getattr(prediction, "protected_keys", ()) or ())
    if cold_frames:
        if len(cold_frames) != len(exact_bytes):
            raise ValueError("pattern-supply cold-key frame count differs")
        dictionary = select_dic_dictionary(prediction, dic_patterns)
        dictionary_set = set(dictionary)
        protected_hits = np.zeros(shape, np.int64)
        for frame, keys in enumerate(cold_frames):
            dic[frame] = sum(bytes(key) in dictionary_set for key in keys)
            risk = protected_frames[frame] if protected_frames else ()
            protected_hits[frame] = sum(
                bytes(key) in dictionary_set for key in risk)
        dic[0] = 0
        protected_hits[0] = 0

        residual_exact_cold = np.maximum(exact_cold - dic, 0)
        residual_protected_cold = np.maximum(
            protected_cold - protected_hits, 0)
        residual_exact_bytes = np.maximum(
            exact_bytes - dic * PATTERN_BYTES, 0)
        residual_protected_bytes = np.maximum(
            protected_bytes - protected_hits * PATTERN_BYTES, 0)
        available = residual_exact_cold.copy()
        available[0] = 0
        allocated = np.zeros(shape, np.int64)
        for parity in (0, 1):
            _allocate_credits(
                wr, available, allocated,
                range(2 if parity == 0 else 1, len(exact_bytes), 2),
                wr_patterns, residual_exact_bytes,
                residual_protected_bytes, residual_exact_cold,
                residual_protected_cold)
        reallocated = 0
        if feedback_enabled:
            move_limit = max(
                1, int(wr_patterns) // FEEDBACK_REALLOCATION_DIVISOR)
            for parity in (0, 1):
                reallocated += _reallocate_feedback_word_credits(
                    wr,
                    available,
                    allocated,
                    range(2 if parity == 0 else 1, len(exact_bytes), 2),
                    move_limit,
                    residual_exact_bytes,
                    residual_protected_bytes,
                    residual_exact_cold,
                    residual_protected_cold,
                    feedback_miss,
                    feedback_cold,
                    feedback_prg_remaining,
                    feedback_mode,
                    active_tiles=int(active_tiles),
                    cold_cap=int(cold_cap),
                    prg_capacity=int(prg_capacity_patterns),
                )
        return FrameSupplyBudget(
            wr=wr,
            dic=dic,
            dic_dictionary=dictionary,
            feedback_miss=feedback_miss,
            feedback_cold=feedback_cold,
            feedback_prg_remaining=feedback_prg_remaining,
            reallocated_wr_patterns=reallocated,
        )

    available = exact_cold.copy()
    available[0] = 0
    allocated = np.zeros(shape, np.int64)
    for parity in (0, 1):
        _allocate_credits(
            wr, available, allocated,
            range(2 if parity == 0 else 1, len(exact_bytes), 2),
            wr_patterns, exact_bytes, protected_bytes,
            exact_cold, protected_cold)
    reallocated = 0
    if feedback_enabled:
        move_limit = max(
            1, int(wr_patterns) // FEEDBACK_REALLOCATION_DIVISOR)
        for parity in (0, 1):
            reallocated += _reallocate_feedback_word_credits(
                wr,
                available,
                allocated,
                range(2 if parity == 0 else 1, len(exact_bytes), 2),
                move_limit,
                exact_bytes,
                protected_bytes,
                exact_cold,
                protected_cold,
                feedback_miss,
                feedback_cold,
                feedback_prg_remaining,
                feedback_mode,
                active_tiles=int(active_tiles),
                cold_cap=int(cold_cap),
                prg_capacity=int(prg_capacity_patterns),
            )
    _allocate_credits(
        dic, available, allocated, range(1, len(exact_bytes)),
        dic_patterns, exact_bytes, protected_bytes,
        exact_cold, protected_cold)

    if int(wr[::2].sum()) > int(wr_patterns):
        raise AssertionError("Wr0 preload planner exceeded capacity")
    if int(wr[1::2].sum()) > int(wr_patterns):
        raise AssertionError("Wr1 preload planner exceeded capacity")
    if int(dic.sum()) > int(dic_patterns):
        raise AssertionError("DicBuf preload planner exceeded capacity")
    if np.any(wr + dic > exact_cold):
        raise AssertionError("preload planner exceeded predicted frame cold demand")
    return FrameSupplyBudget(
        wr=wr,
        dic=dic,
        feedback_miss=feedback_miss,
        feedback_cold=feedback_cold,
        feedback_prg_remaining=feedback_prg_remaining,
        reallocated_wr_patterns=reallocated,
    )


def _cold_runs(entries: Sequence[int], colds: Sequence[bool]) -> list[tuple[int, ...]]:
    """Return update-index runs whose allocated slots are consecutive."""
    runs: list[list[int]] = []
    previous_slot: int | None = None
    for update_index, (entry, cold) in enumerate(zip(entries, colds)):
        if not cold:
            continue
        slot = (int(entry) & 0x07FF) - 1
        if previous_slot is None or slot != previous_slot + 1:
            runs.append([])
        runs[-1].append(update_index)
        previous_slot = slot
    return [tuple(run) for run in runs]


def _frame_risk(log: dict, frame_count: int) -> list[tuple[int, int, int]]:
    """Rank legacy logs by Miss, then by low PrgBuf occupancy."""
    miss = np.asarray(log.get("miss", np.zeros(frame_count)), np.int64)
    if miss.shape != (frame_count,):
        miss = np.zeros(frame_count, np.int64)
    frozen = log.get("stream_schedule") or {}
    ring = np.asarray(
        frozen.get("ring_occupancy", np.full(frame_count, 1 << 30)), np.int64)
    if ring.shape != (frame_count,):
        ring = np.full(frame_count, 1 << 30, np.int64)
    return sorted(
        ((-int(miss[i]), int(ring[i]), i) for i in range(1, frame_count)),
        key=lambda item: item,
    )


def _frozen_sources(
    log: dict,
    per: Sequence[tuple[Sequence[int], Sequence[int], Sequence[bool]]],
) -> list[list[int]] | None:
    """Validate and return source assignments frozen by a current sim."""
    frozen = log.get("pattern_supply")
    if frozen is None:
        return None
    if int(frozen.get("schema_version", 0)) not in (1, 2):
        raise ValueError(
            f"unsupported frozen pattern-supply schema: "
            f"{frozen.get('schema_version')!r}")
    raw_sources = frozen.get("sources")
    if raw_sources is None or len(raw_sources) != len(per):
        raise ValueError("frozen pattern-supply frame count differs from decisions")

    sources: list[list[int]] = []
    for frame, ((_, entries, colds), raw_frame) in enumerate(zip(per, raw_sources)):
        frame_sources = [int(source) for source in raw_frame]
        if len(frame_sources) != len(entries):
            raise ValueError(
                f"frozen pattern-supply update count differs at frame {frame}")
        for update, (cold, source) in enumerate(zip(colds, frame_sources)):
            if source not in (SOURCE_PRG, SOURCE_WR, SOURCE_DIC):
                raise ValueError(
                    f"invalid frozen pattern source {source} at frame "
                    f"{frame}, update {update}")
            if not cold and source != SOURCE_PRG:
                raise ValueError(
                    f"non-cold update has a preload source at frame "
                    f"{frame}, update {update}")
            if frame == 0 and source != SOURCE_PRG:
                raise ValueError("frame zero cannot consume a boot preload")
        sources.append(frame_sources)
    return sources


def plan_supply(
    log: dict,
    per: Sequence[tuple[Sequence[int], Sequence[int], Sequence[bool]]],
    patterns: Sequence[bytes],
    *,
    prefetch_per: Sequence[Sequence[tuple]] | None = None,
    transfer_orders: Sequence[Sequence[int]] | None = None,
    enabled: bool = True,
    wr_patterns: int = WORD_BUF_PATTERNS,
    dic_patterns: int = DIC_BUF_PATTERNS,
) -> SupplyPlan:
    """Materialize cold patterns into their frozen physical sources.

    Current decision logs carry the exact per-update sources selected during
    simulation.  Older logs fall back to the hardware-proof planner, which
    moves complete runs without changing their control length.  Blobs are
    always emitted in chronological consumption order.
    """
    frame_count = len(per)
    if prefetch_per is None:
        prefetch_per = tuple(() for _ in per)
    if len(prefetch_per) != frame_count:
        raise ValueError("prefetch frame count differs from decisions")
    if transfer_orders is None:
        transfer_orders = tuple(
            tuple(index for index, cold in enumerate(colds) if cold)
            for _cells, _entries, colds in per)
    if len(transfer_orders) != frame_count:
        raise ValueError("transfer-order frame count differs from decisions")
    normalized_orders: list[tuple[int, ...]] = []
    for frame, ((_cells, entries, colds), raw_order) in enumerate(
            zip(per, transfer_orders)):
        order = tuple(int(index) for index in raw_order)
        expected = {index for index, cold in enumerate(colds) if cold}
        if (len(order) != len(expected) or set(order) != expected
                or any(not 0 <= index < len(entries) for index in order)):
            raise ValueError(
                f"transfer order does not cover each cold update at frame {frame}")
        normalized_orders.append(order)
    transfer_orders = tuple(normalized_orders)
    sources = [[SOURCE_PRG] * len(entries) for _cells, entries, _colds in per]
    run_map = [_cold_runs(entries, colds) for _cells, entries, colds in per]

    expected_patterns = (
        sum(sum(bool(cold) for cold in colds) for _c, _e, colds in per)
        + sum(sum(bool(item[1]) for item in frame) for frame in prefetch_per)
    )
    if expected_patterns != len(patterns):
        raise ValueError(
            f"cold pattern stream mismatch: updates={expected_patterns} patterns={len(patterns)}")

    frozen_sources = _frozen_sources(log, per) if enabled else None
    if frozen_sources is not None:
        sources = frozen_sources
    elif enabled:
        ranked = _frame_risk(log, frame_count)
        dic_left = int(dic_patterns)
        wr_left = [int(wr_patterns), int(wr_patterns)]

        # Legacy DicBuf can serve either parity, so reserve it for the globally riskiest
        # complete runs before the parity-constrained Word-RAM passes.
        for _neg_miss, _ring, frame in ranked:
            for run in run_map[frame]:
                count = len(run)
                if count <= dic_left:
                    for update_index in run:
                        sources[frame][update_index] = SOURCE_DIC
                    dic_left -= count

        for parity in (0, 1):
            for _neg_miss, _ring, frame in ranked:
                if frame & 1 != parity:
                    continue
                for run in run_map[frame]:
                    if sources[frame][run[0]] != SOURCE_PRG:
                        continue
                    count = len(run)
                    if count <= wr_left[parity]:
                        for update_index in run:
                            sources[frame][update_index] = SOURCE_WR
                        wr_left[parity] -= count

    frozen_supply = log.get("pattern_supply") or {}
    dictionary_mode = int(frozen_supply.get("schema_version", 0)) >= 2
    dictionary = tuple(
        bytes(pattern) for pattern in frozen_supply.get("dic_dictionary", ()))
    if dictionary_mode:
        if len(dictionary) > dic_patterns or len(set(dictionary)) != len(dictionary):
            raise ValueError("invalid DicBuf dictionary size or duplicate entry")
        if any(len(pattern) != PATTERN_BYTES for pattern in dictionary):
            raise ValueError("DicBuf dictionary entry is not 32 bytes")
    dictionary_index = {pattern: index for index, pattern in enumerate(dictionary)}

    prg: list[bytes] = []
    wr0: list[bytes] = []
    wr1: list[bytes] = []
    dic: list[bytes] = list(dictionary)
    prg_loads = np.zeros(frame_count, np.int64)
    wr0_loads = np.zeros(frame_count, np.int64)
    wr1_loads = np.zeros(frame_count, np.int64)
    dic_loads = np.zeros(frame_count, np.int64)
    dic_indices = [[-1] * len(entries) for _cells, entries, _colds in per]
    pattern_index = 0
    def consume_pattern(frame: int, update_index: int | None, source: int) -> None:
        nonlocal pattern_index
        pattern = bytes(patterns[pattern_index])
        pattern_index += 1
        if len(pattern) != PATTERN_BYTES:
            raise ValueError(
                f"pattern is {len(pattern)} bytes, expected {PATTERN_BYTES}")
        if frame == 0 or source == SOURCE_PRG:
            prg.append(pattern)
            prg_loads[frame] += 1
        elif source == SOURCE_WR:
            (wr1 if frame & 1 else wr0).append(pattern)
            (wr1_loads if frame & 1 else wr0_loads)[frame] += 1
        elif source == SOURCE_DIC:
            if dictionary_mode:
                index = dictionary_index.get(pattern)
                if index is None:
                    raise ValueError(
                        f"frame {frame}: Dic source pattern is absent from dictionary")
                if update_index is None:
                    raise AssertionError("prefetch cannot use DicBuf dictionary")
                dic_indices[frame][update_index] = index
            else:
                dic.append(pattern)
            dic_loads[frame] += 1
        else:  # pragma: no cover - guarded by construction
            raise AssertionError(source)

    for frame, ((_cells, _entries, colds), frame_sources, frame_prefetch,
                transfer_order) in enumerate(
            zip(per, sources, prefetch_per, transfer_orders)):
        for update_index in transfer_order:
            if not colds[update_index]:
                raise AssertionError("transfer order selected a reuse update")
            consume_pattern(
                frame, update_index, frame_sources[update_index])
        for item in frame_prefetch:
            if bool(item[1]):
                consume_pattern(frame, None, SOURCE_PRG)

    if len(wr0) > wr_patterns or len(wr1) > wr_patterns or len(dic) > dic_patterns:
        raise AssertionError("pattern supply planner exceeded a physical preload capacity")
    occurrence_count = (
        len(prg) + len(wr0) + len(wr1) + int(dic_loads.sum()))
    if occurrence_count != len(patterns):
        raise AssertionError("pattern supply planner lost or duplicated pattern occurrences")

    return SupplyPlan(
        sources=tuple(tuple(frame) for frame in sources),
        prg_patterns=tuple(prg),
        wr0_patterns=tuple(wr0),
        wr1_patterns=tuple(wr1),
        dic_patterns=tuple(dic),
        prg_loads=prg_loads,
        wr0_loads=wr0_loads,
        wr1_loads=wr1_loads,
        dic_loads=dic_loads,
        dic_indices=tuple(tuple(frame) for frame in dic_indices),
    )
