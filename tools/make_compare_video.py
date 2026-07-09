#!/usr/bin/env python3
"""SEGA-CD化 と SSオリジナル を左右に並べた比較動画を生成する。

レイアウト（静的部分=ラベル/枠線/諸元）を PIL で base.png に焼き、その上に
左=SEGA-CD録画 / 右=オリジナル(op.mp4) を ffmpeg で overlay 合成する。音声は
SEGA-CD側を使う。右(オリジナル)は本編しか無いので、SEGA-CD本編の開始に合わせて
黒で待機→遅延同期させる。

同期(右の遅延 D と速度係数 k):
  右フレームの出力時刻 = D + op_time * k
  これを左(SEGA-CD)の同シーン出力時刻に一致させる。既定 D,k は実証済みの値。
  --verify を付けると生成後に左右パネルの輝度を相互相関して残差を測り報告する。
  --auto-sync は既定値から始めて「レンダリング→残差測定→線形フィット補正」を残差が
  1フレーム弱を切るまで繰り返す(最大3反復)。源が同じ録画なら既定値のままで十分。
  注意: op を SEGA-CD 全長から直接探す相互相関は反復パターンで誤マッチしやすいため、
  ここでは採らず、合成結果の左右残差を実測して詰める方式にしている。

fps: 出力は既定60fps。録画は59.94fps・本編は実質14.985fps、オリジナルは15fps
なので、30fps出力だと不均等リサンプルでカクつく。60fps以上ならクリーン複製になり
リサンプル由来のカクつきが消える(中身は15fpsのままなので“滑らかさ”自体は不変)。

例:
  tools/make_compare_video.py --left "$(cat tmp/v003_final.txt)" \
      --right assets/op.mp4 --out tmp/compare60.mp4 --fps 60 --auto-sync --verify
"""
from pathlib import Path
import argparse
import struct
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---- レイアウト定数 -----------------------------------------------------------
FONT = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
VW, VH = 512, 384          # 各動画の表示サイズ(4:3)
BORD = 3                   # 枠線幅
GAP = 48                   # 左右動画の間隔
ML = 40                    # 左右マージン
TOP = 18                   # 上下マージン
LBLH = 40                  # ラベル行の高さ
G1 = 8                     # ラベルと動画の間
G2 = 14                    # 動画と諸元の間
SPECH = 170               # 諸元ブロックの高さ
SPEC_LH = 21              # 諸元の行送り
VY = TOP + LBLH + G1       # 動画の上端
LX = ML                    # 左動画の左端
RX = ML + VW + GAP         # 右動画の左端

# ---- 諸元テキスト -------------------------------------------------------------
LEFT_LABEL = "SEGA-CD化"
RIGHT_LABEL = "SSオリジナル"

LEFT_SPECS = [
    "Sega CD (Sega CD)",
    "256x144 表示 (H32) / 15fps",
    "per-tile 差分 + dedup + 4パレット",
    "色 毎フレーム最大60色 (4パレット×15)",
    "音声 13.3kHz mono 8bit PCM (RF5C164)",
    "純CBR 5 sector/frame ≒ 149KiB/s",
    "(CD 1x / 1M-1M ダブルバッファ)",
]
RIGHT_SPECS = [
    "Source original (Sega Saturn)",
    "Duck TrueMotion 1.0 (FourCC DUCK)",
    "320x224 (DAR 10:7 / 4:3表示) / 15fps",
    "rgb555 (15bit RGB)",
    "音声 IMA ADPCM (DK4) 44.1kHz ステレオ",
    "映像1,504k + 音声354k = 1,867kbps",
]

# 同期の既定値(過去の実測フィット結果)。--auto-sync 未指定時に使う。
DEFAULT_DELAY = 15.42
DEFAULT_SPEED = 1.0002
DEFAULT_PALETTE = "out/video/061_160x96_global4/palettes.bin"


def count_colors(palette_path):
    """global4 palettes.bin から '最大N色 / 実使用M色' の文字列を作る。

    Genesis は 4パレット×16エントリ。各パレットの index0 は透過扱いなので
    使用可能は 15*4=60 色。実使用は全パレットの非index0スロットの相異なる色数。
    """
    p = Path(palette_path)
    if not p.exists():
        return "最大60色(4パレット×15)"
    vals = struct.unpack("<%dH" % (p.stat().st_size // 2), p.read_bytes())
    used = [c for i, c in enumerate(vals) if i % 16 != 0]  # index0(透過)を除く
    distinct = len(set(used))
    return "最大%d色(4パレット×15) / 実使用%d色" % (len(used), distinct)


def gen_base_png(path, colors):
    f_label = ImageFont.truetype(FONT, 30)
    f_spec = ImageFont.truetype(FONT, 16)
    W = ML + VW + GAP + VW + ML
    H = TOP + LBLH + G1 + VH + 2 * BORD + G2 + SPECH + TOP
    cv = Image.new("RGB", (W, H), (16, 16, 18))
    d = ImageDraw.Draw(cv)

    def col(x, label, specs):
        d.text((x, TOP), label, font=f_label, fill=(255, 255, 255))
        d.rectangle([x, VY, x + VW - 1, VY + VH - 1], fill=(0, 0, 0))  # 動画域は黒
        d.rectangle([x - BORD, VY - BORD, x + VW + BORD - 1, VY + VH + BORD - 1],
                    outline=(180, 180, 190), width=BORD)
        sy = VY + VH + BORD + G2
        for i, line in enumerate(specs):
            d.text((x, sy + i * SPEC_LH), line, font=f_spec, fill=(200, 205, 210))

    left = [s.format(colors=colors) for s in LEFT_SPECS]
    col(LX, LEFT_LABEL, left)
    col(RX, RIGHT_LABEL, RIGHT_SPECS)
    cv.save(path)
    return cv.size


def luma_curve(video, fps, crop=None):
    """fps サンプリングで各フレームの平均輝度カーブを返す。"""
    vf = "fps=%g," % fps
    if crop:
        vf += "crop=%d:%d:%d:%d," % crop
    vf += "scale=80:60,format=gray"
    with tempfile.NamedTemporaryFile(suffix=".raw", delete=True) as tf:
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(video), "-vf", vf, "-f", "rawvideo", tf.name],
                       check=True)
        d = np.fromfile(tf.name, dtype=np.uint8).astype(float)
    fs = 80 * 60
    n = len(d) // fs
    return d[:n * fs].reshape(n, fs).mean(1)


def _norm(x):
    x = x - x.mean()
    s = x.std()
    return x / s if s > 0 else x


def _xcorr_lag(a, b, fr):
    """a に対する b の遅れ(秒)。正 = b が a より遅れている。放物線補間で副標本精度。"""
    n = min(len(a), len(b))
    a, b = _norm(a[:n]), _norm(b[:n])
    c = np.correlate(a, b, "full")
    i = int(np.argmax(c))
    if 0 < i < len(c) - 1:
        y0, y1, y2 = c[i - 1], c[i], c[i + 1]
        denom = (y0 - 2 * y1 + y2)
        dd = (y0 - y2) / (2 * denom) if denom != 0 else 0
    else:
        dd = 0
    k = i - (n - 1) + dd
    return -k / fr, c.max() / n


def render(base, left, right, delay, speed, fps, out, dur=173):
    fc = (
        "[1:v]scale=%d:%d:flags=neighbor,setsar=1,fps=%g[mc];"
        "[2:v]setpts=PTS*%.6f,tpad=start_duration=%.4f:color=black,"
        "scale=%d:%d:flags=lanczos,setsar=1,fps=%g[or];"
        "[0:v][mc]overlay=%d:%d[t1];[t1][or]overlay=%d:%d[vout]"
        % (VW, VH, fps, speed, delay, VW, VH, fps, LX, VY, RX, VY)
    )
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-i", str(base), "-i", str(left), "-i", str(right),
           "-filter_complex", fc, "-map", "[vout]", "-map", "1:a",
           "-t", str(dur), "-r", str(fps),
           "-c:v", "libx264", "-crf", "19", "-pix_fmt", "yuv420p", "-preset", "fast",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)


def verify(out, fps=15):
    """生成物の左右パネルの残差(秒)を測る。正 = 右(オリジナル)が遅れている。"""
    L = luma_curve(out, fps, crop=(VW, VH, LX, VY))
    R = luma_curve(out, fps, crop=(VW, VH, RX, VY))
    res = []
    for t0, t1 in [(20, 45), (70, 95), (120, 150)]:
        a, b = L[t0 * fps:t1 * fps], R[t0 * fps:t1 * fps]
        lag, score = _xcorr_lag(a, b, fps)
        res.append((t0, t1, lag, score))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--left", help="SEGA-CD録画(左)。既定は tmp/v003_final.txt の中身")
    ap.add_argument("--right", default="assets/op.mp4", help="オリジナル(右)")
    ap.add_argument("--out", default="tmp/compare60.mp4")
    ap.add_argument("--fps", type=float, default=60)
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="右の遅延 D[秒]")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="右の速度係数 k")
    ap.add_argument("--auto-sync", action="store_true",
                    help="残差測定→フィット補正を残差<1フレーム弱まで反復(最大3回)")
    ap.add_argument("--verify", action="store_true", help="生成後に左右残差を測って報告")
    ap.add_argument("--palette", default=DEFAULT_PALETTE, help="色数算出用 palettes.bin")
    ap.add_argument("--base", default="tmp/base.png", help="静的レイアウトPNGの出力先")
    ap.add_argument("--dur", type=float, default=173, help="出力長[秒]")
    args = ap.parse_args()

    left = args.left
    if not left and Path("tmp/v003_final.txt").exists():
        left = Path("tmp/v003_final.txt").read_text().strip()
    if not left:
        ap.error("--left を指定してください(または tmp/v003_final.txt を用意)")

    colors = count_colors(args.palette)
    size = gen_base_png(args.base, colors)
    print("base.png %s 生成 (色: %s)" % (size, colors))

    def show(res):
        for t0, t1, lag, score in res:
            print("  %d-%ds: 右の遅れ %+.3fs (score %.2f)" % (t0, t1, lag, score))

    delay, speed = args.delay, args.speed
    render(args.base, left, args.right, delay, speed, args.fps, args.out, args.dur)
    print("生成: %s (%gfps) D=%.3fs k=%.5f" % (args.out, args.fps, delay, speed))

    if args.auto_sync:
        for it in range(3):
            res = verify(args.out)
            show(res)
            worst = max(abs(r[2]) for r in res)
            if worst <= 0.05:  # 1フレーム弱なら収束
                break
            ts = np.array([(t0 + t1) / 2 for t0, t1, _, _ in res])
            ys = np.array([lag for _, _, lag, _ in res])
            b, a = np.polyfit(ts, ys, 1)       # 右の遅れ(t) ≈ a + b t
            delay = delay - (a + b * delay)     # 開始時の遅れを除く
            speed = speed - b                   # 傾き(ドリフト)を除く
            print("補正再レンダリング #%d: D=%.3fs k=%.5f" % (it + 1, delay, speed))
            render(args.base, left, args.right, delay, speed, args.fps, args.out, args.dur)
    elif args.verify:
        show(verify(args.out))
    print("OUT=%s" % args.out)


if __name__ == "__main__":
    sys.exit(main())
