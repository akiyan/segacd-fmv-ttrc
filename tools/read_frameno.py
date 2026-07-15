#!/usr/bin/env python3
"""実機/エミュ録画のデバッグHUD(左上端・1行)から各値を読む。

HUD は boot/movieplay_ip.s の render_dbg が描く1行:
    F<4桁> P<4桁> S<4桁> D<4桁> R<4桁> L<4桁>
各値 = glyph 1セル + hex4桁 4セル + 空け 1セル = 6セル間隔(HUD_PITCH と一致)。
先頭 F の直後4桁が現在の movie フレーム番号(16進, boot/dbgfont.bin の 8x8 フォント)。
8x8セルをテンプレート(gen_debugfont.py と同じ字形)と正規化相互相関(NCC)で照合。
背景映像に強いよう NCC(明暗オフセット不変) + 先頭Fで原点自動較正。

このモジュールの HUD_* 定数は boot/movieplay_ip.s の HUD_ROW/HUD_PITCH/HUD_COL_* と
一致させること(HUDレイアウトを変えたら両方直す)。native 320x224 キャプチャ前提
(1セル=8px)。

使い方:
    from read_frameno import read_frameno, read_hud
    n, conf = read_frameno(pil_img)              # フレーム番号(F)のみ
    hud = read_hud(pil_img)                       # {'F':(v,conf), 'P':..., 'L':...}
"""
import numpy as np

# gen_debugfont.py と同じ 0-F 字形("#"=点灯)
_HEX = {
    0x0: ["..####..", ".##..##.", ".##..##.", ".##..##.", ".##..##.", ".##..##.", "..####..", "........"],
    0x1: ["...##...", "..###...", "...##...", "...##...", "...##...", "...##...", ".######.", "........"],
    0x2: [".#####..", "##...##.", "....##..", "...##...", "..##....", ".##.....", "#######.", "........"],
    0x3: ["#####...", "....##..", "...###..", "....##..", "....##..", "##..##..", ".####...", "........"],
    0x4: ["...###..", "..####..", ".##.##..", "##..##..", "#######.", "....##..", "....##..", "........"],
    0x5: ["#######.", "##......", "######..", ".....##.", ".....##.", "##...##.", ".#####..", "........"],
    0x6: ["..####..", ".##.....", "##......", "######..", "##...##.", "##...##.", ".#####..", "........"],
    0x7: ["#######.", "....##..", "...##...", "..##....", "..##....", "..##....", "..##....", "........"],
    0x8: [".#####..", "##...##.", "##...##.", ".#####..", "##...##.", "##...##.", ".#####..", "........"],
    0x9: [".#####..", "##...##.", "##...##.", ".######.", ".....##.", "....##..", ".####...", "........"],
    0xA: ["..###...", ".##.##..", "##...##.", "##...##.", "#######.", "##...##.", "##...##.", "........"],
    0xB: ["######..", "##...##.", "##...##.", "######..", "##...##.", "##...##.", "######..", "........"],
    0xC: ["..####..", ".##..##.", "##......", "##......", "##......", ".##..##.", "..####..", "........"],
    0xD: ["#####...", "##..##..", "##...##.", "##...##.", "##...##.", "##..##..", "#####...", "........"],
    0xE: ["#######.", "##......", "##......", "#####...", "##......", "##......", "#######.", "........"],
    0xF: ["#######.", "##......", "##......", "#####...", "##......", "##......", "##......", "........"],
}
_T = {v: np.array([[1.0 if c == "#" else 0.0 for c in r] for r in rows]) for v, rows in _HEX.items()}
_Fg = _T[0xF]

# --- HUDレイアウト(boot/movieplay_ip.s の HUD_* と一致させる) ---
CELL = 8                 # 1 HUDセル = 8px
HUD_PITCH_H32 = 5        # native 256px H32: glyph+4 digits, no gap
HUD_PITCH_H40 = 6        # native 320px H40: glyph+4 digits+one gap
HUD_ROW = 0              # HUD行(0=最上段)。ip.s の HUD_ROW と一致
HUD_FIELDS = ["F", "P", "S", "D", "R", "L"]   # H32: col 0,5,10,15,20,25; H40: 0,6,12,18,24,30
DIGITS = 4               # 各値の16進桁数


def _ncc(a, b):
    a = a - a.mean(); b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 1e-6 else -1.0


def _gray(img):
    if hasattr(img, "convert"):
        return np.asarray(img.convert("L"))
    g = np.asarray(img)
    return g.mean(axis=2) if g.ndim == 3 else g


def _calib_origin(gray):
    """先頭 'F' glyph を左上窓で探し、その (x, y) を原点として返す。
    HUD は左上端(col0,row0)。"""
    best, bx, by = -2.0, 0, HUD_ROW * CELL
    h, w = gray.shape[:2]
    for y in range(0, min(17, h - 7)):
        for x in range(0, min(17, w - 39)):
            s = _ncc(gray[y:y + 8, x:x + 8].astype(float), _Fg)
            if s > best:
                best, bx, by = s, x, y
    return bx, by, best


def _read_hex(gray, x0, y):
    """(x0, y) から4桁の16進を読む。x0 は先頭桁の左端。-> (値, 最小NCC)。"""
    val, minsc = 0, 2.0
    for j in range(DIGITS):
        x = x0 + j * CELL
        cell = gray[y:y + 8, x:x + 8].astype(float)
        best, bv = -2.0, 0
        for v, t in _T.items():
            s = _ncc(cell, t)
            if s > best:
                best, bv = s, v
        val = val * 16 + bv
        minsc = min(minsc, best)
    return val, minsc


def read_frameno(img):
    """PIL Image または grayscale ndarray -> (frame_no, confidence)。F(先頭値)のみ。"""
    gray = _gray(img)
    x0, y, fconf = _calib_origin(gray)
    val, minsc = _read_hex(gray, x0 + CELL, y)   # hex は F glyph の1セル後ろから
    return val, min(fconf, minsc)


def read_hud(img):
    """HUDの全値を読む -> {'F':(値,conf), 'P':..., 'S':..., 'D':..., 'R':..., 'L':...}。
    先頭Fで原点較正し、以降は HUD_PITCH セル間隔で各値の hex4桁を読む。"""
    gray = _gray(img)
    x0, y, fconf = _calib_origin(gray)
    pitch = HUD_PITCH_H32 if gray.shape[1] < 300 else HUD_PITCH_H40
    out = {}
    for k, name in enumerate(HUD_FIELDS):
        gx = x0 + k * pitch * CELL               # この値の glyph x
        val, minsc = _read_hex(gray, gx + CELL, y)   # hex は glyph の1セル後ろ
        out[name] = (val, round(min(fconf, minsc), 3))
    return out


if __name__ == "__main__":
    import sys
    from PIL import Image
    for p in sys.argv[1:]:
        hud = read_hud(Image.open(p))
        parts = " ".join("%s=%04X(%.2f)" % (k, hud[k][0], hud[k][1]) for k in HUD_FIELDS)
        print("%s -> %s" % (p, parts))
