#!/usr/bin/env python3
"""MOVIE.DAT を実機ASM(movieplay_ip.s)と同一手順で忠実デコードし、指定フレーム範囲を
256x144 PNG に出す。実機キャプチャとの差分でハード側バグを切り分けるための参照画像。

usage: python3 tools/decode_dump.py <MOVIE.DAT> <palettes.bin> <out_dir> <start> <end>
"""
import sys, struct
from pathlib import Path
import numpy as np
from PIL import Image
import sim as sim

SECTOR = 2048
POOL_TILE_BASE = 1
TILE = 8
FSEC = 5
TCOLS, TROWS = 32, 18
C_CELLS = TCOLS * TROWS


def cells_to_image(img):  # (576,8,8,3) -> (144,256,3)
    return img.reshape(TROWS, TCOLS, TILE, TILE, 3).transpose(0, 2, 1, 3, 4).reshape(TROWS * TILE, TCOLS * TILE, 3)


def main():
    dat, masterd, outd, s0, s1 = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
    from pack_stream import build_palettes
    masters = sorted(Path(masterd).glob("*.png"))
    pals_arr, _ = build_palettes(masters)          # (4,15,3) RGB333(0..7), col0=black
    full16 = np.zeros((4, 16, 3), np.uint8); full16[:, 1:] = pals_arr
    full = sim.rgb333_to_rgb888(full16.reshape(-1, 1, 3)).reshape(4, 16, 3)
    data = Path(dat).read_bytes()
    # MOVIE.DAT は raw(ヘッダ無し)。1フレーム=FSEC*SECTOR、先頭から。
    cap = FSEC * SECTOR
    frames = len(data) // cap
    tile_store = [None] * 4096
    nt_slot = np.zeros(C_CELLS, np.int64)
    nt_pal = np.zeros(C_CELLS, np.int64)
    outd = Path(outd); outd.mkdir(parents=True, exist_ok=True)
    for i in range(min(s1, frames)):
        fr = data[i * cap:(i + 1) * cap]
        p = 0
        n_load = struct.unpack(">H", fr[p:p + 2])[0]; p += 2
        for _ in range(n_load):
            slot = struct.unpack(">H", fr[p:p + 2])[0]; p += 2
            tile_store[slot] = fr[p:p + 32]; p += 32
        n_upd = struct.unpack(">H", fr[p:p + 2])[0]; p += 2
        for _ in range(n_upd):
            cell, ent = struct.unpack(">HH", fr[p:p + 4]); p += 4
            nt_pal[cell] = (ent >> 13) & 3
            nt_slot[cell] = (ent & 0x07FF) - POOL_TILE_BASE
        if i < s0:
            continue
        img = np.zeros((C_CELLS, TILE, TILE, 3), np.uint8)
        for c in range(C_CELLS):
            pat = tile_store[int(nt_slot[c])] if nt_slot[c] >= 0 else None
            if pat is None:
                continue
            idx = np.zeros(64, np.uint8)
            for y in range(8):
                for x in range(4):
                    b = pat[y * 4 + x]
                    idx[y * 8 + x * 2] = b >> 4
                    idx[y * 8 + x * 2 + 1] = b & 0xF
            img[c] = full[nt_pal[c], idx].reshape(8, 8, 3)
        Image.fromarray(cells_to_image(img), "RGB").save(outd / f"dec_{i:05d}.png")
    print(f"dumped frames {s0}..{s1} to {outd} (total frames={frames})")


if __name__ == "__main__":
    main()
