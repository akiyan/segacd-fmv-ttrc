#!/usr/bin/env python3
"""sim の素の出力(映像+音声)を『ストレートに』mp4 化する。解析オーバーレイ無し。

sim(sim.py)は解析フレーム用の素材(preview/ = オーバーレイ無しの復号フレームと復元音声)を
出すが、単体の再生用動画は出さない。本ツールはその preview/ を実機画面(モード別サイズ)へ
中央配置し、表示アスペクト(PAR)を適用して、sim 音声を多重化した素の mp4 を書き出す。
= エミュ録画の「理想版」(ハード再生アーティファクトもデバッグHUDも無い Encoder ideal output)。

ffmpeg の pad+scale だけで完結(PILループ不要=速い)。

env:
  CBRSIM_OUT      sim出力ディレクトリ(既定 videos/<stem>/tmp)。preview/ と stats.npz 指定音声を使う
  CBRSIM_MODE     画面モード H32/H40/mode4 (既定 H32)。画面サイズと PAR に使う
  STRAIGHT_OUT    出力mp4 (既定 videos/<stem>_sim.mp4)
  STRAIGHT_SCALE  整数拡大率 (既定 4)

usage: python3 tools/export_sim_video.py
"""
import os
import sys
import glob
import subprocess
from pathlib import Path
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import layout_preview as L
from cbr_paths import artifact_path, sim_work_dir

SIM = str(sim_work_dir())
MODE = os.environ.get("CBRSIM_MODE", "H32")
SCALE = int(os.environ.get("STRAIGHT_SCALE", "4"))
OUT = os.environ.get("STRAIGHT_OUT", str(artifact_path("sim", sim_dir=SIM)))


def main():
    z = np.load(f"{SIM}/stats.npz", allow_pickle=True)
    fps = int(z["fps"])
    pv = sorted(glob.glob(f"{SIM}/preview/*.png"))
    if not pv:
        sys.exit("no preview frames in %s/preview" % SIM)
    W, H = Image.open(pv[0]).size                       # コンテンツ画素(タイルグリッド)
    m = L.MODES[MODE]
    SW, SH = max(m["sw"], W), max(m["sh"], H)           # 実機画面サイズ(コンテンツを中央配置)
    par = m["par"]                                      # 1ドット横長比(表示アスペクト補正)
    padx, pady = (SW - W) // 2, (SH - H) // 2
    outw = 2 * round(SW * SCALE * par / 2)              # 表示アスペクトを焼く(偶数化=yuv420p)
    outh = 2 * round(SH * SCALE / 2)

    if "audio_playback_file" in z:
        audio = Path(SIM) / str(z["audio_playback_file"])
        if not audio.is_file():
            sys.exit("stats.npz playback audio is missing: %s" % audio)
    else:
        legacy_audio = sorted(glob.glob(f"{SIM}/audio_*.wav"))
        audio = Path(legacy_audio[0]) if len(legacy_audio) == 1 else None
    start = int(Path(pv[0]).stem)                       # 先頭フレーム番号(通常0)
    vf = "pad=%d:%d:%d:%d,scale=%d:%d:flags=neighbor" % (SW, SH, padx, pady, outw, outh)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", str(fps), "-start_number", str(start),
           "-i", "%s/preview/%%05d.png" % SIM]
    if audio is not None and audio.exists():
        cmd += ["-i", str(audio)]
    cmd += ["-vf", vf, "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(fps)]
    if audio is not None and audio.exists():
        cmd += ["-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += [OUT]
    print("straight sim -> %s  (%dx%d @ %dfps, mode=%s, content=%dx%d screen=%dx%d)"
          % (OUT, outw, outh, fps, MODE, W, H, SW, SH), flush=True)
    subprocess.run(cmd, check=True)
    print("done", OUT, flush=True)


if __name__ == "__main__":
    main()
