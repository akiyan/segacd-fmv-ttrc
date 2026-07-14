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

    # ---- residency query (used by the sim for cold/reuse + near/coa reuse) ----
    def is_resident(self, key):
        return key in self.key_slot

    def resident_keys(self):
        return self.key_slot.keys()

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

    def _alloc_slot_contig(self):
        # clock hand: first free/unprotected slot with no live reference; else past
        # protection (tearing), else the least-recently-used.
        if self.free:
            return self.free.pop()
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
            slot = self._alloc_slot_contig()
            self.key_slot[key] = slot
            self.slot_key[slot] = key
        else:
            slot = self.key_slot[key]
        oldc = self.cur_slot[cell]
        if oldc >= 0:
            self.slot_refs[oldc] -= 1
        self.slot_refs[slot] += 1
        self.slot_lastuse[slot] = frame_idx
        self.cur_slot[cell] = slot
        return int(slot), bool(cold)

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
