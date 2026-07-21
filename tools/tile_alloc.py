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

import numpy as np


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
