#!/usr/bin/env python3
"""Comparison フレームを実データで全編mp4化する(左右2動画 + Analysis共通フッター)。

上 = 左右に2動画:
      左 Real output           = 実機/エミュ録画 mp4 (CMP_REAL)。デバッグHUD付き。
      右 Encoder ideal output  = sim の素の復号出力(CBRSIM_OUT/preview, オーバーレイ無し)
    同期 = 左のデバッグ欄の movie フレーム番号 F を読み(read_frameno)、右は sim preview[F]、
    フッターも sim frame F を描く=左右+フッターが常に同一 movie フレームで完全一致。
    F は信頼度閾値+単調性で補正(起動部/読み損ないは直前保持)。
    音声2トラック(track1=Real 既定, track2=Ideal)。
下 = Analysis と共通フッター(render_analysis の実データ status帯 + カテゴリ合計)。

入力(env):
  CMP_REAL   左パネルの実機録画 mp4 (256x192 想定, デバッグHUDあり)
  CMP_OUT    出力 mp4 (既定 videos/comparison.mp4)
  CBRSIM_OUT フッター/右パネル用 sim ディレクトリ(既定 videos/<stem>/tmp)
  CBRSIM_MODE フッターの画面モード(既定 mode4)
  CMP_F0_REAL_FRAME 実機抽出フレーム内の F0000 表示フレーム番号(未指定なら自動検出)
  CMP_FPS    サンプリング/出力 fps (既定 15)
  CMP_EMU    左パネル見出しの emulator 名/ver (既定 "(Genesis Plus GX 1.7.4)")

usage: python3 tools/render_comparison.py            # 全編→mp4
       python3 tools/render_comparison.py A B         # frame [A,B) だけPNG(検証用)
"""
import os
os.environ.setdefault("CBRSIM_MODE", "mode4")

import sys
import glob
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from cbr_paths import sim_work_dir
os.environ.setdefault("CBRSIM_OUT", str(sim_work_dir()))
import layout_preview as L
import render_analysis as R          # loads sim data for frame_data/draw_status_real/CAT_TOTALS
import comparison_preview as CP       # CMP_L/CMP_R/TITLE_BASE/LABEL_BASE/SIDE など
from read_frameno import read_frameno

REAL_MP4 = os.environ.get("CMP_REAL", "videos/machi_op_mode4_256x192_emu.mp4")
OUT_MP4 = os.environ.get("CMP_OUT", "videos/comparison.mp4")
FPS_OUT = int(os.environ.get("CMP_FPS", "15"))
EMU_META = os.environ.get("CMP_EMU", "(Genesis Plus GX 1.7.4)")
TITLE = "SEGA-CD Tile Texture Reuse Codec: Real vs Ideal"

WORK = Path(OUT_MP4).with_suffix("")
FR_REAL = WORK / "real"; FR_OUT = WORK / "frames"
SIMDIR = R.SIM
SCREEN_W, SCREEN_H = R.SCREEN_W, R.SCREEN_H             # mode4 なら 256x192
CONTENT_W, CONTENT_H = R.W, R.H                         # 256x144
# sim preview を実機画面へ載せる縦位置。諸元からの計算値=画面中央配置(既定)。
# TODO(保留): 実機(エミュ)は画面モード/オーバースキャンのジオメトリが sim と異なり、
# 実際の content 配置は中央から数px上(実測 PADY≈20)。厳密一致は実機側の表示ジオメトリ
# (プレイヤー plane_row=5 / HUD行2-3 / overscan crop)を諸元に取り込んでから。CMP_PADY で上書き可。
PADY = int(os.environ.get("CMP_PADY", str((SCREEN_H - CONTENT_H) // 2)))


def extract(mp4, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    if not sorted(glob.glob(str(outdir / "*.png"))):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", mp4,
                        "-vf", "fps=%d" % FPS_OUT, str(outdir / "%05d.png")], check=True)
    return sorted(glob.glob(str(outdir / "*.png")))


def read_real_fnums(real_pngs):
    vals = []
    for k, p in enumerate(real_pngs):
        val, conf = read_frameno(Image.open(p))
        ok = conf >= 0.6 and 0 <= val < R.NF
        vals.append((val, conf, ok))
    return vals


def find_f0000_anchor(reads):
    explicit = os.environ.get("CMP_F0_REAL_FRAME", "").strip()
    if explicit:
        return int(explicit)
    for k, (val, _conf, ok) in enumerate(reads):
        if not ok or val != 0:
            continue
        future = reads[k + 1:k + 8]
        if any(ok2 and 1 <= val2 <= 2 for val2, _conf2, ok2 in future):
            return k
    for k, (val, _conf, ok) in enumerate(reads):
        if ok and 1 <= val <= 4:
            return k - val
    return 0


def build_fseq(real_pngs):
    """Build real->ideal frame mapping from the HUD's F0000 display frame.

    The old median-offset path ignored F0000 and assumed each sampled real frame
    advanced by exactly one movie frame. Native H40 recordings can skip or hold a
    movie frame depending on the 60fps->15fps sampling phase, so we anchor on the
    actual F0000 frame and use confident OCR reads when they are close to the
    anchored prediction.
    """
    reads = read_real_fnums(real_pngs)
    anchor = find_f0000_anchor(reads)
    fseq = []
    for k, (val, _conf, ok) in enumerate(reads):
        pred = k - anchor
        use = val if ok and abs(val - pred) <= 2 else pred
        fseq.append(max(0, min(use, R.NF - 1)))
    return fseq, anchor


REAL = []
FSEQ = []
_CATDATA = {"cat_totals": R.CAT_TOTALS, "cat_uniq": R.CAT_UNIQ}


def _sim_screen(fno):
    """sim preview[fno](256x144, オーバーレイ無し) を実機画面(256x192)へ黒帯付きで載せる。"""
    scr = Image.new("RGB", (SCREEN_W, SCREEN_H), (0, 0, 0))
    c = Image.open("%s/preview/%05d.png" % (SIMDIR, fno)).convert("RGB")
    scr.paste(c, ((SCREEN_W - CONTENT_W) // 2, PADY))
    return scr


def panel(cv, d, rect, im, title, meta, audio_label, audio_muted):
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    cv.paste(im.resize((w, h), Image.NEAREST), (x0, y0))
    d.rectangle([x0, y0, x1, y1], outline=L.COL_BORDER)
    d.text((x0 + 2, CP.LABEL_BASE), title, fill=L.COL_TXT, font=L.f_lbl, anchor="ls")
    if meta:
        d.text((x0 + 2 + L._w(L.f_lbl, title) + 10, CP.LABEL_BASE), meta,
               fill=L.COL_DIM, font=L.f_leg, anchor="ls")
    col = L.COL_DIM if audio_muted else L.COL_TXT
    d.text((x0 + 8, y1 - 8), audio_label, fill=col, font=L.f_leg, anchor="ls")


def top_title(d, fno):
    hx = CP.SIDE
    d.text((hx, CP.TITLE_BASE), TITLE, fill=L.COL_TXT, font=L.f_head, anchor="ls")
    specs = " / ".join([R.MODE, R.RES, R.AUDIO_STR, "%dfps" % R.FPS])
    d.text((hx + L._w(L.f_head, TITLE) + 14, CP.TITLE_BASE), specs,
           fill=L.COL_DIM, font=L.f_meta, anchor="ls")
    lab = "sync Frame:"
    fw = L._w(L.f_meta, lab) + L._w(L.f_meta, str(fno).rjust(5, "0"))
    fx = L.CW - CP.SIDE - fw
    fy = CP.TITLE_BASE - L.f_meta.getmetrics()[0]
    d.text((fx, CP.TITLE_BASE), lab, fill=L.COL_DIM, font=L.f_meta, anchor="ls")
    L.draw_padnum(d, fx + L._w(L.f_meta, lab), fy, fno, 5, L.f_meta, L.COL_TXT)


def compose(k):
    fno = FSEQ[k]                                        # この実機フレームの movie フレーム番号
    cv = Image.new("RGB", (L.CW, L.CH), L.BG)
    d = ImageDraw.Draw(cv)
    top_title(d, fno)
    panel(cv, d, CP.CMP_L, Image.open(REAL[k]).convert("RGB"),
          "Real output", EMU_META, "audio 1 · Emulator (default)", False)
    panel(cv, d, CP.CMP_R, _sim_screen(fno),
          "Encoder ideal output", None, "audio 2 · Encoder ideal", True)
    cv.paste(R.draw_status_real(R.frame_data(fno)), L.STATUS_XY)     # フッター=sim frame fno(完全一致)
    cv.paste(L.draw_cattotals(L.PAL_W, L.PAL_H, _CATDATA), L.PAL_XY)
    return cv


def render_one(k):
    compose(k).save(str(FR_OUT / ("%05d.png" % k)))
    return k


def one_pass_range():
    """本編1周ぶんの出力範囲。F が最初に進み始める所〜最終フレーム(NF-1)に達する所まで。"""
    ks = max(0, min(F0_REAL_FRAME, len(FSEQ) - 1))
    ke = min(len(FSEQ), ks + R.NF)
    return ks, ke


def mux(ks, ke):
    vcodec = ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr",
              "-cq", os.environ.get("CMP_CQ", "23"), "-b:v", "0"]
    # 音声: Real(録画)を track1=既定, Ideal(sim)を track2。開始オフセットを出力先頭に合わせる。
    a_real = REAL_MP4
    a_ideal = sorted(glob.glob("%s/audio_*.wav" % SIMDIR))
    t0 = ks / FPS_OUT                                    # 出力先頭に相当する Real 側の時刻
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", str(FPS_OUT), "-start_number", str(ks), "-i", str(FR_OUT / "%05d.png"),
           "-ss", "%.3f" % t0, "-i", a_real]
    if a_ideal:
        cmd += ["-i", a_ideal[0]]
    cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    if a_ideal:
        cmd += ["-map", "2:a:0"]
    cmd += vcodec + ["-pix_fmt", "yuv420p", "-r", "60", "-c:a", "aac", "-b:a", "160k",
                     "-metadata:s:a:0", "title=Real (Emulator)", "-disposition:a:0", "default"]
    if a_ideal:
        cmd += ["-metadata:s:a:1", "title=Encoder ideal", "-disposition:a:1", "0"]
    cmd += ["-shortest", "-fps_mode", "cfr", OUT_MP4]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    from multiprocessing import Pool
    REAL = extract(REAL_MP4, FR_REAL)
    FSEQ, F0_REAL_FRAME = build_fseq(REAL)
    FR_OUT.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 3:
        rng = list(range(int(sys.argv[1]), int(sys.argv[2]))); ks = rng[0]; ke = rng[-1] + 1
    else:
        ks, ke = one_pass_range(); rng = list(range(ks, ke))
    print("compare frames k=[%d,%d) (real=%d, F=%d..%d, F0000@k=%d) fps=%d -> %s" %
          (ks, ke, len(REAL), FSEQ[ks], FSEQ[min(ke, len(FSEQ)) - 1], F0_REAL_FRAME, FPS_OUT, FR_OUT),
          flush=True)
    nw = max(1, (os.cpu_count() or 2) - 2)
    with Pool(nw) as p:
        for n, _ in enumerate(p.imap_unordered(render_one, rng, chunksize=8)):
            if n % 300 == 0:
                print("  %d/%d" % (n, len(rng)), flush=True)
    if len(sys.argv) != 3:
        print("mux -> %s" % OUT_MP4, flush=True)
        mux(ks, ke)
        print("done", OUT_MP4, flush=True)
    else:
        print("done (frames only)", len(rng), flush=True)
