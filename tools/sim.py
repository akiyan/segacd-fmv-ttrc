#!/usr/bin/env python3
"""OP動画(061.mp4)を対象にした 純CBR差分圧縮 + タイル重複排除のオフライン検証。

方針(2026-07 更新):
- 前処理でディザ除去: 元動画は低解像度+ディザなので、`video_geometry.py` が
  H32/H40のHARに合わせて全画素を保持したpad変換を行い、一度拡大してフルカラー化、
  hqdn3d+gblur後に出力ラスタへ縮小する。
- 表示アスペクト: H32はHAR 8:7、H40は32:35。どちらも224ラインでは
  64:49の可視比になる。`CBRSIM_GEOMETRY_FIT=crop`を明示した場合だけ
  その比率へcropし、既定では黒帯が最小になるpadで情報を落とさない。
- パレット: 4本×15色をクリップ全体から学習し固定・共有(per-frameではない)。
- 分散が非常に低い(=ほぼ単色)タイルだけ平均色へ均して単純化(FLATTEN_STD)。
  ディザ除去済みなので閾値は低めでよい。
- ディザは行わない(圧縮効率優先, 実機フォーマットは無変更)。
- **タイル重複排除(dedup)**: MDのネームテーブルは各セル→(パターンslot, パレット)。
  パターン(8x8 idx配列)はパレット非依存なので、同じidxパターンは VRAM に1つ
  だけ置き、複数セル(パレット違いも可)で使い回す。パターン転送32Bを共有でき、
  各セルはネームテーブル2Bのみ。フレーム内・フレーム跨ぎ両方で効く(VRAMを
  LRUキャッシュとしてモデル化, 容量 VRAM_TILES)。
- 転送は純CBR: 毎フレーム固定 FRAME_BYTES のみ(実機1M/1Mダブルバッファ相当、
  フレーム間の帯域繰り越しは無し)。予算内に収まらない低優先セルは前の内容を保持
  (ゴースト)し、翌フレームへ持ち越す。
- ゴースト対策(キャリーオーバー型エージング): 予算負けで未更新のまま待たされた
  (dirtyが継続する)タイルほど優先度を累積的に底上げ(1+AGING_ALPHA*wait)し、必ず
  いつか拾われるようにする(予算超過の強制更新はしない=CBR厳守)。内容が変わって
  不要になったタイルは changed から外れ wait=0 に戻り自然消滅する。
- 音声: 13.3kHz mono 8bit (実機RF5C164相当)。Plane B オーバーレイは無し。
"""
import os
import sys
import time
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quantize_md_video import (  # noqa: E402
    rgb888_to_rgb333, rgb333_to_rgb888, run, prepare_dir, MD_LEVELS,
)
from quantize_global4_tiles import (  # noqa: E402
    tile_blocks, build_palettes, pals_to_bytes, palette_lut, rgb333_keys, TILE,
)
from palette_algorithms import (  # noqa: E402
    MOSAIC_GM, STL4, PaletteEvaluator, build_mosaic_palettes,
    normalize_palette_algo, score_palettes,
)
from cbr_paths import sim_work_dir  # noqa: E402
from video_geometry import probe_source, parse_ratio, source_filter, raw_filter  # noqa: E402

# 対象動画・寸法・fps は env で差し替え可(既定はサンプル動画)。
# CBRSIM_OUT を指定しない場合は videos/<stem>/tmp に出力する。
SRC = os.environ.get("CBRSIM_SRC", "movies/disc1/061.mp4")
MODE = os.environ.get("CBRSIM_MODE", "H32")
# Keep the historical 144-line codec height, but choose the matching native
# horizontal raster when the mode changes (H32=256, H40=320).
W = int(os.environ.get("CBRSIM_W", "320" if MODE.upper() == "H40" else "256"))
H = int(os.environ.get("CBRSIM_H", "144"))
GEOMETRY_FIT = os.environ.get("CBRSIM_GEOMETRY_FIT", "pad").lower()
SOURCE_SAR_OVERRIDE = os.environ.get("CBRSIM_SOURCE_SAR")
_MASTER_VF_OVERRIDE = os.environ.get("CBRSIM_MASTER_VF")
_RAW_VF_OVERRIDE = os.environ.get("CBRSIM_RAW_VF")
# Import-only users need not have the default sample video installed.  Resolve
# the source geometry at run time in main() when no explicit filter was given.
DEDITHER_VF = _MASTER_VF_OVERRIDE or ""
RAW_VF = _RAW_VF_OVERRIDE or ""
TCOLS, TROWS = W // TILE, H // TILE     # 既定 32 x 18 = 576 cells
C_CELLS = TCOLS * TROWS
# CBRSIM_FPS は整数 "15" でも分数 "30000/1001"(=59.94/2=29.97, NTSCソース準拠) でも受ける。
# FPS_STR は ffmpeg にそのまま渡す(分数を厳密に間引く)。FPS は計算用の float。
FPS_STR = os.environ.get("CBRSIM_FPS", "15").strip()
FPS = (float(FPS_STR.split("/")[0]) / float(FPS_STR.split("/")[1])) if "/" in FPS_STR else float(FPS_STR)
DURATION = os.environ.get("CBRSIM_DURATION", "152.866667")

CD_RATE = 153_600               # CD 1x, B/s (= 150 KiB/s, 絶対上限)
TARGET_RATE = int(os.environ.get("CBRSIM_RATE_KIB", "144")) * 1024  # CBRレート(既定144 KiB/s)。env で調整可
FRAME_BYTES = int(TARGET_RATE / FPS)   # 純CBR: 1フレームで転送できる固定バイト
                                    # 実機1M/1Mダブルバッファ相当。フレーム間の繰り越し無し。
# 音声: 既定 13.3kHz mono 8bit PCM(RF5C164, 出荷経路)。CBRSIM_AUDIO=adpcm22 で 22.05kHz
# mono ADPCM(4bit, 棚上げの調査用=ADPCM.md参照)。CD予算(audio_due)はバイト率で引く。
AUDIO_KIND = os.environ.get("CBRSIM_AUDIO", "pcm13")
if AUDIO_KIND == "pcm13":
    AUDIO_RATE = 13_300; AUDIO_BPS = 1.0; AUDIO_FFCODEC = "pcm_u8"
    AUDIO_LABEL = "13.3kHz mono 8bit PCM"; AUDIO_FILE = "audio_13k3_u8_mono.wav"
else:
    AUDIO_RATE = 22_050; AUDIO_BPS = 0.5; AUDIO_FFCODEC = "adpcm_ima_wav"
    AUDIO_LABEL = "22.05kHz mono ADPCM"; AUDIO_FILE = "audio_22k05_adpcm_mono.wav"
# The player advances on integer NTSC VBlanks. PCM deliberately rounds the
# fixed chunk up against that actual cadence, yielding 444 B at N2 and 888 B at
# N4. This is about 2.94 B/s above FD=0x0345, so lead grows slightly instead of
# slowly draining. The packer uses the same calculation.
NTSC_VSYNC = 60_000 / 1001
VSYNC_N = int(round(NTSC_VSYNC / FPS))
PLAYBACK_FPS = NTSC_VSYNC / VSYNC_N
AUDIO_FRAME_BYTES = (int(math.ceil(AUDIO_RATE / PLAYBACK_FPS))
                     if AUDIO_KIND == "pcm13" else 0)
PATTERN_BYTES = 32              # 4bpp 8x8 パターン
NAME_BYTES = 2                  # ネームテーブル1エントリ(tile index + palette + priority)
VRAM_TILES = int(os.environ.get("CBRSIM_VRAM_TILES", "1400"))   # VRAM常駐パターン数(LRU)。
# デバッグオーバーレイのフォント予約分だけ実機側で減らす(例 1360)ときは env で指定。
FLATTEN_STD = 0.12              # rgb333(0-7)タイル内std平均。ディザ除去済みなので低め
DETAIL_ALPHA = 1.5
BORDER_TILES = 2
BORDER_WEIGHT = 0.4
# キャリーオーバー型エージング: 予算負けで未更新のまま待たされた(dirtyが継続する)
# タイルほど優先度を累積的に引き上げ、必ずいつか拾われるようにする(飢餓/ゴースト対策)。
# 内容が変わって不要になったタイルは changed から外れて自然消滅する。
AGING_ALPHA = 0.6              # 待ちフレーム数あたりの優先度加点(乗算 1+α*wait)
WAIT_CAP = 10                 # エージング加点の飽和上限(フレーム)
NBINS = 12                    # MissCarry年齢分布のビン数(1..11, 12+)
# Comp(same=dedup)を表す色。status帯のCompバー Cs 部と Update tiles パネルの
# dedupタイル枠で共有する(render_statusline.py の COL_SAME と一致させること)。
COL_SAME = (0, 190, 175)      # teal
# カテゴリマップ(catmap)の縁取り色。status/凡例と一致させること。
CAT_RAW = (205, 205, 205)     # Raw = 新規CD転送 (やや暗い白, 枠なし内容)
CAT_SAME = (150, 150, 158)    # Same = 不変 (gray, 枠なし内容)  ※色巡回
CAT_DEDUP = (0, 190, 175)     # Dedup = VRAM流用 (teal, 互換用)
CAT_BUF = (175, 120, 235)     # Buf = PRG先読み (violet)
CAT_MISS = (220, 70, 70)      # Miss = 取りこぼし (red, 塗りつぶし)
CAT_CARRY = (235, 160, 70)    # MissCarry = 繰越Miss (amber)
CAT_NEAR = (95, 115, 215)     # Near = ほぼ同一の常駐を流用 (blue)  ※色巡回
CAT_COA = (45, 240, 70)       # Coa = 近い常駐を流用 (鮮やかな緑)  ※色巡回+判別性
CAT_FLBK = (240, 150, 50)     # Flbk = Missのフォールバック(荒くても常駐で穴埋め) (orange, 太枠)
# 粗い近似dedup(env CBRSIM_COA=1): 平坦なコールドタイルを、見た目(2×2低周波)が近い常駐パターンで
# 流用(ネームテーブルだけ=0転送)。ディザの点々差は無視。構造崩れを避けるため detail が低い平坦タイル限定。
COA_ON = os.environ.get("CBRSIM_COA", "1") != "0"          # 既定ON(全機能ON)。OFFは CBRSIM_COA=0
COA_DETAIL = float(os.environ.get("CBRSIM_COA_DETAIL", "0.7"))  # detailがこれ未満(平坦)のみCoa対象
COA_MEAN = float(os.environ.get("CBRSIM_COA_MEAN", "4"))        # 2×2平均色差の平均しきい(画質優先で厳しめ)
COA_MAX = float(os.environ.get("CBRSIM_COA_MAX", "8"))          # 2×2平均色差の最大しきい
COA_K = int(os.environ.get("CBRSIM_COA_K", "24"))              # バケツ内で照合する最新候補数
COA_BW = 24                                                    # 平均色バケツ幅
# Near(F3): 変化タイルのうち「表示中(old)とtarget(real)が見た目ほぼ同じ」を更新省略。
# 常に old(表示中) vs target を比較するのでドリフトはF3の距離で頭打ち。env CBRSIM_NEAR=1 で有効。
NEAR_ON = os.environ.get("CBRSIM_NEAR", "1") != "0"        # 既定ON(全機能ON)。OFFは CBRSIM_NEAR=0
NEAR_F3 = dict(Ym=float(os.environ.get("CBRSIM_NEAR_YM", "10")),   # 画素輝度差の平均しきい(厳格化)
               Yp=float(os.environ.get("CBRSIM_NEAR_YP", "28")),   # 画素輝度差の最大しきい(形は軽く効く)
               C=float(os.environ.get("CBRSIM_NEAR_C", "24")))     # 画素色差の平均しきい
_LWv = np.array([.299, .587, .114]); _CBv = np.array([-.169, -.331, .5]); _CRv = np.array([.5, -.419, -.081])
_SC1 = (.01 * 255) ** 2; _SC2 = (.03 * 255) ** 2

# --- 統合探索(Same/Near/Coa/Flbk/Miss) + 中央tie-break。既定OFF(実機用simへ影響させない) ---
CENTERTIE_ON = os.environ.get("CBRSIM_CENTERTIE", "1") != "0"   # 既定ON。同点は画面中央に近いセルを優先
MIDFAR_ON = os.environ.get("CBRSIM_MIDFAR", "1") != "0"    # 既定ON。Near/Coa/Flbk を1つのVRAM最良一致探索に統合
# Flbk 判定モード。既定ON=「改善モード」: 絶対しきい(flbk tier)でなく「現在表示より
# 少しでも target に近づく(=改善する)候補なら採る」。どの動画でもFlbkが反応しやすい。
# CBRSIM_FLBK_IMPROVE_ONLY=0 で旧・絶対しきいモード(flbk tier内の候補のみ)。
FLBK_IMPROVE_ONLY = os.environ.get("CBRSIM_FLBK_IMPROVE_ONLY", "1") != "0"
# 改善モードで要求する最小改善量(score差)。既定0=「ちょっとでも改善するなら採る」。
FLBK_MIN_IMPROVE = float(os.environ.get("CBRSIM_FLBK_MIN_IMPROVE", "0"))
# 段階しきい(F3: Ym=画素輝度差平均, Yp=画素輝度差最大, C=画素色差平均)。tight→loose。
# 探索は2×2低周波でバケツ前絞り→最良候補を採り、その候補を下記F3で分類。
# Flbk = Miss のフォールバック。旧Mid/Farを統合し、しきいを広くして「Missを出すくらいなら荒くても穴埋め」。
MIDFAR_TIERS = [
    ("near", NEAR_F3['Ym'], NEAR_F3['Yp'], NEAR_F3['C']),
    ("coa", float(os.environ.get("CBRSIM_TCOA_YM", "20")), float(os.environ.get("CBRSIM_TCOA_YP", "50")), float(os.environ.get("CBRSIM_TCOA_C", "40"))),
    ("flbk", float(os.environ.get("CBRSIM_TFLBK_YM", "120")), float(os.environ.get("CBRSIM_TFLBK_YP", "252")), float(os.environ.get("CBRSIM_TFLBK_C", "200"))),
]


def near_mask_eval(cur, plain, changed):
    """cur(表示中),plain(target): (C,8,8,3)。changed のうち、輝度・色差の変化が十分小さいものを
    True(=更新省略)。各画素の輝度差の平均(dYm)/最大(dYp)と色差の平均(dCm)で見る。形の変化も
    “変化量”として効くが、厳しい構造ゲート(SSIM/エッジ)は外し、しきいを緩めて「形は軽く」効かせる。"""
    o = cur.astype(np.float64); r = plain.astype(np.float64)
    dY = np.abs(o @ _LWv - r @ _LWv)
    dYm = dY.mean(axis=(1, 2)); dYp = dY.max(axis=(1, 2))
    dCm = np.sqrt((o @ _CBv - r @ _CBv) ** 2 + (o @ _CRv - r @ _CRv) ** 2).mean(axis=(1, 2))
    t = NEAR_F3
    return changed & (dYm <= t['Ym']) & (dYp <= t['Yp']) & (dCm <= t['C'])
# L3(PRG-RAM victim cache): VRAMから追い出したパターンを捨てずRAMに退避しておき、
# 再登場したらCDから読み直さずRAM→VRAM DMAで復帰させる(CDバイト0)。CDが唯一の
# ボトルネックなのでDMAは実質フリー扱い。0=無効(既定)。512KB/32B=16384枚。
L3_TILES = int(os.environ.get("CBRSIM_L3", "0"))
NO_PANELS = bool(os.environ.get("CBRSIM_NOPANELS"))   # 計測専用: 解析パネルPNGの書き出しを省く
# PRG-RAM先読みバッファ: 再生前にPRGへ載せた静的タイル集合(pickle set of pattern keys)。
# ここにあるパターンは再生中いつでもCD 0バイト(RAM→VRAM DMAのみ)で出せる=Fill扱い。
PRG_PRELOAD_PATH = os.environ.get("CBRSIM_PRG_PRELOAD", "")
# VBVモード: PRG-RAMを「帯域の貯水池(漏れバケツ)」として使う。全編先読み割当をやめ、毎フレーム
# CD=一定量(frame_cd)を注ぎ、イージーフレームの余りをタンクに貯め、ハードフレームはタンクから引いて
# Miss を埋める。空になった時だけ Miss。開始時は満タン(B0=CAP)。タンク容量 CAP=TANK_KB。
# 実機はVBV必須なので sim は VBV専用(env CBRSIM_VBV は無視して常にON)。非VBVパスは未使用。
VBV_ON = True
# タンク容量は tools/av_config.py の単一真実源(=実機の使えるリング RING_CAP)から取る。
# 旧既定414や実行時440はリング物理容量を超えており、simが実機より広いバッファを仮定して
# 実機で枯渇していた。envで上書き可(実験用)だが、既定はconfigから導出=pack/playerと一致。
import av_config
TANK_KB = int(os.environ.get("CBRSIM_TANK_KB", str(av_config.TANK_KB)))
TANK_CAP_BYTES = TANK_KB * 1024
# 格上げパス(既定ON): 余ったCD + タンクの余剰で、近似(Near/Coa/Flbk)や持ち越しをRaw/Bufに格上げ。
# 0で無効(=従来の帯域余し挙動に戻せる, 比較用)。
UPGRADE_ON = os.environ.get("CBRSIM_UPGRADE", "1") != "0"
# cold(=新規パターン転送: Raw+Buf)の1コマ上限。実機MDの実時間デコード天井対策
# (BUDGETS.md 'Encoder cap')。超過セルは Flbk近似 or Miss繰越。0=無効。
# 1コマの cold 上限は「実機が1コマで描画できる新規タイル数」= 物理描画限界(90/VBLANK)を
# エンコードfpsから計算(av_config.cold_cap_for_fps): 15fps→360, 24fps→225, 30fps→180。
# uncapped(0)は禁止(実機で描画不能なバーストを sim が見逃すため)。CBRSIM_MAX_COLD を正の値で
# 明示した時だけ上書き(特殊ケース用)。frame0 は下の frame_max_cold で別途免除。
_mc = os.environ.get("CBRSIM_MAX_COLD", "").strip()
MAX_COLD = int(_mc) if (_mc and int(_mc) > 0) else av_config.cold_cap_for_fps(FPS)
# タンク(Buff)の温存率。Coa〜Miss(劣化の重い格上げ)にはこの割合を最低残す=将来の劣化タイル需要用に予約。
# Nearの格上げは「余裕があるとき」だけ=より高い割合を残す(NEAR_RESERVE)まで温存。終盤はrampで両方0へ。
UPGRADE_RESERVE = float(os.environ.get("CBRSIM_UPGRADE_RESERVE", "0.4"))       # Coa〜Miss用に最低4割予約
UPGRADE_NEAR_RESERVE = float(os.environ.get("CBRSIM_UPGRADE_NEAR_RESERVE", "0.7"))  # Nearは7割超の余裕時のみ
# 終盤rampの広さ(タンク1杯を吐くコマ数の倍率)。広いほど画質上昇が緩やか(一気に上がらない)。
UPGRADE_RAMP = float(os.environ.get("CBRSIM_UPGRADE_RAMP", "5"))
# Issue#5: Miss0(破綻していない)フレームは、格上げに使える余り帯域のこの割合をTank回復に予約し
# 将来の重いフレームに備える(最大40%)。Missがあるフレームは破綻回復を優先=予約しない。0で無効。
TANK_RECOVER_RESERVE = float(os.environ.get("CBRSIM_TANK_RECOVER_RESERVE", "0.40"))
# 近似流用(Near/Coa/Flbk)が「この秒数」以上そのまま居座ったら、格上げ優先度を Miss級(sev=0)へ
# 昇格させる。一過性の近似は目に見えないが、居座った近似は静的なゴースト=視線が固定される。時間で切る
# のは知覚(何秒出続けたか)がfps非依存だから(重み付けaging=予算コンテストのフレーム数とは別軸)。0で無効。
GHOST_ESCALATE_SEC = float(os.environ.get("CBRSIM_GHOST_ESCALATE_SEC", "0.3"))
GHOST_ESCALATE_N = max(1, round(GHOST_ESCALATE_SEC * FPS)) if GHOST_ESCALATE_SEC > 0 else 0
# issue #10: near_keep(現在表示がほぼ同一なら0Bで維持)を「現在表示が正確(cell_tier==9)」なセルに限定。
# 近似表示(Coa/Flbk)を入力に Near 判定すると近似が居座る(ゴースト)ため。0で旧挙動(近似表示も維持可)。
NEAR_KEEP_ACCURATE_ONLY = os.environ.get("CBRSIM_NEAR_ACCURATE_ONLY", "1") != "0"

# 出力量子化で「位置固定の規則ディザ(Bayer 8x8)」を掛ける。同じ画面座標は常に同じ閾値なので
# 静止タイルは毎コマ同一の333のまま=差分/使い回しを壊さない(誤差拡散は波及するので不採用)。
# 前処理のディザ除去(master抽出)はそのまま。掛け直すのは出力の333化のここだけ。
DITHER_ON = os.environ.get("CBRSIM_DITHER", "1") != "0"   # 既定ON。OFFは CBRSIM_DITHER=0（例外時のみ）
# 深い暗転で区切り、暗転の瞬間に区間別60色パレットへ差し替える(CRAM総入替)。
SEGPAL_ON = os.environ.get("CBRSIM_SEGPAL", "1") != "0"   # 既定ON。OFFは CBRSIM_SEGPAL=0（例外時のみ）
PAL_ALGO = normalize_palette_algo()                          # stl4 (legacy) / mosaic-gm (opt-in while tuning)
PAL_WRITE_BYTES = 0             # CRAM pre-load(PALTAB): 全区間パレットはヘッダ直後のPALTAB領域で
                                # 一括配送しMain-RAM表から引くので、切替フレームの予算控除は無し
                                # (ストリームには1Bの区間参照だけ。旧: in-stream 128B/切替)
_BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]], float)
_BAYER_T = np.tile((_BAYER8 + 0.5) / 64.0, (H // 8 + 1, W // 8 + 1))[:H, :W].astype(np.float32)


def to_rgb333(img888):
    """RGB888(H,W,3)->RGB333。CBRSIM_DITHER時は位置固定ディザ(静止タイルは毎コマ同一)=Bayer。"""
    if not DITHER_ON:
        return rgb888_to_rgb333(img888)
    f = img888.astype(np.float32) * (7.0 / 255.0)
    base = np.floor(f)
    frac = f - base
    return np.clip(base + (frac > _BAYER_T[..., None]), 0, 7).astype(np.uint8)   # Bayerディザ


def detect_palette_segments(frames):
    """Return the legacy dark/uniform candidate ranges without training them."""
    n = len(frames)
    LWv = np.array([.299, .587, .114])
    SEG_GAP = int(os.environ.get("CBRSIM_SEG_GAP", "24"))
    SEG_MIN = int(os.environ.get("CBRSIM_SEG_MIN", "2"))
    DARK_THR = float(os.environ.get("CBRSIM_SEG_DARK", "0.90"))
    UNI_THR = float(os.environ.get("CBRSIM_SEG_UNIFORM", "0.88"))
    UNI_TOL = float(os.environ.get("CBRSIM_SEG_UNIFORM_TOL", "24"))
    UNI_NEAR = int(os.environ.get("CBRSIM_SEG_UNIFORM_NEAR", "8"))
    dark = np.zeros(n)
    uniform = np.zeros(n)
    for i in range(n):
        image = np.asarray(Image.open(frames[i]).convert("RGB")).astype(float)
        dark[i] = ((image @ LWv) < 32).mean()
        distance = np.sqrt(((image - image.reshape(-1, 3).mean(0)) ** 2).sum(2))
        uniform[i] = (distance < UNI_TOL).mean()

    def cluster(metric, hit):
        hits = np.where(hit)[0]
        bounds = []
        if len(hits):
            start = previous = int(hits[0])
            for value in hits[1:]:
                value = int(value)
                if value - previous <= SEG_GAP:
                    previous = value
                else:
                    bounds.append(start + int(np.argmax(metric[start:previous + 1])))
                    start = previous = value
            bounds.append(start + int(np.argmax(metric[start:previous + 1])))
        return bounds

    dark_bounds = cluster(dark, dark >= DARK_THR)
    uniform_bounds = cluster(uniform, uniform >= UNI_THR)
    additions = [value for value in uniform_bounds
                 if min([abs(value - dark_value) for dark_value in dark_bounds] + [1 << 30]) > UNI_NEAR]
    edges = sorted(set([0, *dark_bounds, *additions, n]))
    return [
        (edges[index], edges[index + 1])
        for index in range(len(edges) - 1)
        if edges[index + 1] - edges[index] >= SEG_MIN
    ]


def segment_and_train(frames):
    """Train STL4 unchanged or select MOSAIC-GM lines and useful CRAM segments."""
    n = len(frames)

    def load_tiles(indices):
        return np.concatenate([
            tile_blocks(to_rgb333(np.asarray(Image.open(frames[int(index)]).convert("RGB"))))
            for index in indices
        ], axis=0)

    def sample_indices(start, end, count, half_step=False):
        length = end - start
        count = min(length, max(1, int(count)))
        if count == length:
            return np.arange(start, end, dtype=np.int64)
        offset = 0.5 if half_step else 0.0
        return np.unique(np.clip(
            start + ((np.arange(count) + offset) * length / count).astype(np.int64),
            start, end - 1,
        ))

    if PAL_ALGO == STL4:
        def train_stl4(indices):
            return np.stack(build_palettes(load_tiles(indices), n_pal=4)).astype(np.uint8)

        pals_arr = train_stl4(range(0, n, 6))
        frame_seg = np.zeros(n, np.int32)
        seg_pals = [pals_arr]
        seg_bounds = []
        if SEGPAL_ON:
            segments = detect_palette_segments(frames)
            seg_pals = [
                train_stl4(range(start, end, max(1, (end - start) // 60)))
                for start, end in segments
            ]
            frame_seg[:] = -1
            for segment, (start, end) in enumerate(segments):
                frame_seg[start:end] = segment
            current = 0
            for frame in range(n):
                if frame_seg[frame] < 0:
                    frame_seg[frame] = current
                else:
                    current = int(frame_seg[frame])
            seg_bounds = [
                frame for frame in range(1, n)
                if frame_seg[frame] != frame_seg[frame - 1]
            ]
        stats = {
            "algo": STL4,
            "global": {"active_lines": 4, "training_stride": 6},
            "candidate_segments": len(seg_pals),
            "selected_segments": len(seg_pals),
        }
        return pals_arr, seg_pals, frame_seg, seg_bounds, stats

    sample_counts = sorted({
        max(1, int(value))
        for value in os.environ.get("CBRSIM_PAL_SAMPLE_COUNTS", "120,240,480").split(",")
        if value.strip()
    })
    validation_count = int(os.environ.get("CBRSIM_PAL_VALIDATE_FRAMES", "120"))
    validation_indices = sample_indices(0, n, validation_count, half_step=True)
    validation_tiles = load_tiles(validation_indices)
    validation_flat, _detail = flatten_low_detail(validation_tiles)
    validation_evaluator = PaletteEvaluator(validation_flat)
    candidates = []
    seen_counts = set()
    for requested in sample_counts:
        indices = sample_indices(0, n, requested)
        if len(indices) in seen_counts:
            continue
        seen_counts.add(len(indices))
        training = load_tiles(indices)
        palettes, train_stats = build_mosaic_palettes(training, n_pal=4, return_stats=True)
        active = int(train_stats["active_lines"])
        validation = score_palettes(
            validation_flat, palettes[:active], evaluator=validation_evaluator,
            core_colors=int(train_stats["core_colors"]),
        )
        record = {
            **train_stats,
            "training_frames": len(indices),
            "validation_frames": len(validation_indices),
            "validation": validation.summary(),
            "validation_score": validation.score,
        }
        candidates.append((validation.score, active, len(indices), np.stack(palettes), record))
        print(
            f"[MOSAIC-GM] global sample={len(indices)} validation="
            f"{validation.summary()['score_per_pixel']:.6f} active={active}"
        )
    _score, _active, _count, pals_arr, global_stats = min(
        candidates, key=lambda item: (item[0], item[1], item[2]))
    pals_arr = np.asarray(pals_arr, dtype=np.uint8)
    global_active = int(global_stats["active_lines"])

    frame_seg = np.zeros(n, np.int32)
    if not SEGPAL_ON:
        stats = {
            "algo": MOSAIC_GM,
            "global": global_stats,
            "global_candidates": [record for *_head, record in candidates],
            "candidate_segments": 1,
            "selected_segments": 1,
        }
        return pals_arr, [pals_arr], frame_seg, [], stats

    # A one-line zero-error validation candidate receives an exact all-frame
    # proof. This is a rendered-result decision, not a source-colour-count rule.
    exact_global = False
    if global_active == 1 and global_stats["validation"]["pixel_error_per_pixel"] == 0:
        cost, _index = palette_lut(pals_arr[0], squared=True)
        exact_global = True
        for frame, path in enumerate(frames):
            tiles = tile_blocks(to_rgb333(np.asarray(Image.open(path).convert("RGB"))))
            flat, _detail = flatten_low_detail(tiles)
            if np.any(cost[rgb333_keys(flat)]):
                exact_global = False
                print(f"[MOSAIC-GM] global one-line proof stopped at frame {frame}")
                break
        if exact_global:
            print(f"[MOSAIC-GM] global one-line RGB333 identity proved for all {n} frames")

    if exact_global:
        stats = {
            "algo": MOSAIC_GM,
            "global": global_stats,
            "global_candidates": [record for *_head, record in candidates],
            "global_exact_all_frames": True,
            "candidate_segments": 1,
            "selected_segments": 1,
        }
        return pals_arr, [pals_arr], frame_seg, [], stats

    segments = detect_palette_segments(frames)
    segment_train_count = int(os.environ.get("CBRSIM_PAL_SEG_TRAIN_FRAMES", "240"))
    segment_validation_count = int(os.environ.get("CBRSIM_PAL_SEG_VALIDATE_FRAMES", "60"))
    segment_rel = float(os.environ.get("CBRSIM_PAL_SEG_GAIN_REL", "0.005"))
    segment_abs = float(os.environ.get("CBRSIM_PAL_SEG_GAIN_ABS", "0.002"))
    selected = []
    segment_stats = []
    for start, end in segments:
        train_indices = sample_indices(start, end, segment_train_count)
        local_palettes, local_stats = build_mosaic_palettes(
            load_tiles(train_indices), n_pal=4, return_stats=True)
        validate_indices = sample_indices(start, end, segment_validation_count, half_step=True)
        validate_tiles = load_tiles(validate_indices)
        validate_flat, _detail = flatten_low_detail(validate_tiles)
        evaluator = PaletteEvaluator(validate_flat)
        local_active = int(local_stats["active_lines"])
        local_score = score_palettes(
            validate_flat, local_palettes[:local_active], evaluator=evaluator,
            core_colors=int(local_stats["core_colors"]),
        )
        global_score = score_palettes(
            validate_flat, pals_arr[:global_active], evaluator=evaluator,
            core_colors=int(global_stats["core_colors"]),
        )
        improvement = global_score.score - local_score.score
        relative = improvement / max(1.0, global_score.score)
        per_pixel = improvement / max(1, len(validate_flat) * 64)
        use_local = improvement > 0 and relative >= segment_rel and per_pixel >= segment_abs
        selected.append(np.asarray(local_palettes if use_local else pals_arr, dtype=np.uint8))
        segment_stats.append({
            "start": int(start), "end": int(end),
            "training_frames": len(train_indices),
            "validation_frames": len(validate_indices),
            "local": local_stats,
            "local_score_per_pixel": local_score.summary()["score_per_pixel"],
            "global_score_per_pixel": global_score.summary()["score_per_pixel"],
            "relative_gain": relative,
            "gain_per_pixel": per_pixel,
            "selected": "local" if use_local else "global",
        })

    # Consecutive candidate segments that select the same palette need no CRAM
    # switch and collapse to one frame_seg epoch automatically.
    seg_pals = []
    frame_seg[:] = -1
    for (start, end), palettes in zip(segments, selected):
        if not seg_pals or not np.array_equal(seg_pals[-1], palettes):
            seg_pals.append(palettes)
        frame_seg[start:end] = len(seg_pals) - 1
    current = 0
    for frame in range(n):
        if frame_seg[frame] < 0:
            frame_seg[frame] = current
        else:
            current = int(frame_seg[frame])
    seg_bounds = [
        frame for frame in range(1, n)
        if frame_seg[frame] != frame_seg[frame - 1]
    ]
    stats = {
        "algo": MOSAIC_GM,
        "global": global_stats,
        "global_candidates": [record for *_head, record in candidates],
        "global_exact_all_frames": False,
        "candidate_segments": len(segments),
        "selected_segments": len(seg_pals),
        "segments": segment_stats,
    }
    return pals_arr, seg_pals, frame_seg, seg_bounds, stats


OUT = sim_work_dir()
# 実機TTRCエンコード用の決定ログ出力先。既定off(mp4出力に一切影響しない・追加のみ)。
# 毎フレームの「更新セル(cell,pal,key)」＋区間パレットを吐き、pack_streamが再生してTTRC化する。
_EMIT_DEC_ENV = os.environ.get("CBRSIM_EMIT_DEC", "").strip()
# Boolean-looking values select the conventional file beside the other sim
# artifacts.  An explicit path remains supported for one-off comparisons.
EMIT_DEC = (str(OUT / "decisions.pkl")
            if _EMIT_DEC_ENV.lower() in {"1", "true", "yes", "on"}
            else _EMIT_DEC_ENV)


def border_weight_mask():
    w = np.ones((TROWS, TCOLS), np.float64)
    w[:BORDER_TILES, :] = BORDER_WEIGHT
    w[-BORDER_TILES:, :] = BORDER_WEIGHT
    w[:, :BORDER_TILES] = BORDER_WEIGHT
    w[:, -BORDER_TILES:] = BORDER_WEIGHT
    return w.reshape(-1)


def flatten_low_detail(tiles):
    """tiles (C,64,3) uint8 rgb333 -> (平坦化後, detail (C,))"""
    f = tiles.astype(np.float64)
    detail = f.std(axis=1).mean(axis=1)
    mean = f.mean(axis=1)
    out = tiles.copy()
    m = detail < FLATTEN_STD
    out[m] = np.round(mean[m]).astype(np.uint8)[:, None, :]
    return out, detail


def assign_palette(flat_tiles, pals_arr):
    """flat_tiles (C,64,3) rgb333 -> assign (C,) 最良パレット(RGB二乗誤差が最小の面)。"""
    keys = rgb333_keys(flat_tiles)
    cost = np.stack([palette_lut(pal, squared=True)[0] for pal in pals_arr])
    err = cost[:, keys].sum(2, dtype=np.int64).T               # (C,4)
    return err.argmin(1).astype(np.int8)


def idx_for(pixels, assign, pals_arr):
    """pixels (C,64,3) を、各セルの assign パレットで最近傍量子化 -> idx (C,64) 1..15"""
    keys = rgb333_keys(pixels)
    index = np.stack([palette_lut(pal, squared=True)[1] for pal in pals_arr])
    return (index[assign[:, None], keys] + 1).astype(np.uint8)


def render_cells(idx, assign, pals_arr):
    """idx (C,64) 1..15, assign (C,) -> rgb888 (C,8,8,3)"""
    C = idx.shape[0]
    full16 = np.zeros((4, 16, 3), np.uint8)
    full16[:, 1:] = pals_arr
    rgb333 = full16[assign[:, None], idx]                       # (C,64,3)
    return rgb333_to_rgb888(rgb333).reshape(C, TILE, TILE, 3)


def pin_p0_debug_extremes(seg_pals):
    """Put the darkest colour at P0/index1 and brightest at P0/index15.

    This runs before quantisation. It only permutes the existing 4x15 CRAM
    colours; no RGB333 value is created or changed. Moving colours between
    palette rows can alter the best per-tile palette choice, so every frame is
    quantised against the final grouping afterwards.
    """
    canonical = []
    dark_swaps = []
    bright_swaps = []
    for seg, src in enumerate(seg_pals):
        old = np.asarray(src, np.uint8)
        if old.shape != (4, 15, 3):
            raise ValueError(f"segment {seg} palette shape {old.shape}, expected (4, 15, 3)")
        new = old.copy()

        brightness = new.astype(np.int16).sum(axis=2)
        darkest = int(brightness.min())
        if int(brightness[0, 0]) == darkest:
            dark_row, dark_slot = 0, 0
        else:
            dark_row, dark_slot = map(int, np.argwhere(brightness == darkest)[0])
        new[0, 0], new[dark_row, dark_slot] = (
            new[dark_row, dark_slot].copy(), new[0, 0].copy())

        # Recompute after the first swap so the second source location remains
        # exact even if P0/index1 originally held a globally brightest colour.
        brightness = new.astype(np.int16).sum(axis=2)
        brightest = int(brightness.max())
        if int(brightness[0, 14]) == brightest:
            bright_row, bright_slot = 0, 14
        else:
            bright_row, bright_slot = map(int, np.argwhere(brightness == brightest)[0])
        new[0, 14], new[bright_row, bright_slot] = (
            new[bright_row, bright_slot].copy(), new[0, 14].copy())

        old_code = ((old[:, :, 0].astype(np.int16) << 6)
                    | (old[:, :, 1].astype(np.int16) << 3)
                    | old[:, :, 2].astype(np.int16))
        new_code = ((new[:, :, 0].astype(np.int16) << 6)
                    | (new[:, :, 1].astype(np.int16) << 3)
                    | new[:, :, 2].astype(np.int16))
        if not np.array_equal(np.sort(old_code, axis=None), np.sort(new_code, axis=None)):
            raise AssertionError(f"segment {seg} colour multiset changed")
        final_brightness = new.astype(np.int16).sum(axis=2)
        if int(final_brightness[0, 0]) != int(final_brightness.min()):
            raise AssertionError(f"segment {seg} P0 index1 is not globally darkest")
        if int(final_brightness[0, 14]) != int(final_brightness.max()):
            raise AssertionError(f"segment {seg} P0 index15 is not globally brightest")

        canonical.append(new)
        dark_swaps.append((dark_row, dark_slot + 1))
        bright_swaps.append((bright_row, bright_slot + 1))

    return canonical, {
        "segments": len(canonical),
        "dark_swapped_segments": sum(pos != (0, 1) for pos in dark_swaps),
        "bright_swapped_segments": sum(pos != (0, 15) for pos in bright_swaps),
        "dark_sources": dark_swaps,
        "bright_sources": bright_swaps,
    }


def canonicalize_p0_index15(seg_pals, frame_seg, assigns, pidxs):
    """Put a globally brightest nonzero colour at P0 index 15, losslessly.

    Quantisation must run *before* this function with the original palette
    order.  For each segment, the complete row containing the first globally
    brightest RGB-sum colour is swapped with row 0, then that colour is swapped
    with P0 slot 15.  Tile palette assignments and 1..15 pixel indices receive
    the same permutations.  This avoids nearest-colour tie changes and proves
    every quantised RGB333 pixel is identical.  Hardware index 0 stays fixed in
    every row and is never part of either permutation.

    Returns ``(canonical_palettes, stats)`` and updates ``assigns`` and
    ``pidxs`` in place.
    """
    if len(assigns) != len(pidxs) or len(assigns) != len(frame_seg):
        raise ValueError("palette canonicalization frame arrays have different lengths")

    canonical = []
    originals = []
    row_remaps = []
    index_remaps = []
    row_swaps = []
    index_swaps = []
    for seg, src in enumerate(seg_pals):
        old = np.asarray(src, np.uint8)
        if old.shape != (4, 15, 3):
            raise ValueError(f"segment {seg} palette shape {old.shape}, expected (4, 15, 3)")
        new = old.copy()
        brightness = old.astype(np.int16).sum(axis=2)
        brightest = int(brightness.max())
        # Avoid every permutation when the fixed destination is already tied
        # for globally brightest.  Otherwise choose the first CRAM-order max,
        # matching the old 68000 scanner's deterministic tie behaviour.
        if int(brightness[0, 14]) == brightest:
            src_row, src_slot = 0, 14
        else:
            src_row, src_slot = map(int, np.argwhere(brightness == brightest)[0])

        row_remap = np.arange(4, dtype=np.uint8)          # old row -> new row
        if src_row != 0:
            new[[0, src_row]] = new[[src_row, 0]]
            row_remap[0] = src_row
            row_remap[src_row] = 0
        index_remap = np.arange(16, dtype=np.uint8)       # old index -> new; 0 stays 0
        if src_slot != 14:
            new[0, [src_slot, 14]] = new[0, [14, src_slot]]
            index_remap[src_slot + 1] = 15
            index_remap[15] = src_slot + 1

        # The two permutations preserve the complete 4x15 colour multiset,
        # including duplicates, rather than merely its distinct set.
        old_code = ((old[:, :, 0].astype(np.int16) << 6)
                    | (old[:, :, 1].astype(np.int16) << 3)
                    | old[:, :, 2].astype(np.int16))
        new_code = ((new[:, :, 0].astype(np.int16) << 6)
                    | (new[:, :, 1].astype(np.int16) << 3)
                    | new[:, :, 2].astype(np.int16))
        if not np.array_equal(np.sort(old_code, axis=None), np.sort(new_code, axis=None)):
            raise AssertionError(f"segment {seg} colour multiset changed")
        if int(new[0, 14].astype(np.int16).sum()) != brightest:
            raise AssertionError(f"segment {seg} P0 index15 is not globally brightest")
        if int(index_remap[0]) != 0:
            raise AssertionError("reserved palette index 0 was remapped")

        canonical.append(new)
        originals.append(old.copy())
        row_remaps.append(row_remap)
        index_remaps.append(index_remap)
        row_swaps.append((src_row, 0))
        index_swaps.append((src_slot + 1, 15))

    verified_pixels = 0
    reassigned_tiles = 0
    reindexed_pixels = 0
    for i, (assign, idx) in enumerate(zip(assigns, pidxs)):
        seg = int(frame_seg[i])
        if seg < 0 or seg >= len(canonical):
            raise ValueError(f"frame {i} refers to invalid palette segment {seg}")
        assign = np.asarray(assign)
        idx = np.asarray(idx)
        if idx.shape[0] != assign.shape[0]:
            raise ValueError(f"frame {i} assign/index cell count mismatch")
        before_assign = assign.copy()
        before_idx = idx.copy()
        if before_idx.size and (int(before_idx.min()) < 1 or int(before_idx.max()) > 15):
            raise AssertionError(f"frame {i} contains an index outside 1..15")
        after_assign = row_remaps[seg][before_assign]
        after_idx = before_idx.copy()
        new_p0 = after_assign == 0
        after_idx[new_p0] = index_remaps[seg][before_idx[new_p0]]

        before_rgb = originals[seg][before_assign[:, None], before_idx - 1]
        after_rgb = canonical[seg][after_assign[:, None], after_idx - 1]
        if not np.array_equal(before_rgb, after_rgb):
            raise AssertionError(f"frame {i} RGB changed while canonicalizing P0 index15")
        assign[:] = after_assign
        idx[:] = after_idx
        reassigned_tiles += int((before_assign != after_assign).sum())
        reindexed_pixels += int((before_idx != after_idx).sum())
        verified_pixels += int(before_idx.size)

    stats = {
        "segments": len(canonical),
        "row_swapped_segments": sum(a != b for a, b in row_swaps),
        "index_swapped_segments": sum(a != b for a, b in index_swaps),
        "reassigned_tiles": reassigned_tiles,
        "reindexed_pixels": reindexed_pixels,
        "verified_pixels": verified_pixels,
        "row_swaps": row_swaps,
        "index_swaps": index_swaps,
    }
    return canonical, stats


def cells_to_image(cell_rgb):
    return cell_rgb.reshape(TROWS, TCOLS, TILE, TILE, 3).transpose(0, 2, 1, 3, 4).reshape(H, W, 3)


# --- フレーム独立の量子化(読込→333化→タイル化→パレット割当→索引→レンダ)を並列化 ---
# 差分/VBV本体は逐次(前フレーム状態に依存)だが、ここは各フレーム独立=実行時間の大半。
# ワーカー数は PC の CPU コア数-2(動的)。env CBRSIM_WORKERS で上書き可、1で逐次。
_WG = {}


def _quant_init(frames, seg_pals, frame_seg):
    _WG["frames"] = frames
    _WG["seg_pals"] = seg_pals
    _WG["frame_seg"] = frame_seg


def _quant_one(i):
    # 重い部分(割当/索引)だけ並列で。plain_rgb は逐次側で render_cells(軽い)＝IPCを小さく保つ。
    cur_pals = _WG["seg_pals"][int(_WG["frame_seg"][i])]
    m333 = to_rgb333(np.asarray(Image.open(_WG["frames"][i]).convert("RGB")))
    flat, detail = flatten_low_detail(tile_blocks(m333))
    assign = assign_palette(flat, cur_pals)
    pidx = idx_for(flat, assign, cur_pals)
    return detail.astype(np.float32), assign, pidx


def _quant_one_flat(i):
    # GPU モード用: 割当/索引は GPU 側でやるので、ここは読込→333化→タイル化→平坦化まで。
    # 各ワーカーは cupy に触れない(fork と CUDA は両立しない)。flat を親へ返す。
    m333 = to_rgb333(np.asarray(Image.open(_WG["frames"][i]).convert("RGB")))
    flat, detail = flatten_low_detail(tile_blocks(m333))
    return detail.astype(np.float32), flat.astype(np.uint8)


def n_workers():
    env = os.environ.get("CBRSIM_WORKERS")
    if env:
        return max(1, int(env))
    return max(1, (os.cpu_count() or 4) - 2)      # PCのCPUコア数-2(毎回動的に取得)


def precompute_quant(frames, seg_pals, frame_seg):
    """各フレームの (detail, assign, plain_idx, plain_rgb) を並列に前計算して返す。"""
    n = len(frames)
    w = n_workers()
    import gpu_quant
    if gpu_quant.enabled():
        # CPU(並列)で読込/333化/タイル化 → GPU で割当/索引。imap で両者を重ねる
        # (ワーカーが flat を出す傍から親GPUが処理＝CPU I/OとGPU計算を並行)。
        print(f"precompute quantization: {n} frames, CPU load x{w} + GPU assign/idx ...", flush=True)
        details = [None] * n
        assigns = [None] * n
        pidxs = [None] * n
        cache = gpu_quant.PalCache()
        if w > 1:
            import multiprocessing as mp
            with mp.get_context("fork").Pool(
                    w, initializer=_quant_init, initargs=(frames, seg_pals, frame_seg)) as pool:
                for i, (det, flat) in enumerate(pool.imap(_quant_one_flat, range(n), chunksize=8)):
                    details[i] = det
                    assigns[i], pidxs[i] = gpu_quant.assign_idx_one(
                        flat, int(frame_seg[i]), seg_pals, cache)
        else:
            _quant_init(frames, seg_pals, frame_seg)
            for i in range(n):
                det, flat = _quant_one_flat(i)
                details[i] = det
                assigns[i], pidxs[i] = gpu_quant.assign_idx_one(
                    flat, int(frame_seg[i]), seg_pals, cache)
        return (details, assigns, pidxs)

    print(f"precompute quantization: {n} frames on {w} workers ...", flush=True)
    if w > 1:
        import multiprocessing as mp
        with mp.get_context("fork").Pool(
                w, initializer=_quant_init, initargs=(frames, seg_pals, frame_seg)) as pool:
            Q = pool.map(_quant_one, range(n), chunksize=8)
    else:
        _quant_init(frames, seg_pals, frame_seg)
        Q = [_quant_one(i) for i in range(n)]
    return ([q[0] for q in Q], [q[1] for q in Q], [q[2] for q in Q])


def main():
    global DEDITHER_VF, RAW_VF
    if not DEDITHER_VF or not RAW_VF:
        src_w, src_h, src_sar_num, src_sar_den = probe_source(SRC)
        if SOURCE_SAR_OVERRIDE:
            src_sar_num, src_sar_den = parse_ratio(SOURCE_SAR_OVERRIDE)
        # H32/H40のHARを考慮した既定変換。明示的なVF指定は優先する。
        DEDITHER_VF = DEDITHER_VF or source_filter(
            MODE, W, H, src_w, src_h,
            src_sar_num=src_sar_num, src_sar_den=src_sar_den,
            fit=GEOMETRY_FIT)
        RAW_VF = RAW_VF or raw_filter(
            MODE, W, H, src_w, src_h,
            src_sar_num=src_sar_num, src_sar_den=src_sar_den,
            fit=GEOMETRY_FIT)
    # 各処理フェーズの所要時間を計測し、終了時にフレームあたり秒数付きで報告する。
    _t_all = time.perf_counter()
    _phases = []

    def _mark(name, t0):
        dt = time.perf_counter() - t0
        _phases.append((name, dt))
        return time.perf_counter()
    _t = time.perf_counter()

    # CBRSIM_REUSE=1: 既に展開済みの master/raw/audio を再利用し ffmpeg 展開を省く
    # (レイアウト調整でパネルだけ描き直したいとき用。OUTは丸ごとクリアしない)。
    reuse = bool(os.environ.get("CBRSIM_REUSE"))
    master_dir = OUT / "master"     # ディザ除去済み(量子化入力)
    raw_dir = OUT / "raw"           # 生のオリジナル(比較TR用)
    cached = reuse and any(master_dir.glob("*.png")) and any(raw_dir.glob("*.png"))
    if cached:
        print("CBRSIM_REUSE: cached master/raw/audio を再利用(ffmpeg展開をスキップ)")
    else:
        prepare_dir(OUT, clean=True)
        for d in (master_dir, raw_dir):
            prepare_dir(d, clean=True)
        print("extracting de-dithered master (256x144) ...")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", "0", "-t", DURATION, "-i", SRC,
             "-vf", f"{DEDITHER_VF},fps={FPS_STR}", str(master_dir / "%05d.png")])
        print("extracting raw original (320x144) ...")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", "0", "-t", DURATION, "-i", SRC,
             "-vf", f"{RAW_VF},fps={FPS_STR}", str(raw_dir / "%05d.png")])
        print(f"extracting audio ({AUDIO_LABEL}) ...")
        for old in OUT.glob("audio_*.wav"):     # 別形式の残骸を除去(REUSE時の取り違え防止)
            old.unlink()
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", "0", "-t", DURATION, "-i", SRC,
             "-vn", "-ac", "1", "-ar", str(AUDIO_RATE), "-acodec", AUDIO_FFCODEC,
             str(OUT / AUDIO_FILE)])
    _t = _mark("展開(reuse)" if cached else "抽出(ffmpeg)", _t)
    frames = sorted(master_dir.glob("*.png"))
    n = len(frames)
    print(f"  {n} frames @ {W}x{H} ({TCOLS}x{TROWS}={C_CELLS} cells)")

    # PRG先読みパッチ: {frame_idx: set(pattern_key)}。カット到達時にそのセル群を
    # バッファから適用する。パターンもcell->tile対応も事前ロード済みなのでCDバイト0。
    prg_patch = {}
    if PRG_PRELOAD_PATH and Path(PRG_PRELOAD_PATH).exists():
        import pickle
        prg_patch = pickle.load(open(PRG_PRELOAD_PATH, "rb"))
        uniq = len(set().union(*prg_patch.values())) if prg_patch else 0
        print(f"  PRG先読み(patch): {len(prg_patch)}カット, distinct {uniq} tiles "
              f"({uniq*PATTERN_BYTES/1024:.0f}KB) をロード時バッファ")

    print(f"training palettes ({PAL_ALGO})  DITHER={DITHER_ON} SEGPAL={SEGPAL_ON} NEAR={NEAR_ON} ...")
    _global_pals, seg_pals, frame_seg, seg_bounds, palette_stats = segment_and_train(frames)
    if SEGPAL_ON:
        print(f"  per-segment palettes: {len(seg_pals)}区間, CRAM差替 {len(seg_bounds)}点 "
              f"(candidates={palette_stats['candidate_segments']})")
    _t = _mark("パレット学習", _t)

    main_dir = OUT / "preview"      # SEGA-CD 実出力(ゴースト有り)
    catmap_dir = OUT / "catmap"     # Same/Dedup/Buf を縁取り(Raw/Missは枠なし)
    misscarry_dir = OUT / "misscarry"  # Miss(赤)/MissCarry(amber) を縁取り
    if not NO_PANELS:
        for d in (main_dir, catmap_dir, misscarry_dir):
            prepare_dir(d, clean=True)

    border_mask = border_weight_mask()
    border_bool = border_mask < 1.0        # 外周2タイル(True)。ここのMissは飢餓に数えない
    # 中央距離(同点tie-break用): 画面中心からの二乗距離。小さい=中央=優先。
    _rr, _cc = np.mgrid[0:TROWS, 0:TCOLS].astype(np.float64)
    center_dist = ((_rr - (TROWS - 1) / 2) ** 2 + (_cc - (TCOLS - 1) / 2) ** 2).reshape(-1)

    # --- 状態 ---
    cur_rgb = np.zeros((C_CELLS, TILE, TILE, 3), np.uint8)      # 表示中の各セル
    cur_key = [None] * C_CELLS          # 表示中パターン(idx bytes)
    cur_pal = np.full(C_CELLS, -1, np.int16)
    committed_plain = [None] * C_CELLS  # 直近commitした plain パターン(内容変化検出用)
    from tile_alloc import TileAllocator
    # 共有割り当て(連続, pack と同一コード)。これが residency の真の源=pack の realized と一致=cap=realized。
    # 判定は前フレーム末の状態を参照し、割り当て(スロット付与+追い出し)は各フレーム末に cell順で実行
    # (=pack の resolve と同一順)。VRAM_TILES=pack POOL。
    alloc = TileAllocator(C_CELLS, VRAM_TILES, 1)
    ref_count = {}                       # pattern key -> 参照セル数(repoint用に保持, residencyはallocが真)
    l3 = {}                             # L3(PRG-RAM) victim cache: pattern key -> last_used frame
    pat_rgb = {}                        # 近似dedup: pattern key -> 代表rgb(8,8,3) uint8
    pat_sig = {}                        # pattern key -> 2×2低周波(12,) float32
    pat_pal = {}                        # pattern key -> 表示パレット(assign)
    pat_seg = {}                        # pattern key -> このタイルの index列を量子化した区間(パレットエポック)。
                                        # 区間跨ぎのNear/Coa/near_keep流用は、旧区間の index列を新CRAMで
                                        # 引くと虹色ゴミになる(harness/palette_flashで実証)。cur_segと一致
                                        # するタイルだけ流用可。dedup(fresh keyが常駐keyと一致)は index列が
                                        # そのまま新区間の量子化なので安全=対象外。
    coa_bucket = defaultdict(list)      # 平均色バケツ -> [key,...] (末尾=最新)

    def touch(key, fi):
        pass                            # residency は alloc が真(コマ末に cell順で place)

    def demote_to_l3(key, t):
        if L3_TILES <= 0:
            return
        l3[key] = t
        while len(l3) > L3_TILES:
            del l3[min(l3, key=l3.get)]

    def ensure_capacity(fi):
        pass                            # 追い出しは alloc.place が担う(コマ末の cell順割り当て)

    # CRAMエミュレーション: 表示中タイルは「インデックス列(disp_idx)」と「パレット行(disp_pal)」で
    # 保持し、cur_rgb は現区間パレットで毎フレーム引き直す(=実機の挙動)。これにより区間跨ぎで
    # 旧インデックス列が新CRAMでゴミ化する現象がプレビュー/near判定にそのまま現れる。
    disp_idx = np.zeros((C_CELLS, TILE * TILE), np.uint8)   # 表示中タイルのindex列(0=bg,1..15)
    disp_pal = np.zeros(C_CELLS, np.int32)                  # 表示中タイルのパレット行

    def repoint(c, key, pal, rgb, fi):
        old = cur_key[c]
        if old is not None:
            ref_count[old] -= 1
            if ref_count[old] <= 0:
                ref_count.pop(old, None)
        cur_key[c] = key
        ref_count[key] = ref_count.get(key, 0) + 1
        cur_pal[c] = pal
        disp_idx[c] = np.frombuffer(key, np.uint8)
        disp_pal[c] = pal
        cur_rgb[c] = rgb                                     # 暫定(このフレーム末に引き直す)
        touch(key, fi)

    wait = np.zeros(C_CELLS, np.int32)         # 未更新のdirtyが継続したフレーム数(エージング/滞留)
    cell_tier = np.zeros(C_CELLS, np.int8)     # 現在の表示劣化度(0=Miss,1=Flbk,2=Coa,3=Near,9=正確)
    approx_carry = np.zeros(C_CELLS, np.int32)  # 近似(tier<9)のまま持ち越した連続コマ数(格上げ/正確化で0)
    upgrade_log = []                            # 毎コマ: 格上げ枚数 / まだ近似のセル数(指標)
    guniq = {k: set() for k in ("same", "near", "coa", "flbk")}  # 全編で使った別タイル(ユニーク数)

    frame_bytes_log = []
    tile_records_log = []      # パターン転送数(=32B支払い回数)
    name_records_log = []      # ネームテーブル書換数
    dedup_saved_log = []       # dedupで節約したパターン転送数(L1/L2=VRAM常駐ヒット)
    l3_hits_log = []           # L3(PRG-RAM)ヒット数(CD再読みを回避できた再登場パターン)
    prg_hits_log = []          # PRG先読みヒット数(事前ロード済みで0CD Fillできたタイル)
    coa_hits_log = []          # 粗い近似dedup(Coa)ヒット数
    stat_rows = []             # per-frame status line 用の実測値
    stale_rows = []            # per-frame の Miss(stale)マスク(packbits, 72B/frame)
    wait_hist_rows = []        # per-frame の繰越年齢分布(MissCarryバー用)
    starved_frames = 0
    audio_sent_total = 0
    dec_frames = []            # 実機決定ログ: 各要素 = そのフレームの [(cell, pal, key), ...]
    dec_miss = []              # per-frame Miss数(デバッグオーバーレイ用。デコード側では算出不能)
    dec_cats = []              # per-frame カテゴリ数[raw,same,near,coa,flbk,buf,miss](デバッグ欄用)
    tank = TANK_CAP_BYTES if VBV_ON else 0        # 貯水池残量(bytes)。開始時=満タン
    tank_tiles_log = []                           # 毎フレームのタンク残量(タイル換算)
    cd_used_log = []                              # 毎フレームの有効CD使用量(=FRAME_BYTES - パディング捨て分)

    # DEBUG色はCRAMに既にある色だけを並べ替えて固定する。異なるパレット行との
    # 入替があり得るので、全フレームを最終的な行構成に対して量子化する前に行う。
    seg_pals, pal_extreme_stats = pin_p0_debug_extremes(seg_pals)
    print(f"  P0 DEBUG colours pinned: index1 darkest swaps "
          f"{pal_extreme_stats['dark_swapped_segments']}/{pal_extreme_stats['segments']}, "
          f"index15 brightest swaps "
          f"{pal_extreme_stats['bright_swapped_segments']}/{pal_extreme_stats['segments']}")

    # フレーム独立の割当/索引を並列で前計算(実行時間の大半)。以降のループは逐次(状態依存)。
    Q_detail, Q_assign, Q_pidx = precompute_quant(frames, seg_pals, frame_seg)
    # The older lossless index-15 canonicalizer is now a no-op for current
    # palettes because both DEBUG extremes were pinned before quantisation.
    # Keep the proof here while older palette inputs remain supported.
    seg_pals, pal15_stats = canonicalize_p0_index15(
        seg_pals, frame_seg, Q_assign, Q_pidx)
    # palettes.bin is the legacy fallback CRAM image.  In segmented mode the
    # separately trained global palette was never the actual initial CRAM, so
    # write canonical segment 0 and keep every consumer aligned with PALTAB.
    (OUT / "palettes.bin").write_bytes(pals_to_bytes(list(seg_pals[0])))
    np.savez(OUT / "seg_palettes.npz",
             seg_pals=np.asarray(seg_pals, np.uint8),
             frame_seg=np.asarray(frame_seg, np.int32))
    print(f"  P0 index15 globally brightest: row swaps "
          f"{pal15_stats['row_swapped_segments']}/{pal15_stats['segments']}, "
          f"index swaps {pal15_stats['index_swapped_segments']}/{pal15_stats['segments']}; "
          f"RGB identity verified for {pal15_stats['verified_pixels']} pixels "
          f"({pal15_stats['reassigned_tiles']} tile assignments and "
          f"{pal15_stats['reindexed_pixels']} indices remapped, frame0 included)")
    _t = _mark("量子化", _t)

    _t_render = 0.0        # ループ内訳: 描画+PNG保存に費やした時間(残りがcommit/探索)
    # PNG保存(3枚/コマ)を裏スレッドへ。PILの圧縮はGILを解放するので実並列=次コマのcommitと重なる。
    from concurrent.futures import ThreadPoolExecutor
    import collections as _collections
    _png_pool = None if NO_PANELS else ThreadPoolExecutor(max_workers=6)
    _png_futs = _collections.deque()

    def _save_png(arr, path):
        if len(_png_futs) >= 96:          # 背圧: 生成が速すぎてもメモリ膨張を防ぐ
            _png_futs.popleft().result()
        _png_futs.append(_png_pool.submit(lambda a=arr, p=path: Image.fromarray(a, "RGB").save(p)))

    for i in range(n):
        pal_swap = SEGPAL_ON and i > 0 and int(frame_seg[i]) != int(frame_seg[i - 1])
        if pal_swap:
            cur_pal[:] = -1                                     # CRAM総入替→全セルを再評価(暗転中で安価)
        cur_pals = seg_pals[int(frame_seg[i])]
        cur_seg = int(frame_seg[i])                            # 現フレームのパレットエポック
        # CRAMエミュ: 表示中タイルを現区間パレットで引き直す(pal_swap時は全セルが新CRAMで再色付け
        # =区間跨ぎの旧タイルはここでゴミ色になる)。near/near_keep判定はこの実表示色に対して行う。
        if i > 0:
            cur_rgb[:] = render_cells(disp_idx, disp_pal, cur_pals)
        detail = Q_detail[i]; assign = Q_assign[i]; plain_idx = Q_pidx[i]  # 前計算済み(並列)
        plain_rgb = render_cells(plain_idx, assign, cur_pals)  # 軽いので逐次(IPC回避)
        plain_keys = [plain_idx[c].tobytes() for c in range(C_CELLS)]
        # 近似dedup用: 各タイルの2×2低周波(12,)と平均色バケツ座標(常時計算・軽い)
        sig2 = plain_rgb.reshape(C_CELLS, 2, 4, 2, 4, 3).mean((2, 4)).reshape(C_CELLS, 12).astype(np.float32)
        mbk = (plain_rgb.reshape(C_CELLS, 64, 3).mean(1) // COA_BW).astype(np.int32)

        # 内容変化検出(dither非依存: plain同士で比較)
        key_changed = np.fromiter(
            (plain_keys[c] != committed_plain[c] for c in range(C_CELLS)), bool, C_CELLS)
        pal_changed = assign.astype(np.int16) != cur_pal
        changed = key_changed | pal_changed

        diff = np.abs(plain_rgb.astype(np.int32) - cur_rgb.astype(np.int32)).sum(axis=(1, 2, 3))
        detail_norm = detail / (detail.max() + 1e-6)
        # キャリーオーバー型エージング: 待たされたdirtyタイルほど優先度を底上げ(乗算)。
        aging = 1.0 + AGING_ALPHA * np.minimum(wait, WAIT_CAP)
        # 優先度 = RGB総和の変化量 × タイル内の細かさ × エージング × 枠重み
        score = diff.astype(np.float64) * (1.0 + DETAIL_ALPHA * detail_norm) * aging * border_mask
        # Near: 変化タイルのうち見た目ほぼ同じ(F3)は先に省略(old表示を維持)。買い戻し(Raw更新)は
        # 準備金を食い潰すので入れない(=配給とセットでしか成立しないため今は無し)。
        # MIDFAR時は Near も統合探索(commit_unified)で判定するので事前フィルタしない。
        near = near_mask_eval(cur_rgb, plain_rgb, changed) if (NEAR_ON and not MIDFAR_ON) else np.zeros(C_CELLS, bool)
        # 同点tie-break: CENTERTIE_ON なら中央優先(lexsort: 主=-score, 副=center_dist)。
        # 既定は従来どおり argsort(-score)=不定(実機用simの決定を変えないため)。
        order = np.lexsort((center_dist, -score)) if CENTERTIE_ON else np.argsort(-score)
        order = [int(c) for c in order if changed[c] and not near[c]]

        if AUDIO_KIND == "pcm13":
            audio_due = AUDIO_FRAME_BYTES             # fixed 444B@N2 / 888B@N4, packと一致
        else:
            audio_due = round((i + 1) * AUDIO_RATE * AUDIO_BPS / FPS) - audio_sent_total
            audio_sent_total += audio_due

        # 純CBR: 毎フレーム固定バイトのみ。繰り越し無し。強制更新も無し(CBR厳守)。
        # パレット差替フレームはCRAM書換分だけ予算を引く(暗転中なので影響は小)。
        budget = max(FRAME_BYTES - audio_due - NAME_BYTES - (PAL_WRITE_BYTES if pal_swap else 0), 0)
        frame_cd = budget                             # このフレーム自身のCDタイル予算
        # frame0はDAT冒頭の専用ヘッダとしてboot中に時間無制限でVRAMへロードする(=ストリーミング
        # リング/Tankを一切消費しない)。よってframe0は予算無制限で全面フルロードし、Tankは
        # 満タンのままframe1へ渡す(下のtank更新もスキップ)。実機の崩壊はframe0の大バーストが
        # リングを削っていたのが原因で、ヘッダ化で根絶する。
        tile_budget = (1 << 30) if i == 0 else frame_cd + (tank if VBV_ON else 0)
        frame_patch = frozenset() if VBV_ON else prg_patch.get(i, frozenset())   # VBVは全編先読み割当を使わない

        updated = np.zeros(C_CELLS, bool)
        dedup_mask = np.zeros(C_CELLS, bool)   # 更新したが同一パターン流用(VRAM常駐)だったタイル
        prg_mask = np.zeros(C_CELLS, bool)     # PRG先読みバッファから0CDで埋めたタイル
        raw_mask = np.zeros(C_CELLS, bool)     # 新規CD転送したタイル(Raw)
        coa_mask = np.zeros(C_CELLS, bool)     # 近い常駐を流用したタイル(Coa)
        near_mask = np.zeros(C_CELLS, bool)    # MIDFAR: ほぼ同一常駐を流用/維持(Near)
        flbk_mask = np.zeros(C_CELLS, bool)    # MIDFAR: Missのフォールバック(荒くても常駐で穴埋め)(Flbk)
        flbk_hits = 0
        loaded_keys = set()
        tile_recs = 0
        name_recs = 0
        dedup_saved = 0
        l3_hits = 0
        prg_hits = 0
        coa_hits = 0
        spent_tiles = 0
        cold_spent = 0             # このコマのcold数(Raw+Buf=実機のパターンDMA数)
        # frame0はDAT冒頭ヘッダで別ロード(リング非消費)なので常にcold上限を免除=全面フルロード。
        frame_max_cold = MAX_COLD if i > 0 else 0

        def find_approx(c):
            """平坦なコールドタイルcに、見た目(2×2低周波)が近い常駐パターンを探す。無ければNone。"""
            if detail[c] >= COA_DETAIL:
                return None
            b = (int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2]))
            s = sig2[c]; cnt = 0
            for ck in reversed(coa_bucket[b]):
                if not alloc.is_resident(ck) and ck not in loaded_keys:
                    continue                                     # もう常駐に無い(退避済み)
                if pat_seg.get(ck) != cur_seg:                   # 区間跨ぎ近似流用の禁止
                    continue
                cs = pat_sig.get(ck)
                if cs is None:
                    continue
                d = np.abs(cs - s)
                if d.mean() <= COA_MEAN and d.max() <= COA_MAX:
                    return ck
                cnt += 1
                if cnt >= COA_K:
                    break
            return None

        def commit_plain(c):
            # メインパス: まず安く全部埋める。cold平坦タイルは Coa(NAMEのみ) を優先=飢餓を出さない。
            # (タンク満杯時の余りCDでの Raw 格上げは後段の格上げパスで行う)
            nonlocal tile_recs, name_recs, dedup_saved, l3_hits, prg_hits, coa_hits, spent_tiles, cold_spent
            key = plain_keys[c]
            in_vram = alloc.is_resident(key) or key in loaded_keys      # L1/L2: VRAM常駐(転送ゼロ)
            approx_key = find_approx(c) if (COA_ON and not in_vram) else None
            in_prg = (not in_vram) and (approx_key is None) and key in frame_patch
            in_l3 = (not in_vram) and (approx_key is None) and (not in_prg) and L3_TILES > 0 and key in l3
            free = in_vram or in_l3 or (approx_key is not None)   # パターン転送不要(ネームのみ)
            cost = 0 if in_prg else (NAME_BYTES + (0 if free else PATTERN_BYTES))
            if spent_tiles + cost > tile_budget:
                return False
            rep_key = key; rep_pal = int(assign[c]); rep_rgb = plain_rgb[c]
            if in_vram:
                dedup_saved += 1; dedup_mask[c] = True; pat_seg[key] = cur_seg
            elif approx_key is not None:                          # 粗い近似dedup: 常駐の見た目を流用
                coa_hits += 1; coa_mask[c] = True
                rep_key = approx_key; rep_pal = pat_pal[approx_key]; rep_rgb = pat_rgb[approx_key]
                loaded_keys.add(approx_key)
            elif in_prg:
                if frame_max_cold and cold_spent >= frame_max_cold:
                    return False                                  # cold上限: 今コマは見送り(Miss繰越)
                cold_spent += 1
                prg_hits += 1; prg_mask[c] = True; loaded_keys.add(key)
            elif in_l3:
                l3_hits += 1; loaded_keys.add(key); l3.pop(key, None)
            else:                                                # cold: パターンをVRAMへ(自CD=Raw or 貯水池=Buf)
                if frame_max_cold and cold_spent >= frame_max_cold:
                    return False                                  # cold上限: 今コマは見送り(Miss繰越)
                cold_spent += 1
                loaded_keys.add(key)
                if VBV_ON and spent_tiles >= frame_cd:
                    prg_hits += 1; prg_mask[c] = True
                else:
                    tile_recs += 1; raw_mask[c] = True
                pat_rgb[key] = plain_rgb[c]; pat_sig[key] = sig2[c]; pat_pal[key] = int(assign[c]); pat_seg[key] = cur_seg
                coa_bucket[(int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2]))].append(key)
            name_recs += 1; spent_tiles += cost
            repoint(c, rep_key, rep_pal, rep_rgb, i)
            committed_plain[c] = key; updated[c] = True
            return True

        # === 統合探索(MIDFAR): Same/Near/Coa/Flbk/Miss を1つのVRAM最良一致に統合 ===
        if MIDFAR_ON:
            # 現在表示がほぼ同一=0Bで維持可。ただし near_keep の入力 cur_rgb は「現在表示」なので、前フレームが
            # Coa/Flbk の近似コピーだと、その近似をさらに Near として維持=ゴーストが居座る(issue #10)。
            # よって現在表示が正確(cell_tier==9)なセルに限定。近似中のセルは best_resident で正解基準に再評価。
            near_keep = near_mask_eval(cur_rgb, plain_rgb, changed)   # 現在表示がほぼ同一=0Bで維持可
            if NEAR_KEEP_ACCURATE_ONLY:
                near_keep = near_keep & (cell_tier == 9)
            # 区間跨ぎ維持の禁止: 現在表示タイルが別区間(パレットエポック)由来なら、その index列を
            # 現CRAMで引くとゴミ化する。維持不可=強制更新にする(harness/palette_flashで実証)。
            for c in np.where(near_keep)[0]:
                ck = cur_key[c]
                if ck is None or pat_seg.get(ck) != cur_seg:
                    near_keep[int(c)] = False
            p_lum = plain_rgb @ _LWv; p_cb = plain_rgb @ _CBv; p_cr = plain_rgb @ _CRv  # (C,8,8)

            def best_resident(c):
                """target に最も近い常駐候補 (key, dYm, dYp, dCm)。無ければ (None,大,大,大)。
                平均色バケツで前絞り→候補のF3(画素輝度差平均/最大・色差平均)をベクトル計算し最小を採る。"""
                tl = p_lum[c]; tcb = p_cb[c]; tcr = p_cr[c]
                cand = []
                b = (int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2]))
                cnt = 0
                for ck in reversed(coa_bucket[b]):
                    if (not alloc.is_resident(ck) and ck not in loaded_keys) or ck not in pat_rgb:
                        continue
                    if pat_seg.get(ck) != cur_seg:       # 区間跨ぎ近似流用の禁止(index列が新CRAMでゴミ化)
                        continue
                    cand.append(ck); cnt += 1
                    if cnt >= COA_K:
                        break
                if not cand:
                    return (None, 1e9, 1e9, 1e9)
                arr = np.stack([pat_rgb[k] for k in cand]).astype(np.float64)   # (N,8,8,3)
                dY = np.abs(arr @ _LWv - tl)
                dYm = dY.mean((1, 2)); dYp = dY.max((1, 2))
                dCm = np.sqrt((arr @ _CBv - tcb) ** 2 + (arr @ _CRv - tcr) ** 2).mean((1, 2))
                j = int(np.argmin(dYm + 0.3 * dYp + 0.5 * dCm))
                return (cand[j], float(dYm[j]), float(dYp[j]), float(dCm[j]))

            def tier_of(dYm, dYp, dCm):
                for ti, (_nm, Ym, Yp, C) in enumerate(MIDFAR_TIERS):
                    if dYm <= Ym and dYp <= Yp and dCm <= C:
                        return ti          # 0=near,1=coa,2=flbk
                return -1

            def commit_unified(c):
                nonlocal tile_recs, name_recs, dedup_saved, prg_hits, coa_hits, flbk_hits, spent_tiles, cold_spent
                key = plain_keys[c]
                # 1. 現在表示がほぼ同一 → Near維持(0B, 更新なし・Missでもない)=帯域優先
                if near_keep[c]:
                    near_mask[c] = True
                    return
                exact = alloc.is_resident(key) or key in loaded_keys
                if exact:
                    bk, tier = key, 0
                else:
                    bk, dYm, dYp, dCm = best_resident(c)
                    tier = tier_of(dYm, dYp, dCm) if bk is not None else -1
                # 2. 良い流用(Same=exact / Near=tier0 / Coa=tier1) → 常駐を指す(2B)
                if bk is not None and 0 <= tier <= 1 and spent_tiles + NAME_BYTES <= tile_budget:
                    if exact:
                        dedup_saved += 1; dedup_mask[c] = True                # Same(完全一致流用=Sameへ畳む)
                        rk, rp, rr = key, int(assign[c]), plain_rgb[c]
                        pat_seg[key] = cur_seg                                # fresh keyは現区間の量子化=有効化
                    elif tier == 0:
                        near_mask[c] = True; rk, rp, rr = bk, pat_pal[bk], pat_rgb[bk]
                    else:
                        coa_hits += 1; coa_mask[c] = True; rk, rp, rr = bk, pat_pal[bk], pat_rgb[bk]
                    loaded_keys.add(rk); name_recs += 1; spent_tiles += NAME_BYTES
                    repoint(c, rk, rp, rr, i); committed_plain[c] = key; updated[c] = True
                    return
                # 3. 中途半端(flbk/none) → まず正確ロード(自CD=Raw / 貯水池=Buf)。
                #    cold上限到達時はロードせず 4.のFlbk近似へ(Missより良い穴埋め)
                cost = NAME_BYTES + PATTERN_BYTES
                if spent_tiles + cost <= tile_budget and not (frame_max_cold and cold_spent >= frame_max_cold):
                    cold_spent += 1
                    loaded_keys.add(key)
                    if VBV_ON and spent_tiles >= frame_cd:
                        prg_hits += 1; prg_mask[c] = True
                    else:
                        tile_recs += 1; raw_mask[c] = True
                    pat_rgb[key] = plain_rgb[c]; pat_sig[key] = sig2[c]; pat_pal[key] = int(assign[c]); pat_seg[key] = cur_seg
                    coa_bucket[(int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2]))].append(key)
                    name_recs += 1; spent_tiles += cost
                    repoint(c, key, int(assign[c]), plain_rgb[c], i); committed_plain[c] = key; updated[c] = True
                    return
                # 4. ロード不可(予算/貯水池尽き) → Flbk 近似流用(2B)で穴埋め(Missのフォールバック)。
                #    改善モード(既定): 絶対しきいに縛らず、現在表示より少しでも target に近づく候補なら採る。
                #    絶対モード(CBRSIM_FLBK_IMPROVE_ONLY=0): flbk tier(絶対しきい)内の候補のみ。
                if bk is not None and not exact and spent_tiles + NAME_BYTES <= tile_budget:
                    if FLBK_IMPROVE_ONLY:
                        cur = cur_rgb[c].astype(np.float64)
                        tgt = plain_rgb[c].astype(np.float64)
                        dY0 = np.abs(cur @ _LWv - tgt @ _LWv)
                        dC0 = np.sqrt((cur @ _CBv - tgt @ _CBv) ** 2 +
                                     (cur @ _CRv - tgt @ _CRv) ** 2)
                        old_score = float(dY0.mean() + 0.3 * dY0.max() + 0.5 * dC0.mean())
                        new_score = dYm + 0.3 * dYp + 0.5 * dCm
                        if new_score >= old_score - FLBK_MIN_IMPROVE:
                            return          # 改善しない(僅少含む) → Miss
                    elif tier != 2:
                        return              # 絶対しきいモード: flbk tier外はMiss
                    flbk_hits += 1; flbk_mask[c] = True
                    loaded_keys.add(bk); name_recs += 1; spent_tiles += NAME_BYTES
                    repoint(c, bk, pat_pal[bk], pat_rgb[bk], i); committed_plain[c] = key; updated[c] = True
                    return
                # 5. Miss(何もしない)

        # 優先度順(予算内)。買えない高優先はスキップし、安い(常駐)セルは拾う
        for c in order:
            (commit_unified if MIDFAR_ON else commit_plain)(c)

        # 格上げパス: 余ったCD + タンクの余剰(reserveは温存)で、近似(Near/Coa/Flbk)や
        # 持ち越し(前コマまで近似で今コマ変化なし)を Raw/Buf(正確) に格上げ。Bufの余りを画質へ回す。
        upgraded = 0
        if UPGRADE_ON and VBV_ON:
            # 終盤(残り≈タンクを吐き切れるコマ数×UPGRADE_RAMP)ほど reserve を線形に0へ→タンクを吐き切る。
            # rampが広いほど画質上昇は緩やか(一気に上がらない)。Coa〜Miss=budget_lo(4割予約),
            # Near=budget_hi(7割予約=余裕時のみ)。将来の劣化タイル需要のためタンクを一定残す。
            ramp = max(1, int(UPGRADE_RAMP * TANK_CAP_BYTES / max(frame_cd, 1)))
            rf = max(0.0, min(1.0, (n - 1 - i) / ramp))
            budget_lo = frame_cd + max(0, tank - int(TANK_CAP_BYTES * UPGRADE_RESERVE * rf))
            budget_hi = frame_cd + max(0, tank - int(TANK_CAP_BYTES * UPGRADE_NEAR_RESERVE * rf))
            # Issue#5: このフレームが破綻していない(内側Missが無い)なら、余り帯域の一部をTank回復に予約
            # (格上げ予算を圧縮=使わない分が貯水池に戻る)。終盤rampでは温存不要なので徐々に解除。
            _near_eff = near_mask if MIDFAR_ON else near
            # Flbk は Miss のフォールバック(荒い近似)なので「未解決」に含める。これらだけのフレームを
            # 「破綻なし」と見なすと Tank回復予約が働いて格上げ予算を絞り、Flbk が Raw に上がりにくくなる。
            inner_miss = int((((changed & ~updated & ~_near_eff) | flbk_mask) & ~border_bool).sum())
            if inner_miss == 0 and TANK_RECOVER_RESERVE > 0:
                keep = 1.0 - TANK_RECOVER_RESERVE * rf      # 40%をTank回復へ(終盤ほど解除)
                budget_lo = spent_tiles + int(max(0, budget_lo - spent_tiles) * keep)
                budget_hi = spent_tiles + int(max(0, budget_hi - spent_tiles) * keep)
            if spent_tiles < budget_lo:
                def raw_upgrade(c, lim):
                    nonlocal tile_recs, name_recs, dedup_saved, coa_hits, spent_tiles, upgraded, cold_spent
                    key = plain_keys[c]
                    in_vram = alloc.is_resident(key) or key in loaded_keys
                    cost = NAME_BYTES if in_vram else NAME_BYTES + PATTERN_BYTES
                    if spent_tiles + cost > lim:
                        return
                    if (not in_vram) and frame_max_cold and cold_spent >= frame_max_cold:
                        return                                   # cold上限: 格上げ見送り(近似のまま)
                    if coa_mask[c]:
                        coa_mask[c] = False; coa_hits -= 1
                    near_mask[c] = False; flbk_mask[c] = False   # 近似を取消
                    if in_vram:
                        dedup_saved += 1; dedup_mask[c] = True
                    else:
                        cold_spent += 1
                        loaded_keys.add(key); tile_recs += 1; raw_mask[c] = True
                        pat_rgb[key] = plain_rgb[c]; pat_sig[key] = sig2[c]; pat_pal[key] = int(assign[c]); pat_seg[key] = cur_seg
                        coa_bucket[(int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2]))].append(key)
                    name_recs += 1; spent_tiles += cost
                    repoint(c, key, int(assign[c]), plain_rgb[c], i)
                    committed_plain[c] = key; updated[c] = True; upgraded += 1
                carried = (cell_tier < 9) & ~changed            # 変化せず近似のまま持ち越し(安定Near/Coa等)
                cand_mask = near_mask | coa_mask | flbk_mask | carried
                sev = np.full(C_CELLS, 9, np.int16)             # 劣化が重い順に格上げ(sev小=先)
                sev[carried] = cell_tier[carried]
                sev[flbk_mask] = 1; sev[coa_mask] = 2; sev[near_mask] = 3
                # 0.3秒以上居座った近似ゴーストは Miss級(sev=0)へ昇格: Near温存(budget_hi)の壁を越え、
                # 手厚い budget_lo レーンで最優先に正確化(Rawへ差替)。表示は届くまで近似のまま=悪化しない。
                if GHOST_ESCALATE_N:
                    sev[(approx_carry >= GHOST_ESCALATE_N) & cand_mask] = 0
                for c in sorted((int(x) for x in np.where(cand_mask)[0]),
                                key=lambda c: (int(sev[c]), -int(approx_carry[c]), -score[c])):
                    lim = budget_lo if sev[c] <= 2 else budget_hi   # Flbk〜Miss=lo, Near=hi(余裕時のみ)
                    if spent_tiles >= lim and sev[c] <= 2:
                        break                                        # Coa〜Miss予算尽き=以降も不可
                    raw_upgrade(c, lim)

        # 貯水池更新(漏れバケツ): このフレームのCD余り(frame_cd-使った分)を貯める / 引いた分を減らす
        if VBV_ON:
            # 有効CD使用量 = FRAME_BYTES - パディング(タンク満杯で貯めきれず捨てた余り)。CDは毎コマ
            # FRAME_BYTES を読み、内訳は 音声+ネーム+CRAM+フラグ等の固定分 + 映像書込 + 貯蓄。捨てた分だけが無効。
            over = max(0, tank + frame_cd - spent_tiles - TANK_CAP_BYTES)
            cd_used_log.append(FRAME_BYTES - over)
            if i == 0:
                tank = TANK_CAP_BYTES        # header: frame0はリング/Tankを消費せず満タン維持
            else:
                tank = min(TANK_CAP_BYTES, max(0, tank + frame_cd - spent_tiles))
            tank_tiles_log.append(tank // PATTERN_BYTES)

        # 共有割り当て: このフレームの更新セルを cell順で place(=pack の resolve と同一順・同一コード)。
        # ここで residency/追い出しが確定し、次フレームの cold 判定に反映される。維持(near_keep)セルは
        # 更新でないので place しない=cur_slot/slot_refs が前回のまま(参照継続で保護)。realized=cap の要。
        upd_ck = [(int(c), cur_key[int(c)]) for c in np.where(updated)[0]
                  if cur_key[int(c)] is not None]
        alloc.place_frame(upd_ck, i)
        ensure_capacity(i)

        # CRAMエミュ: このフレームの全更新を反映した最終表示を、現区間パレットで引き直す。
        # プレビュー/カテゴリマップ/miss繰越は全てこの実表示色(=実機と同じ)で描く。
        cur_rgb[:] = render_cells(disp_idx, disp_pal, cur_pals)

        # 実機決定ログ: このフレームで実際に書き換えたセルの (cell, パレット, 表示パターンkey)。
        # keyは64バイト(idx 1..15)を内包=pack_streamがそこから32Bパターンを復元できる。
        # Coaはcur_key=近似先(常駐), Buf/Rawはcur_key=新規ロードkey。dedup/Near/Missの区別は
        # 「更新したか否か」に畳まれる(更新セルのみ列挙)ので、実機はmp4を完全再現できる。
        if EMIT_DEC:
            dec_frames.append([(int(c), int(cur_pal[c]), cur_key[c]) for c in np.where(updated)[0]])

        bytes_spent = spent_tiles + audio_due + NAME_BYTES
        frame_bytes_log.append(bytes_spent)
        tile_records_log.append(tile_recs)
        name_records_log.append(name_recs)
        dedup_saved_log.append(dedup_saved)
        l3_hits_log.append(l3_hits)
        prg_hits_log.append(prg_hits)
        coa_hits_log.append(coa_hits)

        # --- per-frame 実測(status line用) ---
        near_eff = near_mask if MIDFAR_ON else near   # MIDFARは統合探索が埋めたnear_mask
        stale = changed & ~updated & ~near_eff    # Nearは取りこぼしではない(意図的スキップ)
        near_disp = near_eff & ~updated           # 実際に省略したNear(余裕があればRaw済み=除く)
        # 優先度レイヤー/格上げ用に各セルの現在の劣化度を更新(触れたセルのみ。未変化セルは前値を保持)
        if MIDFAR_ON:
            cell_tier[dedup_mask | raw_mask | prg_mask] = 9              # 正確(Same/Raw/Buf)
            cell_tier[near_eff] = 3                                      # Near(近い近似=格上げ候補)
            cell_tier[coa_mask] = 2; cell_tier[flbk_mask] = 1
            cell_tier[stale] = 0                                          # Miss(取りこぼし)
            approx_carry = np.where(cell_tier < 9, approx_carry + 1, 0)  # 近似のまま持ち越した連続コマ数
            upgrade_log.append((upgraded, int((cell_tier < 9).sum())))   # 指標: 格上げ枚数 / まだ近似のセル数
        # カテゴリ別ユニークタイル数(何枚の別タイルを使い回したか)。同一キーは1枚と数える。
        no_update = ~changed

        def _uk(mask):
            return {cur_key[c] for c in np.where(mask)[0] if cur_key[c] is not None}
        u_same = _uk(no_update | dedup_mask); u_near = _uk(near_eff)
        u_coa = _uk(coa_mask); u_flbk = _uk(flbk_mask)
        guniq["same"] |= u_same; guniq["near"] |= u_near; guniq["coa"] |= u_coa
        guniq["flbk"] |= u_flbk
        # 飢餓は「内側タイルのMissがある時」だけ。外周2タイルのMissは許容(数えない)。
        if (stale & ~border_bool).any():
            starved_frames += 1
        stale_rows.append(np.packbits(stale))
        want = int(changed.sum())
        upd = int(updated.sum())
        miss = int(stale.sum())
        if EMIT_DEC:
            dec_miss.append(miss)
            # デバッグ欄用カテゴリ数: catmap と同一定義(Raw/Buf/Coa/Flbk/Near/Miss は互いに素、
            # 残り=Same(不変+Dedup畳み込み))。7種は必ず C_CELLS に合計する。
            _raw = int(raw_mask.sum()); _buf = int(prg_mask.sum())
            _coa = int(coa_mask.sum()); _flbk = int(flbk_mask.sum())
            _near = int(near_disp.sum())
            _same = int(C_CELLS - _raw - _buf - _coa - _flbk - _near - miss)
            dec_cats.append((_raw, _same, _near, _coa, _flbk, _buf, miss))
        # MissCarry = 前フレームでMissして今も未解決(=stale かつ wait>=1)。旧waitで判定。
        carry_mask = stale & (wait >= 1)
        carry = int(carry_mask.sum())
        # 滞留 = 待たされた連続フレーム数(=wait)。今フレームも未更新なので+1
        age_max = int(wait[stale].max()) + 1 if stale.any() else 0
        # MissCarryバー用: stale タイルの繰越年齢(=wait+1)分布
        ages = np.clip(wait[stale] + 1, 1, NBINS)
        wait_hist_rows.append(np.bincount(ages, minlength=NBINS + 1)[1:NBINS + 1])
        # F = 転送速度から決まる最低保証更新数(全タイル新規=34B想定で予算を割る)
        f_fixed = budget // (PATTERN_BYTES + NAME_BYTES)
        stat_rows.append((
            i, f_fixed, want, upd, miss, C_CELLS - want, dedup_saved, tile_recs, carry, age_max,
            want / C_CELLS, int(near_eff.sum()), coa_hits, flbk_hits, prg_hits,
            len(u_same), len(u_near), len(u_coa), len(u_flbk)))

        # エージングの待ちカウンタ更新: 未更新のdirtyは+1、更新済み/変化なしは0へ
        wait = np.where(changed & ~updated & ~near_eff, wait + 1, 0)   # Nearは滞留させない

        # レンダリング(計測専用モードでは省く)
        if not NO_PANELS:
            _r0 = time.perf_counter()
            _save_png(cells_to_image(cur_rgb), main_dir / f"{i:05d}.png")
            no_update = ~changed
            A = 0.5   # 枠線の不透明度(下地とブレンド=細く・暗く見せる)

            def border(base_rgb, mask, col, wpx=1):
                ii = np.where(mask)[0]
                if not ii.size:
                    return
                c = np.array(col, np.float64)
                for k in range(wpx):                       # wpx=太さ(px)。Flbkは太枠
                    for s in (np.s_[ii, k, :, :], np.s_[ii, TILE - 1 - k, :, :],
                              np.s_[ii, :, k, :], np.s_[ii, :, TILE - 1 - k, :]):
                        base_rgb[s] = A * c + (1 - A) * base_rgb[s]

            # カテゴリマップ: Near/Coa/Buf=細枠, Flbk(橙)=太枠。Raw/Same=枠なし内容表示。
            # Miss は黒(render_analysis 側で赤塗りつぶし)。Dedup(完全一致)は Same に畳む=枠なし。
            cat = cur_rgb.astype(np.float64)
            cat[stale] = 0
            border(cat, near_disp, CAT_NEAR); border(cat, coa_mask, CAT_COA)
            border(cat, flbk_mask, CAT_FLBK, 3)
            border(cat, prg_mask, CAT_BUF, 3)
            _save_png(cells_to_image(cat.clip(0, 255).astype(np.uint8)), catmap_dir / f"{i:05d}.png")

            # Miss/Carry マップ: Miss/Carryタイルだけ内容表示+縁取り(fresh=赤/繰越=amber)。他は黒。
            mc = np.zeros((C_CELLS, TILE, TILE, 3), np.float64)
            mc[stale] = cur_rgb[stale]
            border(mc, stale & ~carry_mask, CAT_MISS); border(mc, carry_mask, CAT_CARRY)
            _save_png(cells_to_image(mc.clip(0, 255).astype(np.uint8)), misscarry_dir / f"{i:05d}.png")
            _t_render += time.perf_counter() - _r0

        if (i + 1) % 200 == 0 or i + 1 == n:
            print(f"  {i+1}/{n}", flush=True)

    if _png_pool is not None:                      # 残りのPNG保存を全て完了させてから閉じる
        for _f in _png_futs:
            _f.result()
        _png_pool.shutdown()
    _loop_total = time.perf_counter() - _t
    _phases.append(("差分ループ:commit/探索", _loop_total - _t_render))
    _phases.append(("差分ループ:描画+PNG保存", _t_render))
    _t = time.perf_counter()

    fb = np.array(frame_bytes_log, np.float64)
    tr = np.array(tile_records_log, np.float64)       # cold miss = 実際にCDから読んだパターン数
    ded = np.array(dedup_saved_log, np.float64)        # L1/L2 VRAM常駐ヒット
    l3h = np.array(l3_hits_log, np.float64)            # L3(PRG-RAM)ヒット
    prh = np.array(prg_hits_log, np.float64)           # PRG先読みヒット
    report = "\n".join([
        f"resolution={W}x{H} cells/frame={C_CELLS} fps={FPS}",
        f"cbr_frame_bytes={FRAME_BYTES} (純CBR, 繰り越し無し)",
        f"avg_bytes_per_frame={fb.mean():.1f} (<= {FRAME_BYTES})",
        f"VRAM_tiles={VRAM_TILES}  L3(PRG-RAM)_tiles={L3_TILES}",
        f"avg_cold_miss_per_frame={tr.mean():.1f} (CDから32B/枚を実際に読んだ数)",
        f"avg_L2_dedup_hit_per_frame={ded.mean():.1f} (VRAM常駐で0転送)",
        f"avg_Coa_hit_per_frame={np.array(coa_hits_log).mean():.1f} (粗い近似dedupで0転送流用, COA={COA_ON})",
        f"avg_L3_hit_per_frame={l3h.mean():.1f} (再登場をRAMから0CDで供給)",
        f"PRG_preload_cuts={0 if VBV_ON else len(prg_patch)}  avg_PRG(Buf)_hit_per_frame={prh.mean():.1f} "
        + ("(貯水池=タンクから0追加CDで充当)" if VBV_ON else "(先読みで0CD Fill)"),
        f"total_CD_pattern_bytes={(tr.sum()+prh.sum())*PATTERN_BYTES:.0f}",
        f"L3_saved_CD_bytes={l3h.sum()*PATTERN_BYTES:.0f} (L3が無ければCD再読みしていた分)",
        f"dedup_saved_ratio={ded.sum()/(tr.sum()+prh.sum()+ded.sum()+l3h.sum()+1e-9):.3f}",
        f"VBV={VBV_ON} tank_cap={TANK_CAP_BYTES//PATTERN_BYTES}tiles"
        + (f" tank残量: 開始{tank_tiles_log[0]}→終了{tank_tiles_log[-1]} 最小{min(tank_tiles_log)}" if VBV_ON else ""),
        f"starved_frames={starved_frames} ({starved_frames/n*100:.1f}%)",
        f"avg_bps={fb.mean()*FPS:.0f} (target={TARGET_RATE}, CD1x={CD_RATE})",
        (f"upgrade(格上げ): 余剰でRaw化 avg {np.mean([u for u, _ in upgrade_log]):.1f}/コマ, "
         f"まだ近似のセル avg {np.mean([a for _, a in upgrade_log]):.1f} (reserve={UPGRADE_RESERVE})"
         if upgrade_log else "upgrade: (off)"),
    ])
    (OUT / "report.txt").write_text(report)
    print(report)

    # status line 用の per-frame 実測を保存
    stats = np.array(stat_rows, np.float64)
    cols = ("frame ffix want updated miss delta dedup tx carry age want_frac near coa flbk buf"
            " same_u near_u coa_u flbk_u")
    budget_tiles = int(np.median(stats[:, 1]))   # ffix中央値 = 固定予算タイル数(fps依存)
    # 全編ユニーク(cattotals併記用): same/near/coa/flbk の別タイル総数
    cat_uniq = np.array([len(guniq["same"]), len(guniq["near"]), len(guniq["coa"]),
                         len(guniq["flbk"])], np.int64)
    np.savez(OUT / "stats.npz", stats=stats, cols=cols, fps=FPS, cells=C_CELLS,
             target=TARGET_RATE, cd1x=CD_RATE, frame_bytes=FRAME_BYTES, cat_uniq=cat_uniq,
             audio_label=AUDIO_LABEL, audio_frame_bytes=AUDIO_FRAME_BYTES,
             budget_tiles=budget_tiles,
             wait_hist=np.array(wait_hist_rows), nbins=NBINS)
    np.save(OUT / "miss_masks.npy", np.array(stale_rows, np.uint8))   # (n,72) packbits
    if VBV_ON:                                          # 貯水池残量カーブを実測から保存(下段Bufマップ/メーター用)
        rem = np.array(tank_tiles_log, np.int64)
        np.savez(OUT / "buffer_remaining.npz", remaining=rem, total=TANK_CAP_BYTES // PATTERN_BYTES,
                 cd_used=np.array(cd_used_log, np.int64))   # 有効CD使用量(音声+全ヘッダ+映像+貯蓄, パディング除く)
    print(f"wrote {main_dir}, {catmap_dir}, {misscarry_dir}; stats.npz + miss_masks.npy saved")

    # 実機TTRCエンコード用の決定ログ(既定off)。品質決定(区間パレット/ディザ/Near/Coa/VBV/fill)は
    # すべてこのログに畳み込まれる=pack_streamは再生するだけでmp4と同じ画を出せる(唯一の真実源)。
    if EMIT_DEC:
        import pickle
        pickle.dump({
            "geom": (int(TCOLS), int(TROWS), int(C_CELLS), int(TILE)),
            "mode": MODE.upper(),                              # header display mode
            "pal_algo": PAL_ALGO,
            "pal_stats": palette_stats,
            "seg_pals": [np.asarray(p, np.uint8) for p in seg_pals],  # list of (4,15,3)
            "frame_seg": np.asarray(frame_seg, np.int32),
            "frames": dec_frames,                                     # [[(cell,pal,key),...], ...]
            "miss": dec_miss,                                         # per-frame Miss数(overlay用)
            "cats": dec_cats,                                         # per-frame [raw,same,near,coa,flbk,buf,miss]
            "frame_bytes": int(FRAME_BYTES), "audio_rate": int(AUDIO_RATE),
            "audio_frame_bytes": int(AUDIO_FRAME_BYTES), "fps": float(FPS),
            "vram_tiles": int(VRAM_TILES),
            # エンコード時の実効パラメータを焼き込む(pack/解析が同一値を使い二重管理を防ぐ)。
            "max_cold": int(MAX_COLD), "tank_kb": int(TANK_KB),
        }, open(EMIT_DEC, "wb"), protocol=4)
        print(f"  実機決定ログ: {EMIT_DEC} ({len(dec_frames)} frames)")

    _mark("保存(stats/npy/決定ログ)", _t)
    total = time.perf_counter() - _t_all
    import gpu_quant
    gpu_on = gpu_quant.enabled()
    print("\n==== エンコード時間サマリー ("
          f"{n}フレーム {W}x{H} {C_CELLS}セル gpu={'ON' if gpu_on else 'off'}) ====")
    for name, dt in _phases:
        print(f"  {name:<22s} {dt:8.1f}s  ({dt / n * 1000:7.1f} ms/frame  {dt / n:8.4f} s/frame)")
    print(f"  {'合計':<22s} {total:8.1f}s  ({total / n * 1000:7.1f} ms/frame  {total / n:8.4f} s/frame)")


if __name__ == "__main__":
    main()
