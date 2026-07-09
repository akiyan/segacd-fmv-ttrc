#!/usr/bin/env python3
"""Phase A: 実機描画土台の検証用に、1フレームの 256x144 静止画データを出力する。

出力 still256.bin (実機Mainが読む):
  [0]      tiles     : 576 tiles * 32B = 18432B (VDPタイル順, 4bpp, index1..15)
  [18432]  nametable : 576 entries * 2B  = 1152B (big-endian, (pal<<13)|(1+cell))
  [19584]  palettes  : 4 * 16 * 2B = 128B (CRAMワード, palettes.bin そのまま)
dedup無し=各セル固有タイル(tile index=1+cell)。ネームテーブルは連番+パレットビットのみ。
確認用に still256_expected.png も出力する。
"""
import sys
from pathlib import Path
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cbr_paths import sim_work_dir
import sim as sim
from quantize_md_video import MD_LEVELS, rgb333_to_rgb888

D = sim_work_dir()
FRAME = int(sys.argv[1]) if len(sys.argv) > 1 else 900
TCOLS, TROWS = 32, 18
CELLS = TCOLS * TROWS


def main():
    data = np.frombuffer((D / "palettes.bin").read_bytes(), ">u2").reshape(4, 16)
    pals = [np.array([[(int(data[p, k]) >> 1) & 7, (int(data[p, k]) >> 5) & 7,
                       (int(data[p, k]) >> 9) & 7] for k in range(1, 16)], np.uint8)
            for p in range(4)]
    pals_arr = np.stack(pals).astype(np.uint8)

    m333 = sim.rgb888_to_rgb333(np.asarray(Image.open(D / "master" / f"{FRAME:05d}.png").convert("RGB")))
    tiles = sim.tile_blocks(m333)                       # (576,64,3)
    assign = sim.assign_palette(tiles, pals_arr)        # (576,)
    idx = sim.idx_for(tiles, assign, pals_arr)          # (576,64) 1..15

    # tiles: 各セルを 4bpp 8x8 にパック(VDPタイル順=セル連番)
    tile_bytes = bytearray()
    for c in range(CELLS):
        row = idx[c].reshape(8, 8)
        for r in range(8):
            for x in range(0, 8, 2):
                tile_bytes.append((int(row[r, x]) << 4) | int(row[r, x + 1]))
    # nametable: (pal<<13)|(1+cell)
    nt = bytearray()
    for c in range(CELLS):
        entry = (int(assign[c]) << 13) | (1 + c)
        nt += int(entry).to_bytes(2, "big")
    pal = (D / "palettes.bin").read_bytes()

    out = D / "still256.bin"
    out.write_bytes(bytes(tile_bytes) + bytes(nt) + pal)
    print(f"wrote {out}: tiles={len(tile_bytes)} nt={len(nt)} pal={len(pal)} total={len(tile_bytes)+len(nt)+len(pal)}")

    # expected preview
    full16 = np.zeros((4, 16, 3), np.uint8)
    full16[:, 1:] = pals_arr
    rgb333 = full16[assign[:, None], idx].reshape(TROWS, TCOLS, 8, 8, 3).transpose(0, 2, 1, 3, 4).reshape(144, 256, 3)
    Image.fromarray(rgb333_to_rgb888(rgb333), "RGB").save(D / "still256_expected.png")
    print("wrote", D / "still256_expected.png")


if __name__ == "__main__":
    main()
