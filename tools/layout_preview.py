#!/usr/bin/env python3
"""解析フレーム(1920x1080)の新レイアウトを『ダミー値』で1枚だけ描くプレビュー。
sim/ffmpeg を回さず秒で反復するためのもの。本ファイルがレイアウトの正で、
render_analysis.py が同じ描画関数と定数を実データに使う。

新レイアウト(この版):
  左  = SEGA-CD sim output(4:3枠) + 下に status帯
  右  = Source / Category(Miss赤塗り) / 全編カテゴリ合計 / Audio波形
  下  = Req/Cold/Band/DMA/Run/Prg/Wrd/Pre、パレット、4段タイムライン
        ※ Miss&MissCarryパネルと per-metric flow は廃止。
出力: tmp/layout_preview.png

usage: python3 tools/layout_preview.py
"""
import math
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import av_config   # cold_cap_for_fps (Coldバーのフルスケール)
import analysis_style as style
import stream_schedule

FONT = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
CW, CH = 1920, 1080
BG = (12, 12, 12)

# ---- Canonical analysis colours and category styles ----
# Keep these aliases for callers that use layout_preview as the layout API.
CAT_RAW = style.CAT_RAW
CAT_SAME = style.CAT_SAME
CAT_NEAR = style.CAT_NEAR
CAT_MISS = style.CAT_MISS
CAT_FLBK = style.CAT_FLBK
CAT_DEDUP = style.CAT_DEDUP
CAT_PREFETCH = style.CAT_PREFETCH

COL_BORDER = (200, 200, 200)
COL_FRAME_IN = (70, 70, 70)
COL_TXT = (225, 225, 225)
COL_DIM = (150, 150, 155)
COL_OVH = style.COL_OVH
COL_DMA = style.COL_DMA
COL_RUN = style.COL_RUN
COL_PRG = style.COL_PRG
COL_WR1 = style.COL_WR1
COL_WR0 = style.COL_WR0
COL_WRD = style.COL_WRD
COL_DIC = style.COL_DIC
SUPPLY_COLORS = style.SUPPLY_COLORS
DISPLAY_SOURCE_ORDER = style.DISPLAY_SOURCE_ORDER
METER_SUPPLY_ORDER = style.METER_SUPPLY_ORDER

# Category is now based on display-time behavior and physical supply. Raw is a
# same-frame CD load used immediately. The four supply categories replace the
# old encoder-funding class Buf. Prefetch is not visible yet, so it has only a
# status meter and becomes Same if the resident pattern is used later.
CATS = style.CATS
LEGEND_CATS = style.LEGEND_CATS
DISP = {name: name for name, _ in CATS}
DISP["Wrd"] = "Wrd"
REQ_TIMELINE_CATS = style.REQ_TIMELINE_CATS

# ---- レイアウト定数(枠 = [x0,y0,x1,y1]) ----
PAD = 11
MAIN_FRAME = (40, 52, 1267, 978)      # 左大枠(4:3黒帯)。video (51,63) 1205x904
# 右列: Source(4:3) / 凡例リスト / Category(4:3) / 凡例合計 / 音声波形。左隙間=main直後≈1タイル。
# パネルを左に広げ幅577(=433高, 4:3維持)。大きくした分 Audio(波形)を薄く(32)。
_RCX = 1310                                 # main枠(右端1267)の直後≈43px(sim約1タイル程度)
SRC_FRAME  = (_RCX, 52, 1877, 477)          # Source 4:3 (567x425, outputと同じ比率)
CATLEG_XY  = (_RCX, 483); CATLEG_W, CATLEG_H = 567, 44   # 凡例リスト = Categoryの上
CAT_FRAME  = (_RCX, 533, 1877, 958)         # Category 4:3 (567x425)
CATTOT_XY  = (_RCX, 962); CATTOT_W, CATTOT_H = 567, 24   # 凡例合計(バー高さ1/3に縮小)= Categoryの下
WAVE_FRAME = (_RCX, 1014, 1877, 1064)       # 音声波形(高さ50)。見出しは小フォントで合計バーとの間にmargin
# GRAPH_FRAME(per-metric flow = metricパネル)は廃止
STATUS_XY = (40, 982); STATUS_W, STATUS_H = 1227, 84   # メイン枠(下端978)に寄せる(margin詰め)

# ---- 画面モード表(汎用) ----
# sw,sh = 可視画素 / active = アクティブ表示行 / bpl = 1行あたりのblanking DMA(B) /
# par = 1ドットの横長比。1VBLANK DMA理論値 = bpl × (262 - active) [NTSC 262行]。
# 表示アスペクト = sw × par / sh。低アクティブ行モードほどVBLANKが増えDMA理論値が上がる。
MODES = {
    "H32":   dict(sw=256, sh=224, active=224, bpl=167, par=8 / 7),   # 64:49, 6346 B/VBLANK
    "H40":   dict(sw=320, sh=224, active=224, bpl=205, par=32 / 35), # 64:49, 7790 B/VBLANK
    "mode4": dict(sw=256, sh=192, active=192, bpl=167, par=1.167),  # 14:9, 11690 B/VBLANK
}


def dma_vblank(mode):
    m = MODES[mode]
    return m["bpl"] * (262 - m["active"])


def screen_aspect(mode):
    m = MODES[mode]
    return m["sw"] * m["par"] / m["sh"]


def dma_frame_max(mode, fps):
    """1コマで転送できる理論値 = (1コマ内のVBLANK数=60/fps) × 1VBLANK理論値。
    15fps→4×, 30fps→2×, 24fps→2.5×。"""
    return int(round(60.0 / fps * dma_vblank(mode)))


def dma_tile_capacity(mode, fps, cells):
    """Pattern-tile DMA ceiling after the fixed full name table is paid.

    The byte ceiling includes the per-frame 2-byte name entry for every drawn
    cell. The remainder is available to 32-byte pattern tiles. A frame cannot
    transfer more pattern tiles than it draws.
    """
    cells = int(cells)
    pattern_bytes = max(0, dma_frame_max(mode, fps) - cells * 2)
    return min(cells, pattern_bytes // 32)


def dma_value_digits(cells):
    """Digits needed by the timed DMA tile count for this raster."""
    return len(str(max(0, int(cells))))


def timed_metric_value(frame, value):
    """Hide untimed frame-0 boot work from timing/load meters."""
    return int(value) if int(frame) > 0 else 0


def dma_run_worst_case(dma_tiles):
    """Theoretical worst case: one isolated cold-run record per tile."""
    return max(0, int(dma_tiles))


H40_FULL_TILES = (MODES["H40"]["sw"] // 8) * (MODES["H40"]["sh"] // 8)
DMA_RUN_DIGITS = len(str(dma_run_worst_case(H40_FULL_TILES)))  # 1120 -> 4桁


def dma_label_template(cells):
    return "DMA:" + "0" * dma_value_digits(cells)


def run_label_template():
    return "Run:" + "0" * DMA_RUN_DIGITS

f_head = None; f_leg = None; f_lbl = None; f_sm = None; f_meta = None; f_pal = None


def dummy_data():
    """レイアウト確認用の適当な値。実データもこの形に合わせれば同じ render で描ける。"""
    import random
    random.seed(7)
    C = 396                                   # 総セル(例: SonicJam 22x18)
    # Mutually exclusive per-frame displayed-cell counts. The four physical
    # sources replace the old Buf funding class.
    counts = {
        "Raw": 90, "Same": 130, "Near": 40, "Flbk": 65,
        "Miss": 5, "Prg": 30, "Wr0": 15, "Wr1": 12, "Dic": 9,
    }
    # 線グラフ用: 前後4秒×fps の各指標時系列(中央=現在)
    fps = 30; win = 4
    n = win * fps * 2 + 1
    series = {}
    for k, _ in CATS:
        base = counts[k]
        series[k] = [max(0, base + int(30 * math.sin(i / 7.0 + hash(k) % 7)) + random.randint(-12, 12))
                     for i in range(n)]
    supply_capacities = {"Prg": 12416, "Wr0": 880, "Wr1": 880}
    # 全編タイムライン用の時系列(ダミー): Miss多発帯とPrgBuf枯渇帯を作り込む
    tln = 360
    tl = {}
    for k in REQ_TIMELINE_CATS:
        b = counts[k]
        tl[k] = [max(0, int(b + 22 * math.sin(i / 11.0 + hash(k) % 5) + random.randint(-8, 8))) for i in range(tln)]
    miss_zones = lambda i: (80 <= i <= 112) or (248 <= i <= 276)
    for i in range(tln):
        if miss_zones(i):
            tl["Miss"][i] += random.randint(25, 70)
            tl["Prg"][i] += random.randint(15, 35)
    prg_rem = []; r = supply_capacities["Prg"]
    for i in range(tln):
        r += (-350 if miss_zones(i) else 260) * -1   # miss帯=枯渇へ / それ以外=補充
        r = max(0, min(supply_capacities["Prg"], r + random.randint(-40, 40)))
        prg_rem.append(r)
    supply_series = {
        "Prg": prg_rem,
        "Wr0": [max(0, 820 - i * 2) for i in range(tln)],
        "Wr1": [max(0, 760 - i * 2) for i in range(tln)],
    }
    # BODY物理配送slotのダミー。padを含む物理bytesが各slotのCD実時間を決める。
    body_payload_tl = [
        5600 if i % 47 == 0 else max(0, 3200 + int(900 * math.sin(i / 13.0)))
        for i in range(tln)
    ]
    body_raw_payload_tl = [
        int(payload * (0.58 + 0.12 * math.sin(i / 17.0)))
        for i, payload in enumerate(body_payload_tl)
    ]
    body_prg_payload_tl = [
        payload - raw
        for payload, raw in zip(body_payload_tl, body_raw_payload_tl)
    ]
    body_control_tl = [720 + (160 if i % 31 == 0 else 0) for i in range(tln)]
    body_physical_tl = [5 * 2048 for _ in range(tln)]
    run_tl = [
        max(0, min(96, 18 + int(11 * math.sin(i / 9.0))
                   + (28 if miss_zones(i) else 0)))
        for i in range(tln)
    ]
    body_payload_bytes = body_payload_tl[0]
    body_raw_payload_bytes = body_raw_payload_tl[0]
    body_prg_payload_bytes = body_prg_payload_tl[0]
    body_control_bytes = body_control_tl[0]
    body_useful_tl = [p + c for p, c in zip(body_payload_tl, body_control_tl)]
    body_physical_bytes = body_physical_tl[0]
    band_kbps = int(stream_schedule.body_delivery_rate_bps(
        [body_payload_bytes + body_control_bytes], [body_physical_bytes])[0] // 1024)
    avg_kbps = int(round(stream_schedule.average_body_delivery_rate_bps(
        body_useful_tl, body_physical_tl) / 1024))
    pl_info = {"Prev": dict(pl=11, frame=980), "Current": dict(pl=12, frame=1122),
               "Next": dict(pl=13, frame=1544)}   # 各パレットの番号と切替開始フレーム
    pl_cur, pl_total = 12, 13                       # 現在パレット番号 / 総数(最大番号)
    # パレット状態(4面×15色) Prev/Current/Next の3セット
    def _pal():
        return [[(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) for _ in range(15)]
                for _ in range(4)]
    palettes = {"Prev": _pal(), "Current": _pal(), "Next": _pal()}
    # カテゴリ合計(全編累積のダミー)。unique counts are no longer displayed.
    cat_totals = {k: counts[k] * (200 + (hash(k) % 90)) for k, _ in CATS}
    prefetch = 18
    displayed_cold = sum(counts[name] for name in ("Raw",) + DISPLAY_SOURCE_ORDER)
    dma_tiles = displayed_cold + prefetch
    return dict(C=C, counts=counts, series=series, fps=fps, win=win,
                palettes=palettes, cat_totals=cat_totals,
                body_raw_payload_bytes=body_raw_payload_bytes,
                body_prg_payload_bytes=body_prg_payload_bytes,
                body_payload_bytes=body_payload_bytes,
                body_control_bytes=body_control_bytes,
                body_physical_bytes=body_physical_bytes,
                band_kbps=band_kbps, body_payload_tl=body_payload_tl,
                body_raw_payload_tl=body_raw_payload_tl,
                body_prg_payload_tl=body_prg_payload_tl,
                body_control_tl=body_control_tl, body_physical_tl=body_physical_tl,
                run_tl=run_tl,
                pl_info=pl_info, pl_cur=pl_cur, pl_total=pl_total,
                mode="H32", res="176x144 (22x18)", audio="13.3kHz mono 8bit PCM", avg_kbps=avg_kbps,
                src_spec="256x224 / 30fps / AAC 48kHz stereo",
                req=246, miss=counts["Miss"],
                budget=273,
                comp=counts["Same"] + counts["Near"] + counts["Flbk"],
                supply_capacities=supply_capacities,
                supply_remaining={name: values[126] for name, values in supply_series.items()},
                cold=displayed_cold + prefetch, cold_prefetch=prefetch,
                prefetch_cap=32,
                cold_cap=av_config.cold_cap_for_fps(fps, "H32", 896),
                dma_tiles=dma_tiles, dma_runs=23,
                tl=tl, supply_series=supply_series, tln=tln,
                time_s=42.0, frame=1260, total_frames=2712)


def panel(d, rect, title=None):
    if title:
        d.text((rect[0] + 2, rect[1] - 42), title, fill=COL_TXT, font=f_head)
    d.rectangle(list(rect), outline=COL_BORDER)


def _w(font, s):
    return font.getbbox(s)[2] if s else 0


def draw_padnum(d, x, y, value, width, font, col):
    """value を width 桁ゼロ埋めして同じ文字色で描く。返り値=描画後x。"""
    s = str(int(value)).rjust(width, "0")
    d.text((x, y), s, fill=col, font=font); x += _w(font, s)
    return x


def draw_field(d, x, y, label, value, width, font, col, maxval=None, maxwidth=None, suffix=""):
    """'label' + ゼロ埋め値 (+ '/' + ゼロ埋めmax) + suffix。桁固定でずれない。"""
    d.text((x, y), label, fill=col, font=font); x += _w(font, label)
    x = draw_padnum(d, x, y, value, width, font, col)
    if maxval is not None:
        d.text((x, y), "/", fill=col, font=font); x += _w(font, "/")
        x = draw_padnum(d, x, y, maxval, maxwidth or width, font, col)
    if suffix:
        d.text((x, y), suffix, fill=COL_DIM, font=font); x += _w(font, suffix)
    return x


def meter_widths(cells):
    """Each bar follows its label width.

    Returns Band, Prg, Wrd, DMA, and Run widths.
    """
    return (_w(f_leg, "Band:000") + 3,
            _w(f_leg, "Prg:00000") + 3,
            _w(f_leg, "Wrd:0000") + 3,
            _w(f_leg, dma_label_template(cells)) + 3,
            _w(f_leg, run_label_template()) + 3)


def dummy_image(w, h, seed):
    """ダミーの映像(グラデ＋ブロック)。"""
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(0, w, 1):
            px[x, y] = ((x * 255 // w + seed) % 256, (y * 255 // h) % 256, ((x + y + seed) % 256))
    return im


def draw_catmap(w, h, data):
    """カテゴリマップ: ダミータイル格子。
    Raw=thin black/white dashed frame / Same=no frame / Near/Flbk=thin /
    Dic/Prg/Wr=thin colour-and-black dash / Miss=red fill."""
    im = Image.new("RGB", (w, h), (18, 18, 18))
    d = ImageDraw.Draw(im)
    cols, rows = 22, 18
    tw, th = w / cols, h / rows
    import random
    random.seed(3)
    cats = [c for c, _ in CATS]
    for r in range(rows):
        for c in range(cols):
            x0, y0 = int(c * tw), int(r * th)
            x1, y1 = int((c + 1) * tw) - 1, int((r + 1) * th) - 1
            k = random.choices(
                cats, weights=[24, 34, 8, 15, 2, 7, 4, 3, 3])[0]
            if k == "Miss":
                style.draw_category_border(d, (x0, y0, x1, y1), k)
                continue
            # 内容(色ブロック)を塗る
            d.rectangle([x0, y0, x1, y1], fill=((c * 11 + r * 7) % 256, (r * 13) % 256, (c * 17) % 256))
            style.draw_category_border(d, (x0, y0, x1, y1), k)
    return im


def swatch(d, x, y, sw, name, col):
    """Legend swatch mirroring the category-map border/fill semantics."""
    del col
    style.draw_category_swatch(d, (x, y, x + sw, y + sw), name)


def draw_legend(w, h, data):
    """Five-column, two-row legend with one displayed-cell count per item."""
    im = Image.new("RGB", (w, h), (14, 14, 14))
    d = ImageDraw.Draw(im)
    per_row = 5
    cw = w // per_row
    sw = 14
    for i, (name, col) in enumerate(LEGEND_CATS):
        row = i // per_row; c = i % per_row
        x = c * cw + 6; y = row * (h // 2) + (h // 2 - sw) // 2
        swatch(d, x, y, sw, name, col)
        tx = x + sw + 6
        label = DISP[name] + ":"
        count = (data["counts"]["Wr0"] + data["counts"]["Wr1"]
                 if name == "Wrd" else data["counts"][name])
        draw_field(d, tx, y - 1, label, count, 3, f_leg, COL_TXT)
    return im


def draw_graph(w, h, data):
    """流れる線グラフ: 前後win秒。中央=現在(固定)。各指標を凡例色の線で。左へ流れる。"""
    im = Image.new("RGB", (w, h), (16, 16, 16))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w - 1, h - 1], outline=COL_FRAME_IN)
    ymax = data["C"]
    n = data["fps"] * data["win"] * 2 + 1
    mid = n // 2
    # グリッド(横=時間目盛 -4..+4s, 縦=軽い)
    for s in range(-data["win"], data["win"] + 1):
        x = int((s + data["win"]) / (2 * data["win"]) * (w - 1))
        col = (60, 60, 60) if s != 0 else (200, 200, 200)
        d.line([(x, 0), (x, h - 1)], fill=col)
        d.text((x + 2, h - 14), f"{s:+d}s" if s else "now", fill=COL_DIM, font=f_sm)
    # 各指標の線
    for name, col in CATS:
        ser = data["series"][name]
        pts = []
        for i in range(n):
            x = int(i / (n - 1) * (w - 1))
            y = int((h - 2) - min(ser[i], ymax) / ymax * (h - 4))
            pts.append((x, y))
        d.line(pts, fill=col, width=1)
    # 中央プレイヘッド(既に上でnow線)。右上にラベル
    d.text((6, 4), "per-metric flow (±%ds, now=center, scroll left)" % data["win"], fill=COL_DIM, font=f_sm)
    return im


def draw_status(w, h, data):
    """status帯: Req / Cold / Band / DMA / Run / Prg / Wrd / Pre + timeline。
    数値は同じ文字色のゼロ埋めで桁固定。Tank/BufメーターとMissCarryは廃止。"""
    im = Image.new("RGB", (w, h), (16, 16, 16))
    d = ImageDraw.Draw(im)
    by, BH = 8, 16                   # 上マージンを半分(16→8)。タイムラインもこのbyから始まり下端は据置=縦に伸びる
    C = data["C"]
    GAP = 16
    REQ_W = _w(f_leg, "Req:000  Miss:000") + 3
    dmax = dma_tile_capacity(data["mode"], data["fps"], C)
    dval = data["dma_tiles"]
    # メーター幅の統一を廃止=各バーは自分のラベル幅
    BAND_W, PRG_W, WRD_W, DMA_W, RUN_W = meter_widths(C)
    COLD_W = _w(f_leg, "Cold:000") + 3
    PRE_W = _w(f_leg, "Pre:000") + 3
    ly = by + BH + 3
    x = 4

    def stacked(segs, full, bw):
        px = x
        for val, col in segs:
            seg = int(bw * min(val, full) / full)
            seg = min(seg, x + bw - px)              # 積み上げ合計が枠幅を超えない(はみ出し防止)
            if seg > 0:
                d.rectangle([px, by, px + seg, by + BH], fill=col); px += seg
        d.rectangle([x, by, x + bw, by + BH], outline=COL_FRAME_IN)

    # 1) Req = mutually-exclusive displayed categories; headline values only.
    stacked([(data["counts"][k], dict(CATS)[k]) for k, _ in CATS], C, REQ_W)
    bx = x + int(REQ_W * data["budget"] / C)
    d.line([bx, by - 2, bx, by + BH + 2], fill=style.COL_LIMIT)
    xq = draw_field(d, x, ly, "Req:", data["req"], 3, f_leg, COL_TXT)
    draw_field(d, xq + 8, ly, "Miss:", data["miss"], 3, f_leg, COL_TXT)
    x += REQ_W + GAP
    # 2) Cold = same-frame exact loads by physical source + future prefetch.
    cold_parts = [(data["counts"]["Raw"], CAT_RAW)]
    cold_parts += [
        (data["counts"][name], SUPPLY_COLORS[name])
        for name in DISPLAY_SOURCE_ORDER
    ]
    cold_parts.append((data["cold_prefetch"], CAT_PREFETCH))
    stacked(cold_parts, data["cold_cap"], COLD_W)
    draw_field(d, x, ly, "Cold:", data["cold"], 3, f_leg, COL_TXT)
    x += COLD_W + GAP
    # 3) Band = Raw payload + Prg charge + control; no pad/Header.
    stacked([(data["body_raw_payload_bytes"], CAT_RAW),
             (data["body_prg_payload_bytes"], COL_PRG),
             (data["body_control_bytes"], COL_OVH)],
            max(data["body_physical_bytes"], 1), BAND_W)
    d.line(
        [x + BAND_W, by - 2, x + BAND_W, by + BH + 2],
        fill=style.COL_BAND_LIMIT,
    )
    draw_field(d, x, ly, "Band:", data["band_kbps"], 3, f_leg, COL_TXT)
    x += BAND_W + GAP
    # 4) DMA = 今フレームの32Bパターンタイル数。フル=モード/fpsの理論DMAから全NT分を引いた枚数。
    fillw = int(DMA_W * min(dval, dmax) / max(dmax, 1)); over = dval > dmax
    d.rectangle(
        [x, by, x + fillw, by + BH],
        fill=style.COL_OVER if over else COL_DMA,
    )
    if over:
        d.rectangle(
            [x + fillw, by, x + DMA_W, by + BH],
            fill=style.COL_OVER_REMAINDER,
        )
    d.rectangle([x, by, x + DMA_W, by + BH], outline=COL_FRAME_IN)
    draw_field(d, x, ly, "DMA:", dval, dma_value_digits(C), f_leg, COL_TXT)
    x += DMA_W + GAP

    # 5) Run = playerのcold-run record数。CPU/DMA転送方式にかかわらず1tile/runが理論最悪。
    run_val = int(data["dma_runs"]); run_max = dma_run_worst_case(dval)
    run_fill = (max(1, int(RUN_W * min(run_val, run_max) / run_max))
                if run_val > 0 and run_max > 0 else 0)
    d.rectangle([x, by, x + run_fill, by + BH],
                fill=CAT_MISS if run_val > run_max else COL_RUN)
    d.rectangle([x, by, x + RUN_W, by + BH], outline=COL_FRAME_IN)
    draw_field(d, x, ly, "Run:", run_val, DMA_RUN_DIGITS, f_leg, COL_TXT)
    x += RUN_W + GAP

    # 6) Physical supply meters. WordBuf banks stay separate internally but
    # are shown as one Wrd value and bar.
    prg_remaining = data["supply_remaining"]["Prg"]
    prg_capacity = data["supply_capacities"]["Prg"]
    stacked([(prg_remaining, COL_PRG)], prg_capacity, PRG_W)
    draw_field(d, x, ly, "Prg:", prg_remaining, 5, f_leg, COL_TXT)
    x += PRG_W + GAP

    wrd_remaining = (
        data["supply_remaining"]["Wr0"] + data["supply_remaining"]["Wr1"])
    wrd_capacity = (
        data["supply_capacities"]["Wr0"] + data["supply_capacities"]["Wr1"])
    stacked([(wrd_remaining, COL_WRD)], wrd_capacity, WRD_W)
    draw_field(d, x, ly, "Wrd:", wrd_remaining, 4, f_leg, COL_TXT)
    x += WRD_W + GAP

    # 7) Pre is future exact work and remains visually separate from supply.
    stacked([(data["cold_prefetch"], CAT_PREFETCH)], data["prefetch_cap"], PRE_W)
    draw_field(d, x, ly, "Pre:", data["cold_prefetch"], 3, f_leg, COL_TXT)
    x += PRE_W + GAP

    # メーターの下: パレット Prev/Current/Next(PL/Frame見出し)
    meters_right = x - GAP
    py0 = ly + 16
    draw_palettes_strip(d, 4, py0, meters_right - 4, (h - 2) - py0, data["palettes"], data.get("pl_info"))

    # 8) Four-row timeline: request / supply / physical runs / BODY.
    # The old BODY quarter is split equally between Run and Band.
    x_tl = x
    tlw = w - 4 - x_tl
    if tlw > 20:
        tl = data["tl"]; supply = data["supply_series"]; tln = data["tln"]
        payload_tl = data["body_payload_tl"]
        raw_payload_tl = data["body_raw_payload_tl"]
        control_tl = data["body_control_tl"]
        physical_tl = data["body_physical_tl"]
        run_tl = data["run_tl"]
        tlh = (h - 2) - by
        H_req = tlh // 2                          # Req = 2
        H_supply = tlh // 4                       # supply = 1
        H_bottom = tlh - H_req - H_supply
        H_run = H_bottom // 2
        H_band = H_bottom - H_run
        y_req = by
        y_supply = y_req + H_req
        y_run = y_supply + H_supply
        y_band = y_run + H_run
        # 各段の背景を極暗色で塗る(下段の空きが純黒=marginに見えないように)
        d.rectangle([x_tl, y_supply, x_tl + tlw, y_supply + H_supply], fill=(21, 22, 28))
        d.rectangle([x_tl, y_run, x_tl + tlw, y_run + H_run],
                    fill=(27, 24, 17))
        d.rectangle([x_tl, y_band, x_tl + tlw, y_band + H_band],
                    fill=(18, 26, 20))
        stack_order = [(name, dict(CATS)[name]) for name in REQ_TIMELINE_CATS]
        for col_i in range(tlw):
            fi = min(int(col_i / tlw * tln), tln - 1)
            X = x_tl + col_i
            yb = y_req + H_req                    # 上段: Reqヒートマップ(下から積む)
            for k, col in stack_order:
                seg = int(H_req * tl[k][fi] / C)
                if seg > 0:
                    d.line([(X, yb - seg), (X, yb)], fill=col); yb -= seg
            ys = y_supply + H_supply
            total_capacity = max(
                sum(data["supply_capacities"][name]
                    for name in METER_SUPPLY_ORDER), 1)
            for name in METER_SUPPLY_ORDER:
                hs = int(H_supply * supply[name][fi] / total_capacity)
                if hs > 0:
                    d.line([(X, ys - hs), (X, ys)], fill=SUPPLY_COLORS[name])
                    ys -= hs
            run_max = max(data["cold_cap"], 1)
            hr = int(H_run * min(run_tl[fi], run_max) / run_max)
            if hr > 0:
                d.line([(X, y_run + H_run - hr), (X, y_run + H_run)],
                       fill=COL_RUN)
            physical = max(physical_tl[fi], 1)
            hrw = int(H_band * raw_payload_tl[fi] / physical)
            hp = int(H_band * payload_tl[fi] / physical)
            if hrw > 0:
                d.line([(X, y_band + H_band - hrw),
                        (X, y_band + H_band)], fill=CAT_RAW)
            if hp > hrw:
                d.line([(X, y_band + H_band - hp),
                        (X, y_band + H_band - hrw)], fill=COL_PRG)
            hc = int(H_band * (payload_tl[fi] + control_tl[fi]) / physical)
            if hc > hp:
                d.line([(X, y_band + H_band - hc),
                        (X, y_band + H_band - hp)], fill=COL_OVH)
        d.line([x_tl, y_run, x_tl + tlw, y_run], fill=(110, 105, 70))
        d.line([x_tl, y_band, x_tl + tlw, y_band], fill=(110, 105, 70))
        d.rectangle([x_tl, by, x_tl + tlw, by + tlh], outline=COL_FRAME_IN)
        head = x_tl + int(tlw * data["frame"] / data["total_frames"])
        d.line([head, by, head, by + tlh], fill=(255, 255, 255))
    return im


def draw_palettes_strip(d, x0, y0, w, h, palettes, pl_info=None):
    """Prev/Current/Next の3パレットセットを横並び。各セット=4面15色を2行(各行2面=30色)に。
    pl_info={'Prev':{'pl':,'frame':},...} を渡すと 'Prev PL:xxx Frame:xxxxx' を見出しにする。"""
    names = ["Prev", "Current", "Next"]
    setw = w // 3
    for si, nm in enumerate(names):
        sx = x0 + si * setw
        pal = palettes[nm]                                   # 4面×15色 (Noneなら前後にパレット無し)
        pad = 2 if nm == "Current" else 0                    # Currentは1ドットほどpadding+枠
        per_row = 30                                         # 2面=30色/行
        gw = setw - 10 - 2 * pad
        sw = gw / per_row
        cell = sw                                            # 正方形タイル(高さ=幅)
        grid_h = 2 * cell
        gyc = y0 + h - grid_h - pad                          # タイルは下寄せ
        if pal is None:                                      # 前後にパレット無し=タイルはブランク
            d.text((sx, gyc - 15), "%s -" % nm, fill=COL_DIM, font=f_pal)
            continue
        pli = pl_info.get(nm) if pl_info else None
        lab = "%s PL:%03d Frame:%05d" % (nm, pli["pl"], pli["frame"]) if pli else nm
        d.text((sx, gyc - 15), lab, fill=COL_TXT, font=f_pal)  # 明るく・少し大きく。タイル直上に詰める
        for r in range(2):
            line = pal[r * 2] + pal[r * 2 + 1]               # 2面連結
            for ci, col in enumerate(line):
                cx = sx + pad + ci * sw
                cy = gyc + r * cell
                d.rectangle([cx, cy, cx + sw, cy + cell - 1], fill=col)
        if nm == "Current":
            d.rectangle([sx - 1, gyc - pad, sx + pad + gw + 1, gyc + grid_h + pad], outline=COL_BORDER)


def draw_cattotals(w, h, data):
    """metricパネルの下: カテゴリ合計の積み上げ横棒 + 直上に1行のラベルなし凡例(四角+合計数, バー寄り)。
    左右=パネル幅いっぱい(右列パネルと揃う), 下端=ヒートマップタイムライン下端(abs y1064)に合わせる。
    左右縦ガイドライン + 下横ガイドライン。"""
    im = Image.new("RGB", (w, h), (16, 16, 16))
    d = ImageDraw.Draw(im)
    tot = data["cat_totals"]
    total = max(1, sum(tot.values()))
    bar_l, bar_r = 0, w - 1
    ly = 2                                    # 凡例行(四角+合計数)を上端へ寄せる=Category直下
    bar_top = ly + 14                         # バーは凡例行の直下
    bar_bottom = bar_top + 6                  # バー高さ=1/3(旧18→6)
    px = bar_l                               # 積み上げ横棒(全幅=総計)
    for name, col in CATS:
        seg = int((bar_r - bar_l) * tot[name] / total)
        if seg > 0:
            d.rectangle([px, bar_top, px + seg, bar_bottom], fill=col); px += seg
    d.rectangle([bar_l, bar_top, bar_r, bar_bottom], outline=COL_FRAME_IN)
    # 等間隔でなく左から書き連ねる(全編固定値・重なり防止)。数字は四角の下線にベースラインを揃える
    x = 6
    ty = ly + 11 - f_sm.getmetrics()[0]      # 四角(ly..ly+11)の下線にベースラインを合わせる
    for name, col in LEGEND_CATS:
        swatch(d, x, ly, 11, name, col); x += 11 + 5
        value = tot["Wr0"] + tot["Wr1"] if name == "Wrd" else tot[name]
        s = str(value)                       # 合計値のみ(ユニーク数併記は廃止)
        d.text((x, ty), s, fill=COL_TXT, font=f_sm); x += _w(f_sm, s)
        x += 14                              # 項目間ギャップ
    GL = (85, 85, 92)                        # 下横ガイドラインのみ(タイムライン下端に合わせる)。
    d.line([(bar_l, bar_bottom), (bar_r, bar_bottom)], fill=GL)   # metric↔バーの縦線は削除
    return im


def load_fonts():
    """フォントを読み込みモジュールグローバルへ。layout/comparison 両プレビュー共通。"""
    global f_head, f_leg, f_lbl, f_sm, f_meta, f_pal
    f_head = ImageFont.truetype(FONT, 33)
    f_leg = ImageFont.truetype(FONT, 15)
    f_lbl = ImageFont.truetype(FONT, 20)
    f_sm = ImageFont.truetype(FONT, 12)
    f_meta = ImageFont.truetype(FONT, 18)
    f_pal = ImageFont.truetype(FONT, 14)


def draw_footer(cv, data):
    """Analysis / Comparison 共通フッター。上部レイアウトを差し替えても使い回せる。
    status帯(Req/Cold/Band/DMA/Run/Prg/Wrd/Pre + palettes + timelines)と
    カテゴリ合計バーを、共通の STATUS_XY / PAL_XY へ貼る。"""
    st = draw_status(STATUS_W, STATUS_H, data)
    cv.paste(st, STATUS_XY)
    ct = draw_cattotals(CATTOT_W, CATTOT_H, data)   # カテゴリ合計バー+凡例= Categoryの下へ
    cv.paste(ct, CATTOT_XY)


def draw_waveform_placeholder(w, h):
    """音声波形パネルのプレースホルダ: 前後2s のスクロール波形(中央=現在, 左へ流れる)。
    見出しは枠の外(上)に音声諸元。実装は render_analysis 側(実音声を反映)。"""
    im = Image.new("RGB", (w, h), (16, 16, 16))
    d = ImageDraw.Draw(im)
    mid = h // 2
    d.line([(0, mid), (w - 1, mid)], fill=(60, 60, 66))            # 振幅0の中央線
    for x in range(w):
        amp = (math.sin(x * 0.15) * 0.6 + math.sin(x * 0.045) * 0.4) * (0.45 + 0.55 * abs(math.sin(x * 0.017)))
        yy = int(abs(amp) * (h * 0.44))
        col = (150, 205, 150) if x < w // 2 else (95, 130, 95)     # 過去=明 / 未来=暗
        d.line([(x, mid - yy), (x, mid + yy)], fill=col)
    d.line([(w // 2, 0), (w // 2, h - 1)], fill=(230, 230, 235))   # 現在(now)線。読み方は見出しの後ろに記載
    return im


def main():
    load_fonts()

    data = dummy_data()
    cv = Image.new("RGB", (CW, CH), BG)
    d = ImageDraw.Draw(cv)

    # 枠。メイン枠上部テキスト(見出し/meta/Time・Frame)は共通ベースラインで下端を揃える。
    panel(d, MAIN_FRAME)
    BASE_Y = MAIN_FRAME[1] - 10                    # 上部テキストの共通ベースライン
    hx = MAIN_FRAME[0] + 2
    d.text((hx, BASE_Y), "SEGA-CD sim output", fill=COL_TXT, font=f_head, anchor="ls")
    meta = " / ".join([data["mode"], data["res"], data["audio"],
                       "%dfps" % data["fps"], "avg %d KiB/sec" % data["avg_kbps"]])
    d.text((hx + _w(f_head, "SEGA-CD sim output") + 12, BASE_Y), meta, fill=COL_DIM, font=f_meta, anchor="ls")
    panel(d, SRC_FRAME)          # 見出しは "Source" + ソース諸元(res/fps/音声)を小フォント併記
    _sby = SRC_FRAME[1] - 10; _sx = SRC_FRAME[0] + 2
    d.text((_sx, _sby), "Source", fill=COL_TXT, font=f_head, anchor="ls")
    d.text((_sx + _w(f_head, "Source") + 12, _sby), data["src_spec"], fill=COL_DIM, font=f_meta, anchor="ls")
    panel(d, CAT_FRAME)          # 見出し無し(ユーザー指定)
    panel(d, WAVE_FRAME)         # 音声波形枠。見出し=Audio + 諸元(Sourceと同じく小さく薄く)
    _ax = WAVE_FRAME[0] + 2; _ay = WAVE_FRAME[1] - 4
    d.text((_ax, _ay), "Audio", fill=COL_TXT, font=f_leg, anchor="ls")   # 小さめ+合計バーとmargin
    _sx = _ax + _w(f_leg, "Audio") + _w(f_sm, " ")   # 右スペース=半角1文字ぶん
    d.text((_sx, _ay), data["audio"], fill=COL_DIM, font=f_sm, anchor="ls")
    d.text((_sx + _w(f_sm, data["audio"]) + 14, _ay), "±2s, now=center, scroll left",
           fill=COL_DIM, font=f_sm, anchor="ls")   # 波形の読み方=見出しの後ろへ

    # 現在時間/フレーム番号: メイン枠の右上・枠外・右端揃え。ベースラインは見出しと共通
    ts = data["time_s"]
    f_tf = f_leg                                   # PL/Time/Frameは小さめ(15)。間隔は各1文字
    plw = max(2, len(str(data["pl_total"])))       # パレット総数の桁に合わせる(2桁以上の切替があれば増える)
    lab_t = "PL:%0*d/%0*d Time:%02d:%05.2f Frame:" % (plw, data["pl_cur"], plw, data["pl_total"],
                                                      int(ts // 60), ts % 60)
    fhex = "%04X" % data["frame"]                 # F番号=実機HUDと同じ16進4桁
    tw_all = _w(f_tf, lab_t) + _w(f_tf, fhex)
    tx = MAIN_FRAME[2] - tw_all
    ty = BASE_Y - f_tf.getmetrics()[0]            # ascentぶん上=ベースラインをBASE_Yへ
    d.text((tx, ty), lab_t, fill=COL_TXT, font=f_tf)
    d.text((tx + _w(f_tf, lab_t), ty), fhex, fill=COL_TXT, font=f_tf)

    # メイン映像: 実機同様、画面いっぱいに拡大せず、HAR込みの実機画面
    # (H32 256x224 = 64:49表示)へ中央配置する。
    # ダミーのコンテンツ解像度(例 22x18=176x144)を画面に中央配置する。
    cW, cH = 176, 144
    SW, SH = max(256, cW), max(224, cH)
    bw = MAIN_FRAME[2] - MAIN_FRAME[0] - 2 * PAD; bh = MAIN_FRAME[3] - MAIN_FRAME[1] - 2 * PAD
    scr = Image.new("RGB", (bw, bh), (0, 0, 0))    # 実機画面(4:3, パネルと同じ4:3なので全面)
    cw = round(bw * cW / SW); ch = round(bh * cH / SH)
    cx = round(bw * ((SW - cW) // 2) / SW); cy = round(bh * ((SH - cH) // 2) / SH)
    scr.paste(dummy_image(cw, ch, 30), (cx, cy))
    cv.paste(scr, (MAIN_FRAME[0] + PAD, MAIN_FRAME[1] + PAD))
    sv = dummy_image(SRC_FRAME[2] - SRC_FRAME[0] - 2 * PAD, SRC_FRAME[3] - SRC_FRAME[1] - 2 * PAD, 90)
    cv.paste(sv, (SRC_FRAME[0] + PAD, SRC_FRAME[1] + PAD))
    catv = draw_catmap(CAT_FRAME[2] - CAT_FRAME[0] - 2 * PAD, CAT_FRAME[3] - CAT_FRAME[1] - 2 * PAD, data)
    cv.paste(catv, (CAT_FRAME[0] + PAD, CAT_FRAME[1] + PAD))

    # 凡例リスト(Categoryパネルの「上」へ移動)
    leg = draw_legend(CATLEG_W, CATLEG_H, data)
    cv.paste(leg, CATLEG_XY)
    # VRAMパネル(プレースホルダ: 64KB=2048タイルのタイルマップ。実装は render_analysis 側)
    wv = draw_waveform_placeholder(WAVE_FRAME[2] - WAVE_FRAME[0] - 2, WAVE_FRAME[3] - WAVE_FRAME[1] - 2)
    cv.paste(wv, (WAVE_FRAME[0] + 1, WAVE_FRAME[1] + 1))   # padding無し(枠内1pxのみ)

    # 共通フッター(status帯 + カテゴリ合計バー)
    draw_footer(cv, data)

    out = Path("tmp/layout_preview.png")
    out.parent.mkdir(exist_ok=True)
    cv.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
