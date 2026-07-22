"""Shared VRAM tile-slot allocator — the SINGLE source of the cold/reuse decision.

Both the encoder (``tools/sim.py``) and the packer (``tools/pack_stream.py``) must
agree on *which* tiles are resident in VRAM each frame, because that is what decides
"cold" (a fresh 32-byte pattern load) vs "reuse" (a name-table pointer to a resident
slot). Historically they each ran their own residency model — the sim an LRU dict, the
pack a contiguous clock-hand allocator — so the pack "realized" a few more cold loads
than the sim modelled (e.g. sim cap 350 -> pack realized 357). Even with matching
*policy names* the two implementations diverged (contig +7, lru +5), because a
re-derived allocation is never bit-identical.

The fix: one allocator, imported by both. When the sim caps its per-frame cold at
``cold_cap_for_fps`` using THIS allocator, and the pack replays the SAME allocator on
the SAME per-frame update order, the pack's realized cold equals the sim's cap by
construction. So ``COLD_CAP_REALIZED`` collapses into the single ``cold_cap_for_fps``.

Policy: **contiguous** (a clock hand walks the slot ring), which keeps cold tiles in
neighbouring VRAM slots so the Main CPU can DMA them in long runs. Displayed tiles
(referenced by a cell this or last frame) are protected from eviction.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SlotLocalityPlan:
    """One movie-wide bijection from logical to physical VRAM slots.

    The logical allocator remains the only source of residency and cold/reuse
    decisions.  This plan changes only the physical slot number written to the
    stream, so a cache hit stays a cache hit and every displayed pattern is
    unchanged.  Cold payloads are emitted in physical-slot order afterwards.
    """

    physical_by_logical: tuple[int, ...]
    baseline_runs: np.ndarray
    optimized_runs: np.ndarray
    cold: np.ndarray
    risk_frames: np.ndarray


def validate_physical_slots(physical_by_logical, pool):
    """Return a validated logical->physical slot permutation."""
    mapping = np.asarray(physical_by_logical, dtype=np.int64)
    pool = int(pool)
    if mapping.shape != (pool,):
        raise ValueError(
            f"physical slot map has shape {mapping.shape}, expected {(pool,)}")
    if not np.array_equal(np.sort(mapping), np.arange(pool, dtype=np.int64)):
        raise ValueError("physical slot map is not a complete bijection")
    return mapping


def remap_placements(placements, physical_by_logical):
    """Map update-aligned ``(logical_slot, cold)`` pairs to physical slots."""
    mapping = validate_physical_slots(
        physical_by_logical, len(physical_by_logical))
    return [
        (int(mapping[int(slot)]), bool(cold))
        for slot, cold in placements
    ]


def cold_transfer_order(placements):
    """Return cold update indices sorted by ascending physical VRAM slot.

    Name-table updates remain in cell order.  The v12 run suffix already
    separates the transfer list from those updates, so payload order is free to
    follow physical slots and form the longest possible runs for a fixed slot
    set.
    """
    return tuple(sorted(
        (index for index, (_slot, cold) in enumerate(placements) if cold),
        key=lambda index: (int(placements[index][0]), index),
    ))


def _run_counts(membership, logical_at_physical):
    """Count contiguous true ranges after applying one physical slot order."""
    membership = np.asarray(membership, dtype=bool)
    logical_at_physical = np.asarray(logical_at_physical, dtype=np.int64)
    if not len(logical_at_physical):
        return np.zeros(len(membership), np.int32)
    ordered = membership[:, logical_at_physical]
    return (
        ordered[:, 0].astype(np.int32)
        + np.count_nonzero(ordered[:, 1:] & ~ordered[:, :-1], axis=1)
    )


def _maximum_weight_slot_path(weight):
    """Build a deterministic high-weight Hamiltonian path.

    Each selected edge says that two logical slots should become physical
    neighbours.  A maximum-weight degree-2 spanning forest is inexpensive for
    the 1,518-slot H40 pool and yields a single path without a SciPy dependency.
    """
    weight = np.asarray(weight, dtype=np.float64)
    if weight.ndim != 2 or weight.shape[0] != weight.shape[1]:
        raise ValueError("slot adjacency weight must be square")
    count = int(weight.shape[0])
    if count <= 1:
        return np.arange(count, dtype=np.int32)

    rows, columns = np.triu_indices(count, 1)
    values = weight[rows, columns]
    edge_order = np.argsort(values, kind="stable")[::-1]
    parent = np.arange(count, dtype=np.int32)
    component_size = np.ones(count, dtype=np.int32)
    degree = np.zeros(count, dtype=np.uint8)
    links = [[] for _ in range(count)]

    def root(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = int(parent[value])
        return value

    selected = 0
    for edge in edge_order:
        left = int(rows[edge])
        right = int(columns[edge])
        if degree[left] >= 2 or degree[right] >= 2:
            continue
        left_root = root(left)
        right_root = root(right)
        if left_root == right_root:
            continue
        links[left].append(right)
        links[right].append(left)
        degree[left] += 1
        degree[right] += 1
        if component_size[left_root] < component_size[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        component_size[left_root] += component_size[right_root]
        selected += 1
        if selected == count - 1:
            break
    if selected != count - 1:
        raise AssertionError("slot-locality path did not span the VRAM pool")

    endpoints = np.flatnonzero(degree == 1)
    if len(endpoints) != 2:
        raise AssertionError("slot-locality path has invalid endpoint count")
    path = []
    previous = -1
    current = int(endpoints[0])
    while True:
        path.append(current)
        following = [node for node in links[current] if node != previous]
        if not following:
            break
        previous, current = current, following[0]
    if len(path) != count:
        raise AssertionError("slot-locality path is disconnected")
    return np.asarray(path, dtype=np.int32)


def optimize_slot_locality(
        cold_slots_by_frame, pool, *, cold_cap=0, iterations=20,
        target_heavy_runs=30):
    """Choose one movie-wide slot permutation for deadline-heavy frames.

    The objective deliberately does *not* minimize total runs.  It estimates
    the Main transfer deadline as ``0.7*cold + 9.5*runs`` and repeatedly adds
    weight to the worst frames.  Frames near the measured cold cap that are
    already fragmented receive an explicit target of at most 30 runs.  Run
    increases on light frames are accepted when they lower the worst deadline.
    """
    pool = int(pool)
    if pool <= 0:
        raise ValueError("VRAM pool must be positive")
    if iterations <= 0:
        raise ValueError("slot-locality iterations must be positive")
    frame_count = len(cold_slots_by_frame)
    membership = np.zeros((frame_count, pool), dtype=bool)
    for frame, raw_slots in enumerate(cold_slots_by_frame):
        slots = np.asarray(tuple(int(slot) for slot in raw_slots), np.int64)
        if slots.size:
            if int(slots.min()) < 0 or int(slots.max()) >= pool:
                raise ValueError(f"frame {frame} has a cold slot outside the pool")
            if len(np.unique(slots)) != len(slots):
                raise ValueError(f"frame {frame} repeats a cold slot")
            membership[frame, slots] = True

    identity = np.arange(pool, dtype=np.int32)
    cold = membership.sum(axis=1, dtype=np.int32)
    baseline_runs = _run_counts(membership, identity)
    if frame_count <= 1 or not membership[1:].any():
        return SlotLocalityPlan(
            tuple(int(slot) for slot in identity), baseline_runs,
            baseline_runs.copy(), cold, np.zeros(frame_count, bool))

    measured_cap = int(cold_cap) or int(cold[1:].max(initial=0))
    risk_cold = max(1, int(np.ceil(measured_cap * 0.85)))
    risk_frames = (
        (cold >= risk_cold)
        & (baseline_runs >= 40)
    )
    risk_frames[0] = False

    # Cost units are the empirical p76 transfer model's stopwatch ticks.
    baseline_score = 0.7 * cold + 9.5 * baseline_runs
    weights = np.full(frame_count, 0.001, np.float64)
    weights += (np.maximum(baseline_score - 300.0, 0.0) / 100.0) ** 2
    weights[0] = 0.0
    weights[risk_frames] += 25.0
    if not np.any(weights > 0.0010001):
        return SlotLocalityPlan(
            tuple(int(slot) for slot in identity), baseline_runs,
            baseline_runs.copy(), cold, risk_frames)

    logical_at_physical = identity.copy()
    baseline_risk_max = int(baseline_runs[risk_frames].max(initial=0))
    baseline_streaming_score = baseline_score[1:]
    best = ((
        max(0, baseline_risk_max - int(target_heavy_runs)),
        float(baseline_streaming_score.max(initial=0.0)),
        float(np.percentile(baseline_streaming_score, 99)),
    ), identity.copy(), baseline_runs.copy())
    for _iteration in range(int(iterations)):
        selected_frames = weights > 0.0010001
        adjacency = np.zeros((pool, pool), dtype=np.float64)
        for present, frame_weight in zip(
                membership[selected_frames], weights[selected_frames]):
            slots = np.flatnonzero(present)
            adjacency[np.ix_(slots, slots)] += float(frame_weight)
        np.fill_diagonal(adjacency, 0.0)
        logical_at_physical = _maximum_weight_slot_path(adjacency)
        runs = _run_counts(membership, logical_at_physical)
        score = 0.7 * cold + 9.5 * runs
        streaming_score = score[1:]
        risk_max = int(runs[risk_frames].max(initial=0))
        objective = (
            max(0, risk_max - int(target_heavy_runs)),
            float(streaming_score.max(initial=0.0)),
            float(np.percentile(streaming_score, 99)),
        )
        if objective < best[0]:
            best = (objective, logical_at_physical.copy(), runs.copy())

        excess = np.maximum(score - 450.0, 0.0)
        weights += (excess / 150.0) ** 2
        weights[0] = 0.0
        if risk_frames.any():
            weights[risk_frames] += (
                np.maximum(runs[risk_frames] - 25, 0) * 0.5)

    _objective, logical_at_physical, optimized_runs = best
    physical_by_logical = np.empty(pool, dtype=np.int32)
    physical_by_logical[logical_at_physical] = np.arange(
        pool, dtype=np.int32)
    validate_physical_slots(physical_by_logical, pool)
    return SlotLocalityPlan(
        tuple(int(slot) for slot in physical_by_logical),
        baseline_runs,
        optimized_runs,
        cold,
        risk_frames,
    )


def verify_display_equivalence(
        frames, c_cells, pool, physical_by_logical, *, base=1):
    """Replay every decision and prove the physical permutation is invisible.

    ``frames`` contains update-aligned ``(cell, key)`` pairs.  The verifier
    models physical VRAM contents, cold transfers in their reordered payload
    sequence, and the cell name-table pointers.  It raises at the first frame
    where a displayed cell would read any pattern other than the frozen
    decision key.
    """
    mapping = validate_physical_slots(physical_by_logical, pool)
    allocator = TileAllocator(c_cells, pool, base)
    physical_patterns = [None] * int(pool)
    displayed_slots = np.full(int(c_cells), -1, np.int64)
    expected_patterns = [None] * int(c_cells)
    cold_total = 0
    run_total = 0

    for frame_index, raw_updates in enumerate(frames):
        updates = [(int(cell), key) for cell, key in raw_updates]
        logical = allocator.place_frame(updates, frame_index)
        physical = remap_placements(logical, mapping)
        order = cold_transfer_order(physical)
        cold_total += len(order)
        run_total += count_slot_runs(
            physical[index][0] for index in order)

        for update_index in order:
            slot, cold = physical[update_index]
            if not cold:
                raise AssertionError("transfer order contains a reuse update")
            physical_patterns[slot] = updates[update_index][1]
        for (cell, key), (slot, _cold) in zip(updates, physical):
            displayed_slots[cell] = slot
            expected_patterns[cell] = key

        for cell, expected in enumerate(expected_patterns):
            if expected is None:
                continue
            slot = int(displayed_slots[cell])
            actual = physical_patterns[slot]
            if actual != expected:
                raise AssertionError(
                    f"frame {frame_index} cell {cell}: physical slot {slot} "
                    "does not contain the frozen display pattern")

    return {
        "frames": len(frames),
        "cold": int(cold_total),
        "runs": int(run_total),
        "tearing": int(allocator.tearing),
    }


def slot_runs(slots):
    """Return ascending consecutive VRAM-slot runs in payload order.

    Each result is ``(slot_start, tile_count)``.  The caller supplies cold slots
    only, so reuse entries do not split a run.  A pool wrap is not consecutive:
    ``pool - 1`` followed by ``0`` starts a new run, just like the player.
    """
    runs = []
    for slot in slots:
        slot = int(slot)
        if runs and runs[-1][0] + runs[-1][1] == slot:
            start, count = runs[-1]
            runs[-1] = (start, count + 1)
        else:
            runs.append((slot, 1))
    return runs


def count_slot_runs(slots):
    """Count the packed/player cold-run records for a cold-slot sequence."""
    return len(slot_runs(slots))


class TileAllocator:
    """Slot residency for one stream. Feed it each frame's updated cells in a fixed
    order; it assigns a VRAM slot per tile key and reports cold (new load) vs reuse.

    ``c_cells`` = grid cell count, ``pool`` = resident VRAM slot count, ``base`` =
    ``POOL_TILE_BASE`` (VRAM tile index of slot s = base + s).
    """

    def __init__(self, c_cells, pool, base=1):
        self.C_CELLS = int(c_cells)
        self.POOL = int(pool)
        self.BASE = int(base)
        self.key_slot = {}                                   # tile key -> slot
        self.slot_key = [None] * self.POOL                   # slot -> tile key
        self.slot_refs = np.zeros(self.POOL, np.int32)       # cells currently showing slot
        self.slot_lastuse = np.full(self.POOL, -1, np.int64)
        self.free = list(range(self.POOL - 1, -1, -1))       # so pop() hands out 0,1,2… ascending
        self.hand = 0                                        # clock hand (contig)
        self.tearing = 0                                     # evictions forced past protection
        self.cur_slot = np.full(self.C_CELLS, -1, np.int64)  # cell -> slot it currently shows
        self.prev_slot = np.full(self.C_CELLS, -1, np.int64) # cell -> slot last frame (protect)
        self._prev_protect = np.zeros(self.POOL, bool)
        self._tfp = None                                     # this-frame reuse-tile protection
        # A raw-prefetched pattern has no cell reference yet.  Keep its slot
        # until the first planned use so the ordinary clock hand does not
        # immediately recycle it.  The feature is inert when every value is -1.
        self.slot_pin_until = np.full(self.POOL, -1, np.int64)
        self.pinned_count = 0
        self.prefetch_evictions = 0
        self.prefetch_cache_evictions = 0

    # ---- residency query (used by the sim for cold/reuse + near/coa reuse) ----
    def is_resident(self, key):
        return key in self.key_slot

    def resident_keys(self):
        return self.key_slot.keys()

    def is_pinned(self, key, deadline):
        """Return whether ``key`` is protected through ``deadline``."""
        slot = self.key_slot.get(key)
        return slot is not None and self.slot_pin_until[slot] >= int(deadline)

    # ---- per-frame ----
    def begin_frame(self):
        """Compute which slots are protected (a cell showed them last frame)."""
        self._prev_protect[:] = False
        ps = self.prev_slot[self.prev_slot >= 0]
        self._prev_protect[ps] = True

    def _evict(self, s):
        k = self.slot_key[s]
        if k is not None:
            self.key_slot.pop(k, None)
            self.slot_key[s] = None
        if self.slot_pin_until[s] >= 0:
            self.pinned_count -= 1
        self.slot_pin_until[s] = -1

    def _alloc_slot_contig(self, frame_idx):
        # Clock hand: free slot first. A visible update then reclaims a
        # speculative prefetch before evicting any ordinary resident cache
        # entry, so speculation cannot make the baseline cache smaller.
        if self.free:
            return self.free.pop()
        if self.pinned_count:
            for _ in range(self.POOL):
                s = self.hand
                self.hand = (self.hand + 1) % self.POOL
                if self.slot_refs[s] == 0 and not self._prev_protect[s] and \
                   (self._tfp is None or not self._tfp[s]) and \
                   self.slot_pin_until[s] >= frame_idx:
                    self.prefetch_evictions += 1
                    self._evict(s)
                    return s
        for _ in range(self.POOL):
            s = self.hand
            self.hand = (self.hand + 1) % self.POOL
            if self.slot_refs[s] == 0 and not self._prev_protect[s] and \
               (self._tfp is None or not self._tfp[s]):
                self._evict(s)
                return s
        self.tearing += 1
        for _ in range(self.POOL):
            s = self.hand
            self.hand = (self.hand + 1) % self.POOL
            if self.slot_refs[s] == 0:
                self._evict(s)
                return s
        s = int(np.argmin(self.slot_lastuse))
        self._evict(s)
        return s

    def place(self, cell, key, frame_idx):
        """Ensure ``key`` has a slot; record that ``cell`` now shows it.
        Returns ``(slot, cold)`` where cold=True means a fresh pattern load."""
        cold = key not in self.key_slot
        if cold:
            slot = self._alloc_slot_contig(frame_idx)
            self.key_slot[key] = slot
            self.slot_key[slot] = key
        else:
            slot = self.key_slot[key]
            # The prefetched pattern has reached a real display use.  Ordinary
            # current/previous-frame reference protection takes over now.
            if self.slot_pin_until[slot] >= 0:
                self.pinned_count -= 1
            self.slot_pin_until[slot] = -1
        oldc = self.cur_slot[cell]
        if oldc >= 0:
            self.slot_refs[oldc] -= 1
        self.slot_refs[slot] += 1
        self.slot_lastuse[slot] = frame_idx
        self.cur_slot[cell] = slot
        return int(slot), bool(cold)

    def prefetch(
            self, key, frame_idx, deadline, forced_slot=None, avoid_keys=()):
        """Place one future pattern without changing any displayed cell.

        Returns ``(slot, cold)``.  ``cold`` is true only when a 32-byte VRAM
        write is required.  ``None`` means no safely evictable unreferenced
        slot exists; speculative work is skipped rather than tearing display.
        """
        frame_idx = int(frame_idx)
        deadline = int(deadline)
        if deadline <= frame_idx:
            raise ValueError("prefetch deadline must be after the load frame")
        resident = self.key_slot.get(key)
        if resident is not None:
            # Already-resident data needs no speculative transfer. Do not pin
            # an ordinary cache entry: changing its eviction priority can make
            # a baseline frame worse without moving any work earlier.
            return int(resident), False

        avoid_keys = set(avoid_keys)
        if forced_slot is not None:
            slot = int(forced_slot)
            if not 0 <= slot < self.POOL:
                raise ValueError("forced prefetch slot is outside the pool")
            if slot in self.free:
                self.free.remove(slot)
            else:
                if self.slot_refs[slot] != 0 or self._prev_protect[slot] or \
                   (self._tfp is not None and self._tfp[slot]):
                    return None
                self._evict(slot)
                self.hand = (slot + 1) % self.POOL
        elif self.free:
            slot = self.free.pop()
        else:
            # A full resident cache may still have a safe speculative victim:
            # it must be unreferenced now and in the previous display, must
            # not be another pending prefetch, and must not be needed by the
            # target frame.  This deliberately gives up only cache history;
            # no displayed pattern is overwritten.
            slot = None
            for _ in range(self.POOL):
                candidate = self.hand
                self.hand = (self.hand + 1) % self.POOL
                candidate_key = self.slot_key[candidate]
                if self.slot_refs[candidate] != 0 or self._prev_protect[candidate]:
                    continue
                if self.slot_pin_until[candidate] >= frame_idx:
                    continue
                if candidate_key in avoid_keys:
                    continue
                slot = candidate
                self._evict(slot)
                self.prefetch_cache_evictions += 1
                break
            if slot is None:
                return None

        self.key_slot[key] = slot
        self.slot_key[slot] = key
        self.slot_lastuse[slot] = frame_idx
        self.slot_pin_until[slot] = deadline
        self.pinned_count += 1
        return int(slot), True

    def end_frame(self):
        self.prev_slot[:] = self.cur_slot

    def place_frame(self, cells_keys, frame_idx):
        """Two-pass frame allocation (the disc's true behaviour). ``cells_keys`` = a
        LIST of ``(cell, key)`` in cell order (this frame's updated cells). Pass 1
        protects every reuse tile (already resident) so pass 2's cold allocations
        never evict a tile shown this frame -> no intra-frame reload -> realized cold
        equals the fresh-key count (= the sim's cap). Returns ``[(slot, cold), ...]``
        in the given order. There is always room: a frame shows <= C_CELLS distinct
        tiles and the pool is larger."""
        self.begin_frame()
        tfp = np.zeros(self.POOL, bool)
        for (cell, key) in cells_keys:
            s = self.key_slot.get(key)
            if s is not None:
                tfp[s] = True
        self._tfp = tfp
        out = [self.place(cell, key, frame_idx) for (cell, key) in cells_keys]
        self._tfp = None
        self.end_frame()
        return out
