#!/usr/bin/env python3
"""SEGA-CD向け差分圧縮 + タイル重複排除のオフライン検証。

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
- 出力量子化では位置固定Bayerディザを常に使う。同じ画面座標には同じ
  閾値を使うため、静止タイルの差分は増やさない。
- **タイル重複排除(dedup)**: MDのネームテーブルは各セル→(パターンslot, パレット)。
  パターン(8x8 idx配列)はパレット非依存なので、同じidxパターンは VRAM に1つ
  だけ置き、複数セル(パレット違いも可)で使い回す。パターン転送32Bを共有でき、
  各セルはネームテーブル2Bのみ。フレーム内・フレーム跨ぎ両方で効く(VRAMを
  LRUキャッシュとしてモデル化, 容量 VRAM_TILES)。
- BODYの物理2/3セクタ配送を先に置き、固定controlを差し引いた残りを更新entry、
  run descriptor、Prg pattern payloadで共有する。軽いフレームの余りは有限の
  全編画質予算へ残し、重いフレームへ回す。
- ゴースト対策(距離加重エージング): Miss/Flbk のまま残るタイルは、
  target と現在表示のRGB平均誤差に応じた age_press を累積して優先度を上げる。
  Near/正確表示になれば圧力は0に戻る。整数 wait はTSVのMiss継続観測だけに使う。
- 音声: 22.05kHz mono IMA ADPCM。Plane B オーバーレイは無し。
"""
import json
import os
import sys
import time
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from encode_config import consume_config_arg, profile_identity  # noqa: E402

# Apply a per-source TOML profile before any CBRSIM-backed module constants are
# evaluated.  The internal environment remains compatible with older scripts,
# but TOML values always win over an inherited shell environment.
_ORIGINAL_ARGV = tuple(sys.argv)
CONFIG_PROFILE = consume_config_arg(
    sys.argv, required=__name__ == "__main__")
import av_config  # noqa: E402
import analysis_style as analysis_style  # noqa: E402
import ima_adpcm  # noqa: E402
import pattern_supply  # noqa: E402
import raw_prefetch  # noqa: E402
import stream_schedule  # noqa: E402
import shadow_updates  # noqa: E402
import sim_pass_cache  # noqa: E402
import ttrc_routing  # noqa: E402
import sim_artifact_cache  # noqa: E402
import tmpfs_workspace  # noqa: E402
import upgrade_planner  # noqa: E402

from quantize_md_video import (  # noqa: E402
    rgb888_to_rgb333, rgb333_to_rgb888, run, prepare_dir, MD_LEVELS,
)
from quantize_global4_tiles import (  # noqa: E402
    tile_blocks, build_palettes, edge_strengths, pals_to_bytes, palette_lut,
    rgb333_keys, TILE,
)
from palette_algorithms import (  # noqa: E402
    MOSAIC_GM, STL4, PaletteEvaluator, build_mosaic_palettes,
    coherent_assign_idx, normalize_palette_algo, refine_one_line_palette,
    score_palettes,
)
from cbr_paths import sim_work_dir  # noqa: E402
from video_geometry import (  # noqa: E402
    endpoint_snap_filter, probe_source, parse_ratio, source_filter, raw_filter,
)

# 対象動画・寸法・fps は env で差し替え可(既定はサンプル動画)。
# CBRSIM_OUT を指定しない場合は videos/<stem>/tmp に出力する。
SRC = os.environ.get("CBRSIM_SRC", "movies/disc1/061.mp4")
MODE = os.environ.get("CBRSIM_MODE", "H32")
# Keep the historical 144-line codec height, but choose the matching native
# horizontal raster when the mode changes (H32=256, H40=320).
W = int(os.environ.get("CBRSIM_W", "320" if MODE.upper() == "H40" else "256"))
H = int(os.environ.get("CBRSIM_H", "144"))
GEOMETRY_FIT = os.environ.get("CBRSIM_GEOMETRY_FIT", "pad").lower()
RESIZE_FILTER = os.environ.get("CBRSIM_RESIZE_FILTER", "lanczos").lower()
MASTER_DENOISE = os.environ.get("CBRSIM_MASTER_DENOISE", "1") != "0"
SOURCE_SAR_OVERRIDE = os.environ.get("CBRSIM_SOURCE_SAR")
_MASTER_VF_OVERRIDE = os.environ.get("CBRSIM_MASTER_VF")
_RAW_VF_OVERRIDE = os.environ.get("CBRSIM_RAW_VF")
# Import-only users need not have the default sample video installed.  Resolve
# the source geometry at run time in main() when no explicit filter was given.
DEDITHER_VF = _MASTER_VF_OVERRIDE or ""
RAW_VF = _RAW_VF_OVERRIDE or ""
TCOLS, TROWS = W // TILE, H // TILE     # 既定 32 x 18 = 576 cells
C_CELLS = TCOLS * TROWS
ACTIVE_TILES = int(os.environ.get("CBRSIM_ACTIVE_TILES", str(C_CELLS)))
if not 1 <= ACTIVE_TILES <= C_CELLS:
    raise SystemExit(
        f"CBRSIM_ACTIVE_TILES must be within 1..{C_CELLS}, got {ACTIVE_TILES}")
# CBRSIM_FPS は整数 "15" でも分数 "30000/1001"(=59.94/2=29.97, NTSCソース準拠) でも受ける。
# FPS_STR は ffmpeg にそのまま渡す(分数を厳密に間引く)。FPS は計算用の float。
FPS_STR = os.environ.get("CBRSIM_FPS", "15").strip()
FPS = (float(FPS_STR.split("/")[0]) / float(FPS_STR.split("/")[1])) if "/" in FPS_STR else float(FPS_STR)
DURATION = os.environ.get("CBRSIM_DURATION", "152.866667")

# Integer-VBlank rates use their exact NTSC cadence (N4=14.985, N2=29.97).
# Delivery-paced rates such as 24 fps keep their nominal long-term rate.
VSYNC_N = av_config.vsync_n_for_fps(FPS)
PLAYBACK_FPS = av_config.playback_fps_for_content(FPS)
CD_RATE = av_config.CD_BYTES_PER_SECOND
# Audio is always 22.05 kHz mono ADPCM. The source WAV is signed 16-bit;
# controls carry checkpointed 4-bit IMA and the Sub CPU reconstructs RF5C164
# samples.
AUDIO_FFCODEC = "pcm_s16le"
AUDIO_LABEL = "22.05kHz mono IMA ADPCM"
AUDIO_FILE = "audio_22k05_s16_mono.wav"
AUDIO_PLAYBACK_FILE = "audio_playback_adpcm22_rf5c.wav"
# The packer shares these timing values through av_config.
AUDIO_RATE, AUDIO_PCM_BYTES, AUDIO_CONTROL_BYTES = av_config.audio_frame_layout(
    FPS)
AUDIO_PLAYBACK_RATE = int(round(AUDIO_PCM_BYTES * FPS))
PATTERN_BYTES = 32              # 4bpp 8x8 パターン
NAME_BYTES = 2                  # ネームテーブル1エントリ(tile index + palette + priority)
VRAM_TILES = av_config.VRAM_PATTERN_POOL_TILES
FLATTEN_STD = 0.12              # rgb333(0-7)タイル内std平均。ディザ除去済みなので低め
DETAIL_ALPHA = float(os.environ.get("CBRSIM_DETAIL_ALPHA", "0.0"))
BORDER_TILES = 2
BORDER_WEIGHT = 0.4
# Miss/Flbk にだけ、targetと現在表示の距離に応じた圧力を積む。
# RGB平均誤差 AGING_DIST_REF で1.0/frame、急変時の単フレーム加算はSTEP_CAPまで。
AGING_ALPHA = 0.6
WAIT_CAP = 10.0
AGING_DIST_REF = float(os.environ.get("CBRSIM_AGING_DIST_REF", "24"))
AGING_STEP_CAP = float(os.environ.get("CBRSIM_AGING_STEP_CAP", "2.0"))
if AGING_DIST_REF <= 0:
    raise SystemExit("CBRSIM_AGING_DIST_REF must be greater than zero")
if AGING_STEP_CAP < 0:
    raise SystemExit("CBRSIM_AGING_STEP_CAP must be zero or greater")


def distance_aging_step(diff, dist_ref=AGING_DIST_REF,
                        step_cap=AGING_STEP_CAP):
    """Return one frame's age pressure from per-tile summed RGB error."""
    mean_rgb_error = np.asarray(diff, dtype=np.float64) / (TILE * TILE * 3)
    return np.minimum(mean_rgb_error / float(dist_ref), float(step_cap))


def priority_aging(age_press):
    """Convert accumulated age pressure to the bounded priority multiplier."""
    return 1.0 + AGING_ALPHA * np.minimum(age_press, WAIT_CAP)


def update_age_pressure(age_press, cell_tier, diff):
    """Accumulate pressure for Miss/Flbk and reset Near/exact cells."""
    return np.where(
        np.asarray(cell_tier) < 2,
        np.asarray(age_press, dtype=np.float64) + distance_aging_step(diff),
        0.0,
    )


# Compatibility aliases for diagnostics importing sim constants.  The
# canonical values and border semantics live in analysis_style.py.
COL_SAME = analysis_style.CAT_DEDUP
CAT_RAW = analysis_style.CAT_RAW
CAT_SAME = analysis_style.CAT_SAME
CAT_DEDUP = analysis_style.CAT_DEDUP
CAT_MISS = analysis_style.CAT_MISS
CAT_NEAR = analysis_style.CAT_NEAR
CAT_FLBK = analysis_style.CAT_FLBK
CAT_PRG = analysis_style.COL_PRG
CAT_WR1 = analysis_style.COL_WR1
CAT_WR0 = analysis_style.COL_WR0
CAT_DIC = analysis_style.COL_DIC
# Resident candidate search narrows by rendered mean colour before the full
# F3 comparison. This is search acceleration only; it does not accept a quality
# tier by itself.
RESIDENT_K = int(os.environ.get("CBRSIM_RESIDENT_K", "24"))
RESIDENT_BW = 24
# Near(F3): 変化タイルのうち「表示中(old)とtarget(real)が見た目ほぼ同じ」を更新省略。
# 常に old(表示中) vs target を比較するのでドリフトはF3の距離で頭打ち。env CBRSIM_NEAR=1 で有効。
NEAR_ON = True
NEAR_F3 = dict(Ym=float(os.environ.get("CBRSIM_NEAR_YM", "10")),   # 画素輝度差の平均しきい(厳格化)
               Yp=float(os.environ.get("CBRSIM_NEAR_YP", "28")),   # 画素輝度差の最大しきい(形は軽く効く)
               C=float(os.environ.get("CBRSIM_NEAR_C", "24")))     # 画素色差の平均しきい
_LWv = np.array([.299, .587, .114]); _CBv = np.array([-.169, -.331, .5]); _CRv = np.array([.5, -.419, -.081])
_SC1 = (.01 * 255) ** 2; _SC2 = (.03 * 255) ** 2

# Every rendered codec pixel is one of the 512 RGB333 colours.  Cache the F3
# per-pixel distances once so the resident search does not repeat matrix
# products and square roots for millions of tiny candidate arrays.
_MD_LEVEL_INDEX = np.zeros(256, dtype=np.uint16)
_MD_LEVEL_INDEX[MD_LEVELS] = np.arange(8, dtype=np.uint16)
_MD_RGB333 = np.column_stack((
    (np.arange(512, dtype=np.uint16) >> 6) & 7,
    (np.arange(512, dtype=np.uint16) >> 3) & 7,
    np.arange(512, dtype=np.uint16) & 7,
))
_MD_RGB888 = MD_LEVELS[_MD_RGB333].astype(np.float64)
_MD_LUM = _MD_RGB888 @ _LWv
_MD_CB = _MD_RGB888 @ _CBv
_MD_CR = _MD_RGB888 @ _CRv
_F3_DY_LUT = np.abs(_MD_LUM[:, None] - _MD_LUM[None, :])
_F3_DC_LUT = np.sqrt(
    (_MD_CB[:, None] - _MD_CB[None, :]) ** 2
    + (_MD_CR[:, None] - _MD_CR[None, :]) ** 2)


def rendered_color_keys(rgb):
    """Pack codec-rendered MD_LEVELS RGB into 9-bit colour keys."""
    levels = _MD_LEVEL_INDEX[np.asarray(rgb, dtype=np.uint8)]
    return ((levels[..., 0] << 6) | (levels[..., 1] << 3) | levels[..., 2])

# --- 統合探索(Same/Near/Flbk/Miss) + 中央tie-break ---
CENTERTIE_ON = os.environ.get("CBRSIM_CENTERTIE", "1") != "0"   # 既定ON。同点は画面中央に近いセルを優先
MIDFAR_ON = os.environ.get("CBRSIM_MIDFAR", "1") != "0"    # 既定ON。Near/Flbk を1つのVRAM最良一致探索に統合
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
    ("flbk", float(os.environ.get("CBRSIM_TFLBK_YM", "120")), float(os.environ.get("CBRSIM_TFLBK_YP", "252")), float(os.environ.get("CBRSIM_TFLBK_C", "200"))),
]


def f3_mask_eval(cur, plain, changed, thresholds):
    """Return changed cells within mean/max luma and mean chroma bounds."""
    o = cur.astype(np.float64); r = plain.astype(np.float64)
    dY = np.abs(o @ _LWv - r @ _LWv)
    dYm = dY.mean(axis=(1, 2)); dYp = dY.max(axis=(1, 2))
    dCm = np.sqrt((o @ _CBv - r @ _CBv) ** 2 + (o @ _CRv - r @ _CRv) ** 2).mean(axis=(1, 2))
    t = thresholds
    return changed & (dYm <= t['Ym']) & (dYp <= t['Yp']) & (dCm <= t['C'])


def near_mask_eval(cur, plain, changed):
    """Return changed cells whose displayed error already fits Near."""
    return f3_mask_eval(cur, plain, changed, NEAR_F3)
# L3(PRG-RAM victim cache): VRAMから追い出したパターンを捨てずRAMに退避しておき、
# 再登場したらCDから読み直さずRAM→VRAM DMAで復帰させる(CDバイト0)。CDが唯一の
# ボトルネックなのでDMAは実質フリー扱い。0=無効(既定)。512KB/32B=16384枚。
L3_TILES = int(os.environ.get("CBRSIM_L3", "0"))
NO_PANELS = bool(os.environ.get("CBRSIM_NOPANELS"))   # 計測専用: 解析パネルPNGの書き出しを省く
_SLOT_LOCALITY_STAGE = os.environ.get(
    "CBRSIM_SLOT_LOCALITY_STAGE", "").strip().lower()
# Final source-aware minimax placement is cheap next to the full encode but
# needs enough reweighting rounds to bring every near-cap Sonic frame to the
# 30-run target.  The broad exact-target predictor keeps the faster default.
SLOT_LOCALITY_FINAL_ITERATIONS = 160
SLOT_LOCALITY_HEAVY_RUN_TARGET = 30
SLOT_LOCALITY_RETRY_EXIT = 75
SLOT_LOCALITY_MAX_ACCOUNTING_PASSES = 4
# PRG-RAM先読みバッファ: 再生前にPRGへ載せた静的タイル集合(pickle set of pattern keys)。
# ここにあるパターンは再生中いつでもCD 0バイト(RAM→VRAM DMAのみ)で出せる=Fill扱い。
PRG_PRELOAD_PATH = os.environ.get("CBRSIM_PRG_PRELOAD", "")
# Whole-movie quality budget: easy frames retain virtual bytes and demanding
# frames spend them.  This is encoder accounting, not a fifth physical buffer.
# Its ceiling matches the normal fps-specific PrgBuf capacity. The exact
# physical delivery proof separately owns the larger back-pressure ceiling and
# may use only the cadence-scaled jitter interval above this normal capacity.
QUALITY_BUDGET_ON = True
PRG_BUF_CAP_KB = av_config.prg_buf_cap_kb(FPS)
PRG_DELIVERY_CAP_KB = av_config.physical_delivery_cap_kb(FPS)
PRG_JITTER_HEADROOM_KB = av_config.ring_jitter_headroom_kb(FPS)
QUALITY_BUDGET_KB = av_config.quality_budget_kb(FPS)
QUALITY_BUDGET_BYTES = QUALITY_BUDGET_KB * 1024
# 格上げパス(既定ON): 当該フレームの余り + 画質予算で、近似(Near/Flbk)や持ち越しをRaw/Bufに格上げ。
# 0で無効(=従来の帯域余し挙動に戻せる, 比較用)。
UPGRADE_ON = os.environ.get("CBRSIM_UPGRADE", "1") != "0"
# cold(=新規パターン転送: Raw+Buf)の1コマ上限。実機MDの実時間デコード天井対策
# (BUDGETS.md 'Encoder cap')。超過セルは Flbk近似 or Miss繰越。0=無効。
# 1コマの cold baseline はfpsだけから導出する。モードと画面タイル数は
# baselineに関与しない。profileは全編認定済みの上限へ引き上げられる。
# frame0 は下の frame_max_cold で別途免除。
COLD_CAP_QUALIFICATION = av_config.cold_cap_qualification(FPS)
MAX_COLD = COLD_CAP_QUALIFICATION.cap
MAX_RUN_CONTROL_BYTES = stream_schedule.max_run_control_reservation(
    MAX_COLD, ACTIVE_TILES)
# Boot VRAM prefetch uses otherwise-unused frame-0 HEADER/staging capacity and
# free resident slots.  It is default-on because it adds no timed BODY work.
# Optional runtime prefetch remains profile-gated: only spare cold/BODY
# capacity may move next-frame work earlier, and visible work always wins.
BOOT_VRAM_PREFETCH_ON = True
RAW_PREFETCH_ON = os.environ.get("CBRSIM_RAW_PREFETCH", "0") != "0"
RAW_PREFETCH_LOOKAHEAD = 1
RAW_PREFETCH_MAX_REQUESTS_PER_FRAME = 32
RAW_PREFETCH_MIN_BATCH = 4
RAW_PREFETCH_BUDGET_FLOOR_PATTERNS = 256
# PrgBuf, both parity-specific WordBuf banks, and DicBuf are the single
# physical pattern-supply algorithm at every supported cadence.
PATTERN_SUPPLY_ON = True
# The specialized Sub-CPU player consumes the packed cold-run suffix at 24fps
# and above, or whenever the multi-source pattern-supply path requires it.
# Lower-rate plain-Prg streams deliberately retain the legacy 64-entry polling
# walker.  That walker reconstructs runs in cell/update order, so applying a
# permutation optimized for the suffix's physical-slot-sorted order can turn a
# few long runs into hundreds of one-tile runs.  The contiguous logical
# allocator already emits the legacy order correctly; keep its identity map.
PACKED_COLD_RUN_EXECUTION = ttrc_routing.player_uses_packed_cold_runs(
    FPS,
    ttrc_routing.FEATURE_PATTERN_SUPPLY if PATTERN_SUPPLY_ON else 0,
)
# 近似流用(Near/Flbk)が「この秒数」以上そのまま居座ったら、格上げ優先度を Miss級(sev=0)へ
# 昇格させる。一過性の近似は目に見えないが、居座った近似は静的なゴースト=視線が固定される。時間で切る
# のは知覚(何秒出続けたか)がfps非依存だから(重み付けaging=予算コンテストのフレーム数とは別軸)。0で無効。
def ghost_escalate_frames(seconds, fps):
    return max(1, math.floor(seconds * fps)) if seconds > 0 else 0


GHOST_ESCALATE_SEC = float(os.environ.get("CBRSIM_GHOST_ESCALATE_SEC", "0.2"))
GHOST_ESCALATE_N = ghost_escalate_frames(GHOST_ESCALATE_SEC, FPS)
# issue #10: near_keep(現在表示がほぼ同一なら0Bで維持)を「現在表示が正確(cell_tier==9)」なセルに限定。
# 近似表示(Flbk)を入力に Near 判定すると近似が居座る(ゴースト)ため。0で旧挙動(近似表示も維持可)。
NEAR_KEEP_ACCURATE_ONLY = os.environ.get("CBRSIM_NEAR_ACCURATE_ONLY", "1") != "0"

# 出力量子化で「位置固定の規則ディザ(Bayer 8x8)」を掛ける。同じ画面座標は常に同じ閾値なので
# 静止タイルは毎コマ同一の333のまま=差分/使い回しを壊さない(誤差拡散は波及するので不採用)。
# 前処理のディザ除去(master抽出)はそのまま。掛け直すのは出力の333化のここだけ。既に
# ディザを含む素材では二重ディザによる孤立色を避けるためプロファイルから無効化できる。
DITHER_ON = os.environ.get("CBRSIM_DITHER", "1") != "0"
# Optional source preprocessing, applied before both the master and raw paths.
# Out-of-range defaults disable it for profiles without endpoint_snap.
PREPROCESS_BLACK_MAX = int(os.environ.get(
    "CBRSIM_PREPROCESS_ENDPOINT_SNAP_BLACK_MAX", "-1"))
PREPROCESS_WHITE_MIN = int(os.environ.get(
    "CBRSIM_PREPROCESS_ENDPOINT_SNAP_WHITE_MIN", "256"))
SOURCE_PREPROCESS_VF = endpoint_snap_filter(
    PREPROCESS_BLACK_MAX, PREPROCESS_WHITE_MIN)
# 深い暗転で区切り、暗転の瞬間に区間別60色パレットへ差し替える(CRAM総入替)。
SEGPAL_ON = True
PAL_ALGO = normalize_palette_algo()                          # stl4 (legacy) / mosaic-gm (opt-in while tuning)
PAL_SEAM_WEIGHT = av_config.PALETTE_SEAM_WEIGHT
PAL_SEAM_ITERATIONS = av_config.PALETTE_SEAM_ITERATIONS
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


def detect_palette_segments(frames, metrics=None):
    """Return the legacy dark/uniform candidate ranges without training them."""
    n = len(frames)
    LWv = np.array([.299, .587, .114])
    SEG_GAP = int(os.environ.get("CBRSIM_SEG_GAP", "24"))
    SEG_MIN = int(os.environ.get("CBRSIM_SEG_MIN", "2"))
    DARK_THR = float(os.environ.get("CBRSIM_SEG_DARK", "0.90"))
    UNI_THR = float(os.environ.get("CBRSIM_SEG_UNIFORM", "0.88"))
    UNI_TOL = float(os.environ.get("CBRSIM_SEG_UNIFORM_TOL", "24"))
    UNI_NEAR = int(os.environ.get("CBRSIM_SEG_UNIFORM_NEAR", "8"))
    if metrics is None:
        dark = np.zeros(n)
        uniform = np.zeros(n)
        for i in range(n):
            image = np.asarray(Image.open(frames[i]).convert("RGB")).astype(float)
            dark[i] = ((image @ LWv) < 32).mean()
            distance = np.sqrt(((image - image.reshape(-1, 3).mean(0)) ** 2).sum(2))
            uniform[i] = (distance < UNI_TOL).mean()
    else:
        dark, uniform = metrics
        dark = np.asarray(dark, dtype=np.float64).reshape(n)
        uniform = np.asarray(uniform, dtype=np.float64).reshape(n)

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


def segment_and_train(frames, frame_cache=None):
    """Train STL4 unchanged or select MOSAIC-GM lines and useful CRAM segments."""
    n = len(frames)

    def load_tiles(indices, flattened=False):
        if frame_cache is not None:
            return frame_cache.load_tiles(indices, flattened=flattened)
        return np.concatenate([
            tile_blocks(to_rgb333(np.asarray(Image.open(frames[int(index)]).convert("RGB"))))
            for index in indices
        ], axis=0)

    def train_mosaic(indices):
        if frame_cache is None:
            return build_mosaic_palettes(
                load_tiles(indices), n_pal=4, return_stats=True)
        training, strengths = frame_cache.load_training(indices)
        return build_mosaic_palettes(
            training, n_pal=4, return_stats=True,
            train_strengths=strengths)

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
            segments = detect_palette_segments(
                frames, None if frame_cache is None else frame_cache.segment_metrics)
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

    sample_counts = av_config.PALETTE_SAMPLE_COUNTS
    validation_count = av_config.PALETTE_VALIDATE_FRAMES
    validation_indices = sample_indices(0, n, validation_count, half_step=True)
    if frame_cache is None:
        validation_tiles = load_tiles(validation_indices)
        validation_flat, _detail = flatten_low_detail(validation_tiles)
    else:
        validation_flat = load_tiles(validation_indices, flattened=True)
    validation_evaluator = PaletteEvaluator(validation_flat)
    candidates = []
    seen_counts = set()
    for requested in sample_counts:
        indices = sample_indices(0, n, requested)
        if len(indices) in seen_counts:
            continue
        seen_counts.add(len(indices))
        palettes, train_stats = train_mosaic(indices)
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

    # A one-line candidate receives an exact all-frame histogram and local slot
    # refinement. This is a rendered-error optimization, not a colour-count
    # shortcut; sources over 15 colours use the same decreasing-error swaps.
    exact_global = False
    if global_active == 1:
        full_histogram = np.zeros(512, dtype=np.int64)
        if frame_cache is None:
            for path in frames:
                tiles = tile_blocks(to_rgb333(np.asarray(Image.open(path).convert("RGB"))))
                flat, _detail = flatten_low_detail(tiles)
                full_histogram += np.bincount(
                    rgb333_keys(flat).reshape(-1), minlength=512)
        else:
            for start in range(0, n, 64):
                flat = frame_cache.load_tiles(
                    range(start, min(n, start + 64)), flattened=True)
                full_histogram += np.bincount(
                    rgb333_keys(flat).reshape(-1), minlength=512)
        refined, refinement_stats = refine_one_line_palette(
            pals_arr[0], full_histogram)
        pals_arr = np.stack([refined.copy() for _line in range(4)]).astype(np.uint8)
        global_stats["full_histogram_refinement"] = refinement_stats
        exact_global = bool(refinement_stats["exact"])
        print(
            f"[MOSAIC-GM] global one-line full histogram: "
            f"colours={refinement_stats['source_colours']} "
            f"error={refinement_stats['before_error']}->{refinement_stats['after_error']} "
            f"swaps={len(refinement_stats['swaps'])}"
        )
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

    segments = detect_palette_segments(
        frames, None if frame_cache is None else frame_cache.segment_metrics)
    segment_train_count = av_config.PALETTE_SEGMENT_TRAIN_FRAMES
    segment_validation_count = av_config.PALETTE_SEGMENT_VALIDATE_FRAMES
    segment_rel = av_config.PALETTE_SEGMENT_GAIN_RELATIVE
    segment_abs = av_config.PALETTE_SEGMENT_GAIN_PER_PIXEL
    selected = []
    segment_stats = []
    for start, end in segments:
        train_indices = sample_indices(start, end, segment_train_count)
        local_palettes, local_stats = train_mosaic(train_indices)
        validate_indices = sample_indices(start, end, segment_validation_count, half_step=True)
        if frame_cache is None:
            validate_tiles = load_tiles(validate_indices)
            validate_flat, _detail = flatten_low_detail(validate_tiles)
        else:
            validate_flat = load_tiles(validate_indices, flattened=True)
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
_PASS_CACHE_PATH = os.environ.get("CBRSIM_PASS_CACHE", "").strip()
_PASS_CACHE_INVOCATION = os.environ.get(
    "CBRSIM_PASS_CACHE_INVOCATION", "").strip()
# 実機TTRCエンコード用の決定ログ出力先。既定off(mp4出力に一切影響しない・追加のみ)。
# 毎フレームの「更新セル(cell,pal,key)」＋区間パレットを吐き、pack_streamが再生してTTRC化する。
_EMIT_DEC_ENV = os.environ.get("CBRSIM_EMIT_DEC", "").strip()
# Boolean-looking values select the conventional file beside the other sim
# artifacts.  An explicit path remains supported for one-off comparisons.
EMIT_DEC = (str(OUT / "decisions.pkl")
            if _EMIT_DEC_ENV.lower() in {"1", "true", "yes", "on"}
            else _EMIT_DEC_ENV)


def _source_run_groups(
        replay, sources_by_frame, *, boot_inline_requests=None):
    """Partition each cold trace by the physical source that splits runs.

    Prg and Word loads can join only loads from the same source.  DicBuf also
    requires consecutive dictionary indices, so each Dic load is kept as a
    conservative singleton for placement; the exact final counter may still
    merge compatible neighbours afterwards.
    """
    if len(sources_by_frame) != len(replay.placements):
        raise ValueError("pattern-source frame count differs")
    result = []
    for frame, (placements, prefetch_slots, raw_sources) in enumerate(zip(
            replay.placements,
            replay.prefetch_cold_slots,
            sources_by_frame)):
        if len(raw_sources) != len(placements):
            raise ValueError(
                f"frame {frame} pattern-source/update count differs")
        prg = []
        word = []
        dic = []
        for update, ((slot, cold), raw_source) in enumerate(zip(
                placements, raw_sources)):
            if not cold:
                continue
            source = int(raw_source)
            if source == pattern_supply.SOURCE_PRG:
                prg.append(int(slot))
            elif source == pattern_supply.SOURCE_WR:
                word.append(int(slot))
            elif source == pattern_supply.SOURCE_DIC:
                dic.append((int(slot),))
            else:
                raise ValueError(
                    f"frame {frame} update {update} has invalid source {source}")
        if frame == 0 and boot_inline_requests is not None:
            # Frame 0 is an untimed boot construction.  Its staging region can
            # hold the worst possible descriptor fragmentation, so spending
            # locality iterations on it can only displace timed-frame gains.
            result.append(())
            continue
        groups = []
        if prg:
            groups.append(tuple(prg))
        # Prefetch payload follows all visible cold payload in the stream.
        # Count it as its own physically sorted Prg group; merging it into the
        # visible group would assume a transfer order the packer cannot use.
        if prefetch_slots:
            groups.append(tuple(int(slot) for slot in prefetch_slots))
        if word:
            groups.append(tuple(word))
        groups.extend(dic)
        result.append(tuple(groups))
    return tuple(result)


def _inline_boot_prefetch_slots(
        prefetch_slots, inline_count, physical_by_logical=None):
    """Choose the boot requests carried by frame 0's inline payload.

    The packer sorts the complete boot-prefetch suffix by physical slot, then
    puts the first ``inline_count`` patterns in O_LOADS and the rest in the
    direct-write sidecar.  Apply that same rule whenever a physical mapping is
    known.  The logical/request order remains the deterministic seed rule
    before the first mapping exists.
    """
    slots = tuple(int(slot) for slot in prefetch_slots)
    count = max(0, min(int(inline_count), len(slots)))
    if physical_by_logical is None:
        return slots[:count]
    mapping = np.asarray(physical_by_logical, np.int64)
    return tuple(sorted(
        slots, key=lambda slot: (int(mapping[slot]), slot))[:count])


def _run_accounted_cold_slots(replay, boot_inline_requests):
    """Exclude all untimed frame-0 writes from the run optimizer."""
    cold = list(replay.cold_slots)
    if cold and boot_inline_requests is not None:
        cold[0] = ()
    return tuple(cold)


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


def _frame_features(path):
    """Decode one master frame into reusable palette and quantization data."""
    image_u8 = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    image = image_u8.astype(np.float64)
    luminance = image @ np.array([.299, .587, .114])
    dark = float((luminance < 32).mean())
    distance = np.sqrt(((image - image.reshape(-1, 3).mean(0)) ** 2).sum(2))
    uniform = float((distance < float(os.environ.get(
        "CBRSIM_SEG_UNIFORM_TOL", "24"))).mean())

    tiles = tile_blocks(to_rgb333(image_u8))
    strengths = edge_strengths(tiles)
    flattened, detail = flatten_low_detail(tiles)
    flatten_mask = detail < FLATTEN_STD
    # Low-detail tiles are solid after flattening, so one RGB333 colour is
    # enough to reconstruct them without retaining a second full movie copy.
    flat_color = flattened[:, 0].copy()
    return (
        tiles, strengths, detail.astype(np.float32),
        flatten_mask, flat_color, dark, uniform,
    )


class FrameFeatureCache:
    """One-decode in-memory source for palette learning and final quantization."""

    def __init__(self, frames, workers=None):
        self.frames = list(frames)
        n = len(self.frames)
        self.tiles = np.empty((n, C_CELLS, 64, 3), dtype=np.uint8)
        self.edge_strength = np.empty((n, C_CELLS, 64), dtype=np.uint8)
        self.detail = np.empty((n, C_CELLS), dtype=np.float32)
        self.flatten_mask = np.empty((n, C_CELLS), dtype=bool)
        self.flat_color = np.empty((n, C_CELLS, 3), dtype=np.uint8)
        dark = np.empty(n, dtype=np.float64)
        uniform = np.empty(n, dtype=np.float64)

        worker_count = min(12, n_workers()) if workers is None else max(1, int(workers))
        print(
            f"precompute palette frame features: {n} frames on "
            f"{worker_count} threads ...",
            flush=True,
        )
        if worker_count == 1:
            results = map(_frame_features, self.frames)
            pool = None
        else:
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(max_workers=worker_count)
            results = pool.map(_frame_features, self.frames)
        try:
            for index, result in enumerate(results):
                (self.tiles[index], self.edge_strength[index], self.detail[index],
                 self.flatten_mask[index], self.flat_color[index],
                 dark[index], uniform[index]) = result
        finally:
            if pool is not None:
                pool.shutdown(wait=True)
        self.segment_metrics = dark, uniform
        resident = (
            self.tiles.nbytes + self.edge_strength.nbytes + self.detail.nbytes
            + self.flatten_mask.nbytes + self.flat_color.nbytes
            + dark.nbytes + uniform.nbytes)
        print(f"  palette frame cache: {resident / (1024 ** 2):.1f} MiB", flush=True)

    @staticmethod
    def _indices(indices):
        if isinstance(indices, np.ndarray):
            return indices.astype(np.int64, copy=False).reshape(-1)
        return np.fromiter((int(index) for index in indices), dtype=np.int64)

    def load_tiles(self, indices, flattened=False):
        selected = self._indices(indices)
        tiles = self.tiles[selected].reshape(-1, 64, 3)
        if not flattened:
            return tiles
        result = tiles.copy()
        mask = self.flatten_mask[selected].reshape(-1)
        colors = self.flat_color[selected].reshape(-1, 3)
        result[mask] = colors[mask, None, :]
        return result

    def load_training(self, indices):
        selected = self._indices(indices)
        tiles = self.tiles[selected].reshape(-1, 64, 3)
        strengths = self.edge_strength[selected].reshape(-1, 64)
        return tiles, strengths

    def flattened_frame(self, index):
        tiles = self.tiles[int(index)].copy()
        mask = self.flatten_mask[int(index)]
        tiles[mask] = self.flat_color[int(index), mask, None, :]
        return tiles


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


def own_pattern_cache_rgb(rgb):
    """Return a compact owned RGB array for one cached 8x8 pattern.

    ``plain_rgb[c]`` is a view. Keeping it in the pattern dictionary keeps its
    complete per-frame backing array alive, which grows into hundreds of MiB on
    long sources. Owning the small array also makes its shape explicit.
    """
    owned_rgb = np.array(rgb, dtype=np.uint8, order="C", copy=True)
    if owned_rgb.shape != (TILE, TILE, 3):
        raise ValueError(
            f"pattern RGB shape {owned_rgb.shape}, expected {(TILE, TILE, 3)}")
    return owned_rgb


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
# 差分/画質予算の本体は逐次(前フレーム状態に依存)だが、ここは各フレーム独立=実行時間の大半。
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
    if PAL_ALGO == MOSAIC_GM and PAL_SEAM_WEIGHT > 0:
        assign, pidx = coherent_assign_idx(
            flat, cur_pals, TROWS, TCOLS,
            seam_weight=PAL_SEAM_WEIGHT, iterations=PAL_SEAM_ITERATIONS)
    else:
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
    try:
        available = len(os.sched_getaffinity(0))
    except AttributeError:
        available = os.cpu_count() or 4
    return max(1, available - 2)


def quant_worker_count(gpu_enabled, requested, override_present=False):
    """Keep the verified GPU feeder width unless a diagnostic overrides it."""
    requested = max(1, int(requested))
    return requested if override_present or not gpu_enabled else min(requested, 4)


def quant_pool_start_method(gpu_enabled):
    """Choose a safe process start method for the quantization feeder pool.

    GPU palette work initializes CUDA in the parent before this pool starts.
    Forking that live CUDA process can corrupt CPython/CUDA state, so GPU runs
    must start clean worker interpreters. CPU-only runs retain the cheaper fork
    path because they have no device context to inherit.
    """
    return "spawn" if gpu_enabled else "fork"


def default_png_workers(version_info):
    """Return the safe default PNG writer count for supported CPython versions.

    Concurrent Pillow PNG writes corrupted live NumPy metadata during a long
    CPython 3.13 encode, and had already crashed CPython 3.14. Keep writes
    synchronous on every supported interpreter. The argument remains for API
    compatibility with older callers.
    """
    del version_info
    return 1


def png_workers():
    env = os.environ.get("CBRSIM_PNG_WORKERS")
    if env:
        return max(1, int(env))
    return default_png_workers(sys.version_info)


def precompute_quant(frames, seg_pals, frame_seg, frame_cache=None):
    """各フレームの (detail, assign, plain_idx, plain_rgb) を並列に前計算して返す。"""
    n = len(frames)
    import gpu_quant
    gpu_on = gpu_quant.enabled()
    w = quant_worker_count(
        gpu_on, n_workers(), override_present="CBRSIM_WORKERS" in os.environ)
    if gpu_on:
        # CPU(並列)で読込/333化/タイル化 → GPU で割当/索引。imap で両者を重ねる
        # (ワーカーが flat を出す傍から親GPUが処理＝CPU I/OとGPU計算を並行)。
        if frame_cache is None:
            print(
                f"precompute quantization: {n} frames, "
                f"CPU load x{w} + GPU assign/idx ...",
                flush=True,
            )
        details = [None] * n
        assigns = [None] * n
        pidxs = [None] * n
        cache = gpu_quant.PalCache()
        if frame_cache is not None:
            print(
                f"precompute quantization: {n} cached frames + GPU assign/idx ...",
                flush=True,
            )
            for i in range(n):
                details[i] = frame_cache.detail[i].copy()
                flat = frame_cache.flattened_frame(i)
                assigns[i], pidxs[i] = gpu_quant.assign_idx_one(
                    flat, int(frame_seg[i]), seg_pals, cache,
                    coherent_shape=((TROWS, TCOLS) if PAL_ALGO == MOSAIC_GM else None),
                    seam_weight=PAL_SEAM_WEIGHT,
                    seam_iterations=PAL_SEAM_ITERATIONS)
        elif w > 1:
            import multiprocessing as mp
            with mp.get_context(quant_pool_start_method(gpu_on)).Pool(
                    w, initializer=_quant_init, initargs=(frames, seg_pals, frame_seg)) as pool:
                for i, (det, flat) in enumerate(pool.imap(_quant_one_flat, range(n), chunksize=8)):
                    details[i] = det
                    assigns[i], pidxs[i] = gpu_quant.assign_idx_one(
                        flat, int(frame_seg[i]), seg_pals, cache,
                        coherent_shape=((TROWS, TCOLS) if PAL_ALGO == MOSAIC_GM else None),
                        seam_weight=PAL_SEAM_WEIGHT,
                        seam_iterations=PAL_SEAM_ITERATIONS)
        else:
            _quant_init(frames, seg_pals, frame_seg)
            for i in range(n):
                det, flat = _quant_one_flat(i)
                details[i] = det
                assigns[i], pidxs[i] = gpu_quant.assign_idx_one(
                    flat, int(frame_seg[i]), seg_pals, cache,
                    coherent_shape=((TROWS, TCOLS) if PAL_ALGO == MOSAIC_GM else None),
                    seam_weight=PAL_SEAM_WEIGHT,
                    seam_iterations=PAL_SEAM_ITERATIONS)
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
    if (sys.version_info[:3] == (3, 14, 4)
            and np.__version__ == "2.5.1"):
        raise SystemExit(
            "unsafe numeric runtime: CPython 3.14.4 + NumPy 2.5.1 corrupted "
            "long sim runs on this host. Bootstrap the locked GPU environment "
            "and run tools/python.sh --gpu tools/sim.py (NumPy 2.3.5).")
    if CONFIG_PROFILE is not None:
        print(f"encode profile: {CONFIG_PROFILE.path} sha256={CONFIG_PROFILE.sha256}")
    if not DEDITHER_VF or not RAW_VF:
        src_w, src_h, src_sar_num, src_sar_den = probe_source(SRC)
        if SOURCE_SAR_OVERRIDE:
            src_sar_num, src_sar_den = parse_ratio(SOURCE_SAR_OVERRIDE)
        # H32/H40のHARを考慮した既定変換。明示的なVF指定は優先する。
        DEDITHER_VF = DEDITHER_VF or source_filter(
            MODE, W, H, src_w, src_h,
            src_sar_num=src_sar_num, src_sar_den=src_sar_den,
            fit=GEOMETRY_FIT, denoise=MASTER_DENOISE,
            resize_filter=RESIZE_FILTER)
        RAW_VF = RAW_VF or raw_filter(
            MODE, W, H, src_w, src_h,
            src_sar_num=src_sar_num, src_sar_den=src_sar_den,
            fit=GEOMETRY_FIT, resize_filter=RESIZE_FILTER)
    if SOURCE_PREPROCESS_VF:
        DEDITHER_VF = f"{SOURCE_PREPROCESS_VF},{DEDITHER_VF}"
        RAW_VF = f"{SOURCE_PREPROCESS_VF},{RAW_VF}"
        print(
            "source preprocessing: endpoint_snap "
            f"black_max={PREPROCESS_BLACK_MAX} white_min={PREPROCESS_WHITE_MIN}")
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
    reuse = (
        os.environ.get("CBRSIM_SLOT_LOCALITY_REUSE", "0").strip().lower()
        not in {"", "0", "false", "no", "off"}
        or os.environ.get("CBRSIM_REUSE", "0").strip().lower() not in {
        "", "0", "false", "no", "off",
        }
    )
    master_dir = OUT / "master"     # ディザ除去済み(量子化入力)
    raw_dir = OUT / "raw"           # 生のオリジナル(比較TR用)
    cached = reuse and any(master_dir.glob("*.png")) and any(raw_dir.glob("*.png"))
    if cached:
        print("CBRSIM_REUSE: cached master/raw/audio を再利用(ffmpeg展開をスキップ)")
    else:
        prepare_dir(OUT, clean=True)
        for d in (master_dir, raw_dir):
            prepare_dir(d, clean=True)
        print(f"extracting de-dithered master ({W}x{H}) ...")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", "0", "-t", DURATION, "-i", SRC,
             "-vf", f"{DEDITHER_VF},fps={FPS_STR}", str(master_dir / "%05d.png")])
        print(f"extracting raw comparison frames ({W}x{H} raster) ...")
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
    pass_cache_payload = None
    pass_cache_metadata = None
    if _PASS_CACHE_PATH:
        if CONFIG_PROFILE is None or not _PASS_CACHE_INVOCATION:
            raise SystemExit(
                "sim pass cache requires a profile and invocation identity")
        pass_cache_metadata = sim_pass_cache.expected_metadata(
            profile=CONFIG_PROFILE,
            source=SRC,
            width=W,
            height=H,
            cells=C_CELLS,
            active_tiles=ACTIVE_TILES,
            fps=FPS_STR,
            frame_count=n,
            invocation=_PASS_CACHE_INVOCATION,
        )
        cache_path = Path(_PASS_CACHE_PATH)
        if cache_path.is_file():
            pass_cache_payload = sim_pass_cache.load(
                cache_path, pass_cache_metadata)
            print(
                f"sim pass cache: hit {cache_path} "
                f"({cache_path.stat().st_size / 1024**2:.1f} MiB)",
                flush=True,
            )
        elif _SLOT_LOCALITY_STAGE == "final":
            raise SystemExit(
                f"accounting pass cache is missing: {cache_path}")
        else:
            print(f"sim pass cache: seed will create {cache_path}", flush=True)
    body_fresh = stream_schedule.body_fresh_byte_supply(
        n,
        FPS,
        cells=C_CELLS,
        audio_frame_bytes=AUDIO_CONTROL_BYTES,
    )
    body_gross_bytes = np.asarray(body_fresh["gross"], np.int64)
    body_fixed_control_bytes = np.asarray(
        body_fresh["fixed_control"], np.int64)
    body_variable_supply_bytes = np.asarray(body_fresh["variable"], np.int64)
    if ACTIVE_TILES < C_CELLS:
        ever_nonblack = np.zeros((TROWS, TCOLS), dtype=bool)
        for frame_path in frames:
            image = np.asarray(Image.open(frame_path).convert("RGB"), dtype=np.uint8)
            tiles = image.reshape(TROWS, TILE, TCOLS, TILE, 3).transpose(0, 2, 1, 3, 4)
            ever_nonblack |= np.any(tiles != 0, axis=(2, 3, 4))
        measured_active_tiles = int(ever_nonblack.sum())
        if measured_active_tiles != ACTIVE_TILES:
            raise SystemExit(
                f"configured active_tiles={ACTIVE_TILES}, but the master frames contain "
                f"{measured_active_tiles} tiles that are ever non-black")
    print(f"  {n} frames @ {W}x{H} ({TCOLS}x{TROWS}={C_CELLS} cells, "
          f"active={ACTIVE_TILES})")
    baseline_cap = (
        COLD_CAP_QUALIFICATION.baseline_cap
        if COLD_CAP_QUALIFICATION.baseline_cap is not None
        else MAX_COLD)
    print(
        f"  cold cap={MAX_COLD}: source={COLD_CAP_QUALIFICATION.source} "
        f"baseline={baseline_cap}; "
        f"fps={COLD_CAP_QUALIFICATION.fps:g}")

    # The source WAV remains the packer's input.  Analysis must instead audition
    # the exact stream reconstructed by the Sub CPU and quantized for RF5C164.
    # Give the preview WAV one chunk per source-video frame so its samples remain
    # aligned with the 30/24/15 fps analysis timeline.
    import wave
    source_path = OUT / AUDIO_FILE
    with wave.open(str(source_path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit(
                f"ADPCM source WAV must be mono s16: {source_path}")
        if wav.getframerate() != AUDIO_RATE:
            raise SystemExit(
                f"ADPCM source WAV rate is {wav.getframerate()}, "
                f"expected {AUDIO_RATE}")
        raw = wav.readframes(wav.getnframes())
    source_pcm = np.frombuffer(raw, "<i2").copy()
    target_samples = n * AUDIO_PCM_BYTES
    retimed = ima_adpcm.retime_pcm_s16(source_pcm, target_samples)
    _controls, rf5_chunks = ima_adpcm.encode_decode_chunks(
        retimed, AUDIO_PCM_BYTES)
    playback_pcm = ima_adpcm.sign_magnitude_to_pcm16(b"".join(rf5_chunks))
    playback_path = OUT / AUDIO_PLAYBACK_FILE
    with wave.open(str(playback_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(AUDIO_PLAYBACK_RATE)
        wav.writeframes(playback_pcm.astype("<i2", copy=False).tobytes())
    print(
        f"  ADPCM playback model: {len(source_pcm)} -> {len(retimed)} samples, "
        f"IMA decode + RF5C164 8-bit -> {playback_path.name} "
        f"@ {AUDIO_PLAYBACK_RATE}Hz")

    # PRG先読みパッチ: {frame_idx: set(pattern_key)}。カット到達時にそのセル群を
    # バッファから適用する。パターンもcell->tile対応も事前ロード済みなのでCDバイト0。
    prg_patch = {}
    if PRG_PRELOAD_PATH and Path(PRG_PRELOAD_PATH).exists():
        import pickle
        prg_patch = pickle.load(open(PRG_PRELOAD_PATH, "rb"))
        uniq = len(set().union(*prg_patch.values())) if prg_patch else 0
        print(f"  PRG先読み(patch): {len(prg_patch)}カット, distinct {uniq} tiles "
              f"({uniq*PATTERN_BYTES/1024:.0f}KB) をロード時バッファ")

    cached_precompute = (
        pass_cache_payload.get("precompute")
        if pass_cache_payload is not None else None)
    if cached_precompute is not None:
        frame_cache = None
        _global_pals = None
        seg_pals = cached_precompute["seg_pals"]
        frame_seg = cached_precompute["frame_seg"]
        seg_bounds = cached_precompute["seg_bounds"]
        palette_stats = cached_precompute["palette_stats"]
        print(
            f"training palettes: pass-cache hit "
            f"({len(seg_pals)} segments, {len(seg_bounds)} CRAM switches)",
            flush=True,
        )
        _t = _mark("パレット学習(cache)", _t)
    else:
        print(f"training palettes ({PAL_ALGO})  DITHER={DITHER_ON} SEGPAL={SEGPAL_ON} NEAR={NEAR_ON} ...")
        frame_cache = FrameFeatureCache(frames) if PAL_ALGO == MOSAIC_GM else None
        _global_pals, seg_pals, frame_seg, seg_bounds, palette_stats = segment_and_train(
            frames, frame_cache=frame_cache)
        palette_stats["spatial_assignment"] = {
            "enabled": bool(PAL_ALGO == MOSAIC_GM and PAL_SEAM_WEIGHT > 0),
            "seam_weight": float(PAL_SEAM_WEIGHT),
            "iterations": int(PAL_SEAM_ITERATIONS),
        }
        if SEGPAL_ON:
            print(f"  per-segment palettes: {len(seg_pals)}区間, CRAM差替 {len(seg_bounds)}点 "
                  f"(candidates={palette_stats['candidate_segments']})")
        _t = _mark("パレット学習", _t)

    main_dir = OUT / "preview"      # SEGA-CD 実出力(ゴースト有り)
    catmap_dir = OUT / "catmap"     # category borders; Raw has none, Miss is overlaid later
    if not NO_PANELS:
        for d in (main_dir, catmap_dir):
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
    from tile_alloc import (
        TileAllocator,
        cold_transfer_order,
        evaluate_slot_locality,
        optimize_slot_locality,
        replay_logical_slots,
        remap_placements,
        verify_display_equivalence,
    )
    # 共有割り当て(連続, pack と同一コード)。これが residency の真の源=pack の realized と一致=cap=realized。
    # 判定は前フレーム末の状態を参照し、割り当て(スロット付与+追い出し)は各フレーム末に cell順で実行
    # (=pack の resolve と同一順)。VRAM_TILES=pack POOL。
    alloc = TileAllocator(C_CELLS, VRAM_TILES, 1)
    ref_count = {}                       # pattern key -> 参照セル数(repoint用に保持, residencyはallocが真)
    l3 = {}                             # L3(PRG-RAM) victim cache: pattern key -> last_used frame
    pat_rgb = {}                        # 近似dedup: pattern key -> 代表rgb(8,8,3) uint8
    pat_colors = {}                     # pattern key -> rendered RGB333 colour keys (64,) uint16
    pat_pal = {}                        # pattern key -> 表示パレット(assign)
    pat_seg = {}                        # pattern key -> このタイルの index列を量子化した区間(パレットエポック)。
                                        # 区間跨ぎのNear/near_keep流用は、旧区間の index列を新CRAMで
                                        # 引くと虹色ゴミになる(harness/palette_flashで実証)。cur_segと一致
                                        # するタイルだけ流用可。dedup(fresh keyが常駐keyと一致)は index列が
                                        # そのまま新区間の量子化なので安全=対象外。
    resident_bucket = defaultdict(list)  # 平均色バケツ -> [key,...] (末尾=最新)
    pat_buckets = defaultdict(set)      # pattern key -> every bucket containing that key

    def cache_pattern(key, rgb, pal, seg):
        if not isinstance(key, bytes) or len(key) != 64:
            length = len(key) if hasattr(key, "__len__") else "unknown"
            raise ValueError(
                f"pattern key must be exactly 64 bytes, got "
                f"{type(key).__name__} length {length}")
        cached_rgb = own_pattern_cache_rgb(rgb)
        pat_rgb[key] = cached_rgb
        pat_colors[key] = rendered_color_keys(cached_rgb).reshape(64)
        pat_pal[key] = int(pal)
        pat_seg[key] = int(seg)

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

    wait = np.zeros(C_CELLS, np.int32)         # TSVのMiss carry/age用。優先度には使わない
    age_press = np.zeros(C_CELLS, np.float64)  # Miss/Flbkの距離加重優先度圧力
    cell_tier = np.zeros(C_CELLS, np.int8)     # 現在の表示劣化度(0=Miss,1=Flbk,2=Near,9=正確)
    approx_carry = np.zeros(C_CELLS, np.int32)  # 近似(tier<9)のまま持ち越した連続コマ数(格上げ/正確化で0)
    upgrade_log = []                            # 毎コマ: 格上げ枚数 / まだ近似のセル数(指標)
    guniq = {k: set() for k in ("same", "near", "flbk")}  # 全編で使った別タイル(ユニーク数)

    frame_bytes_log = []
    tile_records_log = []      # パターン転送数(=32B支払い回数)
    name_records_log = []      # ネームテーブル書換数
    dedup_saved_log = []       # dedupで節約したパターン転送数(L1/L2=VRAM常駐ヒット)
    l3_hits_log = []           # L3(PRG-RAM)ヒット数(CD再読みを回避できた再登場パターン)
    prg_hits_log = []          # PRG先読みヒット数(事前ロード済みで0CD Fillできたタイル)
    # coldlife計測: coldロード(Raw/Buf)が次フレーム以降も役立つかの実測。先読み減点
    # ヒューリスティックの効果上限を測るためのカウンタで、エンコード決定には影響しない。
    coldlife_pending = []      # 前フレームのcoldロード [(cell, key, kind), ...]
    coldlife = {
        "total": {"raw": 0, "buf": 0},           # frame 1以降の全coldロード
        "survive_target": {"raw": 0, "buf": 0},  # 次フレームもターゲット不変(そのまま有効)
        "die_settle": {"raw": 0, "buf": 0},      # 次で変わるがその次は安定(1フレーム待てば1ロード節約)
        "die_motion": {"raw": 0, "buf": 0},      # 次もその次も変化(連続運動: 減点するとゴースト蓄積)
        "tail": {"raw": 0, "buf": 0},            # 末尾フレームで分類不能
        "disp_seen1": {"raw": 0, "buf": 0},      # 翌フレームの実表示を確認できた母数
        "disp_alive1": {"raw": 0, "buf": 0},     # 実表示: 翌フレームどこかのセルで表示継続(Near-keep/再利用込み)
        "disp_cell1": {"raw": 0, "buf": 0},      # 実表示: 翌フレーム同じセルで表示継続
    }
    stat_rows = []             # per-frame status line 用の実測値
    stale_rows = []            # per-frame の Miss(stale)マスク(packbits, 72B/frame)
    starved_frames = 0
    dec_frames = []            # 実機決定ログ: 各要素 = そのフレームの [(cell, pal, key), ...]
    dec_miss = []              # per-frame Miss数(デバッグオーバーレイ用。デコード側では算出不能)
    dec_cats = []              # per-frame カテゴリ数[raw,same,near,flbk,buf,miss](デバッグ欄用)
    transfer_tiles_log = []    # pack/player照合用: cold pattern tile数
    transfer_runs_log = []     # pack/player照合用: packed cold-run record数
    supply_sources_log = []    # per-frame update-aligned Prg/Wr/Dic source codes
    prg_loads_log = []         # physical PrgBuf pattern consumption
    prg_cold_cells_log = []    # physical Prg loads mapped to cells (-1=prefetch)
    wr0_loads_log = []         # physical boot-preload consumption by source
    wr1_loads_log = []
    dic_loads_log = []
    prefetch_requests_log = []  # successful (pattern key, deadline) requests
    prefetch_cold_log = []      # physical PrgBuf loads without a name update
    quality_budget = QUALITY_BUDGET_BYTES if QUALITY_BUDGET_ON else 0
    quality_budget_log = []

    if cached_precompute is not None:
        pal_extreme_stats = cached_precompute["pal_extreme_stats"]
        pal15_stats = cached_precompute["pal15_stats"]
        Q_detail = cached_precompute["Q_detail"]
        Q_assign = cached_precompute["Q_assign"]
        Q_pidx = cached_precompute["Q_pidx"]
        print(
            f"precompute quantization: pass-cache hit ({n} frames)",
            flush=True,
        )
        _t = _mark("量子化(cache)", _t)
    else:
        # DEBUG色はCRAMに既にある色だけを並べ替えて固定する。異なるパレット行との
        # 入替があり得るので、全フレームを最終的な行構成に対して量子化する前に行う。
        seg_pals, pal_extreme_stats = pin_p0_debug_extremes(seg_pals)
        print(f"  P0 DEBUG colours pinned: index1 darkest swaps "
              f"{pal_extreme_stats['dark_swapped_segments']}/{pal_extreme_stats['segments']}, "
              f"index15 brightest swaps "
              f"{pal_extreme_stats['bright_swapped_segments']}/{pal_extreme_stats['segments']}")

        # フレーム独立の割当/索引を並列で前計算(実行時間の大半)。以降のループは逐次(状態依存)。
        Q_detail, Q_assign, Q_pidx = precompute_quant(
            frames, seg_pals, frame_seg, frame_cache=frame_cache)
        del frame_cache
        # The older lossless index-15 canonicalizer is now a no-op for current
        # palettes because both DEBUG extremes were pinned before quantisation.
        # Keep the proof here while older palette inputs remain supported.
        seg_pals, pal15_stats = canonicalize_p0_index15(
            seg_pals, frame_seg, Q_assign, Q_pidx)
        _t = _mark("量子化", _t)
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

    # Optional quality upgrades use a whole-movie reserve plan. The dry run
    # follows the exact quantized target with the shared VRAM allocator. A
    # backwards pass then retains only the quality-budget bytes that future
    # bursts cannot replenish from their own frame supply. Its final target is
    # zero, so future protection, recovery, and end draining share one policy.
    upgrade_supply = body_variable_supply_bytes.copy()
    if PAL_WRITE_BYTES:
        for i in range(1, n):
            if int(frame_seg[i]) != int(frame_seg[i - 1]):
                upgrade_supply[i] = max(
                    0, int(upgrade_supply[i]) - PAL_WRITE_BYTES)
    cached_future = (
        pass_cache_payload.get("future")
        if pass_cache_payload is not None else None)
    if cached_future is not None:
        main_protected = cached_future["main_protected"]
        baseline_demand_prediction = cached_future[
            "baseline_demand_prediction"]
        baseline_prefetch_forecast = cached_future[
            "baseline_prefetch_forecast"]
        frame0_keys = cached_future["frame0_keys"]
        frame0_inline_pattern_limit = cached_future[
            "frame0_inline_pattern_limit"]
        boot_inline_capacity = cached_future["boot_inline_capacity"]
        boot_sidecar_capacity = cached_future["boot_sidecar_capacity"]
        boot_prefetch_capacity = cached_future["boot_prefetch_capacity"]
        boot_prefetch_plan = cached_future["boot_prefetch_plan"]
        demand_prediction = cached_future["demand_prediction"]
        supply_budget = cached_future["supply_budget"]
        prefetch_forecast = cached_future["prefetch_forecast"]
        print("future demand plan: pass-cache hit", flush=True)
    else:
        # Only changes that fit Near may degrade gracefully. Anything beyond
        # Near is at risk of Flbk or Miss and justifies moving budget capacity
        # away from an earlier frame.
        main_protected = np.zeros((n, C_CELLS), bool)
        previous_target_rgb = None
        for i in range(n):
            target_pals = seg_pals[int(frame_seg[i])]
            target_rgb = render_cells(Q_pidx[i], Q_assign[i], target_pals)
            if i == 0:
                main_protected[i] = True
            else:
                if int(frame_seg[i]) == int(frame_seg[i - 1]):
                    previous_display_rgb = previous_target_rgb
                else:
                    previous_display_rgb = render_cells(
                        Q_pidx[i - 1], Q_assign[i - 1], target_pals)
                target_changed = (
                    np.any(Q_pidx[i] != Q_pidx[i - 1], axis=1)
                    | (Q_assign[i] != Q_assign[i - 1])
                )
                graceful = f3_mask_eval(
                    previous_display_rgb, target_rgb, target_changed, NEAR_F3)
                main_protected[i] = target_changed & ~graceful
            previous_target_rgb = target_rgb
        baseline_demand_prediction = upgrade_planner.predict_update_demand_details(
            Q_pidx,
            Q_assign,
            vram_tiles=VRAM_TILES,
            name_bytes=NAME_BYTES,
            pattern_bytes=PATTERN_BYTES,
            max_cold=MAX_COLD,
            protected_frames=main_protected,
        )
        baseline_prefetch_forecast = raw_prefetch.forecast_requests(
            Q_pidx,
            Q_assign,
            main_protected,
            vram_tiles=VRAM_TILES,
            max_cold=MAX_COLD,
        )
        frame0_keys = tuple(dict.fromkeys(
            Q_pidx[0][cell].tobytes() for cell in range(C_CELLS)))
        frame0_inline_pattern_limit = min(
            C_CELLS,
            VRAM_TILES,
            av_config.FRAME0_PATTERN_STAGING_KB * 1024 // PATTERN_BYTES,
        )
        boot_inline_capacity = max(
            0, frame0_inline_pattern_limit - len(frame0_keys))
        boot_sidecar_capacity = av_config.boot_vram_sidecar_capacity(
            len(seg_pals))
        boot_prefetch_capacity = min(
            max(0, VRAM_TILES - len(frame0_keys)),
            boot_inline_capacity + boot_sidecar_capacity,
        )
        boot_prefetch_plan = (
            raw_prefetch.plan_boot_requests(
                baseline_demand_prediction,
                baseline_prefetch_forecast,
                frame0_keys,
                capacity=boot_prefetch_capacity,
            )
            if BOOT_VRAM_PREFETCH_ON else ()
        )
        # Re-run the exact-target trace with boot residency installed so every
        # future source and reserve sees the same initial VRAM state.
        demand_prediction = (
            upgrade_planner.predict_update_demand_details(
                Q_pidx,
                Q_assign,
                vram_tiles=VRAM_TILES,
                name_bytes=NAME_BYTES,
                pattern_bytes=PATTERN_BYTES,
                max_cold=MAX_COLD,
                protected_frames=main_protected,
                boot_prefetch_requests=boot_prefetch_plan,
            )
            if boot_prefetch_plan else baseline_demand_prediction
        )
        supply_budget = pattern_supply.plan_frame_budgets(
            demand_prediction, enabled=PATTERN_SUPPLY_ON)
        if RAW_PREFETCH_ON:
            prefetch_forecast = raw_prefetch.forecast_requests(
                Q_pidx,
                Q_assign,
                main_protected,
                vram_tiles=VRAM_TILES,
                max_cold=MAX_COLD,
                boot_prefetch_requests=boot_prefetch_plan,
            )
        else:
            prefetch_forecast = raw_prefetch.PrefetchForecast(
                requests=tuple(() for _ in range(n)),
                protected_cold=np.zeros(n, np.int64),
                requested_patterns=np.zeros(n, np.int64),
            )
        if _PASS_CACHE_PATH:
            cache_payload = {
                "precompute": {
                    "seg_pals": seg_pals,
                    "frame_seg": frame_seg,
                    "seg_bounds": seg_bounds,
                    "palette_stats": palette_stats,
                    "pal_extreme_stats": pal_extreme_stats,
                    "pal15_stats": pal15_stats,
                    "Q_detail": Q_detail,
                    "Q_assign": Q_assign,
                    "Q_pidx": Q_pidx,
                },
                "future": {
                    "main_protected": main_protected,
                    "baseline_demand_prediction": baseline_demand_prediction,
                    "baseline_prefetch_forecast": baseline_prefetch_forecast,
                    "frame0_keys": frame0_keys,
                    "frame0_inline_pattern_limit": frame0_inline_pattern_limit,
                    "boot_inline_capacity": boot_inline_capacity,
                    "boot_sidecar_capacity": boot_sidecar_capacity,
                    "boot_prefetch_capacity": boot_prefetch_capacity,
                    "boot_prefetch_plan": boot_prefetch_plan,
                    "demand_prediction": demand_prediction,
                    "supply_budget": supply_budget,
                    "prefetch_forecast": prefetch_forecast,
                },
            }
            cache_path = Path(_PASS_CACHE_PATH)
            sim_pass_cache.save(
                cache_path, pass_cache_metadata, cache_payload)
            print(
                f"sim pass cache: saved {cache_path} "
                f"({cache_path.stat().st_size / 1024**2:.1f} MiB)",
                flush=True,
            )
    boot_inline_requests = min(
        len(boot_prefetch_plan), boot_inline_capacity)
    boot_sidecar_requests = len(boot_prefetch_plan) - boot_inline_requests

    slot_locality_map = os.environ.get("CBRSIM_SLOT_LOCALITY_MAP", "").strip()
    if slot_locality_map:
        slot_locality = evaluate_slot_locality(
            demand_prediction.cold_slots,
            VRAM_TILES,
            np.load(slot_locality_map),
            cold_cap=MAX_COLD,
        )
    elif not PACKED_COLD_RUN_EXECUTION:
        slot_locality = evaluate_slot_locality(
            demand_prediction.cold_slots,
            VRAM_TILES,
            np.arange(VRAM_TILES, dtype=np.int64),
            cold_cap=MAX_COLD,
        )
    else:
        slot_locality = optimize_slot_locality(
            demand_prediction.cold_slots,
            VRAM_TILES,
            cold_cap=MAX_COLD,
            target_heavy_runs=SLOT_LOCALITY_HEAVY_RUN_TARGET,
        )
    physical_by_logical = np.asarray(
        slot_locality.physical_by_logical, np.int64)
    predicted_risk = np.asarray(slot_locality.risk_frames, bool)
    predicted_baseline_runs = np.asarray(
        slot_locality.baseline_runs, np.int64)
    predicted_local_runs = np.asarray(
        slot_locality.optimized_runs, np.int64)
    print(
        "slot locality: fixed logical->physical bijection; "
        f"execution="
        f"{'packed-suffix' if PACKED_COLD_RUN_EXECUTION else 'legacy-entry-order'}; "
        f"map={'loaded' if slot_locality_map else 'identity' if not PACKED_COLD_RUN_EXECUTION else 'predicted'}; "
        f"predicted max runs {int(predicted_baseline_runs[1:].max(initial=0))}"
        f"->{int(predicted_local_runs[1:].max(initial=0))}, "
        f"risk frames={int(predicted_risk.sum())} "
        f"risk max {int(predicted_baseline_runs[predicted_risk].max(initial=0))}"
        f"->{int(predicted_local_runs[predicted_risk].max(initial=0))}",
        flush=True,
    )
    dic_dictionary_keys = set(supply_budget.dic_dictionary)
    dic_dictionary_index = {
        key: index for index, key in enumerate(supply_budget.dic_dictionary)
    }
    preload_credit_bytes = supply_budget.total * PATTERN_BYTES
    protected_credit_bytes = (
        np.minimum(supply_budget.total, demand_prediction.protected_cold)
        * PATTERN_BYTES)
    # A boot-preloaded cold pattern still needs its two-byte name entry but no
    # BODY payload.  Remove only that 32-byte payload from the future reserve
    # traces; the live encoder below applies the same charge to actual work.
    upgrade_demand = np.maximum(
        demand_prediction.exact_bytes - preload_credit_bytes, 0)
    main_demand = np.maximum(
        demand_prediction.protected_bytes - protected_credit_bytes, 0)
    main_reserve_plan = upgrade_planner.build_balanced_reserve_plan(
        main_demand, upgrade_supply, QUALITY_BUDGET_BYTES)
    # Optional exact upgrades are not required to avoid Miss.  Keep their
    # complete-demand reserve strict: balancing this deliberately infeasible
    # all-exact trace spends too much saved allowance before unpredicted live
    # Main work.  Only the narrower Miss-risk trace shares unavoidable loss.
    upgrade_reserve = upgrade_planner.build_reserve_curve(
        upgrade_demand, upgrade_supply, QUALITY_BUDGET_BYTES)
    main_reserve = main_reserve_plan.reserve
    upgrade_reserve = np.maximum(upgrade_reserve, main_reserve)
    print(
        "quality plan: upgrade exact reserve "
        f"start={upgrade_reserve[0] // 1024 if n else 0}KB "
        f"peak={upgrade_reserve.max() // 1024 if n else 0}KB "
        f"end={upgrade_reserve[-1] // 1024 if n else 0}KB strict; "
        "main Miss-risk reserve "
        f"start={main_reserve[0] // 1024 if n else 0}KB "
        f"peak={main_reserve.max() // 1024 if n else 0}KB "
        f"end={main_reserve[-1] // 1024 if n else 0}KB "
        f"balanced_shortfall={main_reserve_plan.shortfall.sum() // 1024}KB",
        flush=True,
    )
    print(
        "pattern supply plan: "
        f"enabled={int(PATTERN_SUPPLY_ON)} "
        f"Wr0={supply_budget.wr0_patterns}/{pattern_supply.WORD_BUF_PATTERNS} "
        f"Wr1={supply_budget.wr1_patterns}/{pattern_supply.WORD_BUF_PATTERNS} "
        f"Dic={supply_budget.dic_patterns}/{pattern_supply.DIC_BUF_PATTERNS} "
        f"hits={int(supply_budget.dic.sum())} "
        f"frames={int(np.count_nonzero(supply_budget.total))}",
        flush=True,
    )
    print(
        "raw VRAM prefetch: "
        f"boot={int(BOOT_VRAM_PREFETCH_ON)} "
        f"frame0_exact={len(frame0_keys)} "
        f"boot_loads={len(boot_prefetch_plan)}/{boot_prefetch_capacity} "
        f"(inline={boot_inline_requests}/{boot_inline_capacity}, "
        f"backside={boot_sidecar_requests}/{boot_sidecar_capacity}); "
        f"runtime={int(RAW_PREFETCH_ON)} lookahead={RAW_PREFETCH_LOOKAHEAD} "
        f"max_requests/frame={RAW_PREFETCH_MAX_REQUESTS_PER_FRAME} "
        f"min_batch={RAW_PREFETCH_MIN_BATCH} "
        f"budget_floor={RAW_PREFETCH_BUDGET_FLOOR_PATTERNS}patterns "
        f"request_frames={int(np.count_nonzero(prefetch_forecast.requested_patterns))} "
        f"requested_patterns={int(prefetch_forecast.requested_patterns.sum())}",
        flush=True,
    )
    _t = _mark("格上げ残量計画", _t)

    _t_render = 0.0        # ループ内訳: 描画+PNG保存に費やした時間(残りがcommit/探索)
    # Optional, low-frequency timing for the sequential decision loop.  Keep
    # this disabled for normal encodes: the nested resident-search timers are
    # deliberately detailed enough to add measurable profiling overhead.
    _loop_profile = os.environ.get("CBRSIM_LOOP_PROFILE", "0").strip().lower() not in {
        "", "0", "false", "no", "off",
    }
    _lp_sections = (
        "prepare", "decision_setup", "decision_commit", "upgrade",
        "allocate_route", "budget_finalize", "accounting", "tail",
    )
    _lp_totals = {name: 0.0 for name in _lp_sections}
    _lp_frames = {name: [] for name in _lp_sections}
    _lp_nested_totals = {"resident_filter": 0.0, "resident_distance": 0.0}
    _lp_nested_frames = {name: [] for name in _lp_nested_totals}
    _lp_counts = {
        "changed": 0, "ordered": 0, "resident_calls": 0,
        "resident_candidates": 0, "resident_empty": 0,
        "resident_cache_hits": 0, "resident_cache_builds": 0,
        "resident_adjacent_calls": 0, "resident_adjacent_candidates": 0,
        "resident_adjacent_empty": 0,
        "exact_blocked": 0, "flbk_no_candidate": 0,
        "flbk_budget_blocked": 0, "flbk_not_improved": 0,
        "flbk_absolute_reject": 0, "flbk_accepted": 0,
    }

    def _lp_mark(name, started, frame_times):
        now = time.perf_counter()
        elapsed = now - started
        frame_times[name] += elapsed
        return now
    # PNG保存(3枚/コマ)。Pillowの並列保存はCPython 3.13/3.14の長時間simで
    # NumPy配列を壊した実績があるため、全対応版で既定同期保存。
    from concurrent.futures import ThreadPoolExecutor
    import collections as _collections
    _png_workers = png_workers()
    _png_pool = (ThreadPoolExecutor(max_workers=_png_workers)
                 if not NO_PANELS and _png_workers > 1 else None)
    _png_futs = _collections.deque()
    if not NO_PANELS:
        print(f"PNG writers: {_png_workers} ({'async' if _png_pool else 'synchronous'})", flush=True)

    def _write_png(arr, path):
        """Write one complete, decodable PNG before replacing the old frame."""
        temp = path.with_name(path.name + ".tmp")
        try:
            Image.fromarray(arr, "RGB").save(temp, format="PNG")
            with Image.open(temp) as check:
                check.load()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def _save_png(arr, path):
        # cells_to_image can return a view into live frame state.  Freeze it
        # before the worker starts so the next frame cannot mutate its buffer.
        frozen = np.ascontiguousarray(arr).copy()
        if _png_pool is None:
            _write_png(frozen, path)
            return
        if len(_png_futs) >= 96:          # 背圧: 生成が速すぎてもメモリ膨張を防ぐ
            _png_futs.popleft().result()
        _png_futs.append(_png_pool.submit(_write_png, frozen, path))

    # The loop creates and releases many short-lived containers but no
    # intentional reference cycles.  A generation-2 cyclic-GC scan over the
    # growing pattern dictionaries causes isolated 50-90 ms stalls.  Collect
    # once before the loop and restore the caller's GC state afterwards;
    # ordinary reference-count reclamation remains active throughout.
    import gc as _gc
    _gc_was_enabled = _gc.isenabled()
    if _gc_was_enabled:
        _gc.collect()
        _gc.disable()

    for i in range(n):
        if _loop_profile:
            _lp_frame = {name: 0.0 for name in _lp_sections}
            _lp_nested_frame = {name: 0.0 for name in _lp_nested_totals}
            _lp_t = time.perf_counter()
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
        # resident探索用の平均色バケツ座標(常時計算・軽い)
        mbk = (plain_rgb.reshape(C_CELLS, 64, 3).mean(1) // RESIDENT_BW).astype(np.int32)

        # 内容変化検出(dither非依存: plain同士で比較)
        key_changed = np.fromiter(
            (plain_keys[c] != committed_plain[c] for c in range(C_CELLS)), bool, C_CELLS)
        pal_changed = assign.astype(np.int16) != cur_pal
        changed = key_changed | pal_changed

        diff = np.abs(plain_rgb.astype(np.int32) - cur_rgb.astype(np.int32)).sum(axis=(1, 2, 3))
        detail_norm = detail / (detail.max() + 1e-6)
        # Miss/Flbkだけが前フレームまでに積んだ、距離加重圧力で
        # 優先度を底上げする。Nearは短時間なら許容できるため対象外。
        age_press = update_age_pressure(age_press, cell_tier, diff)
        aging = priority_aging(age_press)
        # 優先度 = RGB総和の変化量 × 任意の細かさ項 × 距離加重エージング × 枠重み
        score = diff.astype(np.float64) * (1.0 + DETAIL_ALPHA * detail_norm) * aging * border_mask
        # Near: 変化タイルのうち見た目ほぼ同じ(F3)は先に省略(old表示を維持)。買い戻し(Raw更新)は
        # 準備金を食い潰すので入れない(=配給とセットでしか成立しないため今は無し)。
        # MIDFAR時は Near も統合探索(commit_unified)で判定するので事前フィルタしない。
        near = (near_mask_eval(cur_rgb, plain_rgb, changed)
                if i > 0 and NEAR_ON and not MIDFAR_ON
                else np.zeros(C_CELLS, bool))
        # 同点tie-break: CENTERTIE_ON なら中央優先(lexsort: 主=-score, 副=center_dist)。
        # 既定は従来どおり argsort(-score)=不定(実機用simの決定を変えないため)。
        order = np.lexsort((center_dist, -score)) if CENTERTIE_ON else np.argsort(-score)
        order = [int(c) for c in order if changed[c] and not near[c]]
        if _loop_profile:
            _lp_counts["changed"] += int(changed.sum())
            _lp_counts["ordered"] += len(order)
            _lp_t = _lp_mark("prepare", _lp_t, _lp_frame)

        # Reserve the exact fixed BODY control first.  The remaining bytes come
        # from the player's integer CD-sector cadence and fund variable control
        # entries, run descriptors, and Prg pattern payload together.
        budget = max(
            int(body_variable_supply_bytes[i])
            - (PAL_WRITE_BYTES if pal_swap else 0),
            0,
        )
        frame_cd = budget                             # このフレーム自身のCDタイル予算
        # frame0はDAT冒頭の専用ヘッダとしてboot中に時間無制限でVRAMへロードする(=ストリーミング
        # PrgBuf/quality budgetを一切消費しない)。よってframe0は予算無制限で全面フルロードし、
        # quality budgetは満量のままframe1へ渡す。実機の崩壊はframe0の大バーストが
        # リングを削っていたのが原因で、ヘッダ化で根絶する。
        if i == 0:
            decision_budget = 1 << 30
        elif QUALITY_BUDGET_ON:
            funded_limit = upgrade_planner.planned_spend_limit(
                budget_before=quality_budget,
                frame_supply=frame_cd,
                reserve_after=int(main_reserve[i]),
                already_spent=0,
            )
            decision_budget = funded_limit
        else:
            decision_budget = frame_cd
        frame_patch = (frozenset() if QUALITY_BUDGET_ON
                       else prg_patch.get(i, frozenset()))

        updated = np.zeros(C_CELLS, bool)
        dedup_mask = np.zeros(C_CELLS, bool)   # 更新したが同一パターン流用(VRAM常駐)だったタイル
        prg_mask = np.zeros(C_CELLS, bool)     # PRG先読みバッファから0CDで埋めたタイル
        raw_mask = np.zeros(C_CELLS, bool)     # 新規CD転送したタイル(Raw)
        near_mask = np.zeros(C_CELLS, bool)    # MIDFAR: ほぼ同一常駐を流用/維持(Near)
        flbk_mask = np.zeros(C_CELLS, bool)    # MIDFAR: Missのフォールバック(荒くても常駐で穴埋め)(Flbk)
        loaded_keys = set()
        tile_recs = 0
        name_recs = 0
        dedup_saved = 0
        l3_hits = 0
        prg_hits = 0
        spent_tiles = 0
        cold_spent = 0             # このコマのcold数(Raw+Buf=実機のパターンDMA数)
        prg_spent = 0              # このコマの物理PrgBuf消費数
        frame_wr_budget = int(supply_budget.wr[i])
        wr_used = 0
        dic_used = 0
        preload_sources = {}

        def reserved_variable_spend(
            decision_spent=0,
            cold_tiles=0,
        ):
            """Upper-bound BODY work before actual source runs are known."""

            return (
                decision_spent
                + cold_tiles * stream_schedule.RUN_DESCRIPTOR_BYTES)

        def decision_fits(cost, *, extra_cold=0, limit=None):
            if limit is None:
                limit = decision_budget
            return reserved_variable_spend(
                spent_tiles + cost,
                cold_spent + extra_cold,
            ) <= limit

        def current_reserved_spend():
            return reserved_variable_spend(spent_tiles, cold_spent)

        def preload_source(key):
            if i == 0:
                return pattern_supply.SOURCE_PRG
            if key in dic_dictionary_keys:
                return pattern_supply.SOURCE_DIC
            if wr_used < frame_wr_budget:
                return pattern_supply.SOURCE_WR
            return pattern_supply.SOURCE_PRG

        def commit_preload(key, source):
            nonlocal wr_used, dic_used
            if source == pattern_supply.SOURCE_DIC:
                dic_used += 1
            elif source == pattern_supply.SOURCE_WR:
                wr_used += 1
            else:
                raise AssertionError("Prg is not a preload source")
            preload_sources[key] = source

        def prg_source_fits(source, cell=None):
            return (
                source != pattern_supply.SOURCE_PRG
                or prg_spent < frame_max_prg
            )

        def commit_prg_source(source, cell=None):
            nonlocal prg_spent
            if source == pattern_supply.SOURCE_PRG:
                if not prg_source_fits(source, cell):
                    raise AssertionError("per-frame cold cap exceeded by Prg")
                prg_spent += 1
        # Candidate eligibility is shared by every cell in one mean-colour
        # bucket until this frame loads/revalidates a pattern in that bucket.
        # Cache both the exact legacy key order and its contiguous descriptors.
        resident_bucket_cache = {} if MIDFAR_ON else None

        def append_resident_bucket(key, bucket):
            resident_bucket[bucket].append(key)
            key_buckets = pat_buckets[key]
            old_buckets = tuple(key_buckets)
            key_buckets.add(bucket)
            if resident_bucket_cache is not None:
                # Reloading an existing key makes every older occurrence of
                # that key eligible again, potentially at positions inside
                # the latest 24.  Preserve the legacy duplicate order with a
                # full rescan.  Only a never-seen key is safe to prepend.
                if old_buckets:
                    for affected in key_buckets:
                        resident_bucket_cache.pop(affected, None)
                    return

                descriptor = pat_colors[key]
                # The appended occurrence is the newest valid candidate.  It
                # pushes only the previous 24th entry out of the exact legacy
                # reversed-list window; no historical scan is needed.
                entry = resident_bucket_cache.get(bucket)
                if entry is not None:
                    keys, descriptors = entry
                    next_keys = (key,) + keys[:RESIDENT_K - 1]
                    next_descriptors = np.empty(
                        (len(next_keys), 64), dtype=np.uint16)
                    next_descriptors[0] = descriptor
                    if len(next_keys) > 1:
                        next_descriptors[1:] = descriptors[:len(next_keys) - 1]
                    resident_bucket_cache[bucket] = (
                        next_keys, next_descriptors)

        def revalidate_pattern_segment(key):
            if pat_seg.get(key) == cur_seg:
                return
            pat_seg[key] = cur_seg
            if resident_bucket_cache is not None:
                for affected in pat_buckets.get(key, ()):
                    resident_bucket_cache.pop(affected, None)

        def activate_exact_pattern(key, cell):
            """Publish a prefetched exact pattern to approximation metadata."""
            if key not in pat_colors:
                cache_pattern(key, plain_rgb[cell], assign[cell], cur_seg)
                append_resident_bucket(
                    key,
                    (int(mbk[cell, 0]), int(mbk[cell, 1]), int(mbk[cell, 2])))
            else:
                revalidate_pattern_segment(key)

        def commit_frame0_exact(c):
            """Install frame 0 exactly; approximation has no boot-time value."""
            nonlocal tile_recs, name_recs, dedup_saved, spent_tiles, cold_spent
            key = plain_keys[c]
            in_vram = key in loaded_keys
            if in_vram:
                dedup_saved += 1
                dedup_mask[c] = True
                activate_exact_pattern(key, c)
                cost = NAME_BYTES
            else:
                loaded_keys.add(key)
                cold_spent += 1
                tile_recs += 1
                raw_mask[c] = True
                cost = NAME_BYTES + PATTERN_BYTES
                cache_pattern(key, plain_rgb[c], assign[c], cur_seg)
                append_resident_bucket(
                    key,
                    (int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2])))
            name_recs += 1
            spent_tiles += cost
            repoint(c, key, int(assign[c]), plain_rgb[c], i)
            committed_plain[c] = key
            updated[c] = True

        # frame0はDAT冒頭ヘッダで別ロード(リング非消費)なので常にcold上限を免除=全面フルロード。
        cold_limit_active = i > 0
        frame_max_cold = MAX_COLD if i > 0 else 0
        frame_max_prg = MAX_COLD if i > 0 else 0

        def commit_plain(c):
            # Legacy non-unified path: exact resident/L3/preload/cold only.
            nonlocal tile_recs, name_recs, dedup_saved, l3_hits, prg_hits, spent_tiles, cold_spent
            key = plain_keys[c]
            in_vram = alloc.is_resident(key) or key in loaded_keys      # L1/L2: VRAM常駐(転送ゼロ)
            in_prg = (not in_vram) and key in frame_patch
            in_l3 = (not in_vram) and (not in_prg) and L3_TILES > 0 and key in l3
            free = in_vram or in_l3
            source = (preload_source(key)
                      if not in_prg and not free
                      else pattern_supply.SOURCE_PRG)
            preload = source != pattern_supply.SOURCE_PRG
            cost = 0 if in_prg else (
                NAME_BYTES + (0 if free or preload else PATTERN_BYTES))
            if not decision_fits(cost, extra_cold=int(not free)):
                return False
            rep_key = key; rep_pal = int(assign[c]); rep_rgb = plain_rgb[c]
            if in_vram:
                dedup_saved += 1; dedup_mask[c] = True
                activate_exact_pattern(key, c)
            elif in_prg:
                if ((cold_limit_active and cold_spent >= frame_max_cold)
                        or not prg_source_fits(source, c)):
                    return False
                cold_spent += 1
                commit_prg_source(source, c)
                prg_hits += 1; prg_mask[c] = True; loaded_keys.add(key)
            elif in_l3:
                l3_hits += 1; loaded_keys.add(key); l3.pop(key, None)
            else:                                                # cold: exact pattern load (Raw or saved/preload-funded Buf)
                if ((cold_limit_active and cold_spent >= frame_max_cold)
                        or not prg_source_fits(source, c)):
                    return False
                cold_spent += 1
                commit_prg_source(source, c)
                loaded_keys.add(key)
                if preload:
                    commit_preload(key, source)
                    prg_hits += 1; prg_mask[c] = True
                elif QUALITY_BUDGET_ON and current_reserved_spend() >= frame_cd:
                    prg_hits += 1; prg_mask[c] = True
                else:
                    tile_recs += 1; raw_mask[c] = True
                cache_pattern(key, plain_rgb[c], assign[c], cur_seg)
                append_resident_bucket(
                    key, (int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2])))
            name_recs += 1; spent_tiles += cost
            repoint(c, rep_key, rep_pal, rep_rgb, i)
            committed_plain[c] = key; updated[c] = True
            return True

        # === 統合探索(MIDFAR): Same/Near/Flbk/Miss を1つのVRAM最良一致に統合 ===
        if MIDFAR_ON:
            # 現在表示がほぼ同一=0Bで維持可。ただし near_keep の入力 cur_rgb は「現在表示」なので、前フレームが
            # Flbk の近似コピーだと、その近似をさらに Near として維持=ゴーストが居座る(issue #10)。
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
            plain_colors = rendered_color_keys(plain_rgb).reshape(C_CELLS, 64)

            def resident_entry(bucket):
                """Return cached eligible keys and descriptors for one bucket."""
                entry = resident_bucket_cache.get(bucket)
                if entry is not None:
                    if _loop_profile:
                        _lp_counts["resident_cache_hits"] += 1
                    return entry
                if _loop_profile:
                    _lp_counts["resident_cache_builds"] += 1
                cand = []
                resident = alloc.key_slot
                loaded = loaded_keys
                cached = pat_colors
                segments = pat_seg
                for ck in reversed(resident_bucket[bucket]):
                    if ((ck not in resident and ck not in loaded)
                            or ck not in cached):
                        continue
                    if segments.get(ck) != cur_seg:
                        continue
                    cand.append(ck)
                    if len(cand) >= RESIDENT_K:
                        break
                keys = tuple(cand)
                descriptors = (
                    np.stack([cached[k] for k in keys]) if keys else None)
                entry = (keys, descriptors)
                resident_bucket_cache[bucket] = entry
                return entry

            def best_resident(c):
                """target に最も近い常駐候補 (key, dYm, dYp, dCm)。無ければ (None,大,大,大)。
                平均色バケツで前絞り→候補のF3(画素輝度差平均/最大・色差平均)をベクトル計算し最小を採る。"""
                if _loop_profile:
                    _lp_counts["resident_calls"] += 1
                    _br_t = time.perf_counter()
                b = (int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2]))
                entry = resident_entry(b)
                cand, arr = entry
                if _loop_profile:
                    _lp_counts["resident_candidates"] += len(cand)
                    _elapsed = time.perf_counter() - _br_t
                    _lp_nested_totals["resident_filter"] += _elapsed
                    _lp_nested_frame["resident_filter"] += _elapsed
                if not cand:
                    if _loop_profile:
                        _lp_counts["resident_empty"] += 1
                    return (None, 1e9, 1e9, 1e9)
                if _loop_profile:
                    _br_t = time.perf_counter()
                target = plain_colors[c]
                dY = _F3_DY_LUT[arr, target]
                dYm = dY.mean(1)
                dYp = dY.max(1)
                dCm = _F3_DC_LUT[arr, target].mean(1)
                j = int(np.argmin(dYm + 0.3 * dYp + 0.5 * dCm))
                if _loop_profile:
                    _elapsed = time.perf_counter() - _br_t
                    _lp_nested_totals["resident_distance"] += _elapsed
                    _lp_nested_frame["resident_distance"] += _elapsed
                return (cand[j], float(dYm[j]), float(dYp[j]), float(dCm[j]))

            def best_adjacent_resident(c):
                """Best current-segment candidate from adjacent mean buckets.

                The ordinary search remains confined to the exact target
                bucket.  Fallback alone uses this bounded second chance when
                that result cannot improve the display.  Taking only the
                newest eligible key from each of the 26 neighbours keeps the
                search small while covering newly seeded colours at cuts and
                CRAM boundaries.
                """
                if _loop_profile:
                    _lp_counts["resident_adjacent_calls"] += 1
                    _br_t = time.perf_counter()
                b = (int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2]))
                cand = []
                seen = set()
                offsets = sorted(
                    ((dr, dg, db)
                     for dr in (-1, 0, 1)
                     for dg in (-1, 0, 1)
                     for db in (-1, 0, 1)
                     if (dr, dg, db) != (0, 0, 0)),
                    key=lambda offset: (
                        abs(offset[0]) + abs(offset[1]) + abs(offset[2]),
                        offset),
                )
                for dr, dg, db in offsets:
                    neighbour = (b[0] + dr, b[1] + dg, b[2] + db)
                    if min(neighbour) < 0:
                        continue
                    neighbour_keys, _descriptors = resident_entry(neighbour)
                    for ck in neighbour_keys:
                        if ck not in seen:
                            cand.append(ck)
                            seen.add(ck)
                            break
                if _loop_profile:
                    _lp_counts["resident_adjacent_candidates"] += len(cand)
                if not cand:
                    if _loop_profile:
                        _lp_counts["resident_adjacent_empty"] += 1
                        _elapsed = time.perf_counter() - _br_t
                        _lp_nested_totals["resident_filter"] += _elapsed
                        _lp_nested_frame["resident_filter"] += _elapsed
                    return (None, 1e9, 1e9, 1e9)
                arr = np.stack([pat_colors[k] for k in cand])
                if _loop_profile:
                    _elapsed = time.perf_counter() - _br_t
                    _lp_nested_totals["resident_filter"] += _elapsed
                    _lp_nested_frame["resident_filter"] += _elapsed
                    _br_t = time.perf_counter()
                target = plain_colors[c]
                dY = _F3_DY_LUT[arr, target]
                dYm = dY.mean(1)
                dYp = dY.max(1)
                dCm = _F3_DC_LUT[arr, target].mean(1)
                j = int(np.argmin(dYm + 0.3 * dYp + 0.5 * dCm))
                if _loop_profile:
                    _elapsed = time.perf_counter() - _br_t
                    _lp_nested_totals["resident_distance"] += _elapsed
                    _lp_nested_frame["resident_distance"] += _elapsed
                return (cand[j], float(dYm[j]), float(dYp[j]), float(dCm[j]))

            def tier_of(dYm, dYp, dCm):
                for ti, (_nm, Ym, Yp, C) in enumerate(MIDFAR_TIERS):
                    if dYm <= Ym and dYp <= Yp and dCm <= C:
                        return ti          # 0=near,1=flbk
                return -1

            pending_exact_or_fallback = []

            def commit_unified_cheap(c):
                """Commit zero/2-byte decisions; defer every cold exact choice."""
                nonlocal tile_recs, name_recs, dedup_saved, prg_hits, spent_tiles, cold_spent
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
                # 2. Good reuse (Same=exact / Near=tier0) points at resident VRAM.
                if ((exact or (bk is not None and tier == 0))
                        and decision_fits(NAME_BYTES)):
                    if exact:
                        dedup_saved += 1; dedup_mask[c] = True                # Same(完全一致流用=Sameへ畳む)
                        rk, rp, rr = key, int(assign[c]), plain_rgb[c]
                        activate_exact_pattern(key, c)                        # fresh/prefetched keyを有効化
                    else:
                        near_mask[c] = True; rk, rp, rr = bk, pat_pal[bk], pat_rgb[bk]
                    loaded_keys.add(rk); name_recs += 1; spent_tiles += NAME_BYTES
                    repoint(c, rk, rp, rr, i); committed_plain[c] = key; updated[c] = True
                    return
                # Cold exact and fallback share the remaining frame budget.
                # Defer both so the exact selector can preserve every pending
                # cell's two-byte fallback entry before spending pattern/run
                # bytes.  Otherwise early exact loads can make step 4
                # unreachable even when a resident fallback exists.
                pending_exact_or_fallback.append(int(c))

            def commit_unified_exact(c, limit):
                """Try an exact decision while preserving later fallback names."""
                nonlocal tile_recs, name_recs, dedup_saved, prg_hits, spent_tiles, cold_spent
                key = plain_keys[c]
                exact = alloc.is_resident(key) or key in loaded_keys
                if exact:
                    if not decision_fits(NAME_BYTES, limit=limit):
                        return False
                    dedup_saved += 1
                    dedup_mask[c] = True
                    activate_exact_pattern(key, c)
                    loaded_keys.add(key)
                    name_recs += 1
                    spent_tiles += NAME_BYTES
                    repoint(c, key, int(assign[c]), plain_rgb[c], i)
                    committed_plain[c] = key
                    updated[c] = True
                    return True

                source = preload_source(key)
                preload = source != pattern_supply.SOURCE_PRG
                cost = NAME_BYTES + (0 if preload else PATTERN_BYTES)
                if (decision_fits(cost, extra_cold=1)
                        and decision_fits(
                            cost, extra_cold=1, limit=limit)
                        and prg_source_fits(source, c)
                        and not (
                            cold_limit_active
                            and cold_spent >= frame_max_cold)):
                    cold_spent += 1
                    commit_prg_source(source, c)
                    loaded_keys.add(key)
                    if preload:
                        commit_preload(key, source)
                        prg_hits += 1; prg_mask[c] = True
                    elif QUALITY_BUDGET_ON and current_reserved_spend() >= frame_cd:
                        prg_hits += 1; prg_mask[c] = True
                    else:
                        tile_recs += 1; raw_mask[c] = True
                    cache_pattern(key, plain_rgb[c], assign[c], cur_seg)
                    append_resident_bucket(
                        key, (int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2])))
                    name_recs += 1; spent_tiles += cost
                    repoint(c, key, int(assign[c]), plain_rgb[c], i); committed_plain[c] = key; updated[c] = True
                    return True
                if _loop_profile:
                    _lp_counts["exact_blocked"] += 1
                return False

            def commit_unified_fallback(c):
                """Use the best resident fallback after the exact selection."""
                nonlocal name_recs, dedup_saved, spent_tiles
                key = plain_keys[c]
                exact = alloc.is_resident(key) or key in loaded_keys
                if exact:
                    if not decision_fits(NAME_BYTES):
                        if _loop_profile:
                            _lp_counts["flbk_budget_blocked"] += 1
                        return
                    dedup_saved += 1
                    dedup_mask[c] = True
                    activate_exact_pattern(key, c)
                    loaded_keys.add(key)
                    name_recs += 1
                    spent_tiles += NAME_BYTES
                    repoint(c, key, int(assign[c]), plain_rgb[c], i)
                    committed_plain[c] = key
                    updated[c] = True
                    return

                bk, dYm, dYp, dCm = best_resident(c)

                def acceptable(candidate, ym, yp, cm):
                    if candidate is None:
                        return False
                    if not FLBK_IMPROVE_ONLY:
                        return tier_of(ym, yp, cm) == 1
                    cur = cur_rgb[c].astype(np.float64)
                    tgt = plain_rgb[c].astype(np.float64)
                    dY0 = np.abs(cur @ _LWv - tgt @ _LWv)
                    dC0 = np.sqrt((cur @ _CBv - tgt @ _CBv) ** 2 +
                                 (cur @ _CRv - tgt @ _CRv) ** 2)
                    old_score = float(
                        dY0.mean() + 0.3 * dY0.max() + 0.5 * dC0.mean())
                    new_score = ym + 0.3 * yp + 0.5 * cm
                    return new_score < old_score - FLBK_MIN_IMPROVE

                accepted = acceptable(bk, dYm, dYp, dCm)
                if not accepted:
                    abk, aYm, aYp, aCm = best_adjacent_resident(c)
                    if acceptable(abk, aYm, aYp, aCm):
                        bk, dYm, dYp, dCm = abk, aYm, aYp, aCm
                        accepted = True
                # 4. ロード不可(画質予算尽き) → Flbk 近似流用(2B)で穴埋め(Missのフォールバック)。
                #    改善モード(既定): 絶対しきいに縛らず、現在表示より少しでも target に近づく候補なら採る。
                #    絶対モード(CBRSIM_FLBK_IMPROVE_ONLY=0): flbk tier(絶対しきい)内の候補のみ。
                if bk is None:
                    if _loop_profile:
                        _lp_counts["flbk_no_candidate"] += 1
                    return
                if not decision_fits(NAME_BYTES):
                    if _loop_profile:
                        _lp_counts["flbk_budget_blocked"] += 1
                    return
                if bk is not None and not exact:
                    if not accepted:
                        if _loop_profile:
                            _lp_counts[
                                "flbk_not_improved"
                                if FLBK_IMPROVE_ONLY
                                else "flbk_absolute_reject"] += 1
                        return
                    if _loop_profile:
                        _lp_counts["flbk_accepted"] += 1
                    flbk_mask[c] = True
                    loaded_keys.add(bk); name_recs += 1; spent_tiles += NAME_BYTES
                    repoint(c, bk, pat_pal[bk], pat_rgb[bk], i); committed_plain[c] = key; updated[c] = True
                    return
                # 5. Miss(何もしない)

        if _loop_profile:
            _lp_t = _lp_mark("decision_setup", _lp_t, _lp_frame)

        # 優先度順(予算内)。買えない高優先はスキップし、安い(常駐)セルは拾う
        if i == 0:
            for c in range(C_CELLS):
                commit_frame0_exact(c)
        elif MIDFAR_ON:
            # Phase 1: establish every free/cheap result without allowing a
            # cold load to consume a later cell's fallback entry.
            for c in order:
                commit_unified_cheap(c)

            # Phase 2: select exact loads in the same priority order.  Each
            # accepted exact includes its own two-byte name and must leave two
            # bytes for every still-pending cell.  This makes the final
            # fallback phase reachable by construction.
            exact_deferred = []
            pending_count = len(pending_exact_or_fallback)
            for position, c in enumerate(pending_exact_or_fallback):
                remaining = pending_count - position - 1
                fallback_reserve = len(exact_deferred) + remaining
                exact_limit = (
                    decision_budget - fallback_reserve * NAME_BYTES)
                if not commit_unified_exact(c, exact_limit):
                    exact_deferred.append(c)

            # Phase 3: newly loaded exact patterns are now eligible resident
            # candidates too, so recompute the best fallback at commit time.
            for c in exact_deferred:
                commit_unified_fallback(c)
        else:
            for c in order:
                commit_plain(c)
        if _loop_profile:
            _lp_t = _lp_mark("decision_commit", _lp_t, _lp_frame)

        # Upgrade approximate or carried cells to exact Raw/Buf using only
        # bytes above this frame's whole-movie reserve target.
        upgraded = 0
        if i > 0 and UPGRADE_ON and QUALITY_BUDGET_ON:
            upgrade_funded_limit = upgrade_planner.planned_spend_limit(
                budget_before=quality_budget,
                frame_supply=frame_cd,
                reserve_after=int(upgrade_reserve[i]),
                already_spent=current_reserved_spend(),
            )
            upgrade_limit = max(
                current_reserved_spend(),
                upgrade_funded_limit,
            )
            if current_reserved_spend() < upgrade_limit:
                def raw_upgrade(c, lim):
                    nonlocal tile_recs, name_recs, dedup_saved, prg_hits, spent_tiles, upgraded, cold_spent
                    key = plain_keys[c]
                    in_vram = alloc.is_resident(key) or key in loaded_keys
                    # A same-frame Near/Flbk decision already owns one
                    # packed update entry.  Upgrading it replaces that entry's
                    # final key; it does not append a second two-byte entry.
                    # A carried approximation or Near keep has no entry yet.
                    entry_cost = 0 if updated[c] else NAME_BYTES
                    source = (preload_source(key) if not in_vram
                              else pattern_supply.SOURCE_PRG)
                    preload = source != pattern_supply.SOURCE_PRG
                    cost = entry_cost if in_vram else (
                        entry_cost + (0 if preload else PATTERN_BYTES))
                    if not decision_fits(
                            cost,
                            extra_cold=int(not in_vram),
                            limit=lim):
                        return
                    if ((not in_vram)
                            and (
                                (cold_limit_active
                                 and cold_spent >= frame_max_cold)
                                or not prg_source_fits(source, c))):
                        return
                    near_mask[c] = False; flbk_mask[c] = False   # 近似を取消
                    if in_vram:
                        dedup_saved += 1; dedup_mask[c] = True
                        activate_exact_pattern(key, c)
                    else:
                        cold_spent += 1
                        commit_prg_source(source, c)
                        loaded_keys.add(key)
                        if preload:
                            commit_preload(key, source)
                            prg_hits += 1; prg_mask[c] = True
                        else:
                            tile_recs += 1; raw_mask[c] = True
                        cache_pattern(key, plain_rgb[c], assign[c], cur_seg)
                        append_resident_bucket(
                            key, (int(mbk[c, 0]), int(mbk[c, 1]), int(mbk[c, 2])))
                    if not updated[c]:
                        name_recs += 1
                    spent_tiles += cost
                    repoint(c, key, int(assign[c]), plain_rgb[c], i)
                    committed_plain[c] = key; updated[c] = True; upgraded += 1
                carried = (cell_tier < 9) & ~changed            # 変化せず近似のまま持ち越し(安定Near等)
                cand_mask = near_mask | flbk_mask | carried
                sev = np.full(C_CELLS, 9, np.int16)             # 劣化が重い順に格上げ(sev小=先)
                sev[carried] = cell_tier[carried]
                sev[flbk_mask] = 1; sev[near_mask] = 2
                # A persistent approximation remains the highest-priority
                # exact correction; every severity shares the same reserve.
                if GHOST_ESCALATE_N:
                    sev[(approx_carry >= GHOST_ESCALATE_N) & cand_mask] = 0
                for c in sorted((int(x) for x in np.where(cand_mask)[0]),
                                key=lambda c: (int(sev[c]), -float(age_press[c]), -score[c])):
                    raw_upgrade(c, upgrade_limit)
        if _loop_profile:
            _lp_t = _lp_mark("upgrade", _lp_t, _lp_frame)

        # 共有割り当て: このフレームの更新セルを cell順で place(=pack の resolve と同一順・同一コード)。
        # ここで residency/追い出しが確定し、次フレームの cold 判定に反映される。維持(near_keep)セルは
        # 更新でないので place しない=cur_slot/slot_refs が前回のまま(参照継続で保護)。realized=cap の要。
        upd_ck = [(int(c), cur_key[int(c)]) for c in np.where(updated)[0]
                  if cur_key[int(c)] is not None]
        logical_placements = alloc.place_frame(upd_ck, i)
        placements = remap_placements(
            logical_placements, physical_by_logical)
        transfer_order = cold_transfer_order(placements)
        frame_sources = [pattern_supply.SOURCE_PRG] * len(upd_ck)
        preload_updates = [
            update_index
            for update_index, ((_, key), (_, cold))
            in enumerate(zip(upd_ck, logical_placements))
            if cold and key in preload_sources
        ]
        if len(preload_updates) != wr_used + dic_used:
            raise AssertionError(
                f"frame {i}: preload decisions={wr_used + dic_used} but allocator "
                f"realized {len(preload_updates)} cold preload patterns")
        if wr_used > int(supply_budget.wr[i]):
            raise AssertionError(f"frame {i}: WordBuf source budget underflow")
        for update_index in preload_updates:
            key = upd_ck[update_index][1]
            frame_sources[update_index] = preload_sources[key]

        # Resolve display categories from the allocator's actual cold update,
        # not from decision order. Decisions are priority-ordered but the
        # player applies cells in cell order, so when several cells choose the
        # same key, the cell that physically carries the pattern can differ
        # from the cell that funded it. Only that actual cold cell is Raw or a
        # physical-source category; the other same-frame users are Same.
        raw_display_mask = np.zeros(C_CELLS, bool)
        prg_source_mask = np.zeros(C_CELLS, bool)
        wr0_source_mask = np.zeros(C_CELLS, bool)
        wr1_source_mask = np.zeros(C_CELLS, bool)
        dic_source_mask = np.zeros(C_CELLS, bool)
        raw_keys = {cur_key[int(cell)] for cell in np.where(raw_mask)[0]}
        buf_keys = {cur_key[int(cell)] for cell in np.where(prg_mask)[0]}
        if raw_keys & buf_keys:
            raise AssertionError(
                f"frame {i}: exact key has both Raw and Buf funding")
        for (cell, key), (_slot, cold), source in zip(
                upd_ck, logical_placements, frame_sources):
            if not cold:
                continue
            if key in raw_keys:
                raw_display_mask[cell] = True
                continue
            if key not in buf_keys:
                raise AssertionError(
                    f"frame {i}: cold cell {cell} has no Raw/Buf funding")
            if source == pattern_supply.SOURCE_PRG:
                prg_source_mask[cell] = True
            elif source == pattern_supply.SOURCE_WR:
                (wr0_source_mask if i % 2 == 0 else wr1_source_mask)[cell] = True
            elif source == pattern_supply.SOURCE_DIC:
                dic_source_mask[cell] = True
            else:
                raise AssertionError(
                    f"frame {i}: unknown pattern source {source}")
        if int(raw_display_mask.sum()) != int(raw_mask.sum()):
            raise AssertionError(
                f"frame {i}: allocator Raw split does not match funded loads")
        if sum(mask.sum() for mask in (
                prg_source_mask, wr0_source_mask, wr1_source_mask,
                dic_source_mask)) != int(prg_mask.sum()):
            raise AssertionError(
                f"frame {i}: physical source split does not cover Buf loads")

        # coldlife計測: このフレームのcoldロードを未来ターゲット(前計算済み)で分類。
        # 「次フレームで死ぬ」でも、収束型(その次から安定)と連続運動型(変化が続く)は
        # 意味が正反対: 前者は1フレーム遅らせれば中間ロードが浮き、後者は減点すると飢餓が出る。
        frame_cold = []
        if i > 0:
            for (cl_cell, cl_key), (_cl_slot, cl_cold) in zip(
                    upd_ck, logical_placements):
                if not cl_cold:
                    continue
                kind = "raw" if cl_key in raw_keys else "buf"
                c = int(cl_cell)
                frame_cold.append((c, cl_key, kind))
                coldlife["total"][kind] += 1
                if i + 1 >= n:
                    coldlife["tail"][kind] += 1
                    continue
                seg1_same = int(frame_seg[i + 1]) == int(frame_seg[i])
                t1_same = (seg1_same
                           and Q_pidx[i + 1][c].tobytes() == plain_keys[c]
                           and int(Q_assign[i + 1][c]) == int(assign[c]))
                if t1_same:
                    coldlife["survive_target"][kind] += 1
                elif i + 2 >= n or (
                        int(frame_seg[i + 2]) == int(frame_seg[i + 1])
                        and Q_pidx[i + 2][c].tobytes()
                        == Q_pidx[i + 1][c].tobytes()
                        and int(Q_assign[i + 2][c])
                        == int(Q_assign[i + 1][c])):
                    coldlife["die_settle"][kind] += 1
                else:
                    coldlife["die_motion"][kind] += 1

        # Raw prefetch runs only after visible updates and upgrades have been
        # decided. Frame 0 installs the frozen boot plan into free slots;
        # later frames may optionally spend spare BODY/cold room on the next
        # frame's predicted excess.
        frame_prefetch_requests = []
        prefetch_cold_slots = []
        if i == 0 and boot_prefetch_plan:
            for key, deadline in boot_prefetch_plan:
                result = alloc.prefetch(key, i, deadline)
                if result is None or not result[1]:
                    raise AssertionError(
                        "frame 0 boot prefetch did not use a free VRAM slot")
                logical_slot, _cold = result
                frame_prefetch_requests.append(
                    (key, deadline, logical_slot))
                cold_spent += 1
                spent_tiles += PATTERN_BYTES
                prefetch_cold_slots.append(
                    int(physical_by_logical[logical_slot]))
            if cold_spent > VRAM_TILES:
                raise AssertionError(
                    f"frame 0 exact+prefetch patterns {cold_spent} exceed "
                    f"the {VRAM_TILES}-slot resident pool")
            if len(frame0_keys) + boot_inline_requests > frame0_inline_pattern_limit:
                raise AssertionError(
                    "frame 0 inline patterns exceed the boot staging path")
        elif RAW_PREFETCH_ON and i > 0:
            prefetch_spend_limit = min(
                decision_budget,
                max(
                    current_reserved_spend(),
                    quality_budget + frame_cd
                    - RAW_PREFETCH_BUDGET_FLOOR_PATTERNS * PATTERN_BYTES,
                ),
            )
            last_deadline = min(n, i + RAW_PREFETCH_LOOKAHEAD + 1)
            request_room = sum(
                len(prefetch_forecast.requests[deadline])
                for deadline in range(i + 1, last_deadline))
            cold_room = (
                frame_max_cold - cold_spent
                if cold_limit_active
                else RAW_PREFETCH_MAX_REQUESTS_PER_FRAME)
            prg_room = frame_max_prg - prg_spent
            body_room = max(
                0,
                (prefetch_spend_limit - current_reserved_spend())
                // (PATTERN_BYTES + stream_schedule.RUN_DESCRIPTOR_BYTES),
            )
            capacity = min(
                RAW_PREFETCH_MAX_REQUESTS_PER_FRAME,
                request_room,
                cold_room,
                prg_room,
                body_room,
            )
            if capacity >= RAW_PREFETCH_MIN_BATCH:
                for deadline in range(i + 1, last_deadline):
                    deadline_keys = {
                        Q_pidx[deadline][cell].tobytes()
                        for cell in range(C_CELLS)
                    }
                    for key in prefetch_forecast.requests[deadline]:
                        if len(frame_prefetch_requests) >= capacity:
                            break
                        if alloc.is_pinned(key, deadline):
                            continue
                        resident = alloc.is_resident(key)
                        if resident:
                            continue
                        result = alloc.prefetch(
                            key, i, deadline, avoid_keys=deadline_keys)
                        if result is None:
                            continue
                        logical_slot, cold = result
                        frame_prefetch_requests.append(
                            (key, deadline, logical_slot))
                        if cold:
                            cold_spent += 1
                            commit_prg_source(
                                pattern_supply.SOURCE_PRG)
                            spent_tiles += PATTERN_BYTES
                            prefetch_cold_slots.append(
                                int(physical_by_logical[logical_slot]))
                    if len(frame_prefetch_requests) >= capacity:
                        break

        # Visible cold payload is already in physical-slot order. Prefetch is
        # appended after it, so sort that suffix independently to avoid turning
        # one contiguous speculative batch into request-order one-tile runs.
        prefetch_cold_slots.sort()
        dma_slots = [
            int(placements[index][0]) for index in transfer_order
        ] + prefetch_cold_slots
        dma_sources = [
            frame_sources[index] for index in transfer_order
        ]
        dma_sources.extend(
            [pattern_supply.SOURCE_PRG] * len(prefetch_cold_slots))
        dma_dic_indices = []
        for index in transfer_order:
            source = frame_sources[index]
            key = upd_ck[index][1]
            dma_dic_indices.append(
                dic_dictionary_index[key]
                if source == pattern_supply.SOURCE_DIC else -1)
        dma_dic_indices.extend([-1] * len(prefetch_cold_slots))
        dma_tiles = len(dma_slots)                 # 実際にVRAMへ送る32Bパターンタイル数
        if not L3_TILES and dma_tiles != cold_spent:
            raise AssertionError(
                f"frame {i}: encoder cold={cold_spent} allocator cold={dma_tiles}")
        # MainのHUD Nと同じsource-aware physical run数。p45では1-2 tile runはCPU直書き、長runは
        # VBlank境界で複数DMAに割れるため、物理VDP DMA発行回数とは意図的に異なる。
        run_prefetch_count = (
            boot_inline_requests if i == 0 else len(prefetch_cold_slots))
        run_tile_count = len(transfer_order) + run_prefetch_count
        packed_runs = pattern_supply.count_source_runs(
            dma_slots[:run_tile_count], dma_sources[:run_tile_count],
            dma_dic_indices[:run_tile_count])
        if PACKED_COLD_RUN_EXECUTION:
            dma_runs = packed_runs
        else:
            legacy_order = [
                index for index, (_slot, cold) in enumerate(placements)
                if cold
            ]
            legacy_slots = [
                int(placements[index][0]) for index in legacy_order
            ]
            legacy_sources = [
                int(frame_sources[index]) for index in legacy_order
            ]
            legacy_dic_indices = [
                (dic_dictionary_index[upd_ck[index][1]]
                 if legacy_sources[position] == pattern_supply.SOURCE_DIC
                 else -1)
                for position, index in enumerate(legacy_order)
            ]
            dma_runs = pattern_supply.count_source_runs(
                legacy_slots, legacy_sources, legacy_dic_indices)
            if dma_runs != packed_runs:
                raise AssertionError(
                    f"frame {i}: legacy entry-order runs={dma_runs} differ "
                    f"from packed suffix runs={packed_runs}; lower-rate "
                    "plain-Prg streams must retain the contiguous identity map")
        if dma_runs > cold_spent:
            raise AssertionError(
                f"frame {i}: source-aware runs={dma_runs} exceed "
                f"cold tiles={cold_spent}")
        transfer_tiles_log.append(dma_tiles)
        transfer_runs_log.append(dma_runs)
        supply_sources_log.append(np.asarray(frame_sources, np.uint8))
        prefetch_requests_log.append(frame_prefetch_requests)
        prefetch_cold_log.append(len(prefetch_cold_slots))
        prg_used = sum(
            source == pattern_supply.SOURCE_PRG for source in dma_sources)
        if i > 0 and prg_used != prg_spent:
            raise AssertionError(
                f"frame {i}: selected Prg={prg_spent} differs from "
                f"physical Prg loads={prg_used}")
        prg_loads_log.append(prg_used)
        frame_prg_cells = [
            int(upd_ck[index][0])
            for index in transfer_order
            if frame_sources[index] == pattern_supply.SOURCE_PRG
        ]
        frame_prg_cells.extend([-1] * len(prefetch_cold_slots))
        if len(frame_prg_cells) != prg_used:
            raise AssertionError(
                f"frame {i}: Prg cell trace {len(frame_prg_cells)} != "
                f"physical loads {prg_used}")
        prg_cold_cells_log.append(frame_prg_cells)
        wr0_loads_log.append(
            wr_used if i % 2 == 0 else 0)
        wr1_loads_log.append(
            wr_used if i % 2 == 1 else 0)
        dic_loads_log.append(dic_used)
        ensure_capacity(i)
        if _loop_profile:
            _lp_t = _lp_mark("allocate_route", _lp_t, _lp_frame)

        # Exact variable BODY work is now known: every update contributes its
        # two-byte entry, every source-aware run contributes four control bytes,
        # and only Prg-sourced cold patterns consume BODY payload.  Charge this
        # after allocation so source splits and slot fragmentation are exact.
        variable_body_spent = (
            name_recs * NAME_BYTES
            + dma_runs * stream_schedule.RUN_DESCRIPTOR_BYTES
            + prg_used * PATTERN_BYTES)
        reserved_body_spent = current_reserved_spend()
        if variable_body_spent > reserved_body_spent:
            raise AssertionError(
                f"frame {i}: exact BODY variable work {variable_body_spent}B "
                f"exceeds incremental run reservation {reserved_body_spent}B")
        if QUALITY_BUDGET_ON and i > 0:
            decision_spent = name_recs * NAME_BYTES + prg_used * PATTERN_BYTES
            if spent_tiles != decision_spent:
                raise AssertionError(
                    f"frame {i}: encoder decision spend {spent_tiles}B != "
                    f"BODY update/payload spend {decision_spent}B")
            available = quality_budget + frame_cd
            if variable_body_spent > available:
                raise SystemExit(
                    f"frame {i}: exact BODY variable work {variable_body_spent}B "
                    f"exceeds funded bytes {available}B after fixed control")
            quality_budget = min(
                QUALITY_BUDGET_BYTES,
                available - variable_body_spent)
        elif QUALITY_BUDGET_ON:
            quality_budget = QUALITY_BUDGET_BYTES
        if QUALITY_BUDGET_ON:
            quality_budget_log.append(quality_budget // PATTERN_BYTES)
        if _loop_profile:
            _lp_t = _lp_mark("budget_finalize", _lp_t, _lp_frame)

        # CRAMエミュ: このフレームの全更新を反映した最終表示を、現区間パレットで引き直す。
        # プレビュー/カテゴリマップ/miss繰越は全てこの実表示色(=実機と同じ)で描く。
        cur_rgb[:] = render_cells(disp_idx, disp_pal, cur_pals)

        # 実機決定ログ: このフレームで実際に書き換えたセルの (cell, パレット, 表示パターンkey)。
        # keyは64バイト(idx 1..15)を内包=pack_streamがそこから32Bパターンを復元できる。
        # Flbkはcur_key=近似先(常駐), Buf/Rawはcur_key=新規ロードkey。dedup/Near/Missの区別は
        # 「更新したか否か」に畳まれる(更新セルのみ列挙)ので、実機はmp4を完全再現できる。
        if EMIT_DEC:
            dec_frames.append([(int(c), int(cur_pal[c]), cur_key[c]) for c in np.where(updated)[0]])

        bytes_spent = (
            0 if i == 0 else
            int(body_fixed_control_bytes[i]) + variable_body_spent)
        frame_bytes_log.append(bytes_spent)
        tile_records_log.append(tile_recs)
        name_records_log.append(name_recs)
        dedup_saved_log.append(dedup_saved)
        l3_hits_log.append(l3_hits)
        prg_hits_log.append(prg_hits)

        # coldlife計測: 前フレームのcoldロードが今フレームの実表示に残ったか
        # (Near-keepや他セルでの再利用も「生存」と数える表示ベースの実測)。
        if coldlife_pending:
            disp_now = {k for k in cur_key if k is not None}
            for cl_cell, cl_key, kind in coldlife_pending:
                coldlife["disp_seen1"][kind] += 1
                if cl_key in disp_now:
                    coldlife["disp_alive1"][kind] += 1
                if cur_key[cl_cell] == cl_key:
                    coldlife["disp_cell1"][kind] += 1
        coldlife_pending = frame_cold

        # --- per-frame 実測(status line用) ---
        near_eff = near_mask if MIDFAR_ON else near   # MIDFARは統合探索が埋めたnear_mask
        stale = changed & ~updated & ~near_eff    # Nearは取りこぼしではない(意図的スキップ)
        near_disp = near_eff & ~updated           # 実際に省略したNear(余裕があればRaw済み=除く)
        # 優先度レイヤー/格上げ用に各セルの現在の劣化度を更新(触れたセルのみ。未変化セルは前値を保持)
        if MIDFAR_ON:
            cell_tier[dedup_mask | raw_mask | prg_mask] = 9              # 正確(Same/Raw/Buf)
            cell_tier[near_eff] = 2                                      # Near(近い近似=格上げ候補)
            cell_tier[flbk_mask] = 1
            cell_tier[stale] = 0                                          # Miss(取りこぼし)
            approx_carry = np.where(cell_tier < 9, approx_carry + 1, 0)  # 近似のまま持ち越した連続コマ数
            upgrade_log.append((upgraded, int((cell_tier < 9).sum())))   # 指標: 格上げ枚数 / まだ近似のセル数
        # カテゴリ別ユニークタイル数(何枚の別タイルを使い回したか)。同一キーは1枚と数える。
        no_update = ~changed

        def _uk(mask):
            return {cur_key[c] for c in np.where(mask)[0] if cur_key[c] is not None}
        u_same = _uk(no_update | dedup_mask); u_near = _uk(near_eff)
        u_flbk = _uk(flbk_mask)
        guniq["same"] |= u_same; guniq["near"] |= u_near
        guniq["flbk"] |= u_flbk
        # 飢餓は「内側タイルのMissがある時」だけ。外周2タイルのMissは許容(数えない)。
        if (stale & ~border_bool).any():
            starved_frames += 1
        stale_rows.append(np.packbits(stale))
        want = int(changed.sum())
        upd = int(updated.sum())
        miss = int(stale.sum())
        raw_count = int(raw_display_mask.sum())
        source_count = sum(int(mask.sum()) for mask in (
            prg_source_mask, wr0_source_mask, wr1_source_mask,
            dic_source_mask))
        near_count = int(near_eff.sum())
        flbk_count = int(flbk_mask.sum())
        same_count = (
            C_CELLS - raw_count - source_count - near_count
            - flbk_count - miss)
        if same_count < 0:
            raise AssertionError(
                f"frame {i}: display categories exceed {C_CELLS} cells")
        if i == 0:
            if not bool(updated.all()):
                raise AssertionError("frame 0 did not update every display cell")
            if (source_count or near_count or flbk_count or miss):
                raise AssertionError(
                    "frame 0 contains a non-Raw/Same display category")
            if raw_count != len(frame0_keys):
                raise AssertionError(
                    f"frame 0 Raw={raw_count} but has "
                    f"{len(frame0_keys)} exact unique patterns")
            if raw_count + same_count != C_CELLS:
                raise AssertionError("frame 0 Raw/Same coverage is incomplete")
            if (not np.array_equal(disp_idx, plain_idx)
                    or not np.array_equal(disp_pal, assign)):
                raise AssertionError("frame 0 display is not the exact target")
        if EMIT_DEC:
            dec_miss.append(miss)
            # デバッグ欄用カテゴリ数: catmap と同一定義(Raw/Buf/Flbk/Near/Miss は互いに素、
            # 残り=Same(不変+Dedup畳み込み))。6種は必ず C_CELLS に合計する。
            dec_cats.append((
                raw_count, same_count, near_count, flbk_count,
                source_count, miss))
        # waitはTSVのMiss継続観測専用。優先度のage_pressとは独立。
        carry = int((stale & (wait >= 1)).sum())
        # 滞留 = 待たされた連続フレーム数(=wait)。今フレームも未更新なので+1
        age_max = int(wait[stale].max()) + 1 if stale.any() else 0
        # F = every cold tile forming its own runでもfresh supplyで払える
        # 最低保証Raw更新数。実runが連結すれば差分はquality budgetへ戻る。
        f_fixed = budget // (
            PATTERN_BYTES + NAME_BYTES
            + stream_schedule.RUN_DESCRIPTOR_BYTES)
        stat_rows.append((
            i, f_fixed, want, upd, miss, C_CELLS - want, dedup_saved, tile_recs, carry, age_max,
            want / C_CELLS, int(near_eff.sum()), flbk_count, prg_hits,
            int(prg_source_mask.sum()), int(wr0_source_mask.sum()),
            int(wr1_source_mask.sum()), int(dic_source_mask.sum()),
            same_count,
            len(u_same), len(u_near), len(u_flbk), dma_tiles, dma_runs,
            len(prefetch_cold_slots)))

        # TSV観測用のMiss待ちカウンタを更新。NearはMissではない。
        wait = np.where(changed & ~updated & ~near_eff, wait + 1, 0)   # Nearは滞留させない
        if _loop_profile:
            _lp_t = _lp_mark("accounting", _lp_t, _lp_frame)

        # レンダリング(計測専用モードでは省く)
        if not NO_PANELS:
            _r0 = time.perf_counter()
            _save_png(cells_to_image(cur_rgb), main_dir / f"{i:05d}.png")

            # Category map: Raw=thin dashed frame; Same=no frame;
            # Near/Flbk use thin frames.
            # Dic/Prg/Wr use thin colour-and-black dashed frames.
            # Miss becomes a red fill in the renderer.
            cat = cur_rgb.astype(np.float64)
            cat[stale] = 0
            analysis_style.apply_numpy_category_border(
                cat, raw_display_mask, "Raw")
            analysis_style.apply_numpy_category_border(cat, near_eff, "Near")
            analysis_style.apply_numpy_category_border(cat, flbk_mask, "Flbk")
            analysis_style.apply_numpy_category_border(
                cat, prg_source_mask, "Prg")
            analysis_style.apply_numpy_category_border(
                cat, wr0_source_mask, "Wr0")
            analysis_style.apply_numpy_category_border(
                cat, wr1_source_mask, "Wr1")
            analysis_style.apply_numpy_category_border(
                cat, dic_source_mask, "Dic")
            _save_png(cells_to_image(cat.clip(0, 255).astype(np.uint8)), catmap_dir / f"{i:05d}.png")

            _t_render += time.perf_counter() - _r0

        if _loop_profile:
            # Panel generation is intentionally outside the decision profile.
            _lp_t = time.perf_counter()

        if (i + 1) % 200 == 0 or i + 1 == n:
            print(f"  {i+1}/{n}", flush=True)
        if _loop_profile:
            _lp_mark("tail", _lp_t, _lp_frame)
            for _name in _lp_sections:
                _lp_totals[_name] += _lp_frame[_name]
                _lp_frames[_name].append(_lp_frame[_name])
            for _name in _lp_nested_totals:
                _lp_nested_frames[_name].append(_lp_nested_frame[_name])

    if _gc_was_enabled:
        _gc.enable()
    if _png_pool is not None:                      # 残りのPNG保存を全て完了させてから閉じる
        for _f in _png_futs:
            _f.result()
        _png_pool.shutdown()
    _loop_total = time.perf_counter() - _t
    _phases.append(("差分ループ:commit/探索", _loop_total - _t_render))
    _phases.append(("差分ループ:描画+PNG保存", _t_render))
    if _loop_profile:
        _profiled_total = sum(_lp_totals.values())
        print("loop profile (exclusive decision-loop sections):", flush=True)
        for _name in _lp_sections:
            _values = np.asarray(_lp_frames[_name], np.float64) * 1000.0
            print(
                f"  {_name:18s} {_lp_totals[_name]:8.3f}s "
                f"p50={np.percentile(_values, 50):7.3f}ms "
                f"p95={np.percentile(_values, 95):7.3f}ms "
                f"p99={np.percentile(_values, 99):7.3f}ms "
                f"max={_values.max():7.3f}ms",
                flush=True,
            )
        print(
            f"  profiled total      {_profiled_total:8.3f}s; "
            f"loop commit/search={_loop_total - _t_render:.3f}s",
            flush=True,
        )
        print("loop profile (nested resident search; included in decision_commit):", flush=True)
        for _name in _lp_nested_totals:
            _values = np.asarray(_lp_nested_frames[_name], np.float64) * 1000.0
            print(
                f"  {_name:18s} {_lp_nested_totals[_name]:8.3f}s "
                f"p95/frame={np.percentile(_values, 95):7.3f}ms "
                f"max/frame={_values.max():7.3f}ms",
                flush=True,
            )
        _calls = max(_lp_counts["resident_calls"], 1)
        print(
            "loop profile counts: "
            f"changed={_lp_counts['changed']} ordered={_lp_counts['ordered']} "
            f"resident_calls={_lp_counts['resident_calls']} "
            f"candidates={_lp_counts['resident_candidates']} "
            f"avg_candidates={_lp_counts['resident_candidates'] / _calls:.2f} "
            f"empty={_lp_counts['resident_empty']} "
            f"cache_hits={_lp_counts['resident_cache_hits']} "
            f"cache_builds={_lp_counts['resident_cache_builds']} "
            f"adjacent_calls={_lp_counts['resident_adjacent_calls']} "
            f"adjacent_candidates={_lp_counts['resident_adjacent_candidates']} "
            f"adjacent_empty={_lp_counts['resident_adjacent_empty']} "
            f"exact_blocked={_lp_counts['exact_blocked']} "
            f"flbk_no_candidate={_lp_counts['flbk_no_candidate']} "
            f"flbk_budget_blocked={_lp_counts['flbk_budget_blocked']} "
            f"flbk_not_improved={_lp_counts['flbk_not_improved']} "
            f"flbk_absolute_reject={_lp_counts['flbk_absolute_reject']} "
            f"flbk_accepted={_lp_counts['flbk_accepted']}",
            flush=True,
        )

    # The first pass chooses a physical map from its completed logical
    # decisions.  The second pass accounts for that map's real run cost while
    # making its quality decisions.  Only now is the trace stable enough to
    # choose the delivered map.  Recompute every run-dependent artifact from
    # these frozen decisions; cold/reuse membership and displayed pixels stay
    # unchanged.
    if _SLOT_LOCALITY_STAGE == "final":
        decision_key_frames = [
            [(int(cell), key) for cell, _palette, key in sorted(frame)]
            for frame in dec_frames
        ]
        replay = replay_logical_slots(
            decision_key_frames,
            C_CELLS,
            VRAM_TILES,
            prefetch_requests=prefetch_requests_log,
        )
        if replay.tearing:
            raise AssertionError(
                f"final slot-locality logical replay tore {replay.tearing} patterns")
        accounted_mapping = np.asarray(physical_by_logical, np.int64)
        run_groups = _source_run_groups(
            replay, supply_sources_log,
            boot_inline_requests=boot_inline_requests)
        accounted_locality = evaluate_slot_locality(
            _run_accounted_cold_slots(replay, boot_inline_requests),
            VRAM_TILES,
            accounted_mapping,
            cold_cap=MAX_COLD,
            run_groups_by_frame=run_groups,
        )
        accounted_proof = verify_display_equivalence(
            decision_key_frames,
            C_CELLS,
            VRAM_TILES,
            accounted_mapping,
            prefetch_requests=prefetch_requests_log,
        )
        if accounted_proof["cold"] != int(np.sum(transfer_tiles_log)):
            raise AssertionError(
                "accounted slot-locality proof changed the frozen cold total")
        if PACKED_COLD_RUN_EXECUTION:
            final_locality = optimize_slot_locality(
                _run_accounted_cold_slots(replay, boot_inline_requests),
                VRAM_TILES,
                cold_cap=MAX_COLD,
                iterations=SLOT_LOCALITY_FINAL_ITERATIONS,
                target_heavy_runs=SLOT_LOCALITY_HEAVY_RUN_TARGET,
                run_groups_by_frame=run_groups,
            )
        else:
            final_locality = evaluate_slot_locality(
                _run_accounted_cold_slots(replay, boot_inline_requests),
                VRAM_TILES,
                np.arange(VRAM_TILES, dtype=np.int64),
                cold_cap=MAX_COLD,
                run_groups_by_frame=run_groups,
            )
        final_mapping = np.asarray(
            final_locality.physical_by_logical, np.int64)
        proof = verify_display_equivalence(
            decision_key_frames,
            C_CELLS,
            VRAM_TILES,
            final_mapping,
            prefetch_requests=prefetch_requests_log,
        )
        if proof["cold"] != int(np.sum(transfer_tiles_log)):
            raise AssertionError(
                "final slot-locality proof changed the frozen cold total")

        final_runs = []
        for frame_index, (
                frame, logical, prefetch_slots, raw_sources) in enumerate(zip(
                    decision_key_frames,
                    replay.placements,
                    replay.prefetch_cold_slots,
                    supply_sources_log)):
            physical = remap_placements(logical, final_mapping)
            order = cold_transfer_order(physical)
            slots = [int(physical[index][0]) for index in order]
            sources = [int(raw_sources[index]) for index in order]
            dic_indices = [
                (dic_dictionary_index[frame[index][1]]
                 if sources[position] == pattern_supply.SOURCE_DIC else -1)
                for position, index in enumerate(order)
            ]
            run_prefetch_slots = (
                _inline_boot_prefetch_slots(
                    prefetch_slots, boot_inline_requests, final_mapping)
                if frame_index == 0 else prefetch_slots)
            mapped_prefetch_slots = sorted(
                int(final_mapping[slot]) for slot in run_prefetch_slots)
            slots.extend(mapped_prefetch_slots)
            sources.extend(
                [pattern_supply.SOURCE_PRG] * len(run_prefetch_slots))
            dic_indices.extend([-1] * len(run_prefetch_slots))
            expected_run_tiles = (
                int(transfer_tiles_log[frame_index]) - boot_sidecar_requests
                if frame_index == 0 else int(transfer_tiles_log[frame_index]))
            if len(slots) != expected_run_tiles:
                raise AssertionError(
                    f"frame {frame_index}: final slot-locality changed "
                    "inline cold count")
            final_runs.append(pattern_supply.count_source_runs(
                slots, sources, dic_indices))
            if not PACKED_COLD_RUN_EXECUTION:
                legacy_order = [
                    index for index, (_slot, cold) in enumerate(physical)
                    if cold
                ]
                legacy_slots = [
                    int(physical[index][0]) for index in legacy_order
                ]
                legacy_sources = [
                    int(raw_sources[index]) for index in legacy_order
                ]
                legacy_dic_indices = [
                    (dic_dictionary_index[frame[index][1]]
                     if legacy_sources[position]
                     == pattern_supply.SOURCE_DIC else -1)
                    for position, index in enumerate(legacy_order)
                ]
                legacy_runs = pattern_supply.count_source_runs(
                    legacy_slots, legacy_sources, legacy_dic_indices)
                if legacy_runs != final_runs[-1]:
                    raise AssertionError(
                        f"frame {frame_index}: final legacy entry-order "
                        f"runs={legacy_runs} differ from packed suffix "
                        f"runs={final_runs[-1]}")

        old_runs = np.asarray(transfer_runs_log, np.int64)
        final_runs_array = np.asarray(final_runs, np.int64)

        def replay_quality_budget(runs_by_frame):
            """Return the exact budget trace, floor, and first failure."""
            trace = []
            budget_after = QUALITY_BUDGET_BYTES if QUALITY_BUDGET_ON else 0
            minimum = budget_after
            failure = None
            for frame_index, runs in enumerate(runs_by_frame):
                if frame_index == 0:
                    budget_after = QUALITY_BUDGET_BYTES
                elif QUALITY_BUDGET_ON:
                    pal_swap = (
                        SEGPAL_ON
                        and int(frame_seg[frame_index])
                        != int(frame_seg[frame_index - 1])
                    )
                    frame_supply = max(
                        int(body_variable_supply_bytes[frame_index])
                        - (PAL_WRITE_BYTES if pal_swap else 0),
                        0,
                    )
                    variable_work = (
                        int(name_records_log[frame_index]) * NAME_BYTES
                        + int(prg_loads_log[frame_index]) * PATTERN_BYTES
                        + int(runs) * stream_schedule.RUN_DESCRIPTOR_BYTES
                    )
                    available = budget_after + frame_supply
                    if variable_work > available:
                        failure = (frame_index, variable_work, available)
                        break
                    budget_after = min(
                        QUALITY_BUDGET_BYTES, available - variable_work)
                    minimum = min(minimum, budget_after)
                if QUALITY_BUDGET_ON:
                    trace.append(budget_after // PATTERN_BYTES)
            return trace, minimum, failure

        final_quality_budget, minimum_budget, final_failure = (
            replay_quality_budget(final_runs_array))
        accounted_quality_budget, accounted_minimum, accounted_failure = (
            replay_quality_budget(old_runs))
        if accounted_failure is not None:
            frame_index, variable_work, available = accounted_failure
            raise SystemExit(
                "accounted slot-locality map became unfunded: "
                f"frame {frame_index} needs {variable_work}B with "
                f"{available}B available")

        final_risk = np.asarray(final_locality.risk_frames, bool)
        final_risk_max = int(
            final_runs_array[final_risk].max(initial=0))
        accounted_risk = np.asarray(accounted_locality.risk_frames, bool)
        accounted_risk_max = int(
            old_runs[accounted_risk].max(initial=0))
        final_ok = final_failure is None
        candidate_issues = []
        if final_failure is not None:
            frame_index, variable_work, available = final_failure
            candidate_issues.append(
                f"frame {frame_index} needs {variable_work}B with "
                f"{available}B available")
        candidate_issue = "; ".join(candidate_issues) or "none"

        if not final_ok:
            retry_allowed = os.environ.get(
                "CBRSIM_SLOT_LOCALITY_RETRY_ALLOWED", "0").strip().lower()
            retry_allowed = retry_allowed not in {
                "", "0", "false", "no", "off",
            }
            retry_path = os.environ.get(
                "CBRSIM_SLOT_LOCALITY_RETRY_MAP", "").strip()
            if (retry_allowed and retry_path
                    and final_risk_max < accounted_risk_max):
                retry_path = Path(retry_path)
                retry_path.parent.mkdir(parents=True, exist_ok=True)
                np.save(retry_path, np.asarray(final_mapping, np.uint16))
                print(
                    "slot locality requests another accounting pass: "
                    f"candidate {candidate_issue}; deadline-heavy max "
                    f"{accounted_risk_max}->{final_risk_max} runs",
                    flush=True,
                )
                raise SystemExit(SLOT_LOCALITY_RETRY_EXIT)
            print(
                "slot locality final candidate rejected: "
                f"{candidate_issue}; retaining the already-accounted "
                f"map (deadline-heavy max {accounted_risk_max} runs; "
                f"candidate {final_risk_max})",
                flush=True,
            )
            final_locality = accounted_locality
            final_mapping = accounted_mapping
            proof = accounted_proof
            final_runs_array = old_runs
            final_quality_budget = accounted_quality_budget
            minimum_budget = accounted_minimum

        for frame_index, runs in enumerate(final_runs_array):
            if frame_index == 0:
                frame_bytes_log[frame_index] = 0
            else:
                frame_bytes_log[frame_index] = (
                    int(body_fixed_control_bytes[frame_index])
                    + int(name_records_log[frame_index]) * NAME_BYTES
                    + int(prg_loads_log[frame_index]) * PATTERN_BYTES
                    + int(runs) * stream_schedule.RUN_DESCRIPTOR_BYTES
                )
            row = list(stat_rows[frame_index])
            row[23] = int(runs)
            stat_rows[frame_index] = tuple(row)

        final_risk = np.asarray(final_locality.risk_frames, bool)
        print(
            "slot locality final: frozen-decision map; "
            f"display={proof['frames']}/{n} exact tearing={proof['tearing']}; "
            f"source runs max {int(old_runs[1:].max(initial=0))}"
            f"->{int(final_runs_array[1:].max(initial=0))}; "
            f"deadline-heavy source-aware runs "
            f"{int(final_locality.baseline_runs[final_risk].max(initial=0))}"
            f"->{int(final_runs_array[final_risk].max(initial=0))}; "
            f"quality floor={minimum_budget}B",
            flush=True,
        )
        physical_by_logical = final_mapping
        predicted_baseline_runs = np.asarray(
            final_locality.baseline_runs, np.int64)
        predicted_local_runs = np.asarray(
            final_locality.optimized_runs, np.int64)
        predicted_risk = final_risk
        transfer_runs_log = final_runs_array.tolist()
        if QUALITY_BUDGET_ON:
            quality_budget_log = final_quality_budget

    # coldlife計測の集計: 先読み減点ヒューリスティックの効果上限。
    with open(os.path.join(OUT, "coldlife.json"), "w") as f:
        json.dump(coldlife, f, indent=1)

    def _cl_line(kind):
        tot = coldlife["total"][kind]
        if tot == 0:
            return f"{kind}: total=0"
        parts = []
        for name in ("survive_target", "die_settle", "die_motion", "tail"):
            v = coldlife[name][kind]
            parts.append(f"{name}={v}({100.0 * v / tot:.1f}%)")
        seen = coldlife["disp_seen1"][kind]
        if seen:
            a = coldlife["disp_alive1"][kind]
            cc = coldlife["disp_cell1"][kind]
            parts.append(
                f"disp_alive1={a}({100.0 * a / seen:.1f}%) "
                f"disp_cell1={cc}({100.0 * cc / seen:.1f}%)")
        return f"{kind}: total={tot} " + " ".join(parts)

    print(f"coldlife {_cl_line('raw')}", flush=True)
    print(f"coldlife {_cl_line('buf')}", flush=True)

    _t = time.perf_counter()

    fb_baseline = np.array(frame_bytes_log, np.float64)
    tr = np.array(tile_records_log, np.float64)       # encoder Raw funding class
    ded = np.array(dedup_saved_log, np.float64)        # L1/L2 VRAM常駐ヒット
    l3h = np.array(l3_hits_log, np.float64)            # L3(PRG-RAM)ヒット
    prh = np.array(prg_hits_log, np.float64)           # PRG先読みヒット
    stats = np.array(stat_rows, np.float64)
    prg_loads = np.asarray(prg_loads_log, np.int64)
    wr0_loads = np.asarray(wr0_loads_log, np.int64)
    wr1_loads = np.asarray(wr1_loads_log, np.int64)
    dic_loads = np.asarray(dic_loads_log, np.int64)

    # The encoder's whole-movie budget above is a quality-allocation model, not the
    # physical PRG-RAM PrgBuf. Re-run the packer's exact sector schedule
    # from the frozen update/run counts so the analysis curve shows hardware
    # occupancy, including prebuffering and final-sector padding.
    shadow_cells = [[int(item[0]) for item in frame] for frame in dec_frames]
    shadow_costs = tuple(
        shadow_updates.frame_cost(cells, C_CELLS) for cells in shadow_cells)
    legacy_lengths = stream_schedule.control_block_lengths(
        stats[:, 3].astype(np.int64),
        np.asarray(transfer_runs_log, np.int64),
        cells=C_CELLS,
        audio_frame_bytes=AUDIO_CONTROL_BYTES,
    )
    try:
        # Shadow-list selection runs the same exact physical schedule as the
        # final pack.  Keep it inside the delivery-feedback boundary: its
        # legacy baseline can be the first place a real payload deadline is
        # discovered, before ``physical_schedule`` below has been assigned.
        shadow_plan = stream_schedule.select_shadow_update_lists(
            shadow_cells,
            np.asarray(transfer_runs_log, np.int64),
            prg_loads,
            cells=C_CELLS,
            fps=FPS,
            ring_capacity_patterns=(
                PRG_DELIVERY_CAP_KB * 1024 // PATTERN_BYTES),
            prebuffer_capacity_patterns=(
                PRG_BUF_CAP_KB * 1024 // PATTERN_BYTES),
            frame_sectors=ttrc_routing.FRAME_SECTORS,
            audio_frame_bytes=AUDIO_CONTROL_BYTES,
            fill=av_config.PACK_FORWARD_FILL,
        ) if PATTERN_SUPPLY_ON else None
        shadow_list_flags = (
            np.asarray(shadow_plan["selected"], np.bool_)
            if shadow_plan is not None else np.zeros(len(dec_frames), np.bool_)
        )
        control_lengths = (
            np.asarray(shadow_plan["block_lengths"], np.int64)
            if shadow_plan is not None else legacy_lengths
        )
        exact_body_work = stream_schedule.body_funded_work_bytes(
            prg_loads,
            stats[:, 3].astype(np.int64),
            np.asarray(transfer_runs_log, np.int64),
            cells=C_CELLS,
            audio_frame_bytes=AUDIO_CONTROL_BYTES,
            update_lists=shadow_list_flags,
        )
        fb = fb_baseline + control_lengths - legacy_lengths
        if not np.array_equal(fb.astype(np.int64), exact_body_work):
            bad = int(np.flatnonzero(fb.astype(np.int64) != exact_body_work)[0])
            raise AssertionError(
                f"frame {bad}: encoder BODY accounting {int(fb[bad])}B != "
                f"exact useful demand {int(exact_body_work[bad])}B")
        physical_schedule = (
            shadow_plan["schedule"] if shadow_plan is not None
            else stream_schedule.schedule_payload_ring(
                np.asarray(prg_loads_log, np.int64),
                control_lengths,
                fps=FPS,
                ring_capacity_patterns=(
                    PRG_DELIVERY_CAP_KB * 1024 // PATTERN_BYTES),
                prebuffer_capacity_patterns=(
                    PRG_BUF_CAP_KB * 1024 // PATTERN_BYTES),
                frame_sectors=ttrc_routing.FRAME_SECTORS,
                fill=av_config.PACK_FORWARD_FILL,
            )
        )
    except stream_schedule.ScheduleError as exc:
        raise SystemExit(
            f"sim: physical PrgBuf schedule failed: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(
            f"sim: physical PrgBuf schedule failed: {exc}") from exc
    if not physical_schedule["feasible"]:
        raise SystemExit(
            "sim: physical PrgBuf schedule is infeasible "
            f"(over={physical_schedule['over']} under={physical_schedule['under']} "
            f"ready_min={physical_schedule['ready_min']} "
            f"ctrl_min={physical_schedule['ctrl_min']} "
            f"rate_lead_end={physical_schedule['rate_lead_end']})")
    prg_remaining = np.asarray(
        physical_schedule["ring_occupancy"], np.int64)
    quality_budget_remaining = np.asarray(quality_budget_log, np.int64)

    def preload_remaining(loads):
        total = int(loads.sum())
        return total - np.cumsum(loads, dtype=np.int64)

    wr0_remaining = preload_remaining(wr0_loads)
    wr1_remaining = preload_remaining(wr1_loads)
    # DicBuf is persistent: hits never consume dictionary entries. Keep the
    # schema field for readers while reporting a constant installed count.
    dic_remaining = np.full(
        len(dic_loads), supply_budget.dic_patterns, np.int64)
    body_payload_bytes = np.asarray(
        physical_schedule["body_useful_payload_bytes"], np.int64)
    body_control_bytes = np.asarray(
        physical_schedule["body_useful_control_bytes"], np.int64)
    body_pad_bytes = np.asarray(
        physical_schedule["body_pad_bytes"], np.int64)
    body_physical_bytes = np.asarray(
        physical_schedule["body_physical_bytes"], np.int64)
    body_useful_bytes = body_payload_bytes + body_control_bytes
    body_useful_bps = stream_schedule.average_body_delivery_rate_bps(
        body_useful_bytes, body_physical_bytes)

    report = "\n".join([
        f"resolution={W}x{H} cells/frame={C_CELLS} active_tiles={ACTIVE_TILES} fps={FPS}",
        f"body_gross_bytes_per_frame={body_gross_bytes[1:].mean():.1f} "
        f"(exact sectors {sorted(set(int(x // stream_schedule.SECTOR_BYTES) for x in body_gross_bytes[1:]))})",
        f"body_fixed_control_bytes_per_frame={body_fixed_control_bytes[1:].mean():.1f}",
        f"body_variable_supply_bytes_per_frame={body_variable_supply_bytes[1:].mean():.1f} "
        f"(updates + runs + Prg payload)",
        f"incremental_run_control_reservation="
        f"{stream_schedule.RUN_DESCRIPTOR_BYTES}B/cold "
        f"({MAX_RUN_CONTROL_BYTES}B cap); unused bytes refunded after exact run charge",
        "physical_delivery_feedback=disabled (schedule failure stops sim)",
        f"PrgBuf_geometry=normal {PRG_BUF_CAP_KB}KiB + "
        f"jitter {PRG_JITTER_HEADROOM_KB}KiB = "
        f"delivery {PRG_DELIVERY_CAP_KB}KiB; "
        f"physical ring {av_config.RING_SIZE_KB}KiB; "
        f"scheduled jitter peak "
        f"{int(physical_schedule['ring_jitter_peak']) * PATTERN_BYTES / 1024:.1f}KiB",
        f"avg_codec_work_bytes_per_frame={fb.mean():.1f}",
        f"VRAM_tiles={VRAM_TILES}  L3(PRG-RAM)_tiles={L3_TILES}",
        f"avg_PrgBuf_loads_per_frame={prg_loads.mean():.1f}",
        f"boot_vram_prefetch={int(BOOT_VRAM_PREFETCH_ON)} "
        f"boot_loads={int(prefetch_cold_log[0]) if prefetch_cold_log else 0} "
        f"runtime_raw_vram_prefetch={int(RAW_PREFETCH_ON)} "
        f"total_loads={int(np.sum(prefetch_cold_log))} "
        f"request_events={sum(len(items) for items in prefetch_requests_log)} "
        f"cache_evictions={alloc.prefetch_cache_evictions} "
        f"pin_evictions={alloc.prefetch_evictions}",
        f"boot_preload_patterns=Wr0:{int(wr0_loads.sum())} "
        f"Wr1:{int(wr1_loads.sum())} "
        f"Dic:{supply_budget.dic_patterns} hits:{int(dic_loads.sum())}",
        f"avg_L2_dedup_hit_per_frame={ded.mean():.1f} (VRAM常駐で0転送)",
        f"avg_L3_hit_per_frame={l3h.mean():.1f} (再登場をRAMから0CDで供給)",
        f"avg_noncurrent_budget_exact_loads={prh.mean():.1f}",
        f"total_PrgBuf_pattern_bytes={prg_loads.sum()*PATTERN_BYTES:.0f}",
        f"L3_saved_CD_bytes={l3h.sum()*PATTERN_BYTES:.0f} (L3が無ければCD再読みしていた分)",
        f"dedup_saved_ratio={ded.sum()/(tr.sum()+prh.sum()+ded.sum()+l3h.sum()+1e-9):.3f}",
        f"quality_budget={QUALITY_BUDGET_ON} "
        f"cap={QUALITY_BUDGET_BYTES//PATTERN_BYTES}patterns"
        + (f" budget: start={quality_budget_remaining[0]} "
           f"end={quality_budget_remaining[-1]} "
           f"min={quality_budget_remaining.min()}"
           if QUALITY_BUDGET_ON else ""),
        f"PrgBuf: start={prg_remaining[0]} end={prg_remaining[-1]} "
        f"min={prg_remaining.min()} peak={prg_remaining.max()}patterns "
        f"(normal={PRG_BUF_CAP_KB * 1024 // PATTERN_BYTES}, "
        f"delivery={PRG_DELIVERY_CAP_KB * 1024 // PATTERN_BYTES})",
        f"starved_frames={starved_frames} ({starved_frames/n*100:.1f}%)",
        f"codec_work_bps={fb.mean()*FPS:.0f} (quality-allocation diagnostic)",
        f"body_useful_bps={body_useful_bps:.0f} "
        f"(useful BODY / physical CD read time; HEADER/frame0/pad excluded; "
        f"CD1x={CD_RATE})",
        (f"shadow_update_lists={int(shadow_list_flags.sum())}/{len(shadow_list_flags)} "
         f"main_saved_avg="
         f"{sum(cost.saved_cycles for cost, chosen in zip(shadow_plan['costs'], shadow_list_flags) if chosen) / max(1, len(shadow_list_flags)):.1f}cycles/frame "
         f"control_delta={int((control_lengths - legacy_lengths).sum())}B "
         f"threshold="
         f"{'schedule' if shadow_plan['control_growth_enabled'] else 'no-control-growth'} "
         f"baseline/selected ring_min eval="
         f"{shadow_plan['baseline_schedule']['ring_min_evaluation']}/"
         f"{physical_schedule['ring_min_evaluation']} "
         f"(full {shadow_plan['baseline_schedule']['ring_min']}/"
         f"{physical_schedule['ring_min']}, "
         f"tail f{physical_schedule['evaluation_end_frame']}..) "
         f"ready_min={shadow_plan['baseline_schedule']['ready_min']}/{physical_schedule['ready_min']}"
         if shadow_plan is not None else "shadow_update_lists=0 (pattern supply disabled)"),
        (f"upgrade(格上げ): 余剰でRaw化 avg {np.mean([u for u, _ in upgrade_log]):.1f}/コマ, "
         f"まだ近似のセル avg {np.mean([a for _, a in upgrade_log]):.1f}; "
         f"upgrade reserve start/peak/end="
         f"{upgrade_reserve[0]//1024}/{upgrade_reserve.max()//1024}/"
         f"{upgrade_reserve[-1]//1024}KB; main risk="
         f"{main_reserve[0]//1024}/{main_reserve.max()//1024}/"
         f"{main_reserve[-1]//1024}KB"
         if upgrade_log else "upgrade: (off)"),
    ])
    (OUT / "report.txt").write_text(report)
    print(report)

    # status line 用の per-frame 実測を保存
    cols = ("frame ffix want updated miss delta dedup tx carry age want_frac near flbk buf"
            " prg wr0 wr1 dic same same_u near_u flbk_u dma_tiles dma_runs prefetch")
    budget_tiles = int(np.median(stats[:, 1]))   # ffix中央値 = 固定予算タイル数(fps依存)
    # 全編ユニーク(cattotals併記用): same/near/flbk の別タイル総数
    cat_uniq = np.array([
        len(guniq["same"]), len(guniq["near"]), len(guniq["flbk"]),
    ], np.int64)
    np.savez(OUT / "stats.npz", stats=stats, cols=cols, fps=FPS, cells=C_CELLS,
             active_tiles=ACTIVE_TILES, max_cold=MAX_COLD,
             raw_prefetch=np.asarray(prefetch_cold_log, np.int64),
             raw_prefetch_cap=np.int64(max(
                 1, RAW_PREFETCH_MAX_REQUESTS_PER_FRAME)),
             cd1x=CD_RATE,
             body_gross_bytes=body_gross_bytes,
             body_fixed_control_bytes=body_fixed_control_bytes,
             body_variable_supply_bytes=body_variable_supply_bytes,
             cat_uniq=cat_uniq,
             audio_label=AUDIO_LABEL, audio_frame_bytes=AUDIO_CONTROL_BYTES,
             audio_pcm_bytes=AUDIO_PCM_BYTES,
             audio_source_file=AUDIO_FILE,
             audio_playback_file=AUDIO_PLAYBACK_FILE,
             audio_playback_rate=AUDIO_PLAYBACK_RATE,
             budget_tiles=budget_tiles)
    np.save(OUT / "miss_masks.npy", np.array(stale_rows, np.uint8))   # (n,72) packbits
    if QUALITY_BUDGET_ON:
        # Schema 4 exposes every physical pattern source independently.  The
        # encoder's quality budget remains an offline diagnostic and must not
        # silently drive any of the four hardware meters.
        np.savez(
            OUT / "buffer_remaining.npz",
            schema_version=np.int64(6),
            remaining_kind=np.array("three_consumptive_plus_dicbuf"),
            # Compatibility aliases for offline readers predating schema 4.
            remaining=prg_remaining,
            total=PRG_DELIVERY_CAP_KB * 1024 // PATTERN_BYTES,
            prg_remaining=prg_remaining,
            wr0_remaining=wr0_remaining,
            wr1_remaining=wr1_remaining,
            dic_remaining=dic_remaining,
            prg_capacity=PRG_DELIVERY_CAP_KB * 1024 // PATTERN_BYTES,
            prg_normal_capacity=PRG_BUF_CAP_KB * 1024 // PATTERN_BYTES,
            prg_jitter_headroom=(
                PRG_JITTER_HEADROOM_KB * 1024 // PATTERN_BYTES),
            wr0_capacity=pattern_supply.WORD_BUF_PATTERNS,
            wr1_capacity=pattern_supply.WORD_BUF_PATTERNS,
            dic_capacity=pattern_supply.DIC_BUF_PATTERNS,
            prg_loads=prg_loads,
            wr0_loads=wr0_loads,
            wr1_loads=wr1_loads,
            dic_loads=dic_loads,
            wr0_preloaded=np.int64(wr0_loads.sum()),
            wr1_preloaded=np.int64(wr1_loads.sum()),
            dic_preloaded=np.int64(supply_budget.dic_patterns),
            quality_budget_remaining=quality_budget_remaining,
            exact_demand_bytes=demand_prediction.exact_bytes,
            protected_demand_bytes=demand_prediction.protected_bytes,
            preload_credit_bytes=preload_credit_bytes,
            upgrade_demand_bytes=upgrade_demand,
            upgrade_planned_demand_bytes=upgrade_demand,
            upgrade_unavoidable_shortfall_bytes=np.zeros(
                len(upgrade_demand), np.int64),
            upgrade_reserve_bytes=upgrade_reserve,
            main_risk_demand_bytes=main_demand,
            main_risk_planned_demand_bytes=(
                main_reserve_plan.planned_demand),
            main_risk_unavoidable_shortfall_bytes=(
                main_reserve_plan.shortfall),
            main_risk_reserve_bytes=main_reserve,
            block_lengths=control_lengths,
            shadow_update_lists=shadow_list_flags,
            payload_sectors=np.asarray(
                physical_schedule["n_pay_sec"], np.int64),
            control_sectors=np.asarray(
                physical_schedule["n_ctrl_sec"], np.int64),
            body_useful_payload_bytes=body_payload_bytes,
            body_useful_control_bytes=body_control_bytes,
            body_pad_bytes=body_pad_bytes,
            body_physical_bytes=body_physical_bytes,
            body_gross_bytes=body_gross_bytes,
            body_fixed_control_bytes=body_fixed_control_bytes,
            body_variable_supply_bytes=body_variable_supply_bytes,
        )
    print(f"wrote {main_dir}, {catmap_dir}; stats.npz + miss_masks.npy saved")

    # 実機TTRCエンコード用の決定ログ(既定off)。品質決定(区間パレット/ディザ/Near/Flbk/画質予算/fill)は
    # すべてこのログに畳み込まれる=pack_streamは再生するだけでmp4と同じ画を出せる(唯一の真実源)。
    if EMIT_DEC:
        import pickle
        frozen_config = {
            "schema_version": 1,
            "profile": profile_identity(CONFIG_PROFILE),
            "source": {
                "path": str(SRC), "fps": str(FPS_STR), "duration": str(DURATION),
                "sar": SOURCE_SAR_OVERRIDE,
                "preprocess": dict(
                    CONFIG_PROFILE.section("source").get("preprocess", {})
                    if CONFIG_PROFILE else {}),
            },
            "video": {
                "mode": MODE.upper(), "width": int(W), "height": int(H),
                "cols": int(TCOLS), "rows": int(TROWS), "cells": int(C_CELLS),
                "active_tiles": int(ACTIVE_TILES),
                "tile": int(TILE), "fit": GEOMETRY_FIT,
                "resize_filter": RESIZE_FILTER,
                "master_denoise": bool(MASTER_DENOISE),
            },
            "timing": {
                "content_fps": str(FPS_STR), "fps": float(FPS),
                "vsync_n": int(VSYNC_N), "playback_fps": float(PLAYBACK_FPS),
            },
            "audio": {
                "rate": int(AUDIO_RATE),
                "frame_bytes": int(AUDIO_CONTROL_BYTES),
                "control_bytes": int(AUDIO_CONTROL_BYTES),
                "pcm_bytes": int(AUDIO_PCM_BYTES),
                "checkpoint_bytes": int(av_config.IMA_CHECKPOINT_BYTES),
                "file": AUDIO_FILE,
                "playback_file": AUDIO_PLAYBACK_FILE,
                "playback_rate": int(AUDIO_PLAYBACK_RATE),
            },
            "stream": {
                "cd_rate_bps": int(CD_RATE),
                "body_gross_bytes": body_gross_bytes,
                "body_fixed_control_bytes": body_fixed_control_bytes,
                "body_variable_supply_bytes": body_variable_supply_bytes,
            },
            "hardware": {
                "vram_tiles": int(VRAM_TILES),
                "prg_buf_kb": int(PRG_BUF_CAP_KB),
                "prg_delivery_cap_kb": int(PRG_DELIVERY_CAP_KB),
                "prg_jitter_headroom_kb": int(PRG_JITTER_HEADROOM_KB),
                "prg_physical_ring_kb": int(av_config.RING_SIZE_KB),
                "quality_budget_kb": int(QUALITY_BUDGET_KB),
                "max_cold": int(MAX_COLD),
                "baseline_cold_cap": int(
                    COLD_CAP_QUALIFICATION.baseline_cap
                    if COLD_CAP_QUALIFICATION.baseline_cap is not None
                    else MAX_COLD),
                "cold_cap_source": COLD_CAP_QUALIFICATION.source,
            },
            "encoder": {
                "detail_alpha": float(DETAIL_ALPHA),
                "aging_alpha": float(AGING_ALPHA),
                "aging_dist_ref": float(AGING_DIST_REF),
                "aging_step_cap": float(AGING_STEP_CAP),
                "aging_press_cap": float(WAIT_CAP),
                "ghost_escalate_sec": float(GHOST_ESCALATE_SEC),
                "ghost_escalate_frames": int(GHOST_ESCALATE_N),
                "boot_vram_prefetch": bool(BOOT_VRAM_PREFETCH_ON),
                "raw_prefetch": bool(RAW_PREFETCH_ON),
                "raw_prefetch_lookahead": int(RAW_PREFETCH_LOOKAHEAD),
                "raw_prefetch_max_requests_per_frame": int(
                    RAW_PREFETCH_MAX_REQUESTS_PER_FRAME),
                "raw_prefetch_min_batch": int(RAW_PREFETCH_MIN_BATCH),
                "raw_prefetch_budget_floor_patterns": int(
                    RAW_PREFETCH_BUDGET_FLOOR_PATTERNS),
            },
            "palette": {
                "algorithm": PAL_ALGO, "seam_weight": float(PAL_SEAM_WEIGHT),
                "seam_iterations": int(PAL_SEAM_ITERATIONS),
            },
        }
        pickle.dump({
            "config": frozen_config,
            "geom": (int(TCOLS), int(TROWS), int(C_CELLS), int(TILE)),
            "mode": MODE.upper(),                              # header display mode
            "fps_str": str(FPS_STR),
            "pal_algo": PAL_ALGO,
            "pal_stats": palette_stats,
            "seg_pals": [np.asarray(p, np.uint8) for p in seg_pals],  # list of (4,15,3)
            "frame_seg": np.asarray(frame_seg, np.int32),
            "frames": dec_frames,                                     # [[(cell,pal,key),...], ...]
            "slot_locality": {
                "schema_version": 2,
                "trace": (
                    "final_decisions"
                    if _SLOT_LOCALITY_STAGE == "final"
                    else "predictive_exact_target"
                ),
                "physical_by_logical": np.asarray(
                    physical_by_logical, np.uint16),
                "baseline_runs": np.asarray(
                    predicted_baseline_runs, np.uint16),
                "optimized_runs": np.asarray(
                    predicted_local_runs, np.uint16),
                "risk_frames": np.asarray(
                    predicted_risk, np.bool_),
                "player_execution": (
                    "packed_suffix"
                    if PACKED_COLD_RUN_EXECUTION else "legacy_entry_order"
                ),
            },
            "pattern_supply": {
                "schema_version": 2,
                "enabled": bool(PATTERN_SUPPLY_ON),
                "sources": supply_sources_log,
                "planned_wr": np.asarray(supply_budget.wr, np.uint16),
                "planned_dic": np.asarray(supply_budget.dic, np.uint16),
                "dic_dictionary": list(
                    supply_budget.dic_dictionary_packed),
                "prg_loads": prg_loads.astype(np.uint16),
                "wr0_loads": wr0_loads.astype(np.uint16),
                "wr1_loads": wr1_loads.astype(np.uint16),
                "dic_loads": dic_loads.astype(np.uint16),
                "capacities": {
                    "wr0": pattern_supply.WORD_BUF_PATTERNS,
                    "wr1": pattern_supply.WORD_BUF_PATTERNS,
                    "dic": pattern_supply.DIC_BUF_PATTERNS,
                },
            },
            "raw_prefetch": {
                "schema_version": 3,
                "enabled": bool(BOOT_VRAM_PREFETCH_ON or RAW_PREFETCH_ON),
                "boot_enabled": bool(BOOT_VRAM_PREFETCH_ON),
                "runtime_enabled": bool(RAW_PREFETCH_ON),
                "boot_capacity": int(boot_prefetch_capacity),
                "boot_requests": int(len(boot_prefetch_plan)),
                "boot_inline_capacity": int(boot_inline_capacity),
                "boot_sidecar_capacity": int(boot_sidecar_capacity),
                "boot_inline_requests": int(boot_inline_requests),
                "boot_sidecar_requests": int(boot_sidecar_requests),
                "lookahead": int(RAW_PREFETCH_LOOKAHEAD),
                "max_requests_per_frame": int(
                    RAW_PREFETCH_MAX_REQUESTS_PER_FRAME),
                "min_batch": int(RAW_PREFETCH_MIN_BATCH),
                "budget_floor_patterns": int(
                    RAW_PREFETCH_BUDGET_FLOOR_PATTERNS),
                "requests": prefetch_requests_log,
                "cold": np.asarray(prefetch_cold_log, np.uint16),
                "forecast_protected_cold": np.asarray(
                    prefetch_forecast.protected_cold, np.uint16),
                "forecast_requested": np.asarray(
                    prefetch_forecast.requested_patterns, np.uint16),
            },
            # simが決めた値をpackで全frame再計算し、descriptor/HUD Nとのズレを即時検出する。
            "pattern_transfers": {
                "schema_version": 2,
                "tiles": np.asarray(transfer_tiles_log, np.uint16),
                "runs": np.asarray(transfer_runs_log, np.uint16),
                "prg": prg_loads.astype(np.uint16),
                "wr0": wr0_loads.astype(np.uint16),
                "wr1": wr1_loads.astype(np.uint16),
                "dic": dic_loads.astype(np.uint16),
            },
            # Analysis and pack must show the same physical PrgBuf trace.
            # The packer compares this frozen trace with its built control data.
            "stream_schedule": {
                "schema_version": stream_schedule.STREAM_SCHEDULE_SCHEMA_VERSION,
                "block_lengths": control_lengths,
                "ring_occupancy": prg_remaining,
                "payload_sectors": np.asarray(
                    physical_schedule["n_pay_sec"], np.int64),
                "evaluation_end_frame": int(
                    physical_schedule["evaluation_end_frame"]),
                "ring_min_evaluation": int(
                    physical_schedule["ring_min_evaluation"]),
                "ring_min_full": int(physical_schedule["ring_min"]),
                "control_sectors": np.asarray(
                    physical_schedule["n_ctrl_sec"], np.int64),
                "body_useful_payload_bytes": body_payload_bytes,
                "body_useful_control_bytes": body_control_bytes,
                "body_pad_bytes": body_pad_bytes,
                "body_physical_bytes": body_physical_bytes,
            },
            "shadow_updates": {
                "schema_version": 1,
                "selected": shadow_list_flags,
                "legacy_cycles": np.asarray(
                    [cost.legacy_cycles for cost in shadow_costs], np.int64),
                "list_cycles": np.asarray(
                    [cost.list_cycles for cost in shadow_costs], np.int64),
                "added_bytes": np.asarray(
                    [cost.added_bytes for cost in shadow_costs], np.int64),
                "cutoff_numerator": (
                    int(shadow_plan["cutoff_numerator"]) if shadow_plan is not None else 0),
                "cutoff_denominator": (
                    int(shadow_plan["cutoff_denominator"]) if shadow_plan is not None else 1),
                "baseline_ring_min": (
                    int(shadow_plan["baseline_schedule"]["ring_min"])
                    if shadow_plan is not None else int(physical_schedule["ring_min"])),
                "baseline_ring_min_evaluation": (
                    int(shadow_plan["baseline_schedule"]["ring_min_evaluation"])
                    if shadow_plan is not None
                    else int(physical_schedule["ring_min_evaluation"])),
                "baseline_ready_min": (
                    int(shadow_plan["baseline_schedule"]["ready_min"])
                    if shadow_plan is not None else int(physical_schedule["ready_min"])),
                "selected_ring_min": int(physical_schedule["ring_min"]),
                "selected_ring_min_evaluation": int(
                    physical_schedule["ring_min_evaluation"]),
                "evaluation_end_frame": int(
                    physical_schedule["evaluation_end_frame"]),
                "selected_ready_min": int(physical_schedule["ready_min"]),
                "control_growth_enabled": (
                    bool(shadow_plan["control_growth_enabled"])
                    if shadow_plan is not None else False),
            },
            "miss": dec_miss,                                         # per-frame Miss数(overlay用)
            "cats": dec_cats,                                         # per-frame [raw,same,near,flbk,buf,miss]
            "body_gross_bytes": body_gross_bytes,
            "body_fixed_control_bytes": body_fixed_control_bytes,
            "body_variable_supply_bytes": body_variable_supply_bytes,
            "audio_rate": int(AUDIO_RATE),
            "audio_frame_bytes": int(AUDIO_CONTROL_BYTES),
            "audio_pcm_bytes": int(AUDIO_PCM_BYTES), "fps": float(FPS),
            "vram_tiles": int(VRAM_TILES),
            # エンコード時の実効パラメータを焼き込む(pack/解析が同一値を使い二重管理を防ぐ)。
            "max_cold": int(MAX_COLD),
            "prg_buf_kb": int(PRG_BUF_CAP_KB),
            "prg_delivery_cap_kb": int(PRG_DELIVERY_CAP_KB),
            "prg_jitter_headroom_kb": int(PRG_JITTER_HEADROOM_KB),
            "prg_physical_ring_kb": int(av_config.RING_SIZE_KB),
            "quality_budget_kb": int(QUALITY_BUDGET_KB),
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


def _derive_completed_slot_map(decision_log, output_path):
    """Derive and prove the physical map used by the accounting pass."""
    import pickle
    from tile_alloc import (
        evaluate_slot_locality,
        optimize_slot_locality,
        replay_logical_slots,
        verify_display_equivalence,
    )

    with Path(decision_log).open("rb") as source:
        log = pickle.load(source)
    frames = [
        [(int(cell), key) for cell, _palette, key in sorted(frame)]
        for frame in log["frames"]
    ]
    prefetch_requests = (log.get("raw_prefetch") or {}).get("requests")
    replay = replay_logical_slots(
        frames,
        int(log["geom"][2]),
        int(log["vram_tiles"]),
        prefetch_requests=prefetch_requests,
    )
    if replay.tearing:
        raise AssertionError(
            f"seed slot-locality replay tore {replay.tearing} patterns")
    cold_trace = _run_accounted_cold_slots(
        replay,
        int((log.get("raw_prefetch") or {}).get(
            "boot_inline_requests", 0)))
    run_groups = _source_run_groups(
        replay, (log.get("pattern_supply") or {}).get("sources", ()),
        boot_inline_requests=int(
            (log.get("raw_prefetch") or {}).get(
                "boot_inline_requests", 0)))
    if PACKED_COLD_RUN_EXECUTION:
        plan = optimize_slot_locality(
            cold_trace,
            int(log["vram_tiles"]),
            cold_cap=int(log.get("max_cold", 0)),
            iterations=SLOT_LOCALITY_FINAL_ITERATIONS,
            target_heavy_runs=SLOT_LOCALITY_HEAVY_RUN_TARGET,
            run_groups_by_frame=run_groups,
        )
    else:
        plan = evaluate_slot_locality(
            cold_trace,
            int(log["vram_tiles"]),
            np.arange(int(log["vram_tiles"]), dtype=np.int64),
            cold_cap=int(log.get("max_cold", 0)),
            run_groups_by_frame=run_groups,
        )
    proof = verify_display_equivalence(
        frames,
        int(log["geom"][2]),
        int(log["vram_tiles"]),
        plan.physical_by_logical,
        prefetch_requests=prefetch_requests,
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(output_path, np.asarray(plan.physical_by_logical, np.uint16))
    risk = np.asarray(plan.risk_frames, bool)
    print(
        "slot locality seed proof: "
        f"execution="
        f"{'packed-suffix' if PACKED_COLD_RUN_EXECUTION else 'legacy-entry-order'}; "
        f"display={proof['frames']}/{len(frames)} exact "
        f"cold={proof['cold']} tearing={proof['tearing']}; "
        f"deadline-heavy source-aware runs "
        f"{int(plan.baseline_runs[risk].max(initial=0))}"
        f"->{int(plan.optimized_runs[risk].max(initial=0))}",
        flush=True,
    )


def _run_slot_locality_pipeline():
    """Use one logical seed and bounded slot-map accounting passes."""
    import subprocess

    command = [sys.executable, *_ORIGINAL_ARGV]
    stem = str(os.getpid())
    map_path = Path("tmp") / f"slot_locality_seed_{stem}.npy"
    retry_path = Path("tmp") / f"slot_locality_retry_{stem}.npy"
    workspace_lease = _activate_sim_tmpfs()
    if workspace_lease is not None and workspace_lease.reused:
        print(
            "sim artifact cache: complete matching encode reused; "
            "seed and accounting passes skipped",
            flush=True,
        )
        workspace_lease.release()
        return
    cache_lease = None
    completed = False
    try:
        cache_lease = tmpfs_workspace.create_run_directory(
            _tmpfs_key(), required_bytes=_pass_cache_required_bytes())
        cache_path = cache_lease.entry / "pass-cache.pkl"
        common_env = os.environ.copy()
        common_env["CBRSIM_TMPFS_PREPARED"] = "1"
        common_env["CBRSIM_PASS_CACHE"] = str(cache_path)
        common_env["CBRSIM_PASS_CACHE_INVOCATION"] = cache_lease.entry.name

        seed_env = common_env.copy()
        seed_env["CBRSIM_SLOT_LOCALITY_STAGE"] = "seed"
        seed_env["CBRSIM_NOPANELS"] = "1"
        seed_env.pop("CBRSIM_SLOT_LOCALITY_MAP", None)
        seed_env.pop("CBRSIM_SLOT_LOCALITY_REUSE", None)
        print("slot locality seed pass: logical decisions", flush=True)
        result = subprocess.run(command, env=seed_env, check=False)
        result.check_returncode()

        # Physical delivery is a proof, not an automatic cold-cap tuning loop.
        # If it fails, stop this run and report that layer unchanged.
        _derive_completed_slot_map(EMIT_DEC, map_path)
        accounting_pass = 1
        while accounting_pass <= SLOT_LOCALITY_MAX_ACCOUNTING_PASSES:
            retry_path.unlink(missing_ok=True)
            final_env = common_env.copy()
            final_env["CBRSIM_SLOT_LOCALITY_STAGE"] = "final"
            final_env["CBRSIM_SLOT_LOCALITY_MAP"] = str(map_path.resolve())
            final_env["CBRSIM_SLOT_LOCALITY_RETRY_MAP"] = str(
                retry_path.resolve())
            final_env["CBRSIM_SLOT_LOCALITY_RETRY_ALLOWED"] = (
                "1" if accounting_pass < SLOT_LOCALITY_MAX_ACCOUNTING_PASSES
                else "0")
            final_env["CBRSIM_SLOT_LOCALITY_REUSE"] = "1"
            print(
                "slot locality accounting pass "
                f"{accounting_pass}/{SLOT_LOCALITY_MAX_ACCOUNTING_PASSES}: "
                "pay frozen map, then validate completed decisions",
                flush=True,
            )
            result = subprocess.run(command, env=final_env, check=False)
            if result.returncode == 0:
                break
            if result.returncode != SLOT_LOCALITY_RETRY_EXIT:
                result.check_returncode()
            if not retry_path.is_file():
                raise SystemExit(
                    "slot-locality accounting requested a retry without a map")
            current = np.load(map_path)
            retry = np.load(retry_path)
            if np.array_equal(current, retry):
                raise SystemExit(
                    "slot-locality accounting cannot progress: retry map "
                    "equals the current map")
            retry_path.replace(map_path)
            accounting_pass += 1
        else:
            raise SystemExit(
                "slot-locality accounting did not converge within "
                f"{SLOT_LOCALITY_MAX_ACCOUNTING_PASSES} passes")
        completed = True
    finally:
        map_path.unlink(missing_ok=True)
        retry_path.unlink(missing_ok=True)
        if cache_lease is not None:
            tmpfs_workspace.remove_run_directory(cache_lease)
        if workspace_lease is not None:
            if completed:
                _mark_sim_tmpfs_complete(workspace_lease)
            workspace_lease.release()


_SIM_CACHE_IDENTITY = None


def _sim_cache_identity():
    global _SIM_CACHE_IDENTITY
    if _SIM_CACHE_IDENTITY is None:
        _SIM_CACHE_IDENTITY = sim_artifact_cache.build_identity(
            source=SRC,
            emit_decisions=bool(EMIT_DEC),
        )
    return _SIM_CACHE_IDENTITY


def _tmpfs_key():
    return sim_artifact_cache.readable_key(
        _sim_cache_identity(),
        mode=MODE,
        width=W,
        height=H,
        fps=FPS_STR,
        fit=GEOMETRY_FIT,
        cold_cap=MAX_COLD,
    )


def _estimated_frame_count():
    return max(1, int(math.ceil(float(DURATION) * FPS)) + 2)


def _sim_tmpfs_required_bytes():
    # PNG compression varies substantially by source. This estimate covers the
    # two extracted inputs, the three sim panels, and ordinary sidecars while
    # retaining a fixed headroom for palettes, decisions, and audio.
    pixels = _estimated_frame_count() * W * H
    return pixels * 10 + 1024 ** 3


def _pass_cache_required_bytes():
    # Quantized targets are compact, but palette details and future-planning
    # objects vary with the source. Reserve roughly four indexed frame copies.
    pixels = _estimated_frame_count() * W * H
    return pixels * 4 + 512 * 1024 ** 2


def _activate_sim_tmpfs():
    if os.environ.get("CBRSIM_TMPFS_PREPARED") == "1":
        return None
    videos = (Path(__file__).resolve().parents[1] / "videos").absolute()
    try:
        OUT.absolute().relative_to(videos)
    except ValueError:
        print(
            f"tmpfs artifacts: CBRSIM_OUT is outside videos/, keeping {OUT}",
            flush=True,
        )
        return None
    identity = _sim_cache_identity()
    token = sim_artifact_cache.identity_sha256(identity)
    force_reencode = os.environ.get(
        "CBRSIM_FORCE_REENCODE", "0").strip().lower() in {
            "1", "true", "yes", "on",
        }
    reuse_token = (
        None if force_reencode or not EMIT_DEC else token
    )
    lease = tmpfs_workspace.activate_directory(
        OUT,
        kind="sim",
        key=_tmpfs_key(),
        required_bytes=_sim_tmpfs_required_bytes(),
        reuse_token=reuse_token,
    )
    if lease.reused:
        try:
            marker = json.loads(
                (lease.entry / ".complete.json").read_text(encoding="utf-8"))
            result = sim_artifact_cache.validate_completed_data(
                lease.entry / "data", identity, marker=marker)
            _rebind_cached_profile(lease.entry / "data")
        except Exception as exc:
            print(
                f"sim artifact cache: rejected incomplete/corrupt entry: {exc}",
                flush=True,
            )
            lease.release()
            lease = tmpfs_workspace.activate_directory(
                OUT,
                kind="sim",
                key=_tmpfs_key(),
                required_bytes=_sim_tmpfs_required_bytes(),
            )
        else:
            print(
                f"sim artifact cache: hit {result['frames']} frames "
                f"identity={token[:12]}",
                flush=True,
            )
    print(f"tmpfs sim workspace: {OUT} -> {lease.entry / 'data'}", flush=True)
    return lease


def _rebind_cached_profile(data):
    """Authenticate output-neutral profile renames without re-encoding."""

    if CONFIG_PROFILE is None or not EMIT_DEC:
        return
    import pickle

    decision_path = Path(data) / "decisions.pkl"
    with decision_path.open("rb") as source:
        log = pickle.load(source)
    config = log.get("config")
    if not isinstance(config, dict):
        raise sim_artifact_cache.CacheValidationError(
            "decision log has no frozen config")
    config["profile"] = profile_identity(CONFIG_PROFILE)
    source_config = config.get("source")
    if isinstance(source_config, dict):
        source_config["path"] = str(SRC)
    hardware = config.get("hardware")
    if isinstance(hardware, dict):
        hardware["baseline_cold_cap"] = int(
            COLD_CAP_QUALIFICATION.baseline_cap
            if COLD_CAP_QUALIFICATION.baseline_cap is not None else MAX_COLD)
        hardware["cold_cap_source"] = COLD_CAP_QUALIFICATION.source
    temporary = decision_path.with_name(
        f".{decision_path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as output:
        pickle.dump(log, output, protocol=4)
    temporary.replace(decision_path)


def _mark_sim_tmpfs_complete(lease):
    if not EMIT_DEC:
        return
    identity = _sim_cache_identity()
    result = sim_artifact_cache.validate_completed_data(
        lease.entry / "data", identity)
    token = sim_artifact_cache.identity_sha256(identity)
    tmpfs_workspace.mark_directory_complete(
        lease,
        reuse_token=token,
        details={
            "schema_version": sim_artifact_cache.CACHE_SCHEMA_VERSION,
            "identity_sha256": token,
            "identity": identity,
            "frames": result["frames"],
        },
    )
    print(
        f"sim artifact cache: completed {result['frames']} frames "
        f"identity={token[:12]}",
        flush=True,
    )


if __name__ == "__main__":
    locality_disabled = os.environ.get(
        "CBRSIM_SLOT_LOCALITY_PASSES", "1").strip().lower() in {
            "", "0", "false", "no", "off",
        }
    if (_SLOT_LOCALITY_STAGE or locality_disabled or not EMIT_DEC
            or os.environ.get("CBRSIM_SLOT_LOCALITY_MAP", "").strip()):
        _standalone_lease = _activate_sim_tmpfs()
        _standalone_completed = False
        try:
            if _standalone_lease is None or not _standalone_lease.reused:
                main()
                _standalone_completed = True
            else:
                print(
                    "sim artifact cache: complete matching encode reused; "
                    "simulation skipped",
                    flush=True,
                )
        finally:
            if _standalone_lease is not None:
                if _standalone_completed:
                    _mark_sim_tmpfs_complete(_standalone_lease)
                _standalone_lease.release()
    else:
        _run_slot_locality_pipeline()
