#!/usr/bin/env python3
"""Legacy TTRC v1 decoder with per-frame snapshot + arbitrary-CRAM render.

Used by the palette-flash detector: at a palette boundary we need to render the
same displayed content with EITHER segment's CRAM to model the "CRAM applied too
early / too late" flash. So the decoder exposes, at any target frame:
  cells : (C,2) int  -> per cell (palrow 0..3, slot)  = the front name table
  pool  : dict slot -> (8,8) uint8 palette-index tile  = VRAM pattern pool state
  cram  : (n_seg,4,16,3) uint8                          = every segment's CRAM
  seg_of: (nframes,) int                                = segment index per frame
Render any (cells, pool) with any segment's CRAM via render().
"""
import struct
from collections import deque
import numpy as np

SECTOR = 2048
PAT = 32
PPS = SECTOR // PAT


def _w2rgb(w):
    return (((w >> 1) & 7) * 255 // 7, ((w >> 5) & 7) * 255 // 7, ((w >> 9) & 7) * 255 // 7)


def load(path):
    d = open(path, "rb").read()
    (mg, ver, nfr, TC, TR, CC, POOL, BASE, FS, NS) = struct.unpack(">4sHHHHHHHHH", d[:22])
    if mg != b"TTRC" or ver != 1:
        raise SystemExit(
            "palette_flash/decode.py supports only the legacy monolithic TTRC v1 "
            f"layout, not {mg!r} v{ver}; v6/v7 use split files, PALTAB, frame-0 "
            "boot data, control-first BODY slots, and rate-matched frame sizes")
    Bpat, RSEC, PSEC, RPEAK = struct.unpack(">LLLL", d[22:38])
    return dict(d=d, mg=mg, nfr=nfr, TC=TC, TR=TR, CC=CC, POOL=POOL, BASE=BASE,
                FS=FS, NS=NS, Bpat=Bpat, RSEC=RSEC, PSEC=PSEC, BMB=(CC + 7) // 8)


class Decoder:
    """Streams frames, snapshotting (cells, pool, seg) at requested frame indices."""

    def __init__(self, hdr):
        self.h = hdr
        d = hdr["d"]
        sec = lambda i: d[i * SECTOR:(i + 1) * SECTOR]
        self.sec = sec
        self.routing = b"".join(sec(1 + j) for j in range(hdr["RSEC"]))
        self.fstart = 1 + hdr["RSEC"] + hdr["PSEC"]
        self.ring = deque()
        pre = b"".join(sec(1 + hdr["RSEC"] + j) for j in range(hdr["PSEC"]))
        for k in range(hdr["Bpat"]):
            self.ring.append(pre[k * PAT:(k + 1) * PAT])
        self.ab = bytearray()
        self.pool = {}                       # slot -> (8,8) idx tile
        self.cells = np.zeros((hdr["CC"], 2), np.int32)   # (palrow, slot)
        self.crams = []                      # list of (4,16,3)
        self.cur_cram = np.zeros((4, 16, 3), np.uint8)
        self.seg = -1

    def _decode_pattern(self, patbytes):
        a = np.frombuffer(patbytes, np.uint8)
        idx = np.zeros(64, np.uint8)
        idx[0::2] = a >> 4
        idx[1::2] = a & 0xF
        return idx.reshape(8, 8)

    def step(self, i):
        h = self.h
        bs = self.fstart + i * h["FS"]
        npay = self.routing[2 * i]
        nctrl = self.routing[2 * i + 1]
        for k in range(h["FS"]):
            s = self.sec(bs + k)
            if k < npay:
                for p in range(PPS):
                    self.ring.append(s[p * PAT:(p + 1) * PAT])
            elif k < npay + nctrl:
                self.ab += s
        total = struct.unpack(">H", self.ab[:2])[0]
        blk = bytes(self.ab[:total])
        del self.ab[:total]
        p = 2
        nu = struct.unpack(">H", blk[p:p + 2])[0]; p += 2
        pal = blk[p]; dbg = blk[p + 1]; p += 2
        if dbg:
            p += 22
        if pal:
            cram = np.zeros((4, 16, 3), np.uint8)
            for pr in range(4):
                for c in range(16):
                    w = struct.unpack(">H", blk[p:p + 2])[0]; p += 2
                    cram[pr, c] = _w2rgb(w)
            self.cur_cram = cram
            self.crams.append(cram)
            self.seg = len(self.crams) - 1
        bm = blk[p:p + h["BMB"]]; p += h["BMB"]
        BASE = h["BASE"]
        for c in range(h["CC"]):
            if not (bm[c >> 3] & (1 << (c & 7))):
                continue
            e = struct.unpack(">H", blk[p:p + 2])[0]; p += 2
            palrow = ((e & 0x7FFF) >> 13) & 3
            slot = ((e & 0x7FFF) & 0x7FF) - BASE
            if e >> 15:
                self.pool[slot] = self._decode_pattern(self.ring.popleft())
            self.cells[c] = (palrow, slot)

    def snapshot(self):
        return (self.cells.copy(), dict(self.pool), self.seg, self.cur_cram.copy())


def render(hdr, cells, pool, cram):
    """cells (C,2)=(palrow,slot), pool slot->(8,8)idx, cram (4,16,3) -> RGB (TR*8, TC*8, 3)."""
    TC, TR, CC = hdr["TC"], hdr["TR"], hdr["CC"]
    img = np.zeros((CC, 8, 8, 3), np.uint8)
    for c in range(CC):
        palrow, slot = int(cells[c, 0]), int(cells[c, 1])
        tile = pool.get(slot)
        if tile is None:
            continue
        img[c] = cram[palrow, tile]
    return img.reshape(TR, TC, 8, 8, 3).transpose(0, 2, 1, 3, 4).reshape(TR * 8, TC * 8, 3)


def palette_boundaries(hdr):
    """Frames that carry a fresh CRAM (segment change)."""
    dec = Decoder(hdr)
    bounds = []
    d = hdr["d"]
    ab = bytearray()
    routing = dec.routing
    for i in range(hdr["nfr"]):
        bs = dec.fstart + i * hdr["FS"]
        npay = routing[2 * i]; nctrl = routing[2 * i + 1]
        for k in range(hdr["FS"]):
            s = dec.sec(bs + k)
            if npay <= k < npay + nctrl:
                ab += s
        total = struct.unpack(">H", ab[:2])[0]
        if ab[4]:
            bounds.append(i)
        del ab[:total]
    return bounds
