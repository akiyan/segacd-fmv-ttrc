#!/usr/bin/env python3
"""調査用の俯瞰1枚: 全編カテゴリ・ヒートマップ(Raw/Dedup/Coa/Buf/Miss 積み上げ)+ Bufマップ(貯水池残量)。
OUT/stats.npz と OUT/buffer_remaining.npz から作る。数値だけでなく「どの区間で何が起きたか」を見せる用。
使い方: python3 tools/render_overview.py [出力png] [見出し文字列]
"""
import sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cbr_paths import sim_work_dir  # noqa: E402

OUT = sim_work_dir()
FONT = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
W = 1600                     # 画像幅
HM_H, BUF_H = 340, 150       # ヒートマップ高 / Bufマップ高
PADL, PADR, PADT = 8, 8, 46  # 左右余白・上見出し余白
GAP, AXIS = 22, 22           # マップ間の隙間 / 時間軸の高さ
COL = dict(raw=(205, 205, 205), dedup=(0, 190, 175), coa=(150, 150, 158),
           buf=(175, 120, 235), miss=(220, 70, 70), bufline=(175, 120, 235))
BG = (16, 16, 18)


def mmss(sec):
    return "%d:%02d" % (int(sec // 60), int(sec % 60))


def main():
    out_png = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT / "overview.png"
    title = sys.argv[2] if len(sys.argv) > 2 else ""
    z = np.load(OUT / "stats.npz", allow_pickle=True)
    S = z["stats"]; fps = float(z["fps"]); cells = int(z["cells"])
    idx = {k: i for i, k in enumerate(str(z["cols"]).split())}
    nfr = len(S); dur = nfr / fps
    Raws = S[:, idx["tx"]]; Deds = S[:, idx["dedup"]]
    Coas = S[:, idx["coa"]] if "coa" in idx else np.zeros(nfr)
    Miss = S[:, idx["miss"]]
    Bufs = np.maximum(S[:, idx["updated"]] - Raws - Deds - Coas, 0)
    buf_rem = buf_total = None
    bpath = OUT / "buffer_remaining.npz"
    if bpath.exists():
        bz = np.load(bpath); buf_rem = bz["remaining"]; buf_total = int(bz["total"])
    have_buf = buf_rem is not None

    plotW = W - PADL - PADR
    H = PADT + HM_H + AXIS + (GAP + BUF_H if have_buf else 0) + 10
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    f_t = ImageFont.truetype(FONT, 20); f_s = ImageFont.truetype(FONT, 14); f_a = ImageFont.truetype(FONT, 13)

    # 見出し + 凡例
    d.text((PADL, 8), title or "コーデック俯瞰", fill=(235, 235, 235), font=f_t)
    lx = PADL; ly = 32
    for name, key in (("Raw", "raw"), ("Dedup", "dedup"), ("Coa", "coa"), ("Buf", "buf"), ("Miss", "miss")):
        d.rectangle([lx, ly, lx + 11, ly + 11], fill=COL[key]); lx += 16
        d.text((lx, ly - 2), name, fill=(210, 210, 210), font=f_s); lx += int(f_s.getbbox(name)[2]) + 16

    # ヒートマップ(積み上げ, 下から Raw/Dedup/Coa/Buf/Miss)。縦=1フレームのタイル数(0..cells)
    top = PADT
    hm = np.zeros((HM_H, plotW, 3), np.uint8)
    for xx in range(plotW):
        fi = min(int(xx / plotW * nfr), nfr - 1)
        yb = HM_H
        for val, col in ((Raws[fi], COL["raw"]), (Deds[fi], COL["dedup"]), (Coas[fi], COL["coa"]),
                         (Bufs[fi], COL["buf"]), (Miss[fi], COL["miss"])):
            h = int(HM_H * val / cells)
            if h > 0:
                hm[max(0, yb - h):yb, xx] = col; yb -= h
    im.paste(Image.fromarray(hm, "RGB"), (PADL, top))
    d.rectangle([PADL, top, PADL + plotW - 1, top + HM_H - 1], outline=(70, 70, 74))
    d.text((PADL + 3, top + 2), "更新タイル/枠 (積み上げ)", fill=(150, 150, 155), font=f_a)

    # 時間軸
    ay = top + HM_H + 2
    for k in range(0, int(dur) + 1, 30):
        xx = PADL + int(k / dur * plotW)
        d.line([xx, ay, xx, ay + 5], fill=(120, 120, 124))
        d.text((xx + 2, ay + 4), mmss(k), fill=(150, 150, 155), font=f_a)

    # Bufマップ(貯水池残量, violet, 下から)
    if have_buf:
        bt = ay + AXIS + GAP
        bm = np.zeros((BUF_H, plotW, 3), np.uint8)
        for xx in range(plotW):
            fi = min(int(xx / plotW * nfr), nfr - 1)
            h = int(BUF_H * int(buf_rem[fi]) / max(buf_total, 1))
            if h > 0:
                bm[BUF_H - h:BUF_H, xx] = COL["bufline"]
        im.paste(Image.fromarray(bm, "RGB"), (PADL, bt))
        d.rectangle([PADL, bt, PADL + plotW - 1, bt + BUF_H - 1], outline=(70, 70, 74))
        d.text((PADL + 3, bt + 2), "Buf(貯水池)残量 満%d→" % buf_total, fill=(150, 150, 155), font=f_a)

    im.save(out_png)
    print("wrote", out_png, "(%d frames, %.0fs)" % (nfr, dur))


if __name__ == "__main__":
    main()
