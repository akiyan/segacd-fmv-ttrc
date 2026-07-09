#!/usr/bin/env python3
"""sim の stats.npz から各フレームの status 帯PNG(720x40)を生成する。

4バー(上=バー / 下=同幅ラベル)を左端ガイドライン(=動画枠/説明ブロックの左端)に
揃えて横並びにする:
  1) Req/Fill/Miss  : 全幅=576tile。0..273=固定予算(緑)、273超=無償で埋めた分(青)、
                      Fill..Req=Miss(赤)。273に黄の予算ラインを立てる。
  2) MissCarry      : 全幅=303(=576-273)。繰越Missの年齢分布(若→古 = cool→warm)。
  3) Comp           : 全幅=576。no-update(indigo)+ same/dedup(teal)。少なければ埋めない。
  4) Timeline       : 全長ヒートマップ + 再生ヘッド、Time/Frame。
各バーの幅は真下のラベル文字列の幅に合わせる。
"""
import os
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

OUT = Path(os.environ.get("CBRSIM_OUT", "tmp/sim"))
STATUS = OUT / "status"
CATLEG = OUT / "catleg"          # 右2段目の凡例+カウント(□Raw Same Dedup Buf)
MISSLEG = OUT / "missleg"        # 右3段目の凡例+カウント(■Miss ■MissCarry)
TIMEFRAME = OUT / "timeframe"    # Time/Frame strip(メイン動画右下の黒帯に置く)
LEG_W, LEG_H = 569, 30           # 凡例ストリップのサイズ(右カラム幅に一致)
TF_W, TF_H = 360, 34             # Time/Frame strip サイズ
COL_CARRY = (235, 160, 70)       # MissCarry amber
FONT = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
LW, SH = 1227, 76               # 左カラム(MEGA-CD枠)幅に一致。上に▼余白(MARK)を追加
MARK = 12                       # 上余白(▼マーカー用)。全バー/ヒートマップの上端をこの下に揃える
by = 5 + MARK                   # バー/ヒートマップ 上端(すべて揃える)
BH = 18                         # バー高さ
LY = 36 + MARK                  # ラベル行 y
X0 = 0                          # 左端

BUDGET = 273                    # 1フレームで確実に更新できる固定タイル数
CELLS_MAX = 576                 # 総セル数(Req/Comp バーの全幅が表す値)
CARRY_MAX = CELLS_MAX - BUDGET  # 303。MissCarry バーの全幅が表す値

# 色
COL_FILL = (70, 190, 90)        # 予算内で更新(緑)
COL_FREE = (90, 150, 235)       # 273超を無償(dedup等)で埋めた分(青)
COL_MISS = (220, 70, 70)        # 未更新/ゴースト(赤)
COL_BUDGET = (255, 214, 0)      # 273 予算ライン(黄)
COL_RAWBAR = (205, 205, 205)    # bar1: Raw(新規CD転送) 白(カテゴリ色と一致)
COL_SAMEBAR = (95, 115, 215)    # Same(不変/no-update) indigo
COL_NEAR = (110, 200, 110)      # Near(近似で更新省略) green
COL_DEDUP = (0, 190, 175)       # Dedup(VRAM流用) teal
COL_COA = (150, 150, 158)       # Coa(粗い近似dedup=見た目が近い常駐を流用) gray
COL_BORDER = (70, 70, 70)
COL_TXT = (215, 215, 215)
COL_BUFFER = (175, 120, 235)    # PRG先読みバッファ(violet)。塗り=残量、減っていく
COL_BUFLBL = (200, 170, 240)    # Bufラベルの通常色
COL_EMPH = (255, 235, 120)      # Buf消費の強調色(→減衰して黒/元色へ)
BUF_DECAY = 12                  # 消費強調が消えるまでのフレーム数


def make_legend(items, font):
    """items = [(kind, color, label)]。kind='hatch'(斜線□) or 'fill'(塗り□)。
    四角の下辺と文字ベースラインを下部ガイドラインに揃える。"""
    im = Image.new("RGB", (LEG_W, LEG_H), (14, 14, 14))
    dd = ImageDraw.Draw(im)
    sq = LEG_H - 12
    yb = LEG_H - 5                    # 下部ガイドライン
    x = 2
    for kind, col, label in items:
        top = yb - sq
        if kind == "hatch":
            for t in range(3, 2 * sq, 4):    # "/" 斜線
                if t <= sq:
                    dd.line([(x, top + t), (x + t, top)], fill=(185, 185, 185))
                else:
                    dd.line([(x + t - sq, top + sq), (x + sq, top + t - sq)], fill=(185, 185, 185))
            dd.rectangle([x, top, x + sq, yb], outline=(195, 195, 195))
        else:
            dd.rectangle([x, top, x + sq, yb], fill=col, outline=(110, 110, 110))
        dd.text((x + sq + 6, yb), label, fill=(230, 230, 230), font=font, anchor="ls")
        x += sq + 6 + int(font.getbbox(label)[2]) + 16
    return im


def main():
    global CELLS_MAX, BUDGET, CARRY_MAX
    z = np.load(OUT / "stats.npz", allow_pickle=True)
    S = z["stats"]; fps = float(z["fps"]); cells = int(z["cells"])
    wh = z["wait_hist"]; nbins = int(z["nbins"])
    idx = {k: i for i, k in enumerate(str(z["cols"]).split())}
    # 総セル数・固定予算はstatsから取得(素材/fpsで変わる)
    CELLS_MAX = cells
    BUDGET = int(z["budget_tiles"]) if "budget_tiles" in z.files else BUDGET
    CARRY_MAX = max(CELLS_MAX - BUDGET, 1)
    # PRG先読みバッファの残量カーブ(あれば): タイムラインの左にメーターを差し込む
    buf_rem = buf_total = None
    buf_evt_f = buf_evt_hi = buf_evt_lo = None
    bpath = OUT / "buffer_remaining.npz"
    if bpath.exists():
        bz = np.load(bpath); buf_rem = bz["remaining"]; buf_total = int(bz["total"])
        # 消費イベント(残量が減ったフレーム)を前計算: 各フレームの直近イベントと消費帯
        dr = buf_rem.astype(int); prevr = np.r_[dr[0], dr[:-1]]
        buf_evt_f = np.full(len(dr), -10000)
        buf_evt_hi = np.zeros(len(dr), int); buf_evt_lo = np.zeros(len(dr), int)
        last = -10000; hi = lo = 0
        for f in range(len(dr)):
            if prevr[f] - dr[f] > 0:
                last = f; hi = int(prevr[f]); lo = int(dr[f])
            buf_evt_f[f] = last; buf_evt_hi[f] = hi; buf_evt_lo[f] = lo
    for D in (STATUS, CATLEG, MISSLEG, TIMEFRAME):
        D.mkdir(parents=True, exist_ok=True)
        for c in D.glob("*.png"):
            c.unlink()
    f_leg = ImageFont.truetype(FONT, 14)   # 6項目(Raw/Same/Near/Dedup/Coa/Buf)が幅に収まるサイズ
    f_tf = ImageFont.truetype(FONT, 24)    # Time/Frame

    # --- バー幅 = 真下ラベル(最大桁テンプレ)の文字幅。Bufバーは幅2倍。全体が720に
    #     収まる最大フォントを選ぶ(2倍Bufで溢れないよう自動縮小)。---
    nfr = len(S)
    have_buf = buf_rem is not None
    MINGAP = 7

    def layout_at(sz):
        f = ImageFont.truetype(FONT, sz)
        def tw(s):
            return int(f.getbbox(s)[2])
        w1 = tw("Req:576 Raw:576 Dedup:576 Coa:576 Buf:576 Miss:576")
        w2 = tw("MissCarry:303")
        w3 = tw("Same:576 Near:576 Dedup:576 Coa:576")
        wB = 2 * tw("Buf:%d" % buf_total) if have_buf else 0   # Bufバーは2倍幅
        fixed = [w1, w2, w3] + ([wB] if have_buf else [])       # 左4ブロック(固定幅)
        return f, fixed

    MIN_TL = 300   # タイムライン最小幅(右端まで伸ばす)
    for sz in (20, 18, 16, 15, 14):
        fs, fixed = layout_at(sz)
        if sum(fixed) + MINGAP * len(fixed) + MIN_TL <= LW:
            break
    # 順序: Req / MissCarry / Same+Dedup / [Buffer(2x)] / Timeline(右端まで)
    gap = MINGAP + 6
    xs = []
    x = X0
    for w in fixed:
        xs.append(x); x += w + gap
    x4 = x                      # タイムライン左端
    w4 = LW - x4                # タイムラインは右端(=メイン枠右)まで伸ばす
    if have_buf:
        w1, w2, w3, wB = fixed
        x1, x2, x3, xB = xs
    else:
        w1, w2, w3 = fixed
        x1, x2, x3 = xs

    # Timeline = 積み上げヒートマップ(全フレーム)。各列=1フレームのReq構成を縦積み
    # (下から Raw白/Dedup teal/Buf violet/Miss red)。全体を最初から塗る=俯瞰でヤバい所が判る。
    H_tl = SH - by - 2              # タイムライン全高。上端=by(Reqバー等と揃う)、上のMARKは▼余白
    H_top = (H_tl - 1) // 2         # 上=ヒートマップ(半分)、下=Buf残量マップ、間に1px区切り
    H_bot = H_tl - H_top - 1
    Raws = S[:, idx["tx"]]; Deds = S[:, idx["dedup"]]
    Coas = S[:, idx["coa"]] if "coa" in idx else np.zeros(len(S))
    Bufs = np.maximum(S[:, idx["updated"]] - Raws - Deds - Coas, 0)
    Miss_ = S[:, idx["miss"]]
    tlmap = np.zeros((H_top, w4, 3), np.uint8)
    for xx in range(w4):
        fi = min(int(xx / w4 * nfr), nfr - 1)
        yb = H_top
        for val, col in ((Raws[fi], (205, 205, 205)), (Deds[fi], (0, 190, 175)),
                         (Coas[fi], COL_COA), (Bufs[fi], (175, 120, 235)), (Miss_[fi], (220, 70, 70))):
            h = int(H_top * val / CELLS_MAX)
            if h > 0:
                tlmap[max(0, yb - h):yb, xx] = col
                yb -= h
    tlmap_img = Image.fromarray(tlmap, "RGB")
    # 時間ごとのBuf残量マップ(PRG先読み残量。violetで下から。左=満→右=空へ単調減少)
    bufmap = np.zeros((H_bot, w4, 3), np.uint8)
    if have_buf:
        for xx in range(w4):
            fi = min(int(xx / w4 * nfr), nfr - 1)
            h = int(H_bot * int(buf_rem[fi]) / max(buf_total, 1))
            if h > 0:
                bufmap[H_bot - h:H_bot, xx] = COL_BUFFER
    bufmap_img = Image.fromarray(bufmap, "RGB")

    for r, row in enumerate(S):
        frame = int(row[idx["frame"]])
        Fill = int(row[idx["updated"]]); Miss = int(row[idx["miss"]])
        MC = int(row[idx["carry"]])
        Raw = int(row[idx["tx"]])                       # Raw: 新規CD転送
        Same = int(row[idx["delta"]])                   # 不変(no-update)
        Dedup = int(row[idx["dedup"]])                  # VRAM流用
        Coa = int(row[idx["coa"]]) if "coa" in idx else 0      # 粗い近似dedup
        Buf = max(Fill - Raw - Dedup - Coa, 0)           # PRG先読み
        Near = int(row[idx["near"]]) if "near" in idx else 0   # 近似で更新省略
        Req = Raw + Dedup + Coa + Buf + Miss             # 実際に更新対象になった数(Near/Same除く)
        sl = Image.new("RGB", (LW, SH), (16, 16, 16))
        d = ImageDraw.Draw(sl)

        # 1) Raw + Dedup + Buf + Miss を積む(カテゴリ色)。塗り幅 = 合計 = Req(=want) で右は空く。273据置。
        def px1(v):
            return x1 + int(w1 * min(v, CELLS_MAX) / CELLS_MAX)
        acc = 0
        for v, col in ((Raw, COL_RAWBAR), (Dedup, COL_DEDUP), (Coa, COL_COA), (Buf, COL_BUFFER), (Miss, COL_MISS)):
            d.rectangle([px1(acc), by, px1(acc + v), by + BH], fill=col); acc += v
        bx = x1 + int(w1 * BUDGET / CELLS_MAX)
        d.line([bx, by - 1, bx, by + BH + 1], fill=COL_BUDGET)                      # 273 予算ライン(黄)
        d.rectangle([x1, by, x1 + w1, by + BH], outline=COL_BORDER)
        d.text((x1, LY), "Req:%-3d Raw:%-3d Dedup:%-3d Coa:%-3d Buf:%-3d Miss:%-3d" % (Req, Raw, Dedup, Coa, Buf, Miss),
               fill=COL_TXT, font=fs)

        # 2) MissCarry(全幅=303, 年齢グラデ 若→古)
        fill_len = int(w2 * min(MC, CARRY_MAX) / CARRY_MAX)
        h = wh[r]; tot = int(h.sum())
        px = x2
        if tot > 0 and fill_len > 0:
            for b in range(nbins):
                seg = int(fill_len * h[b] / tot)
                age = b + 1
                col = (min(255, 90 + age * 16), max(50, 190 - age * 16), 55)
                d.rectangle([px, by, px + seg, by + BH], fill=col); px += seg
        d.rectangle([x2, by, x2 + w2, by + BH], outline=COL_BORDER)
        d.text((x2, LY), "MissCarry:%-3d" % MC, fill=(230, 180, 90), font=fs)

        # 3) 圧縮効果 = Same + Near + Dedup + Coa (全幅=576)
        s1 = int(w3 * Same / CELLS_MAX)
        s2 = int(w3 * (Same + Near) / CELLS_MAX)
        s3 = int(w3 * (Same + Near + Dedup) / CELLS_MAX)
        s4 = int(w3 * (Same + Near + Dedup + Coa) / CELLS_MAX)
        d.rectangle([x3, by, x3 + s1, by + BH], fill=COL_SAMEBAR)      # Same(不変, indigo)
        d.rectangle([x3 + s1, by, x3 + s2, by + BH], fill=COL_NEAR)     # Near(省略, green)
        d.rectangle([x3 + s2, by, x3 + s3, by + BH], fill=COL_DEDUP)    # Dedup(再利用, teal)
        d.rectangle([x3 + s3, by, x3 + s4, by + BH], fill=COL_COA)      # Coa(近似流用, pink)
        d.rectangle([x3, by, x3 + w3, by + BH], outline=COL_BORDER)
        d.text((x3, LY), "Same:%-3d Near:%-3d Dedup:%-3d Coa:%-3d" % (Same, Near, Dedup, Coa), fill=(150, 200, 220), font=fs)

        # 4) PRG先読みバッファ残量 + 消費強調(減衰) — タイムラインの左
        if have_buf:
            fi = frame if frame < len(buf_rem) else len(buf_rem) - 1
            rem = int(buf_rem[fi])
            remx = xB + int(wB * rem / max(buf_total, 1))
            d.rectangle([xB, by, remx, by + BH], fill=COL_BUFFER)           # 残量(violet)
            inten = max(0.0, 1.0 - (fi - int(buf_evt_f[fi])) / BUF_DECAY)   # 消費直後=1 → 減衰0
            if inten > 0:                                                   # 消費した幅: 強調→黒
                bx0 = xB + int(wB * int(buf_evt_lo[fi]) / buf_total)
                bx1 = xB + int(wB * int(buf_evt_hi[fi]) / buf_total)
                ecol = tuple(int(COL_EMPH[k] * inten) for k in range(3))
                d.rectangle([bx0, by, bx1, by + BH], fill=ecol)
            d.rectangle([xB, by, xB + wB, by + BH], outline=COL_BORDER)
            lcol = tuple(int(COL_EMPH[k] * inten + COL_BUFLBL[k] * (1 - inten)) for k in range(3))
            d.text((xB, LY), "Buf:%-5d" % rem, fill=lcol, font=fs)         # ラベルも強調→元色

        # 5) Timeline 積み上げヒートマップ + 再生ヘッド(左→右)。Time/Frameは別strip(下記)
        sl.paste(tlmap_img, (x4, by))                            # 上: ヒートマップ(半分)
        sep = by + H_top
        sl.paste(bufmap_img, (x4, sep + 1))                      # 下: Buf残量マップ
        d.rectangle([x4, by, x4 + w4, by + H_tl], outline=COL_BORDER)
        d.line([x4, sep, x4 + w4, sep], fill=COL_BORDER)         # 2マップの区切り
        head = x4 + int(w4 * frame / nfr)
        d.line([head, by - 1, head, by + H_tl + 1], fill=(255, 255, 255))   # シークバー: 両マップに伸ばす
        # ▼ 現在位置マーカー(上余白=ヒートマップの外・上に置き、現在位置を指し示す)
        d.polygon([(head - 6, 3), (head + 6, 3), (head, by - 2)], fill=(255, 235, 120), outline=(25, 25, 25))

        sl.save(STATUS / f"{frame:05d}.png")

        # 右2段目の凡例+カウント: 斜線□Raw  ■Same  ■Dedup  ■Buf
        make_legend([
            ("hatch", None, "Raw:%-3d" % Raw),
            ("fill", COL_SAMEBAR, "Same:%-3d" % Same),
            ("fill", COL_NEAR, "Near:%-3d" % Near),
            ("fill", COL_DEDUP, "Dedup:%-3d" % Dedup),
            ("fill", COL_COA, "Coa:%-3d" % Coa),
            ("fill", COL_BUFFER, "Buf:%-3d" % Buf),
        ], f_leg).save(CATLEG / f"{frame:05d}.png")
        # 右3段目の凡例+カウント: ■Miss  ■MissCarry
        make_legend([
            ("fill", COL_MISS, "Miss:%-3d" % Miss),
            ("fill", COL_CARRY, "MissCarry:%-3d" % MC),
        ], f_leg).save(MISSLEG / f"{frame:05d}.png")
        # Time/Frame strip(黒地): メイン動画右下の黒帯に置く
        tsec = frame / fps
        tf = Image.new("RGB", (TF_W, TF_H), (0, 0, 0))
        ImageDraw.Draw(tf).text((TF_W - 6, TF_H - 8), "Time:%02d:%05.2f Frame:%05d" % (int(tsec // 60), tsec % 60, frame),
                                fill=(235, 235, 235), font=f_tf, anchor="rs")   # 右寄せ(枠内右下)
        tf.save(TIMEFRAME / f"{frame:05d}.png")
    print("wrote", len(S), "status frames to", STATUS)


if __name__ == "__main__":
    main()
