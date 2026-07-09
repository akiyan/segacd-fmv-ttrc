#!/usr/bin/env python3
"""Comparison フレームを実データで全編mp4化する(左右2動画 + Analysis共通フッター)。

上 = 左右に2動画:
      左 Real output           = 実機/エミュ録画 mp4 (CMP_REAL)
      右 Encoder ideal output  = sim 出力 mp4    (CMP_IDEAL)
    2本は互いにフレーム同期済み(同じ番号で並べる)。最上部にタイトル+諸元、右端に
    同期フレームカウンタ。音声は2トラック(track1=Real が既定, track2=Ideal)。
下 = Analysis と共通フッター(render_analysis の実データ status帯 + カテゴリ合計)。
    フッターは sim(tmp/sim)の per-frame データを再生位置に比例スクラブ(諸元は近似)。

レイアウトの正は tools/comparison_preview.py(ダミー) / フッター実データは render_analysis。

入力(env):
  CMP_REAL   左パネルの実機録画 mp4
  CMP_IDEAL  右パネルの sim 出力 mp4
  CMP_OUT    出力 mp4 (既定 videos/comparison.mp4)
  CBRSIM_OUT フッター用 sim ディレクトリ(既定 tmp/sim)
  CBRSIM_MODE フッターの画面モード(既定 mode4)
  CMP_FPS    サンプリング/出力 fps (既定 15)
  CMP_EMU    左パネル見出しの emulator 名/ver 併記 (既定 "(Genesis Plus GX 1.7.4)")

usage: python3 tools/render_comparison.py            # 全編→mp4
       python3 tools/render_comparison.py A B         # frame [A,B) だけPNG(検証用)
"""
import os
os.environ.setdefault("CBRSIM_OUT", "tmp/sim")
os.environ.setdefault("CBRSIM_MODE", "mode4")

import sys
import glob
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
import layout_preview as L
import render_analysis as R          # tmp/sim をロードし frame_data/draw_status_real/CAT_TOTALS を提供
import comparison_preview as CP       # CMP_L/CMP_R/TITLE_BASE/LABEL_BASE/SIDE など

REAL_MP4 = os.environ.get("CMP_REAL", "tmp/op_loop.mp4")
IDEAL_MP4 = os.environ.get("CMP_IDEAL", "tmp/op_dbg2_src.mp4")
OUT_MP4 = os.environ.get("CMP_OUT", "videos/comparison.mp4")
FPS_OUT = int(os.environ.get("CMP_FPS", "15"))
EMU_META = os.environ.get("CMP_EMU", "(Genesis Plus GX 1.7.4)")
TITLE = "SEGA-CD Tile Texture Reuse Codec Encoding Comparison Testing"

WORK = Path(OUT_MP4).with_suffix("")                 # videos/<stem>/
FR_REAL = WORK / "real"; FR_IDEAL = WORK / "ideal"; FR_OUT = WORK / "frames"


def extract(mp4, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    if not sorted(glob.glob(str(outdir / "*.png"))):     # 既に抽出済みなら再利用
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", mp4,
                        "-vf", "fps=%d" % FPS_OUT, str(outdir / "%05d.png")], check=True)
    return sorted(glob.glob(str(outdir / "*.png")))


def panel(cv, d, rect, img_path, title, meta, audio_label, audio_muted):
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    im = Image.open(img_path).convert("RGB").resize((w, h), Image.NEAREST)   # 4:3動画を枠いっぱい(素の拡大)
    cv.paste(im, (x0, y0))
    d.rectangle([x0, y0, x1, y1], outline=L.COL_BORDER)
    d.text((x0 + 2, CP.LABEL_BASE), title, fill=L.COL_TXT, font=L.f_lbl, anchor="ls")
    if meta:
        d.text((x0 + 2 + L._w(L.f_lbl, title) + 10, CP.LABEL_BASE), meta,
               fill=L.COL_DIM, font=L.f_leg, anchor="ls")
    col = L.COL_DIM if audio_muted else L.COL_TXT
    d.text((x0 + 8, y1 - 8), audio_label, fill=col, font=L.f_leg, anchor="ls")


def top_title(d, k):
    hx = CP.SIDE
    d.text((hx, CP.TITLE_BASE), TITLE, fill=L.COL_TXT, font=L.f_head, anchor="ls")
    specs = " / ".join([R.MODE, R.RES, R.AUDIO_STR, "%dfps" % R.FPS])
    d.text((hx + L._w(L.f_head, TITLE) + 14, CP.TITLE_BASE), specs,
           fill=L.COL_DIM, font=L.f_meta, anchor="ls")
    lab = "sync Frame:"
    fw = L._w(L.f_meta, lab) + L._w(L.f_meta, str(k).rjust(5, "0"))
    fx = L.CW - CP.SIDE - fw
    fy = CP.TITLE_BASE - L.f_meta.getmetrics()[0]
    d.text((fx, CP.TITLE_BASE), lab, fill=L.COL_DIM, font=L.f_meta, anchor="ls")
    L.draw_padnum(d, fx + L._w(L.f_meta, lab), fy, k, 5, L.f_meta, L.COL_TXT)


REAL = IDEAL = None
NCMP = 0
_CATDATA = {"cat_totals": R.CAT_TOTALS, "cat_uniq": R.CAT_UNIQ}


def compose(k):
    cv = Image.new("RGB", (L.CW, L.CH), L.BG)
    d = ImageDraw.Draw(cv)
    top_title(d, k)
    panel(cv, d, CP.CMP_L, REAL[k], "Real output", EMU_META,
          "audio 1 · Emulator (default)", False)
    panel(cv, d, CP.CMP_R, IDEAL[k], "Encoder ideal output", None,
          "audio 2 · Encoder ideal", True)
    # フッター: sim per-frame データを再生位置に比例スクラブ(諸元は近似)
    si = min(int(k / max(NCMP, 1) * R.NF), R.NF - 1)
    cv.paste(R.draw_status_real(R.frame_data(si)), L.STATUS_XY)
    cv.paste(L.draw_cattotals(L.PAL_W, L.PAL_H, _CATDATA), L.PAL_XY)
    return cv


def render_one(k):
    compose(k).save(str(FR_OUT / ("%05d.png" % k)))
    return k


def mux():
    vcodec = ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr",
              "-cq", os.environ.get("CMP_CQ", "23"), "-b:v", "0"]
    # 映像 + 音声2トラック(0:=Real 既定, 1:=Ideal)。既定トラックは track1(Real)。
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", str(FPS_OUT), "-start_number", "0", "-i", str(FR_OUT / "%05d.png"),
           "-i", REAL_MP4, "-i", IDEAL_MP4,
           "-map", "0:v:0", "-map", "1:a:0", "-map", "2:a:0"]
    cmd += vcodec + ["-pix_fmt", "yuv420p", "-r", "60",
                     "-c:a", "aac", "-b:a", "160k",
                     "-metadata:s:a:0", "title=Real (Emulator)", "-disposition:a:0", "default",
                     "-metadata:s:a:1", "title=Encoder ideal", "-disposition:a:1", "0",
                     "-shortest", "-fps_mode", "cfr", OUT_MP4]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    from multiprocessing import Pool
    REAL = extract(REAL_MP4, FR_REAL)
    IDEAL = extract(IDEAL_MP4, FR_IDEAL)
    NCMP = min(len(REAL), len(IDEAL))
    FR_OUT.mkdir(parents=True, exist_ok=True)
    rng = list(range(int(sys.argv[1]), int(sys.argv[2]))) if len(sys.argv) == 3 else list(range(NCMP))
    print("compare %d frames (real=%d ideal=%d) fps=%d -> %s" %
          (len(rng), len(REAL), len(IDEAL), FPS_OUT, FR_OUT), flush=True)
    nw = max(1, (os.cpu_count() or 2) - 2)
    with Pool(nw) as p:
        for n, _ in enumerate(p.imap_unordered(render_one, rng, chunksize=8)):
            if n % 300 == 0:
                print("  %d/%d" % (n, len(rng)), flush=True)
    if len(sys.argv) != 3:
        print("mux -> %s" % OUT_MP4, flush=True)
        mux()
        print("done", OUT_MP4, flush=True)
    else:
        print("done (frames only)", len(rng), flush=True)
