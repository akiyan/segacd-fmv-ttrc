#!/usr/bin/env python3
"""解析フレーム(新レイアウト)で sim 出力を全編mp4化する本パイプライン。

新レイアウトの『正』は tools/layout_preview.py(ダミー値で秒プレビュー)。本スクリプトは
その描画関数を実データで回して1920x1080/全フレームを描き、ffmpegでmp4(音声付き)にする。
sim 側(sim.py)や旧 compose(make_base/render_statusline/compose_*.sh)は使わない。

入力(env):
  CBRSIM_OUT       sim出力ディレクトリ(preview/raw/catmap/stats.npz/miss_masks.npy/
                   buffer_remaining.npz/palettes.bin/audio_13k3_u8_mono.wav/report.txt)
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
import re
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
NF = len(S)
if "audio_label" in z:
    AUDIO_STR = str(z["audio_label"])        # sim側の音声形式(13.3kHz PCM / 22.05kHz ADPCM 等)
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
BUF_CAP = int(BUF["total"]); BUF_REM = BUF["remaining"].astype(np.int64)
BUF_SCHEMA = int(BUF["schema_version"]) if "schema_version" in BUF else 1
BUF_KIND = str(BUF["remaining_kind"]) if "remaining_kind" in BUF else "legacy_vbv"
if BUF_SCHEMA >= 2 and BUF_KIND != "payload_ring_patterns":
    raise SystemExit(
        f"unsupported buffer_remaining metric {BUF_KIND!r}; re-run sim")
if len(BUF_REM) != NF:
    raise SystemExit(
        f"payload RING trace has {len(BUF_REM)} frames, expected {NF}; re-run sim")
if (BUF_REM < 0).any() or (BUF_REM > BUF_CAP).any():
    raise SystemExit(
        "payload RING trace is outside its physical capacity; re-run sim")
if BUF_SCHEMA < 2:
    print(
        "Tank: legacy virtual VBV trace; re-run sim for physical payload RING occupancy")
CD_USED = BUF["cd_used"].astype(np.int64) if "cd_used" in BUF else None   # 有効CD使用量(音声+全ヘッダ+映像+貯蓄)
MISS_MASKS = np.load(f"{SIM}/miss_masks.npy")


def _avg_kbps():
    try:
        t = Path(f"{SIM}/report.txt").read_text()
        m = re.search(r"avg_bps=(\d+)", t)
        if m:
            return int(round(int(m.group(1)) / 1024))   # bytes/s -> KiB/s(KB/sec)
    except Exception:
        pass
    return 0


AVG_KBPS = _avg_kbps()

# ---- stats -> 8カテゴリ時系列。mid/far/buf 列が無い旧statsは0扱い(後方互換) ----
col = lambda k: S[:, idx[k]].astype(np.int64) if k in idx else np.zeros(NF, np.int64)
Raw = col("tx"); Dedup = col("dedup"); Coa = col("coa"); Near = col("near")
# Flbk = 旧Mid+Farを統合(Missのフォールバック)。新statsは flbk 列, 旧statsは mid+far を合算(後方互換)
Flbk = col("flbk") + col("mid") + col("far")
Want = col("want"); Miss = col("miss")
Buf = col("buf") if "buf" in idx else np.maximum(col("updated") - Raw - Dedup - Coa, 0)
Same = np.maximum(C - Want, 0) + Dedup          # Dedup(完全一致流用)を Same に畳む
# カテゴリ別ユニークタイル数(何枚の別タイルを使い回したか)。旧statsに無ければ0(後方互換)
Same_u = col("same_u"); Near_u = col("near_u"); Coa_u = col("coa_u")
Flbk_u = col("flbk_u") + col("mid_u") + col("far_u")
DMA_TILES = col("dma_tiles") if "dma_tiles" in idx else Raw + Buf


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
FULL = {"Raw": Raw, "Same": Same, "Near": Near, "Coa": Coa,
        "Flbk": Flbk, "Buf": Buf, "Miss": Miss}
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
    _wf = _wave.open(f"{SIM}/audio_13k3_u8_mono.wav", "rb")
    AUDIO_RATE = _wf.getframerate()
    _araw = np.abs(np.frombuffer(_wf.readframes(_wf.getnframes()), np.uint8).astype(np.int16) - 128)
    _wf.close()
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


CAT_TOTALS = {k: int(FULL[k].sum()) for k in FULL}   # cattotals(全編合計)
_cu = z["cat_uniq"] if "cat_uniq" in z else np.zeros(4, np.int64)   # 全編ユニーク(same/near/coa/flbk; 旧はfar含む5)
CAT_UNIQ = {"Same": int(_cu[0]), "Near": int(_cu[1]), "Coa": int(_cu[2]),
            "Flbk": int(_cu[3]) + (int(_cu[4]) if len(_cu) > 4 else 0)}

# ---- 有効転送量(新規パターンのCDバイト) + CD1x/コマ + パレット切替フレーム ----
Updated = col("updated")
_cram = np.zeros(NF, np.int64); _cram[1:] = (FRAME_SEG[1:] != FRAME_SEG[:-1]).astype(np.int64) * 128
FB = Raw * 32 + Buf * 32 + Updated * 2 + _cram        # 1コマの映像書込量(パターン+全ネーム+CRAM, タンク供給込み)
FRAME_CD = int(z["frame_bytes"]) if "frame_bytes" in z else int(153600 / FPS)  # CBR配給/コマ(=このコマのCD読み量)
# 有効Band = このコマのCDを「有効に使った」量 = 映像に使った分 + RINGに貯めた分(貯蓄も有効)。CDは毎コマ
# FRAME_CD を読み、内訳は映像 or 貯蓄。タンク満杯で貯めきれず捨てたときだけ FRAME_CD を下回る。
TANK_DELTA = np.zeros(NF, np.int64); TANK_DELTA[1:] = BUF_REM[1:] - BUF_REM[:-1]   # コマ毎payload RING増減(タイル)
RAW_BYTES = np.minimum(FB, FRAME_CD)                  # 映像書込(Bandバーの Raw色)
BUF_BYTES = np.maximum(0, TANK_DELTA) * 32            # タンクに貯めたバイト(Bandバーの Buf色)
# 有効CD使用量: sim報告値 cd_used(音声+ネーム+CRAM+フラグ等の全ヘッダ+映像+貯蓄, パディング捨て分のみ除外)。
# 無い旧simは 映像+貯蓄 で近似(音声等は含まれない)。
_cd_used = CD_USED if CD_USED is not None else np.minimum(RAW_BYTES + BUF_BYTES, FRAME_CD)
OVH_BYTES = np.maximum(0, _cd_used - RAW_BYTES - BUF_BYTES)   # 音声+その他ヘッダ(Bandバーの dim色)
BAND = (_cd_used * FPS // 1024).astype(np.int64)                          # 有効Band(全部込み=FRAME_BYTES-パディング)
EFF = FB                                              # (互換)
AVG_KBPS = int(round(float(BAND.mean())))            # 平均も有効Band基準(全部込み)
CD1X_BPF = int(153600 / FPS)                         # CD1xのコマあたりバイト(有効転送メーターのフル)
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
REQ_W = 180
COLD_W = L._w(L.f_leg, "Cold:000") + 3                    # Coldバー(Req↔Bandの間)
BAND_W, TANK_W, BUFF_W, DMA_W, RUN_W = L.meter_widths(C)
X_TL_STATUS = (4 + REQ_W + GAP + COLD_W + GAP + BAND_W + GAP + TANK_W + GAP
               + BUFF_W + GAP + DMA_W + GAP + RUN_W + GAP)
# 指針器フルスケールの基準 C-MAX_RAW の MAX_RAW は「1コマのRaw予算」(=CDで新規に読める最大タイル数)。
# 観測最大(Raw.max)は初期タンク放出で全タイル≈Cになり scale≈0=全塗りになるので使わない。
MAX_RAW = int(z["budget_tiles"]) if "budget_tiles" in z else FRAME_CD // 34   # TANK_DELTA は上の Band 節で計算済み


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
    cv.paste(L.draw_cattotals(L.CATTOT_W, L.CATTOT_H, {"cat_totals": CAT_TOTALS, "cat_uniq": CAT_UNIQ}),
             L.CATTOT_XY)
    return cv


# ---- タイムライン背景(全編共通・再生ヘッド無し) ----
def build_tl_bg():
    by = 8; BAR_W = 180; GAP = 16          # 上マージン半減(タイムラインは下端据置=縦に伸びる)
    x_tl = X_TL_STATUS
    tlw = L.STATUS_W - 4 - x_tl
    tlh = (L.STATUS_H - 2) - by
    H_req = tlh // 2; H_buf = tlh // 4; H_dma = tlh - H_req - H_buf
    im = Image.new("RGB", (tlw, tlh), (16, 16, 16))
    d = ImageDraw.Draw(im)
    d.rectangle([0, H_req, tlw, H_req + H_buf], fill=(26, 20, 34))
    d.rectangle([0, H_req + H_buf, tlw, tlh], fill=(18, 26, 20))
    escale = max(CD1X_BPF, 1)                        # 3段目=有効転送量(フル=CD1x/コマ)
    order = [("Raw", L.CAT_RAW), ("Coa", L.CAT_COA), ("Flbk", L.CAT_FLBK),
             ("Buf", L.CAT_BUF), ("Miss", L.CAT_MISS)]
    for cx in range(tlw):
        fi = min(int(cx / tlw * NF), NF - 1)
        yb = H_req
        for k, c in order:
            seg = int(H_req * FULL[k][fi] / C)
            if seg > 0:
                d.line([(cx, yb - seg), (cx, yb)], fill=c); yb -= seg
        hb = int(H_buf * BUF_REM[fi] / max(BUF_CAP, 1))
        d.line([(cx, H_req + H_buf - hb), (cx, H_req + H_buf)], fill=L.CAT_BUF)
        hr = int(H_dma * min(int(RAW_BYTES[fi]), escale) / escale)   # 3段目: Raw色(新規CD)下 + Buf色上
        d.line([(cx, tlh - hr), (cx, tlh)], fill=L.CAT_RAW)
        hb2 = int(H_dma * min(int(RAW_BYTES[fi] + BUF_BYTES[fi]), escale) / escale)
        if hb2 > hr:
            d.line([(cx, tlh - hb2), (cx, tlh - hr)], fill=L.CAT_BUF)
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

    # 1) Req(広め) + 同ラインに Raw / Comp
    stacked([(cn[k], dict(L.CATS)[k]) for k, _ in L.CATS], C, REQ_W)
    bx = x + int(REQ_W * data["budget"] / C)
    d.line([bx, by - 2, bx, by + BH + 2], fill=(255, 214, 0))
    xq = L.draw_field(d, x, ly, "Req:", data["req"], 3, L.f_leg, L.COL_TXT)
    xr = L.draw_field(d, xq + 10, ly, "Raw:", cn["Raw"], 3, L.f_leg, L.COL_DIM)
    L.draw_field(d, xr + 8, ly, "Comp:", data["comp"], 3, L.f_leg, L.COL_DIM)
    x += REQ_W + GAP
    # 1.5) Cold = このコマの新規タイル(Raw+Buf)。フルスケール=COLD_CAP_REALIZED
    stacked([(data["cold_raw"], L.CAT_RAW), (data["cold_buf"], L.CAT_BUF)], data["cold_cap"], COLD_W)
    L.draw_field(d, x, ly, "Cold:", data["cold"], 3, L.f_leg, L.COL_TXT)
    x += COLD_W + GAP
    # 2) 有効Band = 映像(Raw色) + 貯蓄(Buf色) + 音声/その他ヘッダ(dim色)。バー幅=ラベル幅。単位 KiB/sec
    stacked([(data["raw_bytes"], L.CAT_RAW), (data["buf_bytes"], L.CAT_BUF),
             (data["ovh_bytes"], L.COL_OVH)], data["cd1x_bpf"], BAND_W)
    xb = L.draw_field(d, x, ly, "Band:", data["band_kbps"], 3, L.f_leg, L.COL_TXT)
    d.text((xb, ly), "KiB/sec", fill=L.COL_DIM, font=L.f_leg)
    x += BAND_W + GAP
    # 3) Tank = 実payload RINGの現在残量(violet)。ラベルは現在数のみ、バー幅=ラベル幅
    stacked([(data["buf_rem"], L.CAT_BUF)], data["buf_cap"], TANK_W)
    L.draw_field(d, x, ly, "Tank:", data["buf_rem"], 5, L.f_leg, L.COL_TXT)
    x += TANK_W + GAP
    # 4) payload RING増減の指針器(中央薄線・減=左赤/増=右青)。フルスケール=描画範囲タイル数-最大Raw数
    L.draw_tank_delta(d, x, by, BH, ly, BUFF_W, data["tank_delta"], max(1, C - data["max_raw"]))
    x += BUFF_W + GAP
    # 5) DMA = 今フレームの32Bパターンタイル数
    fillw = int(DMA_W * min(dval, dmax) / max(dmax, 1)); over = dval > dmax
    d.rectangle([x, by, x + fillw, by + BH], fill=(220, 130, 60) if over else L.COL_DMA)
    if over:
        d.rectangle([x + fillw, by, x + DMA_W, by + BH], fill=(150, 60, 60))
    d.rectangle([x, by, x + DMA_W, by + BH], outline=L.COL_FRAME_IN)
    L.draw_field(d, x, ly, "DMA:", dval, L.dma_value_digits(C), L.f_leg, L.COL_TXT)
    x += DMA_W + GAP

    # 6) Run = playerのcold-run record数。フル=1tile/runの理論最悪ケース。
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
    cu = {"Same": int(Same_u[i]), "Near": int(Near_u[i]), "Coa": int(Coa_u[i]),
          "Flbk": int(Flbk_u[i]), "Raw": cn["Raw"], "Buf": cn["Buf"], "Miss": cn["Miss"]}
    return dict(C=C, counts=cn, counts_uniq=cu, fps=FPS, win=WIN,
                mode=MODE, res=RES, audio=AUDIO_STR, avg_kbps=AVG_KBPS,
                req=int(Want[i]), budget=BUDGET,
                comp=cn["Same"] + cn["Near"] + cn["Coa"] + cn["Flbk"],
                buf_cap=BUF_CAP, buf_rem=int(BUF_REM[i]),
                dma_tiles=int(DMA_TILES[i]), dma_runs=int(DMA_RUNS[i]),
                raw_bytes=int(RAW_BYTES[i]), buf_bytes=int(BUF_BYTES[i]), ovh_bytes=int(OVH_BYTES[i]),
                band_kbps=int(BAND[i]), cd1x_bpf=CD1X_BPF,
                cold=cn["Raw"] + cn["Buf"], cold_raw=cn["Raw"], cold_buf=cn["Buf"],
                cold_cap=L.av_config.cold_cap_for_fps(FPS, MODE),
                tank_delta=int(TANK_DELTA[i]), max_raw=MAX_RAW,
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
    _a = sorted(glob.glob(f"{SIM}/audio_*.wav"))     # 音声形式によりファイル名が変わる(PCM/ADPCM)
    audio = _a[0] if _a else f"{SIM}/audio_13k3_u8_mono.wav"
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
