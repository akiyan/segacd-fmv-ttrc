#!/usr/bin/env python3
"""解析フレーム(新レイアウト)で sim 出力を全編mp4化する本パイプライン。

新レイアウトの『正』は tools/layout_preview.py(ダミー値で秒プレビュー)。本スクリプトは
その描画関数を実データで回して1920x1080/全フレームを描き、ffmpegでmp4(音声付き)にする。
sim 側(sim.py)や旧 compose(make_base/render_statusline/compose_*.sh)は使わない。

入力(env):
  CBRSIM_OUT       sim出力ディレクトリ(preview/raw/catmap/stats.npz/miss_masks.npy/
                   buffer_remaining.npz/palettes.bin/audio WAV/report.txt)
  CBRSIM_SRCLABEL  右Sourceパネル見出し(既定 "Source")
  CBRSIM_MODE      画面モード H32/H40 (既定 H32。DMA理論値に使う)
  ANALYSIS_OUT     出力mp4パス (既定 videos/<stem>_analysis.mp4)
  ANALYSIS_CQ      h264_nvenc cq (既定 23)
W/H/タイル数/表示アスペクト/諸元は sim 出力から自動導出。

usage: python3 tools/render_analysis.py            # 全編→mp4
       python3 tools/render_analysis.py A B         # frame [A,B) だけPNG(検証用, mp4化しない)
"""
import sys
import os
import glob
import pickle
import subprocess
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
from encode_config import consume_config_arg

# Match the sim invocation exactly without requiring callers to repeat its
# resolved CBRSIM_* environment by hand.
CONFIG_PROFILE = consume_config_arg(sys.argv)

import layout_preview as L
import stream_schedule
from cbr_paths import artifact_path, sim_work_dir

SIM = str(sim_work_dir())
SRCLABEL = os.environ.get("CBRSIM_SRCLABEL", "Source")


def _source_spec():
    """Source見出し併記用: 元動画の 解像度 / fps / 音声仕様 を ffprobe で組み立てる(ビットレートは省略)。"""
    src = os.environ.get("CBRSIM_SRC", "")
    if not src or not Path(src).exists():
        return ""
    import subprocess
    import json as _json
    try:
        vj = _json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate", "-of", "json", src],
            capture_output=True, text=True).stdout)["streams"][0]
        num, den = vj["r_frame_rate"].split("/")
        fps = round(float(num) / float(den))
        parts = ["%dx%d" % (vj["width"], vj["height"]), "%dfps" % fps]
        aj = _json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "json", src],
            capture_output=True, text=True).stdout).get("streams", [])
        if aj:
            a = aj[0]; ch = int(a.get("channels", 0)); sr = int(a.get("sample_rate", 0))
            chs = {1: "mono", 2: "stereo"}.get(ch, "%dch" % ch)
            parts.append("%s %gkHz %s" % (a["codec_name"].upper(), sr / 1000.0, chs))
        return " / ".join(parts)
    except Exception:
        return ""


SRC_SPEC = _source_spec()
MODE = os.environ.get("CBRSIM_MODE", "H32")
OUT_MP4 = os.environ.get("ANALYSIS_OUT", str(artifact_path("analysis", sim_dir=SIM)))
CQ = os.environ.get("ANALYSIS_CQ", "23")
FRAMES_DIR = f"{SIM}/analysis_frames"
AUDIO_STR = "13.3kHz mono 8bit PCM"          # 既定。sim出力(stats)にラベルがあればそれを使う

# ---- フォント(layout_preview のグローバルへ) ----
L.f_head = ImageFont.truetype(L.FONT, 33)
L.f_leg = ImageFont.truetype(L.FONT, 15)
L.f_lbl = ImageFont.truetype(L.FONT, 20)
L.f_sm = ImageFont.truetype(L.FONT, 12)
L.f_meta = ImageFont.truetype(L.FONT, 18)
L.f_pal = ImageFont.truetype(L.FONT, 14)

# ---- sim出力から諸元を自動導出 ----
z = np.load(f"{SIM}/stats.npz", allow_pickle=True)
S = z["stats"]
idx = {k: i for i, k in enumerate(str(z["cols"]).split())}
FPS = float(z["fps"]); C = int(z["cells"]); BUDGET = int(z["budget_tiles"])
ACTIVE_TILES = int(z["active_tiles"]) if "active_tiles" in z else C
COLD_CAP = (int(z["max_cold"]) if "max_cold" in z else
            L.av_config.cold_cap_for_fps(FPS, MODE, ACTIVE_TILES))
NF = len(S)
if "audio_label" in z:
    AUDIO_STR = str(z["audio_label"])        # sim側の音声形式(13.3kHz PCM / 22.05kHz ADPCM 等)
if "audio_playback_file" in z:
    AUDIO_PATH = Path(SIM) / str(z["audio_playback_file"])
    if not AUDIO_PATH.is_file():
        raise FileNotFoundError(
            f"stats.npz playback audio is missing: {AUDIO_PATH}")
else:
    # Legacy sim outputs contained one extracted source WAV only.
    _legacy_audio = sorted(glob.glob(f"{SIM}/audio_*.wav"))
    AUDIO_PATH = Path(_legacy_audio[0]) if len(_legacy_audio) == 1 else Path(
        SIM) / "audio_13k3_u8_mono.wav"
_pv = sorted(glob.glob(f"{SIM}/preview/*.png"))
_raw = sorted(glob.glob(f"{SIM}/raw/*.png"))
W, H = Image.open(_pv[0]).size                # タイルグリッド画素(=WxH)
TCOLS, TROWS = W // 8, H // 8
RW, RH = Image.open(_raw[0]).size             # Sourceパネル素材の画素
# 画面モード(H32/H40/mode4)から PAR・実機画面サイズ・表示アスペクトを取得
_M = L.MODES[MODE]
PAR = _M["par"]                                # 1ドット横長比
A_CONTENT = (W / H) * PAR                      # カテゴリ(タイル解析)の表示比
A_SRC = RW / RH                                # Sourceの表示比(crop済素材そのまま)
RES = f"{W}x{H} ({TCOLS}x{TROWS})"
# 実機画面(この解像度を画面いっぱいに拡大せず中央配置する)。
SCREEN_W = max(_M["sw"], W)
SCREEN_H = max(_M["sh"], H)
SCREEN_A = L.screen_aspect(MODE)               # 画面の表示アスペクト(H32/H40=64:49, mode4≈14:9)
BUF = np.load(f"{SIM}/buffer_remaining.npz")
BUF_SCHEMA = int(BUF["schema_version"]) if "schema_version" in BUF else 1
BUF_KIND = str(BUF["remaining_kind"]) if "remaining_kind" in BUF else "legacy"
if BUF_SCHEMA < 4 or BUF_KIND != "four_source_pattern_supply":
    raise SystemExit(
        f"analysis requires four-source pattern supply schema 4, got "
        f"schema={BUF_SCHEMA} kind={BUF_KIND!r}; re-run sim")
SUPPLY_CAPACITIES = {
    "Prg": int(BUF["prg_capacity"]),
    "Wr0": int(BUF["wr0_capacity"]),
    "Wr1": int(BUF["wr1_capacity"]),
}
SUPPLY_REMAINING = {
    "Prg": BUF["prg_remaining"].astype(np.int64),
    "Wr0": BUF["wr0_remaining"].astype(np.int64),
    "Wr1": BUF["wr1_remaining"].astype(np.int64),
}
if "quality_budget_remaining" not in BUF:
    raise SystemExit("analysis quality-budget trace is missing; re-run sim")
QUALITY_REM = BUF["quality_budget_remaining"].astype(np.int64)
for _name, _remaining in SUPPLY_REMAINING.items():
    _capacity = SUPPLY_CAPACITIES[_name]
    if len(_remaining) != NF:
        raise SystemExit(
            f"{_name} trace has {len(_remaining)} frames, expected {NF}; re-run sim")
    if (_remaining < 0).any() or (_remaining > _capacity).any():
        raise SystemExit(
            f"{_name} trace is outside capacity {_capacity}; re-run sim")
if len(QUALITY_REM) != NF:
    raise SystemExit(
        f"quality-budget trace has {len(QUALITY_REM)} frames, expected {NF}; re-run sim")
_body_fields = (
    "body_useful_payload_bytes",
    "body_useful_control_bytes",
    "body_pad_bytes",
    "body_physical_bytes",
)
if any(name not in BUF for name in _body_fields):
    raise SystemExit(
        "BODY useful-delivery trace is incomplete; re-run sim")
BODY_PAYLOAD_BYTES = BUF["body_useful_payload_bytes"].astype(np.int64)
BODY_CONTROL_BYTES = BUF["body_useful_control_bytes"].astype(np.int64)
BODY_PAD_BYTES = BUF["body_pad_bytes"].astype(np.int64)
BODY_PHYSICAL_BYTES = BUF["body_physical_bytes"].astype(np.int64)
for _name, _values in (
        ("payload", BODY_PAYLOAD_BYTES),
        ("control", BODY_CONTROL_BYTES),
        ("pad", BODY_PAD_BYTES),
        ("physical", BODY_PHYSICAL_BYTES)):
    if len(_values) != NF:
        raise SystemExit(
            f"BODY {_name} trace has {len(_values)} slots, expected {NF}; re-run sim")
if not np.array_equal(
        BODY_PAYLOAD_BYTES + BODY_CONTROL_BYTES + BODY_PAD_BYTES,
        BODY_PHYSICAL_BYTES):
    raise SystemExit(
        "BODY useful/pad trace does not sum to physical slots; re-run sim")
if any(int(values[0]) != 0 for values in (
        BODY_PAYLOAD_BYTES, BODY_CONTROL_BYTES, BODY_PAD_BYTES,
        BODY_PHYSICAL_BYTES)):
    raise SystemExit("BODY delivery slot 0 must exclude HEADER/frame 0; re-run sim")
MISS_MASKS = np.load(f"{SIM}/miss_masks.npy")

# ---- stats -> mutually-exclusive display categories ----
col = lambda k: S[:, idx[k]].astype(np.int64) if k in idx else np.zeros(NF, np.int64)
Raw = col("tx"); Dedup = col("dedup"); Coa = col("coa"); Near = col("near")
# Flbk = 旧Mid+Farを統合(Missのフォールバック)。新statsは flbk 列, 旧statsは mid+far を合算(後方互換)
Flbk = col("flbk") + col("mid") + col("far")
Want = col("want"); Miss = col("miss")
Buf = col("buf") if "buf" in idx else np.maximum(col("updated") - Raw - Dedup - Coa, 0)
_source_fields = ("prg", "wr0", "wr1", "main")
if any(name not in idx for name in _source_fields):
    raise SystemExit(
        "analysis physical-source categories are missing; re-run sim")
Prg, Wr0, Wr1, Main = (col(name) for name in _source_fields)
if not np.array_equal(Prg + Wr0 + Wr1 + Main, Buf):
    raise SystemExit(
        "analysis physical-source categories do not sum to legacy Buf; re-run sim")
if "same" not in idx:
    raise SystemExit("analysis exact Same category is missing; re-run sim")
Same = col("same")
DMA_TILES = col("dma_tiles") if "dma_tiles" in idx else Raw + Buf
PREFETCH = col("prefetch")
PREFETCH_CAP = int(z["raw_prefetch_cap"]) if "raw_prefetch_cap" in z else max(
    1, int(PREFETCH.max(initial=0)))


def _legacy_dma_runs():
    """Replay the shared allocator when rendering an older stats.npz.

    Fresh sims save dma_runs directly. Existing verified sims can still render
    the exact packed-run count from decisions.pkl without a full re-encode.
    """
    path = Path(SIM) / "decisions.pkl"
    if not path.exists():
        raise SystemExit(
            "DMA runs: stats.npz has no dma_runs and decisions.pkl is missing; "
            "re-run sim instead of displaying an estimated value")
    try:
        from tile_alloc import TileAllocator, count_slot_runs
        with path.open("rb") as fh:
            log = pickle.load(fh)
        frames = log["frames"]
        if len(frames) != NF:
            raise ValueError(f"decision frames {len(frames)} != stats frames {NF}")
        pool = int(log.get(
            "vram_tiles",
            log.get("config", {}).get("hardware", {}).get("vram_tiles", 1400)))
        alloc = TileAllocator(C, pool, 1)
        result = np.zeros(NF, np.int64)
        replay_tiles = np.zeros(NF, np.int64)
        for i, frame in enumerate(frames):
            ordered = sorted(frame, key=lambda item: item[0])
            placed = alloc.place_frame([(int(cell), key) for cell, _pal, key in ordered], i)
            cold_slots = [slot for slot, cold in placed if cold]
            replay_tiles[i] = len(cold_slots)
            result[i] = count_slot_runs(cold_slots)
        mismatch = np.flatnonzero(replay_tiles != DMA_TILES)
        if mismatch.size:
            i = int(mismatch[0])
            raise ValueError(
                f"frame {i} cold tiles decisions={int(replay_tiles[i])} "
                f"stats={int(DMA_TILES[i])}")
        print("Pattern runs: replayed exact values from legacy decisions.pkl")
        return result
    except Exception as exc:
        raise SystemExit(
            f"Pattern runs: exact legacy replay failed ({exc}); re-run sim") from exc


DMA_RUNS = col("dma_runs") if "dma_runs" in idx else _legacy_dma_runs()
FULL = {
    "Raw": Raw, "Same": Same, "Near": Near, "Coa": Coa,
    "Flbk": Flbk, "Miss": Miss,
    "Prg": Prg, "Wr0": Wr0, "Wr1": Wr1, "Dic": Main,
}
_category_sum = sum(FULL.values())
if not np.array_equal(_category_sum, np.full(NF, C, np.int64)):
    bad = int(np.flatnonzero(_category_sum != C)[0])
    raise SystemExit(
        f"analysis categories do not cover frame {bad}: "
        f"{int(_category_sum[bad])} != {C}; re-run sim")
WIN = 4; HALF = int(round(FPS * WIN))                       # 線グラフ ±4秒

# ---- palettes.bin(MDワード 0000BBB0GGG0RRR0) -> RGB(使用色枠なし) ----
pb = np.frombuffer(Path(f"{SIM}/palettes.bin").read_bytes(), ">u2").reshape(4, 16)


def md_rgb(w):
    r = (int(w) >> 1) & 7; g = (int(w) >> 5) & 7; b = (int(w) >> 9) & 7
    return (r * 36, g * 36, b * 36)


PAL = [[md_rgb(pb[p, c]) for c in range(16)] for p in range(4)]

# ---- 区間パレット(Prev/Current/Next 用) + カテゴリ合計(全編) ----
_SP = np.load(f"{SIM}/seg_palettes.npz")
SEG_PALS = _SP["seg_pals"]                     # (nseg,4,15,3) rgb333(0-7)
FRAME_SEG = _SP["frame_seg"]                   # (NF,)

# ---- 音声波形パネル用データ(sim OUT の音声wav) ----
import wave as _wave  # noqa: E402
WAVE_WIN_S = 2.0                                          # 前後2s
WAVE_BW = L.WAVE_FRAME[2] - L.WAVE_FRAME[0] - 2
try:
    _wf = _wave.open(str(AUDIO_PATH), "rb")
    AUDIO_RATE = _wf.getframerate()
    _audio_width = _wf.getsampwidth()
    _audio_raw = _wf.readframes(_wf.getnframes())
    _wf.close()
    if _audio_width == 1:
        _araw = np.abs(
            np.frombuffer(_audio_raw, np.uint8).astype(np.int16) - 128)
    elif _audio_width == 2:
        _araw = (
            np.abs(np.frombuffer(_audio_raw, "<i2").astype(np.int32)) >> 8
        ).astype(np.int16)
    else:
        raise ValueError(f"unsupported waveform sample width: {_audio_width}")
except Exception as _e:
    AUDIO_RATE = 13300; _araw = np.zeros(1, np.int16); print("waveform: 音声wav 無し ->", _e)
_PPS = WAVE_BW / (2 * WAVE_WIN_S)                         # pixels/秒
_BIN = max(1, int(round(AUDIO_RATE / _PPS)))             # samples/pixel(1px=1bin)
_nb = len(_araw) // _BIN
AUDIO_ENV = ((_araw[:_nb * _BIN].reshape(_nb, _BIN).max(axis=1)) if _nb > 0
             else np.zeros(1, np.int16)).astype(np.int16)   # px解像度の包絡(0..128)


def seg_pal_rgb(seg):
    seg = int(np.clip(seg, 0, len(SEG_PALS) - 1))
    p = SEG_PALS[seg].astype(int)              # (4,15,3) 0-7 -> *36 で表示
    return [[(int(p[pl][c][0]) * 36, int(p[pl][c][1]) * 36, int(p[pl][c][2]) * 36) for c in range(15)]
            for pl in range(4)]


def frame_palettes(i):
    s = int(FRAME_SEG[i]) if i < len(FRAME_SEG) else 0
    last = len(SEG_PALS) - 1
    return {"Prev": seg_pal_rgb(s - 1) if s > 0 else None,      # 前後にパレット無し=ブランク
            "Current": seg_pal_rgb(s),
            "Next": seg_pal_rgb(s + 1) if s < last else None}


CAT_TOTALS = {k: int(FULL[k].sum()) for k, _ in L.CATS}

# ---- 有効転送量(新規パターンのCDバイト) + CD1x/コマ + パレット切替フレーム ----
Updated = col("updated")
_cram = np.zeros(NF, np.int64); _cram[1:] = (FRAME_SEG[1:] != FRAME_SEG[:-1]).astype(np.int64) * 128
FB = Raw * 32 + Buf * 32 + Updated * 2 + _cram        # 1コマの映像書込量(パターン+全ネーム+CRAM, タンク供給込み)
# Band is useful BODY.DAT bytes in the physical delivery slot.  It excludes
# HEADER/frame 0, stream-tail alignment zeros, and rate-match pad.
BODY_USEFUL_BYTES = BODY_PAYLOAD_BYTES + BODY_CONTROL_BYTES
BAND_BPS = stream_schedule.body_delivery_rate_bps(
    BODY_USEFUL_BYTES, BODY_PHYSICAL_BYTES)
BAND = BAND_BPS // 1024
EFF = FB                                              # (互換)
AVG_KBPS = int(round(stream_schedule.average_body_delivery_rate_bps(
    BODY_USEFUL_BYTES, BODY_PHYSICAL_BYTES) / 1024))
SEG_STARTS = {}
for _i, _s in enumerate(FRAME_SEG):
    SEG_STARTS.setdefault(int(_s), _i)               # 各区間の開始フレーム=CRAM切替点


def frame_plinfo(i):
    s = int(FRAME_SEG[i]) if i < len(FRAME_SEG) else 0
    last = len(SEG_PALS) - 1
    def one(sg):
        sg = int(max(0, min(sg, last)))
        return dict(pl=sg, frame=SEG_STARTS.get(sg, 0))
    return {"Prev": one(s - 1) if s > 0 else None, "Current": one(s),
            "Next": one(s + 1) if s < last else None}


# ---- メーター幅(統一廃止=各バーは自分のラベル幅) ----
GAP = 16
REQ_W = L._w(L.f_leg, "Req:000  Miss:000") + 3
COLD_W = L._w(L.f_leg, "Cold:000") + 3
PRE_W = L._w(L.f_leg, "Pre:000") + 3
BAND_W, PRG_W, WR0_W, WR1_W, DMA_W, RUN_W = L.meter_widths(C)
X_TL_STATUS = (
    4 + REQ_W + GAP + COLD_W + GAP + PRE_W + GAP + BAND_W + GAP
    + PRG_W + GAP + WR0_W + GAP + WR1_W + GAP
    + DMA_W + GAP + RUN_W + GAP)


def fit(A, bw, bh):
    """表示アスペクトA を box(bw,bh) にレターボックスで収める -> (sw,sh,ox,oy)。"""
    if A >= bw / bh:
        sw, sh = bw, round(bw / A)
    else:
        sh, sw = bh, round(bh * A)
    return sw, sh, (bw - sw) // 2, (bh - sh) // 2


# ---- 静的ベース(枠/見出し/meta/palstate) ----
def build_base():
    cv = Image.new("RGB", (L.CW, L.CH), L.BG)
    d = ImageDraw.Draw(cv)
    L.panel(d, L.MAIN_FRAME)
    base_y = L.MAIN_FRAME[1] - 10
    hx = L.MAIN_FRAME[0] + 2
    d.text((hx, base_y), "SEGA-CD sim output", fill=L.COL_TXT, font=L.f_head, anchor="ls")
    meta = " / ".join([MODE, RES, AUDIO_STR, "%gfps" % round(FPS, 2), "avg %d KiB/sec" % AVG_KBPS])
    d.text((hx + L._w(L.f_head, "SEGA-CD sim output") + 12, base_y), meta,
           fill=L.COL_DIM, font=L.f_meta, anchor="ls")
    L.panel(d, L.SRC_FRAME)          # 見出しは "Source" + ソース諸元(res/fps/音声)を小フォント併記
    _sby = L.SRC_FRAME[1] - 10; _sx = L.SRC_FRAME[0] + 2
    d.text((_sx, _sby), "Source", fill=L.COL_TXT, font=L.f_head, anchor="ls")
    if SRC_SPEC:
        d.text((_sx + L._w(L.f_head, "Source") + 12, _sby), SRC_SPEC, fill=L.COL_DIM, font=L.f_meta, anchor="ls")
    L.panel(d, L.CAT_FRAME)
    L.panel(d, L.WAVE_FRAME)         # 音声波形パネル。見出し=Audio + 諸元 + 読み方(小フォント, 枠外)
    _ax = L.WAVE_FRAME[0] + 2; _ay = L.WAVE_FRAME[1] - 4
    d.text((_ax, _ay), "Audio", fill=L.COL_TXT, font=L.f_leg, anchor="ls")
    _sx = _ax + L._w(L.f_leg, "Audio") + L._w(L.f_sm, " ")   # 右スペース=半角1文字
    d.text((_sx, _ay), AUDIO_STR, fill=L.COL_DIM, font=L.f_sm, anchor="ls")
    d.text((_sx + L._w(L.f_sm, AUDIO_STR) + 14, _ay), "±2s, now=center, scroll left",
           fill=L.COL_DIM, font=L.f_sm, anchor="ls")   # 波形の読み方=見出しの後ろ
    # カテゴリ合計(全編合計=静的)を Category の下へ
    cv.paste(L.draw_cattotals(L.CATTOT_W, L.CATTOT_H, {"cat_totals": CAT_TOTALS}),
             L.CATTOT_XY)
    return cv


# ---- タイムライン背景(全編共通・再生ヘッド無し) ----
def build_tl_bg():
    by = 8; BAR_W = 180; GAP = 16          # 上マージン半減(タイムラインは下端据置=縦に伸びる)
    x_tl = X_TL_STATUS
    tlw = L.STATUS_W - 4 - x_tl
    tlh = (L.STATUS_H - 2) - by
    H_req = tlh // 2
    H_supply = tlh // 4
    H_dma = tlh - H_req - H_supply
    im = Image.new("RGB", (tlw, tlh), (16, 16, 16))
    d = ImageDraw.Draw(im)
    d.rectangle([0, H_req, tlw, H_req + H_supply], fill=(21, 22, 28))
    d.rectangle([0, H_req + H_supply, tlw, tlh], fill=(18, 26, 20))
    order = [(name, dict(L.CATS)[name]) for name in L.REQ_TIMELINE_CATS]
    for cx in range(tlw):
        fi = min(int(cx / tlw * NF), NF - 1)
        yb = H_req
        for k, c in order:
            seg = int(H_req * FULL[k][fi] / C)
            if seg > 0:
                d.line([(cx, yb - seg), (cx, yb)], fill=c); yb -= seg
        ys = H_req + H_supply
        total_capacity = max(sum(SUPPLY_CAPACITIES.values()), 1)
        for name in L.METER_SUPPLY_ORDER:
            hs = int(H_supply * SUPPLY_REMAINING[name][fi] / total_capacity)
            if hs > 0:
                d.line([(cx, ys - hs), (cx, ys)], fill=L.SUPPLY_COLORS[name])
                ys -= hs
        physical = max(int(BODY_PHYSICAL_BYTES[fi]), 1)
        hp = int(H_dma * int(BODY_PAYLOAD_BYTES[fi]) / physical)
        if hp > 0:
            d.line([(cx, tlh - hp), (cx, tlh)], fill=L.CAT_RAW)
        hc = int(H_dma * int(BODY_USEFUL_BYTES[fi]) / physical)
        if hc > hp:
            d.line([(cx, tlh - hc), (cx, tlh - hp)], fill=L.COL_OVH)
    d.line([(0, tlh - H_dma), (tlw - 1, tlh - H_dma)], fill=(110, 105, 70))
    d.rectangle([0, 0, tlw - 1, tlh - 1], outline=L.COL_FRAME_IN)
    return im, x_tl, by, tlw, tlh


BASE = build_base()
TL_BG, X_TL, BY, TLW, TLH = build_tl_bg()


def draw_status_real(data):
    im = Image.new("RGB", (L.STATUS_W, L.STATUS_H), (16, 16, 16))
    d = ImageDraw.Draw(im)
    by, BH = 8, 16
    ly = by + BH + 3
    x = 4
    cn = data["counts"]
    dmax = L.dma_tile_capacity(MODE, FPS, C); dval = data["dma_tiles"]

    def stacked(segs, full, bw):
        px = x
        for val, c in segs:
            seg = int(bw * min(val, full) / full)
            seg = min(seg, x + bw - px)              # 積み上げ合計が枠幅を超えない(はみ出し防止)
            if seg > 0:
                d.rectangle([px, by, px + seg, by + BH], fill=c); px += seg
        d.rectangle([x, by, x + bw, by + BH], outline=L.COL_FRAME_IN)

    # 1) Req + Miss headline values.
    stacked([(cn[k], dict(L.CATS)[k]) for k, _ in L.CATS], C, REQ_W)
    bx = x + int(REQ_W * data["budget"] / C)
    d.line([bx, by - 2, bx, by + BH + 2], fill=(255, 214, 0))
    xq = L.draw_field(d, x, ly, "Req:", data["req"], 3, L.f_leg, L.COL_TXT)
    L.draw_field(d, xq + 8, ly, "Miss:", data["miss"], 3, L.f_leg, L.COL_TXT)
    x += REQ_W + GAP
    # 2) Cold = same-frame exact loads by source + future prefetch.
    cold_parts = [(cn["Raw"], L.CAT_RAW)]
    cold_parts += [
        (cn[name], L.SUPPLY_COLORS[name])
        for name in L.DISPLAY_SOURCE_ORDER
    ]
    cold_parts.append((data["cold_prefetch"], L.CAT_PREFETCH))
    stacked(cold_parts, data["cold_cap"], COLD_W)
    L.draw_field(d, x, ly, "Cold:", data["cold"], 3, L.f_leg, L.COL_TXT)
    x += COLD_W + GAP
    # 3) Prefetch activity is not a displayed-cell category.
    stacked([(data["cold_prefetch"], L.CAT_PREFETCH)],
            data["prefetch_cap"], PRE_W)
    L.draw_field(d, x, ly, "Pre:", data["cold_prefetch"], 3, L.f_leg, L.COL_TXT)
    x += PRE_W + GAP
    # 4) Band = physical slot useful BODY payload + control, excluding pad/Header.
    stacked([(data["body_payload_bytes"], L.CAT_RAW),
             (data["body_control_bytes"], L.COL_OVH)],
            max(data["body_physical_bytes"], 1), BAND_W)
    d.line([x + BAND_W, by - 2, x + BAND_W, by + BH + 2], fill=(210, 190, 90))
    L.draw_field(d, x, ly, "Band:", data["band_kbps"], 3, L.f_leg, L.COL_TXT)
    x += BAND_W + GAP
    # 5) MainBuf is a persistent dictionary and has no remaining meter.
    supply_widths = {
        "Prg": (PRG_W, 5), "Wr0": (WR0_W, 3),
        "Wr1": (WR1_W, 3),
    }
    for name in L.METER_SUPPLY_ORDER:
        width, digits = supply_widths[name]
        remaining = data["supply_remaining"][name]
        capacity = data["supply_capacities"][name]
        stacked([(remaining, L.SUPPLY_COLORS[name])], capacity, width)
        L.draw_field(
            d, x, ly, name + ":", remaining, digits, L.f_leg, L.COL_TXT)
        x += width + GAP

    # 4) DMA = 今フレームの32Bパターンタイル数
    fillw = int(DMA_W * min(dval, dmax) / max(dmax, 1)); over = dval > dmax
    d.rectangle([x, by, x + fillw, by + BH], fill=(220, 130, 60) if over else L.COL_DMA)
    if over:
        d.rectangle([x + fillw, by, x + DMA_W, by + BH], fill=(150, 60, 60))
    d.rectangle([x, by, x + DMA_W, by + BH], outline=L.COL_FRAME_IN)
    L.draw_field(d, x, ly, "DMA:", dval, L.dma_value_digits(C), L.f_leg, L.COL_TXT)
    x += DMA_W + GAP

    # 5) Run = playerのcold-run record数。フル=1tile/runの理論最悪ケース。
    run_val = int(data["dma_runs"]); run_max = L.dma_run_worst_case(dval)
    run_fill = (max(1, int(RUN_W * min(run_val, run_max) / run_max))
                if run_val > 0 and run_max > 0 else 0)
    d.rectangle([x, by, x + run_fill, by + BH],
                fill=(220, 70, 70) if run_val > run_max else L.COL_RUN)
    d.rectangle([x, by, x + RUN_W, by + BH], outline=L.COL_FRAME_IN)
    L.draw_field(d, x, ly, "Run:", run_val, L.DMA_RUN_DIGITS, L.f_leg, L.COL_TXT)
    x += RUN_W + GAP
    # メーター下: パレット Prev/Current/Next(PL/Frame見出し, 正方形タイル)
    meters_right = x - GAP
    py0 = ly + 16
    L.draw_palettes_strip(d, 4, py0, meters_right - 4, (L.STATUS_H - 2) - py0,
                          data["palettes"], data.get("pl_info"))
    im.paste(TL_BG, (X_TL, BY))
    head = X_TL + int(TLW * data["frame"] / NF)
    ImageDraw.Draw(im).line([head, BY, head, BY + TLH], fill=(255, 255, 255))
    return im


def catmap_panel(i, sw, sh):
    """catmap を(sw,sh)へ拡大 → Missセル(miss_masks)を『赤で塗りつぶし』で上書き。"""
    cm = Image.open(f"{SIM}/catmap/{i:05d}.png").convert("RGB").resize((sw, sh), Image.NEAREST)
    bits = np.unpackbits(MISS_MASKS[i])[:C]
    if bits.any():
        d = ImageDraw.Draw(cm)
        for cell in np.where(bits)[0]:
            r, c = int(cell) // TCOLS, int(cell) % TCOLS
            x0 = round(c * sw / TCOLS); y0 = round(r * sh / TROWS)
            x1 = round((c + 1) * sw / TCOLS) - 1; y1 = round((r + 1) * sh / TROWS) - 1
            d.rectangle([x0, y0, x1, y1], fill=L.CAT_MISS)     # 赤で塗りつぶし
    return cm


def frame_data(i):
    cn = {k: int(FULL[k][i]) for k in FULL}
    return dict(C=C, counts=cn, fps=FPS, win=WIN,
                mode=MODE, res=RES, audio=AUDIO_STR, avg_kbps=AVG_KBPS,
                req=int(Want[i]), miss=cn["Miss"], budget=BUDGET,
                comp=cn["Same"] + cn["Near"] + cn["Coa"] + cn["Flbk"],
                supply_capacities=SUPPLY_CAPACITIES,
                supply_remaining={
                    name: int(values[i])
                    for name, values in SUPPLY_REMAINING.items()
                },
                dma_tiles=int(DMA_TILES[i]), dma_runs=int(DMA_RUNS[i]),
                body_payload_bytes=int(BODY_PAYLOAD_BYTES[i]),
                body_control_bytes=int(BODY_CONTROL_BYTES[i]),
                body_physical_bytes=int(BODY_PHYSICAL_BYTES[i]),
                band_kbps=int(BAND[i]),
                cold=(cn["Raw"] + sum(
                    cn[name] for name in L.DISPLAY_SOURCE_ORDER)
                      + int(PREFETCH[i])),
                cold_prefetch=int(PREFETCH[i]),
                prefetch_cap=PREFETCH_CAP,
                cold_cap=COLD_CAP,
                pl_info=frame_plinfo(i),
                frame=i, total_frames=NF, time_s=i / FPS, palettes=frame_palettes(i),
                series={k: [int(FULL[k][min(max(j, 0), NF - 1)]) for j in range(i - HALF, i + HALF + 1)]
                        for k in FULL})


def draw_waveform_real(i):
    """音声波形パネル: このコマの前後2sを描く。中央=現在(now)、左=過去(明)/右=未来(暗)、左へ流れる。"""
    bw, bh = WAVE_BW, L.WAVE_FRAME[3] - L.WAVE_FRAME[1] - 2
    im = Image.new("RGB", (bw, bh), (16, 16, 16))
    d = ImageDraw.Draw(im)
    mid = bh // 2
    d.line([(0, mid), (bw - 1, mid)], fill=(60, 60, 66))          # 振幅0の中央線
    now_bin = int(i / FPS * _PPS); half = bw // 2
    scale = bh * 0.46 / 128.0
    for x in range(bw):
        b = now_bin - half + x
        if 0 <= b < len(AUDIO_ENV):
            yy = int(AUDIO_ENV[b] * scale)
            if yy > 0:
                col = (150, 205, 150) if x < half else (95, 130, 95)   # 過去=明 / 未来=暗
                d.line([(x, mid - yy), (x, mid + yy)], fill=col)
    d.line([(half, 0), (half, bh - 1)], fill=(230, 230, 235))    # 現在(now)線
    return im


def render(i):
    data = frame_data(i)
    cv = BASE.copy()
    # メイン(SEGA-CD出力): 実機同様、画面いっぱいに拡大せず 実機画面(4:3)へ中央配置。
    mv = Image.open(f"{SIM}/preview/{i:05d}.png").convert("RGB")
    bw = L.MAIN_FRAME[2] - L.MAIN_FRAME[0] - 2 * L.PAD; bh = L.MAIN_FRAME[3] - L.MAIN_FRAME[1] - 2 * L.PAD
    Fw, Fh, ox, oy = fit(SCREEN_A, bw, bh)         # 4:3の実機画面をパネルへ
    scr = Image.new("RGB", (Fw, Fh), (0, 0, 0))
    cw = round(Fw * W / SCREEN_W); ch = round(Fh * H / SCREEN_H)   # 画面内のコンテンツ画素
    cx = round(Fw * ((SCREEN_W - W) // 2) / SCREEN_W); cy = round(Fh * ((SCREEN_H - H) // 2) / SCREEN_H)
    scr.paste(mv.resize((cw, ch), Image.LANCZOS), (cx, cy))         # 中央配置(周囲は黒縁)
    cv.paste(scr, (L.MAIN_FRAME[0] + L.PAD + ox, L.MAIN_FRAME[1] + L.PAD + oy))
    # Source(raw は 1始点)
    sv = Image.open(f"{SIM}/raw/{i + 1:05d}.png").convert("RGB")
    bw = L.SRC_FRAME[2] - L.SRC_FRAME[0] - 2 * L.PAD; bh = L.SRC_FRAME[3] - L.SRC_FRAME[1] - 2 * L.PAD
    sw, sh, ox, oy = fit(A_SRC, bw, bh)
    cv.paste(sv.resize((sw, sh), Image.LANCZOS), (L.SRC_FRAME[0] + L.PAD + ox, L.SRC_FRAME[1] + L.PAD + oy))
    # Category(Miss=中身なし赤枠)
    bw = L.CAT_FRAME[2] - L.CAT_FRAME[0] - 2 * L.PAD; bh = L.CAT_FRAME[3] - L.CAT_FRAME[1] - 2 * L.PAD
    sw, sh, ox, oy = fit(A_CONTENT, bw, bh)
    cv.paste(catmap_panel(i, sw, sh), (L.CAT_FRAME[0] + L.PAD + ox, L.CAT_FRAME[1] + L.PAD + oy))
    d = ImageDraw.Draw(cv)
    # Time/Frame(右上・小15px・ベースライン揃え)
    base_y = L.MAIN_FRAME[1] - 10
    _plt = len(SEG_PALS) - 1                        # 総数(最大パレット番号)
    _plw = max(2, len(str(_plt)))
    lab_t = "PL:%0*d/%0*d Time:%02d:%05.2f Frame:" % (_plw, int(FRAME_SEG[i]), _plw, _plt,
                                                      int(data["time_s"] // 60), data["time_s"] % 60)
    fhex = "%04X" % i                              # F番号=実機HUDと同じ16進4桁
    tw = L._w(L.f_leg, lab_t) + L._w(L.f_leg, fhex)
    tx = L.MAIN_FRAME[2] - tw; ty = base_y - L.f_leg.getmetrics()[0]
    d.text((tx, ty), lab_t, fill=L.COL_TXT, font=L.f_leg)
    d.text((tx + L._w(L.f_leg, lab_t), ty), fhex, fill=L.COL_TXT, font=L.f_leg)
    # 凡例リスト(Categoryの上) / VRAMパネル(右下) / status
    cv.paste(L.draw_legend(L.CATLEG_W, L.CATLEG_H, data), L.CATLEG_XY)
    cv.paste(draw_waveform_real(i), (L.WAVE_FRAME[0] + 1, L.WAVE_FRAME[1] + 1))   # padding無し(枠内1px)
    cv.paste(draw_status_real(data), L.STATUS_XY)
    cv.save(f"{FRAMES_DIR}/{i:05d}.png")
    return i


def mux():
    audio = str(AUDIO_PATH)
    vcodec = ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr",
              "-cq", CQ, "-b:v", "0"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", str(FPS), "-start_number", "0", "-i", f"{FRAMES_DIR}/%05d.png"]
    if Path(audio).exists():
        cmd += ["-i", audio]
    cmd += vcodec + ["-pix_fmt", "yuv420p", "-r", "60"]
    if Path(audio).exists():
        cmd += ["-c:a", "aac", "-ar", "22050", "-b:a", "96k", "-shortest"]  # 音声の標本化を保つ(ADPCM 22kHz対応)
    cmd += ["-fps_mode", "cfr", OUT_MP4]
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    from multiprocessing import get_context
    os.makedirs(FRAMES_DIR, exist_ok=True)
    rng = None
    if len(sys.argv) == 3:                     # 範囲指定(検証用): PNGのみ, mp4化しない
        rng = list(range(int(sys.argv[1]), int(sys.argv[2])))
    frames = rng if rng is not None else list(range(NF))
    print(f"render {len(frames)} frames @ {W}x{H} ({TCOLS}x{TROWS}) fps={FPS} -> {FRAMES_DIR}", flush=True)
    nw = min(max(1, len(frames)), max(1, (os.cpu_count() or 2) - 2))
    # Python 3.14 changed POSIX's default from fork to forkserver.  This renderer
    # deliberately loads its large read-only frame/stat tables before starting
    # workers; Linux fork shares those pages and is the proven project path.
    mp = get_context("fork") if sys.platform.startswith("linux") else get_context()
    with mp.Pool(nw) as p:
        for k, _ in enumerate(p.imap_unordered(render, frames, chunksize=8)):
            if k % 300 == 0:
                print(f"  {k}/{len(frames)}", flush=True)
    if rng is None:
        print(f"mux -> {OUT_MP4}", flush=True)
        mux()
        print("done", OUT_MP4, flush=True)
    else:
        print("done (frames only)", len(frames), flush=True)
