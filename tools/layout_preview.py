#!/usr/bin/env python3
"""解析フレーム(1920x1080)の新レイアウトを『ダミー値』で1枚だけ描くプレビュー。
sim/ffmpeg を回さず秒で反復するためのもの。本ファイルがレイアウトの正で、
render_analysis.py が同じ描画関数と定数を実データに使う。

新レイアウト(この版):
  左  = SEGA-CD sim output(4:3枠) + 下に status帯
  右  = Source / Category(Miss赤枠を内包) / [カテゴリ枠の下]凡例(2行) / 流れる線グラフ
        ※ Miss&MissCarryパネルは廃止。凡例の元位置(枠の上)は margin として残す。
  下右 = パレット状態パネル
出力: tmp/layout_preview.png

usage: python3 tools/layout_preview.py
"""
import math
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import av_config   # cold_cap_for_fps (Coldバーのフルスケール)

FONT = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
CW, CH = 1920, 1080
BG = (12, 12, 12)

# ---- カテゴリ色(sim.py と一致) ----
CAT_RAW   = (205, 205, 205)   # Raw   新規CD転送(塗り=枠なし内容)
CAT_SAME  = (150, 150, 158)   # Same  不変(塗り=枠なし内容)  ※旧Coaの色
CAT_NEAR  = (95, 115, 215)    # Near  近似で更新省略        ※旧Sameの色
CAT_FLBK  = (240, 150, 50)    # Flbk  Missのフォールバック(荒くても常駐で穴埋め)(オレンジ・太枠)
CAT_DEDUP = (0, 190, 175)     # Dedup(旧・表示では Same に畳む。互換用に定義だけ残す)
CAT_COA   = (45, 240, 70)     # Coa   粗い近似dedup            ※判別しやすい鮮やかな緑
CAT_BUF   = (175, 120, 235)   # Buf   PRG先読み(貯水池)
CAT_MISS  = (220, 70, 70)     # Miss  取りこぼし(赤・塗りつぶし)

# カテゴリマップ/凡例での描き方: fill=枠なし内容塗り, thick=太枠(px), 他=細枠(1px)
CAT_FILL = {"Raw", "Same", "Miss"}          # 塗り(▓)で表現(Missは赤塗り)
CAT_THICK = {"Flbk": 3, "Buf": 3}  # 太枠カテゴリと枠幅
# 凡例/線グラフで使う項目(順序=表示順)。1要素目=データキー
CATS = [("Raw", CAT_RAW), ("Same", CAT_SAME), ("Near", CAT_NEAR), ("Coa", CAT_COA),
        ("Flbk", CAT_FLBK), ("Buf", CAT_BUF), ("Miss", CAT_MISS)]
# 表示ラベルは全て4文字に揃える(桁揃え)。データキー -> 表示4文字
DISP = {"Raw": "Raw ", "Same": "Same", "Near": "Near", "Coa": "Coa ",
        "Flbk": "Flbk", "Buf": "Buff", "Miss": "Miss"}
# 常駐流用カテゴリ=「ユニーク数/総数」を併記(区別できる=何枚の別タイルを何セルで使い回したか)
UNIQ_CATS = {"Same", "Near", "Coa", "Flbk"}

COL_BORDER = (200, 200, 200)
COL_FRAME_IN = (70, 70, 70)
COL_TXT = (225, 225, 225)
COL_DIM = (150, 150, 155)
COL_OVH = (95, 110, 122)         # 有効Bandの「音声+その他ヘッダ」セグメント(くすんだ青灰)
COL_DMA = (70, 190, 90)          # DMAパターンタイル数(green)
COL_RUN = (215, 165, 65)         # cold pattern run分断度(amber)

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
    """Digits needed by the DMA tile count for this raster."""
    return len(str(max(0, int(cells))))


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
    # カテゴリ別カウント(1フレーム) と そのユニーク数(別タイル数)
    counts = {"Raw": 118, "Same": 150, "Near": 40, "Coa": 52, "Flbk": 24, "Buf": 10, "Miss": 2}
    counts_uniq = {"Raw": 118, "Same": 96, "Near": 21, "Coa": 15, "Flbk": 13, "Buf": 8, "Miss": 2}
    # 線グラフ用: 前後4秒×fps の各指標時系列(中央=現在)
    fps = 30; win = 4
    n = win * fps * 2 + 1
    series = {}
    for k, _ in CATS:
        base = counts[k]
        series[k] = [max(0, base + int(30 * math.sin(i / 7.0 + hash(k) % 7)) + random.randint(-12, 12))
                     for i in range(n)]
    buf_cap = 15360
    # 全編タイムライン用の時系列(ダミー): Miss多発帯とBuf枯渇帯を作り込む
    tln = 360
    tl = {}
    for k in ["Raw", "Coa", "Flbk", "Buf", "Miss"]:
        b = counts[k]
        tl[k] = [max(0, int(b + 22 * math.sin(i / 11.0 + hash(k) % 5) + random.randint(-8, 8))) for i in range(tln)]
    miss_zones = lambda i: (80 <= i <= 112) or (248 <= i <= 276)
    for i in range(tln):
        if miss_zones(i):
            tl["Miss"][i] += random.randint(25, 70); tl["Buf"][i] += random.randint(15, 35)
    rem = []; r = buf_cap
    for i in range(tln):
        r += (-350 if miss_zones(i) else 260) * -1   # miss帯=枯渇へ / それ以外=補充
        r = max(0, min(buf_cap, r + random.randint(-40, 40)))
        rem.append(r)
    dma_tl = [(tl["Raw"][i] + tl["Buf"][i]) * 32 + C * 2 for i in range(tln)]   # 毎コマVRAM転送量
    cd1x_bpf = int(153600 / fps)                 # CD1xのコマ上限(CD読みはこれを超えない)
    frame_cd = int(147456 / fps)                 # CBR予算/コマ(この範囲=新規CD, 超過はタンク供給)
    # 有効Band = 映像に使った分(Raw色) + タンクに貯めた分(Buf色, 貯蓄も有効)。CD読み量(frame_cd)が上限。
    _upd = counts["Raw"] + counts["Buf"] + counts["Coa"] + counts["Flbk"] + counts["Near"]
    _fb = counts["Raw"] * 32 + counts["Buf"] * 32 + _upd * 2
    max_raw = frame_cd // 34                      # 1コマのRaw予算(指針器フルスケール C-max_raw の基準)
    # デモ(充填コマ・パディング無し): 有効Band = frame_cd を 映像 + 貯蓄 + 音声/ヘッダ で満たす
    ovh_bytes = int(frame_cd * 0.10)             # 音声+その他ヘッダ(dim色, デモ約10%)
    raw_bytes = min(_fb, int((frame_cd - ovh_bytes) * 0.70))  # 映像(Raw色, デモは残りを貯蓄に回して3セグ見せる)
    buf_bytes = max(0, frame_cd - raw_bytes - ovh_bytes)   # 貯蓄(Buf色)=余りをタンクへ
    band_kbps = int((raw_bytes + buf_bytes + ovh_bytes) * fps / 1024)   # 有効Band(全部込み=frame_cd)
    tank_delta = buf_bytes // 32                  # このコマのタンク充填(タイル, 指針器=右へ青)
    def _fbi(i):
        u = tl["Raw"][i] + tl["Coa"][i] + tl["Flbk"][i] + tl["Buf"][i]
        return tl["Raw"][i] * 32 + tl["Buf"][i] * 32 + u * 2
    raw_tl = [min(_fbi(i), frame_cd) for i in range(tln)]                   # 映像分
    buf_tl = [max(0, frame_cd - min(_fbi(i), frame_cd)) for i in range(tln)]  # 貯蓄分(有効Bandを満たす)
    pl_info = {"Prev": dict(pl=11, frame=980), "Current": dict(pl=12, frame=1122),
               "Next": dict(pl=13, frame=1544)}   # 各パレットの番号と切替開始フレーム
    pl_cur, pl_total = 12, 13                       # 現在パレット番号 / 総数(最大番号)
    # パレット状態(4面×15色) Prev/Current/Next の3セット
    def _pal():
        return [[(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)) for _ in range(15)]
                for _ in range(4)]
    palettes = {"Prev": _pal(), "Current": _pal(), "Next": _pal()}
    # カテゴリ合計(全編累積のダミー) と そのユニーク数(全編で使われた別タイル数)
    cat_totals = {k: counts[k] * (200 + (hash(k) % 90)) for k, _ in CATS}
    cat_uniq = {k: max(1, int(cat_totals[k] * (0.06 + 0.05 * (hash(k) % 7) / 7))) for k, _ in CATS}
    dma_tiles = counts["Raw"] + counts["Buf"]
    return dict(C=C, counts=counts, counts_uniq=counts_uniq, series=series, fps=fps, win=win,
                palettes=palettes, cat_totals=cat_totals, cat_uniq=cat_uniq, tank_delta=tank_delta, max_raw=max_raw,
                cd1x_bpf=cd1x_bpf, raw_bytes=raw_bytes, buf_bytes=buf_bytes, ovh_bytes=ovh_bytes, band_kbps=band_kbps,
                raw_tl=raw_tl, buf_tl=buf_tl, pl_info=pl_info, pl_cur=pl_cur, pl_total=pl_total,
                mode="H32", res="176x144 (22x18)", audio="13.3kHz mono 8bit PCM", avg_kbps=146,
                src_spec="256x224 / 30fps / AAC 48kHz stereo",
                req=sum(counts[k] for k, _ in CATS),
                budget=273,
                comp=counts["Same"] + counts["Near"] + counts["Coa"] + counts["Flbk"],
                buf_cap=buf_cap, buf_rem=13900,
                cold=counts["Raw"] + counts["Buf"], cold_raw=counts["Raw"], cold_buf=counts["Buf"],
                cold_cap=av_config.cold_cap_for_fps(fps, "H32", 22 * 18),
                dma_tiles=dma_tiles, dma_runs=23,
                tl=tl, buf_rem_series=rem, dma_tl=dma_tl, tln=tln,
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
    """Each bar follows its label width. Returns Band, Tank, Buff, DMA, Run."""
    return (_w(f_leg, "Band:000KiB/sec") + 3,
            _w(f_leg, "Tank:00000") + 3,
            _w(f_leg, "Buff:-000") + 3,
            _w(f_leg, dma_label_template(cells)) + 3,
            _w(f_leg, run_label_template()) + 3)


def draw_tank_delta(d, x, by, BH, ly, bw, delta, scale):
    """Tank増減の指針器メーター: 中央に薄線、減(<0)=左へ赤 / 増(>0)=右へ青。フルスケール=scale。
    ラベルは Buff:-xxx / +xxx / ±000(バー幅=ラベル幅)。"""
    half = bw // 2
    cxm = x + half
    fillw = int(half * min(abs(delta), scale) / max(scale, 1))
    if delta < 0:
        d.rectangle([cxm - fillw, by, cxm, by + BH], fill=(220, 70, 70))     # 減=左へ赤
    elif delta > 0:
        d.rectangle([cxm, by, cxm + fillw, by + BH], fill=(80, 130, 230))    # 増=右へ青
    d.rectangle([x, by, x + bw, by + BH], outline=COL_FRAME_IN)
    d.line([cxm, by + 2, cxm, by + BH - 2], fill=(110, 110, 120))            # 中央の薄線
    sign = "-" if delta < 0 else ("+" if delta > 0 else "±")
    d.text((x, ly), "Buff:", fill=COL_TXT, font=f_leg); lx = x + _w(f_leg, "Buff:")
    d.text((lx, ly), sign, fill=COL_TXT, font=f_leg); lx += _w(f_leg, sign)
    draw_padnum(d, lx, ly, abs(delta), 3, f_leg, COL_TXT)


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
    Raw/Same=枠なし内容塗り(▓) / Near/Coa/Buf=細枠 / Flbk(橙)=太枠 / Miss=赤塗りつぶし。"""
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
            k = random.choices(cats, weights=[25, 40, 8, 10, 10, 3, 4])[0]  # Raw/Same/Near/Coa/Flbk/Buf/Miss
            col = dict(CATS)[k]
            if k == "Miss":
                d.rectangle([x0, y0, x1, y1], fill=CAT_MISS)                  # 赤で塗りつぶし
                continue
            # 内容(色ブロック)を塗る
            d.rectangle([x0, y0, x1, y1], fill=((c * 11 + r * 7) % 256, (r * 13) % 256, (c * 17) % 256))
            if k in CAT_FILL:
                continue                                                     # Raw/Same=枠なし
            d.rectangle([x0, y0, x1, y1], outline=col, width=CAT_THICK.get(k, 1))  # 細枠/太枠
    return im


def swatch(d, x, y, sw, name, col):
    """凡例四角。Raw=白黒の▓ / Same=Same色(グレー)濃淡の▓(どちらも枠を描かない内容塗りの意) /
    Miss=赤塗り / Near/Coa/Buf=細枠 / Flbk=太枠。"""
    if name in ("Raw", "Same"):
        hi, lo = ((210, 210, 210), (45, 45, 45)) if name == "Raw" \
            else (col, tuple(int(v * 0.35) for v in col))        # Same=グレー濃淡
        cs = max(2, (sw + 1) // 4)                               # 市松のマス
        for iy in range(0, sw + 1, cs):
            for ix in range(0, sw + 1, cs):
                on = (((ix // cs) + (iy // cs)) % 2 == 0)
                d.rectangle([x + ix, y + iy, min(x + ix + cs - 1, x + sw), min(y + iy + cs - 1, y + sw)],
                            fill=hi if on else lo)
    elif name in CAT_FILL:                                        # Miss
        d.rectangle([x, y, x + sw, y + sw], fill=col)
    else:
        d.rectangle([x, y, x + sw, y + sw], outline=col, width=CAT_THICK.get(name, 1))  # 枠(細/太)


def _mix_col(base, tint, amount):
    """RGBを混ぜる。凡例メーターはカテゴリ色を暗い背景へ薄く足す。"""
    return tuple(round(b + (t - b) * amount) for b, t in zip(base, tint))


def legend_level(d, x0, x1, y, value, maximum, col):
    """数字欄の背景を、frame内の最大カテゴリ比で伸びる薄い横メーターとして描く。"""
    bg = (14, 14, 14)
    top, bottom = y - 1, y + 16
    d.rectangle([x0, top, x1, bottom], fill=_mix_col(bg, col, 0.14))
    ratio = min(max(float(value) / max(int(maximum), 1), 0.0), 1.0)
    fillw = round((x1 - x0 + 1) * ratio)
    if value > 0:
        fillw = max(1, fillw)
    if fillw:
        d.rectangle([x0, top, min(x1, x0 + fillw - 1), bottom],
                    fill=_mix_col(bg, col, 0.38))


def draw_legend(w, h, data):
    """凡例(2行)。数字欄はカテゴリ色の薄いframe内レベルメーター。"""
    im = Image.new("RGB", (w, h), (14, 14, 14))
    d = ImageDraw.Draw(im)
    per_row = 4
    cw = w // per_row
    sw = 14
    meter_max = max(data["counts"].values(), default=1)
    meter_chars_w = _w(f_leg, "000/000")
    for i, (name, col) in enumerate(CATS):
        row = i // per_row; c = i % per_row
        x = c * cw + 6; y = row * (h // 2) + (h // 2 - sw) // 2
        swatch(d, x, y, sw, name, col)
        tx = x + sw + 6
        label = DISP[name] + ":"
        nx = tx + _w(f_leg, label) - 2
        meter_right = min(nx + meter_chars_w - 1, (c + 1) * cw - 6)
        legend_level(d, nx, meter_right, y - 1,
                     data["counts"][name], meter_max, col)
        if name in UNIQ_CATS:      # ユニーク数/総数 を併記
            draw_field(d, tx, y - 1, label, data["counts_uniq"][name], 3, f_leg,
                       COL_TXT, data["counts"][name], 3)
        else:
            draw_field(d, tx, y - 1, label, data["counts"][name], 3, f_leg, COL_TXT)
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
    """status帯: Req / Cold / Band / Tank / Buff / DMA / Run + 3段Timeline。
    数値は同じ文字色のゼロ埋めで桁固定。MissCarryは廃止。"""
    im = Image.new("RGB", (w, h), (16, 16, 16))
    d = ImageDraw.Draw(im)
    by, BH = 8, 16                   # 上マージンを半分(16→8)。タイムラインもこのbyから始まり下端は据置=縦に伸びる
    C = data["C"]
    GAP = 16
    REQ_W = 180                     # Req は同ラインに Raw/Comp を並べるので広め
    dmax = dma_tile_capacity(data["mode"], data["fps"], C)
    dval = data["dma_tiles"]
    # メーター幅の統一を廃止=各バーは自分のラベル幅
    BAND_W, TANK_W, BUFF_W, DMA_W, RUN_W = meter_widths(C)
    COLD_W = _w(f_leg, "Cold:000") + 3          # 新: Coldバー幅=ラベル幅(Req↔Bandの間に挿入)
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

    # 1) Req = 全カテゴリ積み(全幅=C) + 予算ライン(黄)。同ラインに Raw数 / Comp数を横並び
    stacked([(data["counts"][k], dict(CATS)[k]) for k, _ in CATS], C, REQ_W)
    bx = x + int(REQ_W * data["budget"] / C)
    d.line([bx, by - 2, bx, by + BH + 2], fill=(255, 214, 0))
    xq = draw_field(d, x, ly, "Req:", data["req"], 3, f_leg, COL_TXT)
    xr = draw_field(d, xq + 10, ly, "Raw:", data["counts"]["Raw"], 3, f_leg, COL_DIM)
    draw_field(d, xr + 8, ly, "Comp:", data["comp"], 3, f_leg, COL_DIM)
    x += REQ_W + GAP
    # 1.5) Cold = このコマの新規タイル(Raw+Buf)。フルスケール=計測済みcold cap。
    #      これまで苦しんだcold値を視覚化。Req↔Bandの空間に配置。
    stacked([(data["cold_raw"], CAT_RAW), (data["cold_buf"], CAT_BUF)], data["cold_cap"], COLD_W)
    draw_field(d, x, ly, "Cold:", data["cold"], 3, f_leg, COL_TXT)
    x += COLD_W + GAP
    # 2) 有効Band = 映像(Raw色) + 貯蓄(Buf色) + 音声/その他ヘッダ(dim色)。バー幅=ラベル幅。単位 KiB/sec
    stacked([(data["raw_bytes"], CAT_RAW), (data["buf_bytes"], CAT_BUF),
             (data["ovh_bytes"], COL_OVH)], data["cd1x_bpf"], BAND_W)
    xb = draw_field(d, x, ly, "Band:", data["band_kbps"], 3, f_leg, COL_TXT)
    d.text((xb, ly), "KiB/sec", fill=COL_DIM, font=f_leg)
    x += BAND_W + GAP
    # 3) Tank = 貯水池の現在残量(violet)。ラベルは現在数のみ、バー幅=ラベル幅
    stacked([(data["buf_rem"], CAT_BUF)], data["buf_cap"], TANK_W)
    draw_field(d, x, ly, "Tank:", data["buf_rem"], 5, f_leg, COL_TXT)
    x += TANK_W + GAP
    # 4) Tank増減の指針器(中央薄線・減=左赤/増=右青)。フルスケール=描画範囲タイル数-最大Raw数
    draw_tank_delta(d, x, by, BH, ly, BUFF_W, data["tank_delta"], max(1, C - data["max_raw"]))
    x += BUFF_W + GAP
    # 5) DMA = 今フレームの32Bパターンタイル数。フル=モード/fpsの理論DMAから全NT分を引いた枚数。
    fillw = int(DMA_W * min(dval, dmax) / max(dmax, 1)); over = dval > dmax
    d.rectangle([x, by, x + fillw, by + BH], fill=(220, 130, 60) if over else COL_DMA)
    if over:
        d.rectangle([x + fillw, by, x + DMA_W, by + BH], fill=(150, 60, 60))
    d.rectangle([x, by, x + DMA_W, by + BH], outline=COL_FRAME_IN)
    draw_field(d, x, ly, "DMA:", dval, dma_value_digits(C), f_leg, COL_TXT)
    x += DMA_W + GAP

    # 6) Run = playerのcold-run record数。CPU/DMA転送方式にかかわらず1tile/runが理論最悪。
    run_val = int(data["dma_runs"]); run_max = dma_run_worst_case(dval)
    run_fill = (max(1, int(RUN_W * min(run_val, run_max) / run_max))
                if run_val > 0 and run_max > 0 else 0)
    d.rectangle([x, by, x + run_fill, by + BH],
                fill=(220, 70, 70) if run_val > run_max else COL_RUN)
    d.rectangle([x, by, x + RUN_W, by + BH], outline=COL_FRAME_IN)
    draw_field(d, x, ly, "Run:", run_val, DMA_RUN_DIGITS, f_leg, COL_TXT)
    x += RUN_W + GAP

    # メーターの下: パレット Prev/Current/Next(PL/Frame見出し)
    meters_right = x - GAP
    py0 = ly + 16
    draw_palettes_strip(d, 4, py0, meters_right - 4, (h - 2) - py0, data["palettes"], data.get("pl_info"))

    # 7) 3段Timeline(右端まで): 上=Reqヒートマップ / 中=Buf残量マップ / 下=有効転送量。比=2:1:1
    x_tl = x
    tlw = w - 4 - x_tl
    if tlw > 20:
        tl = data["tl"]; rem = data["buf_rem_series"]; tln = data["tln"]
        raw_tl = data["raw_tl"]; buf_tl = data["buf_tl"]
        tlh = (h - 2) - by
        # 正確に 2:1:1(区切り無し・隙間無し)
        H_req = tlh // 2                          # Req = 2
        H_buf = tlh // 4                          # Buf = 1
        H_dma = tlh - H_req - H_buf               # DMA = 1
        y_req = by
        y_buf = y_req + H_req
        y_dma = y_buf + H_buf
        # 各段の背景を極暗色で塗る(下段の空きが純黒=marginに見えないように)
        d.rectangle([x_tl, y_buf, x_tl + tlw, y_buf + H_buf], fill=(26, 20, 34))   # Buf段 暗violet
        d.rectangle([x_tl, y_dma, x_tl + tlw, y_dma + H_dma], fill=(18, 26, 20))   # 有効転送段 暗green
        escale = max(data["cd1x_bpf"], 1)                   # 有効転送段フルスケール=CD1x/コマ
        stack_order = [("Raw", CAT_RAW), ("Coa", CAT_COA), ("Flbk", CAT_FLBK),
                       ("Buf", CAT_BUF), ("Miss", CAT_MISS)]
        for col_i in range(tlw):
            fi = min(int(col_i / tlw * tln), tln - 1)
            X = x_tl + col_i
            yb = y_req + H_req                    # 上段: Reqヒートマップ(下から積む)
            for k, col in stack_order:
                seg = int(H_req * tl[k][fi] / C)
                if seg > 0:
                    d.line([(X, yb - seg), (X, yb)], fill=col); yb -= seg
            hb = int(H_buf * rem[fi] / max(data["buf_cap"], 1))   # 中段: Buf残量(violet下から)
            d.line([(X, y_buf + H_buf - hb), (X, y_buf + H_buf)], fill=CAT_BUF)
            hr = int(H_dma * min(raw_tl[fi], escale) / escale)   # 下段: 有効転送量(Raw色下+Buf色上)
            d.line([(X, y_dma + H_dma - hr), (X, y_dma + H_dma)], fill=CAT_RAW)
            hb2 = int(H_dma * min(raw_tl[fi] + buf_tl[fi], escale) / escale)
            if hb2 > hr:
                d.line([(X, y_dma + H_dma - hb2), (X, y_dma + H_dma - hr)], fill=CAT_BUF)
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
    for name, col in CATS:
        swatch(d, x, ly, 11, name, col); x += 11 + 5
        s = str(tot[name])                   # 合計値のみ(ユニーク数併記は廃止)
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
    status帯(Req/Comp/Buff/DMA + パレット Prev/Current/Next + 3段タイムライン)と
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
