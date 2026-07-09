#!/usr/bin/env python3
"""MOVIE.DAT を忠実デコードし、各フレームの平均輝度と『純白セル数』を出す。
白フラッシュ(高輝度フレーム)と、白ブロック(白セルが多いフレーム)を特定する。"""
import sys, struct
from pathlib import Path
import numpy as np
import sim as sim
from pack_stream import build_palettes

SECTOR = 2048; POOL_TILE_BASE = 1; TILE = 8; FSEC = 5
TCOLS, TROWS = 32, 18; C_CELLS = TCOLS * TROWS

dat = sys.argv[1]; masterd = sys.argv[2]
masters = sorted(Path(masterd).glob("*.png"))
pals_arr, _ = build_palettes(masters)
full16 = np.zeros((4, 16, 3), np.uint8); full16[:, 1:] = pals_arr
full = sim.rgb333_to_rgb888(full16.reshape(-1, 1, 3)).reshape(4, 16, 3)
lum = full @ np.array([0.299, 0.587, 0.114])          # (4,16) per (pal,idx) 輝度

data = Path(dat).read_bytes()
cap = FSEC * SECTOR; frames = len(data) // cap
tile_store = [None] * 4096
nt_slot = np.zeros(C_CELLS, np.int64); nt_pal = np.zeros(C_CELLS, np.int64)
# 各slotの平均輝度(その中身タイルの)。tile_store更新時に再計算。
slot_lum = np.zeros(4096)
rows = []
for i in range(frames):
    fr = data[i * cap:(i + 1) * cap]; p = 0
    nl = struct.unpack(">H", fr[p:p + 2])[0]; p += 2
    for _ in range(nl):
        slot = struct.unpack(">H", fr[p:p + 2])[0]; p += 2
        pat = fr[p:p + 32]; p += 32
        tile_store[slot] = pat
        idx = np.frombuffer(pat, np.uint8)
        hi = idx >> 4; lo = idx & 0xF
        slot_lum[slot] = 0  # palごとに違うが概算: idxの平均を後でpalで引く。ここはidx平均輝度(pal0)で近似
    nu = struct.unpack(">H", fr[p:p + 2])[0]; p += 2
    for _ in range(nu):
        cell, ent = struct.unpack(">HH", fr[p:p + 4]); p += 4
        nt_pal[cell] = (ent >> 13) & 3
        nt_slot[cell] = (ent & 0x07FF) - POOL_TILE_BASE
    # 各セルの平均輝度を計算(pal, タイルidx)
    cell_lum = np.zeros(C_CELLS)
    whitecells = 0
    for c in range(C_CELLS):
        s = int(nt_slot[c])
        pat = tile_store[s] if s >= 0 else None
        if pat is None:
            continue
        idx = np.frombuffer(pat, np.uint8)
        allidx = np.empty(64, np.uint8)
        allidx[0::2] = idx >> 4; allidx[1::2] = idx & 0xF
        L = lum[nt_pal[c], allidx]
        cell_lum[c] = L.mean()
        if L.min() > 200:      # タイル全体がほぼ白
            whitecells += 1
    rows.append((i, cell_lum.mean(), whitecells))

rows = np.array(rows)
print("=== 高輝度(白フラッシュ)上位 ===")
for r in rows[np.argsort(-rows[:, 1])[:8]]:
    print(f"frame {int(r[0]):4d} meanL={r[1]:.0f} whitecells={int(r[2])}")
print("=== 白セル数 上位 ===")
for r in rows[np.argsort(-rows[:, 2])[:12]]:
    print(f"frame {int(r[0]):4d} meanL={r[1]:.0f} whitecells={int(r[2])}")
