#!/usr/bin/env python3
"""Plane B オーバーレイ方式のオフライン実証実験。

現状のプレーヤーは Plane A 1枚に「タイル毎に4パレットから1つ選択」で動画を描く。
量子化が厳しい(=1セル内で2パレット欲しい)セルへ、Plane B のタイルを優先ビット付きで
重ねる。Plane B タイルは同じCRAM 4パレットのうち1つ(p_ov)を使い、色0=透過なので
画素ごとに「背景(Plane A, p_bg) か オーバーレイ(p_ov) か」を選べる→擬似的に2パレット。

このスクリプトは実機を使わず、既存の量子化出力(pal/ pmap/)と元映像から:
  1) 全フレームの現状量子化誤差を測り「厳しいフレーム」を探す
  2) そのフレームで Plane B が最も効く 24 セルを選ぶ
  3) オリジナル / 現状 / +PlaneB / Plane Bのみ を PNG 比較画像に描く
を行う。誤差は量子化器と同じ RGB333 の絶対値和(0..7/ch)で測る。

使い方:
  python3 tools/planeb_overlay_sim.py --root out/video/061_160x96_perframe4 \
      --src movies/disc1/061.mp4 --cells 24 --out-dir tmp/planeb
"""
import argparse
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quantize_md_video import rgb888_to_rgb333, MD_LEVELS  # noqa: E402

TILE = 8
FONT = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
PAL_COLS = [(255, 80, 80), (80, 200, 255), (120, 255, 120), (255, 220, 80)]  # p_ov 表示色


def load_pal(path):
    """pal/NNNNN.pal (4 lines * 16 BE words) -> (4,16,3) rgb333. word0=透過。"""
    raw = path.read_bytes()
    words = struct.unpack(">%dH" % (len(raw) // 2), raw)
    pal = np.zeros((4, 16, 3), np.int16)
    for line in range(4):
        for c in range(16):
            w = words[line * 16 + c]
            pal[line, c] = ((w >> 1) & 7, (w >> 5) & 7, (w >> 9) & 7)  # r,g,b
    return pal


def cellize(img):
    """(H,W,3) -> (cells, 64, 3) row-major 8x8 cells."""
    h, w, _ = img.shape
    ty, tx = h // TILE, w // TILE
    out = []
    for r in range(ty):
        for c in range(tx):
            out.append(img[r*TILE:(r+1)*TILE, c*TILE:(c+1)*TILE].reshape(64, 3))
    return np.array(out, np.int16), ty, tx


def nearest(px, pal_line):
    """px (N,3) int, pal_line (16,3) int (col0 含む)。色1..15への最近傍 idx と誤差。

    背景/オーバーレイとも色0は使わない(透過/黒)ので 1..15 から選ぶ。"""
    d = np.abs(px[:, None, :] - pal_line[None, 1:, :]).sum(2)  # (N,15)
    j = d.argmin(1)
    return j + 1, d[np.arange(len(px)), j]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="out/video/061_160x96_perframe4")
    ap.add_argument("--src", default="movies/disc1/061.mp4")
    ap.add_argument("--start", default="0")
    ap.add_argument("--duration", default="152.866667")
    ap.add_argument("--fps", default="15")
    ap.add_argument("--crop", default="320:152:0:34")
    ap.add_argument("--w", type=int, default=160)
    ap.add_argument("--h", type=int, default=96)
    ap.add_argument("--cells", type=int, default=24)
    ap.add_argument("--frame", type=int, default=-1, help="固定フレーム(負=自動探索)")
    ap.add_argument("--top", type=int, default=40, help="自動探索の精査候補数")
    ap.add_argument("--scale", type=int, default=6, help="表示拡大率")
    ap.add_argument("--workdir", default="tmp/planeb/frames")
    ap.add_argument("--out-dir", default="tmp/planeb")
    args = ap.parse_args()

    root = Path(args.root)
    work = Path(args.workdir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- 元フレーム抽出(無ければ) ---
    if not work.exists() or not list(work.glob("*.png")):
        work.mkdir(parents=True, exist_ok=True)
        import subprocess
        vf = f"crop={args.crop},scale={args.w}:{args.h}:flags=lanczos,fps={args.fps}"
        print("extracting frames ...")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-ss", args.start, "-t", args.duration, "-i", args.src,
                        "-vf", vf, str(work / "%05d.png")], check=True)
    frames = sorted(work.glob("*.png"))
    print(f"  {len(frames)} frames")

    pmap_dir, pal_dir = root / "pmap", root / "pal"
    n = min(len(frames), len(list(pmap_dir.glob("*.pmap"))))

    def load_frame(i):
        # ffmpeg は 00001 始まり、pal/pmap は 00000 始まり
        rgb = rgb888_to_rgb333(np.asarray(Image.open(frames[i]).convert("RGB"))).astype(np.int16)
        pmap = np.frombuffer((pmap_dir / f"{i:05d}.pmap").read_bytes(), np.uint8)
        pal = load_pal(pal_dir / f"{i:05d}.pal")
        return rgb, pmap, pal

    def analyze(i):
        """戻り: cells(C,64,3), pmap, pal, bg_err_px(C,64), bg_idx(C,64),
        per-cell improvement, best p_ov per cell。"""
        rgb, pmap, pal = load_frame(i)
        cells, ty, tx = cellize(rgb)
        C = cells.shape[0]
        # 全4パレットの per-pixel 最近傍誤差/idx
        err = np.zeros((4, C, 64), np.int32)
        idx = np.zeros((4, C, 64), np.uint8)
        flat = cells.reshape(-1, 3)
        for p in range(4):
            j, d = nearest(flat, pal[p])
            idx[p] = j.reshape(C, 64)
            err[p] = d.reshape(C, 64)
        bg = pmap[:C].astype(int)
        bg_err_px = err[bg, np.arange(C)]          # (C,64)
        bg_idx = idx[bg, np.arange(C)]
        bg_cell = bg_err_px.sum(1)                  # (C,)
        # 各 p_ov について per-pixel min(bg, ov)
        comb = np.minimum(bg_err_px[None], err)     # (4,C,64)
        comb_cell = comb.sum(2)                      # (4,C)
        best_pov = comb_cell.argmin(0)              # (C,)
        new_cell = comb_cell[best_pov, np.arange(C)]
        improve = bg_cell - new_cell
        return dict(rgb=rgb, cells=cells, pmap=pmap[:C], pal=pal, err=err, idx=idx,
                    bg_err_px=bg_err_px, bg_idx=bg_idx, bg_cell=bg_cell,
                    best_pov=best_pov, improve=improve, ty=ty, tx=tx,
                    total_err=int(bg_cell.sum()))

    # --- 厳しいフレーム探索 ---
    if args.frame >= 0:
        target = args.frame
    else:
        print("pass1: 全フレームの現状量子化誤差 ...")
        tot = np.zeros(n, np.int64)
        for i in range(n):
            rgb, pmap, pal = load_frame(i)
            cells, _, _ = cellize(rgb)
            C = cells.shape[0]
            flat = cells.reshape(-1, 3)
            bg = pmap[:C].astype(int)
            # 割当パレットのみで誤差(高速)
            e = 0
            for p in range(4):
                m = bg == p
                if m.any():
                    sub = cells[m].reshape(-1, 3)
                    _, d = nearest(sub, pal[p])
                    e += int(d.sum())
            tot[i] = e
            if i % 300 == 0:
                print(f"  {i}/{n}")
        cand = np.argsort(tot)[::-1][:args.top]
        print(f"  最悪フレーム上位: {cand[:8].tolist()}")
        print("pass2: 候補のオーバーレイ改善量を精査 ...")
        best = None
        for i in cand:
            a = analyze(int(i))
            sel = np.sort(a["improve"])[::-1][:args.cells].sum()
            if best is None or sel > best[1]:
                best = (int(i), float(sel))
        target = best[0]
        print(f"  選定フレーム = {target} (24セル改善量={best[1]:.0f})")

    a = analyze(target)
    C = a["cells"].shape[0]
    ty, tx = a["ty"], a["tx"]
    chosen = np.argsort(a["improve"])[::-1][:args.cells]
    chosen = [c for c in chosen if a["improve"][c] > 0]
    print(f"  使用セル {len(chosen)} 個, p_ov={a['best_pov'][chosen].tolist()}")

    # --- 再構成画像(rgb333 -> rgb888) ---
    def recon_before():
        out = np.zeros((C, 64, 3), np.uint8)
        for p in range(4):
            full = MD_LEVELS[a["pal"][p]]          # (16,3) rgb888
            m = a["pmap"] == p
            if m.any():
                out[m] = full[a["bg_idx"][m]]
        return cells_to_img(out, ty, tx)

    def recon_after(overlay_mask_out=None):
        out = np.zeros((C, 64, 3), np.uint8)
        for p in range(4):
            full = MD_LEVELS[a["pal"][p]]
            m = a["pmap"] == p
            if m.any():
                out[m] = full[a["bg_idx"][m]]
        ov_layer = np.full((C, 64, 3), -1, np.int16)   # -1=透過
        for c in chosen:
            pov = a["best_pov"][c]
            use_ov = a["err"][pov, c] < a["bg_err_px"][c]   # 画素ごとに有利な方
            full = MD_LEVELS[a["pal"][pov]]
            ov_idx = a["idx"][pov, c]
            out[c][use_ov] = full[ov_idx][use_ov]
            ov_layer[c][use_ov] = full[ov_idx][use_ov]
        if overlay_mask_out is not None:
            overlay_mask_out.append(ov_layer)
        return cells_to_img(out, ty, tx)

    def planeb_only():
        # 透過=暗いチェッカー、オーバーレイ画素=その色
        layer = []
        recon_after(layer)
        ov = layer[0]
        img = np.zeros((C, 64, 3), np.uint8)
        # チェッカー背景
        yy, xx = np.meshgrid(np.arange(8), np.arange(8), indexing="ij")
        checker = (((yy // 2 + xx // 2) % 2) * 18 + 28).astype(np.uint8)
        base = np.stack([checker]*3, -1).reshape(64, 3)
        for c in range(C):
            img[c] = base
            m = ov[c, :, 0] >= 0
            img[c][m] = ov[c][m].astype(np.uint8)
        return cells_to_img(img, ty, tx)

    before = recon_before()
    after = recon_after()
    pbonly = planeb_only()
    orig = MD_LEVELS[a["rgb"]]                       # オリジナル(rgb333量子化済み表示)

    # 誤差(平均/画素, rgb333スケール)
    e_before = a["bg_cell"].sum() / (C * 64)
    after_cell = a["bg_cell"].copy().astype(float)
    for c in chosen:
        pov = a["best_pov"][c]
        after_cell[c] = np.minimum(a["bg_err_px"][c], a["err"][pov, c]).sum()
    e_after = after_cell.sum() / (C * 64)

    # --- 描画 ---
    sc = args.scale
    font = ImageFont.truetype(FONT, 22)
    small = ImageFont.truetype(FONT, 15)

    def up(arr):
        return Image.fromarray(arr, "RGB").resize((tx*TILE*sc, ty*TILE*sc), Image.NEAREST)

    def grid_boxes(im, cells_list, color, labels=None):
        d = ImageDraw.Draw(im)
        for k, c in enumerate(cells_list):
            cy, cx = (c // tx) * TILE * sc, (c % tx) * TILE * sc
            d.rectangle([cx, cy, cx + TILE*sc - 1, cy + TILE*sc - 1], outline=color, width=2)
            if labels is not None:
                d.text((cx + 2, cy + 1), str(labels[k]), font=small, fill=color)
        return im

    pw, phh = tx*TILE*sc, ty*TILE*sc
    # 画像1: 3面比較
    panels = [("(1) オリジナル", up(orig), None),
              ("(2) 現状: タイル毎4パレット  誤差 %.3f/px" % e_before,
               grid_boxes(up(before), chosen, (255, 230, 0)), None),
              ("(3) +Plane B オーバーレイ %d セル  誤差 %.3f/px (-%.0f%%)"
               % (len(chosen), e_after, 100*(e_before-e_after)/max(e_before, 1e-6)),
               grid_boxes(up(after), chosen, (255, 230, 0)), None)]
    hdr = 34
    W = pw * 3 + 16 * 4
    H = phh + hdr + 16
    cmp_img = Image.new("RGB", (W, H), (18, 18, 20))
    d = ImageDraw.Draw(cmp_img)
    d.text((16, 6), "Plane B オーバーレイ実証  frame %d" % target, font=font, fill=(255, 255, 255))
    for k, (title, im, _) in enumerate(panels):
        x = 16 + k * (pw + 16)
        cmp_img.paste(im, (x, hdr))
        d.text((x, hdr - 0), "", font=small)
    # タイトルは上に重なるので個別ヘッダを下に
    cmp2 = Image.new("RGB", (W, H + 24), (18, 18, 20))
    cmp2.paste(cmp_img, (0, 0))
    d2 = ImageDraw.Draw(cmp2)
    for k, (title, im, _) in enumerate(panels):
        x = 16 + k * (pw + 16)
        d2.text((x, H + 2), title, font=small, fill=(210, 215, 220))
    cmp2.save(out_dir / "1_compare.png")

    # 画像2: before/after 拡大(変化セルの外接領域)
    rows = [c // tx for c in chosen]; cols = [c % tx for c in chosen]
    r0, r1 = max(min(rows)-1, 0), min(max(rows)+2, ty)
    c0, c1 = max(min(cols)-1, 0), min(max(cols)+2, tx)
    zsc = max(sc, 10)

    def zoom(arr):
        sub = arr[r0*TILE:r1*TILE, c0*TILE:c1*TILE]
        return Image.fromarray(sub, "RGB").resize(
            ((c1-c0)*TILE*zsc, (r1-r0)*TILE*zsc), Image.NEAREST)

    zb, za = zoom(before), zoom(after)
    zw, zh = zb.size
    Z = Image.new("RGB", (zw*2 + 48, zh + 60), (18, 18, 20))
    dz = ImageDraw.Draw(Z)
    dz.text((16, 8), "拡大比較 (変化領域)  frame %d" % target, font=font, fill=(255, 255, 255))
    Z.paste(zb, (16, 40)); Z.paste(za, (zw + 32, 40))
    dz.text((16, zh + 42), "現状 (4パレット)", font=small, fill=(210, 215, 220))
    dz.text((zw + 32, zh + 42), "+Plane B オーバーレイ", font=small, fill=(210, 215, 220))
    Z.save(out_dir / "2_zoom.png")

    # 画像3: Plane B に置いたタイルのみ
    labels = ["P%d" % a["best_pov"][c] for c in chosen]
    pb = grid_boxes(up(pbonly), chosen, (255, 230, 0), labels=labels)
    PB = Image.new("RGB", (pw + 32, phh + 64), (18, 18, 20))
    dp = ImageDraw.Draw(PB)
    dp.text((16, 8), "Plane B レイヤのみ (透過=チェッカー, 枠=配置セル/Pn=使用パレット)",
            font=small, fill=(255, 255, 255))
    PB.paste(pb, (16, 40))
    dp.text((16, phh + 44), "%d セル / 1セル=8x8タイル1枚 + 配置(セル番号)+パレット選択"
            % len(chosen), font=small, fill=(210, 215, 220))
    PB.save(out_dir / "3_planeb_only.png")

    print("誤差/px(rgb333): before=%.4f after=%.4f (-%.1f%%)"
          % (e_before, e_after, 100*(e_before-e_after)/max(e_before, 1e-6)))
    print("OUT:", out_dir / "1_compare.png", out_dir / "2_zoom.png",
          out_dir / "3_planeb_only.png")


def cells_to_img(cells, ty, tx):
    """(C,64,3) -> (ty*8, tx*8, 3)"""
    img = np.zeros((ty*TILE, tx*TILE, 3), np.uint8)
    for c in range(cells.shape[0]):
        ry, rx = (c // tx) * TILE, (c % tx) * TILE
        img[ry:ry+TILE, rx:rx+TILE] = cells[c].reshape(TILE, TILE, 3)
    return img


if __name__ == "__main__":
    main()
