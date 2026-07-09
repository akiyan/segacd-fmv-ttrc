#!/usr/bin/env python3
"""現在のパレット状態パネル(palstate/%05d.png)を生成する。右下の空き領域に置く。
4行(PL0..PL3=CRAMの4面) × 3列(直前 / 現在 / 次 の区間パレット)。各列見出しに切替時刻(mm:ss)。
現在列を黄枠で強調。暗転で区間別パレットが差し替わる様子が分かる。
区間分割/パレット学習は sim と同じ segment_and_train を使う(env CBRSIM_DITHER/SEGPAL を合わせる)。"""
import os
import sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sim import (segment_and_train, to_rgb333, flatten_low_detail,
                           assign_palette, idx_for, FPS)
from quantize_global4_tiles import tile_blocks
from quantize_md_video import rgb333_to_rgb888

OUT = Path(os.environ.get("CBRSIM_OUT", "tmp/sim"))
FONT = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
DST = OUT / "palstate"

# 幅は上パネル(Source/cat/miss)の枠に合わせる: 左枠線x=1308〜右枠線x=1877=569px。
# compose で x=1308 に overlay する前提。PL見出しの左端=左枠線、グリッド右端=右枠線に一致。
PSW, PSH = 569, 76
LBLW, HDR, NCOL = 38, 16, 3
COLW = (PSW - LBLW) // NCOL           # 177
ROWS = 4
ROWH = (PSH - HDR) // ROWS            # 15


def mmss(fr):
    t = fr / FPS
    return "%d:%02d" % (int(t // 60), int(t % 60))


def main():
    master = sorted((OUT / "master").glob("*.png"))
    n = len(master)
    pals_arr, seg_pals, frame_seg, seg_bounds = segment_and_train(master)
    nseg = len(seg_pals)
    seg_start = [int(np.argmax(frame_seg == s)) for s in range(nseg)]   # 各区間の開始フレーム
    seg_rgb = [rgb333_to_rgb888(sp) for sp in seg_pals]                 # [(4,15,3)]
    DST.mkdir(parents=True, exist_ok=True)
    for c in DST.glob("*.png"):
        c.unlink()
    f_hdr = ImageFont.truetype(FONT, 13)
    f_lbl = ImageFont.truetype(FONT, 13)
    labels = ["Prev", "Current", "Next"]

    def draw_col(d, sc, x0, lab):
        rgb = seg_rgb[sc]
        d.text((x0 + 3, 1), "%s %s" % (lab, mmss(seg_start[sc])), fill=(215, 215, 220), font=f_hdr)
        sw = COLW / 15.0
        for p in range(4):
            for k in range(15):
                x = x0 + int(k * sw); yy = HDR + p * ROWH
                d.rectangle([x, yy, x0 + int((k + 1) * sw) - 1, yy + ROWH - 2],
                            fill=tuple(int(v) for v in rgb[p, k]))

    for f in range(n):
        s = int(frame_seg[f])
        im = Image.new("RGB", (PSW, PSH), (20, 20, 22))
        d = ImageDraw.Draw(im)
        for p in range(4):
            d.text((1, HDR + p * ROWH + 1), "PL%d" % p, fill=(200, 200, 205), font=f_lbl)
        for ci, sc in enumerate([s - 1, s, s + 1]):
            x0 = LBLW + ci * COLW
            if 0 <= sc < nseg:
                draw_col(d, sc, x0, labels[ci])
            else:
                d.text((x0 + 3, 1), "%s —" % labels[ci], fill=(120, 120, 124), font=f_hdr)
        xc = LBLW + COLW                                # 現在列(中央)を強調
        d.rectangle([xc - 1, 0, xc + COLW - 1, PSH - 1], outline=(255, 235, 120), width=2)
        # 今フレームで実際に使われている「色」を Current 列で 1マスずつ枠付け(cyan)
        m888 = np.asarray(Image.open(master[f]).convert("RGB"))
        flat, _ = flatten_low_detail(tile_blocks(to_rgb333(m888)))
        assign = assign_palette(flat, seg_pals[s])
        idx = idx_for(flat, assign, seg_pals[s])        # (C,64) 各画素の色index 1..15
        sw = COLW / 15.0
        for p in range(4):
            mp = assign == p
            if not mp.any():
                continue
            for t in np.unique(idx[mp]):                # この面で使われた色index → その1マスだけ枠
                k = int(t) - 1                          # swatch列(index1..15 → 0..14)
                x0s = xc + int(k * sw); x1s = xc + int((k + 1) * sw) - 1
                y0s = HDR + p * ROWH; y1s = y0s + ROWH - 2
                # 内側にインセット(隣のマスと離す)=1色づつ見えるように
                d.rectangle([x0s + 1, y0s + 1, x1s - 1, y1s - 1], outline=(0, 255, 255), width=1)
        im.save(DST / f"{f:05d}.png")
    print("wrote", n, "palstate frames to", DST, "(", nseg, "segments )")


if __name__ == "__main__":
    main()
