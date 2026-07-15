#!/usr/bin/env python3
"""実機/エミュ録画のデバッグHUD(左上端・1行)から各値を読む。

HUD は boot/movieplay_ip.s の render_dbg が描く1行:
    FxxxxPxxSxxDxxRxxLxxxx
F/L は16進4桁、P/S/D/Rはlow byteの16進2桁で、間隔なしの連続22セル。
先頭 F の直後4桁が現在の movie フレーム番号(16進, boot/dbgfont.bin の 8x8 フォント)。
8x8セルをテンプレート(gen_debugfont.py と同じ字形)と正規化相互相関(NCC)で照合。
背景映像に強いよう NCC(明暗オフセット不変) + 先頭Fで原点自動較正。

このモジュールの HUD_LAYOUT は boot/movieplay_ip.s の render_dbg と一致させること
(HUDレイアウトを変えたら両方直す)。H32/H40とも1セル=8pxの同じ並びを使う。

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

# --- HUDレイアウト(boot/movieplay_ip.s の render_dbg と一致させる) ---
CELL = 8                 # 1 HUDセル = 8px
HUD_ROW = 0              # Window planeの最上段
HUD_FIELD_DIGITS = (     # label 1セル + 指定桁数。field間の空けはない
    ("F", 4),
    ("P", 2),
    ("S", 2),
    ("D", 2),
    ("R", 2),
    ("L", 4),
)


def _make_layout():
    col = 0
    fields = []
    for name, digits in HUD_FIELD_DIGITS:
        fields.append((name, col, digits))
        col += 1 + digits
    return tuple(fields), col


HUD_LAYOUT, HUD_CELLS = _make_layout()
HUD_FIELDS = tuple(name for name, _col, _digits in HUD_LAYOUT)


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


def _read_hex(gray, x0, y, digits=4):
    """(x0, y) から指定桁の16進を読む。x0 は先頭桁の左端。-> (値, 最小NCC)。"""
    val, minsc = 0, 2.0
    for j in range(digits):
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
    先頭Fで原点較正し、連続22セルの固定レイアウトを各field固有の桁数で読む。"""
    gray = _gray(img)
    x0, y, fconf = _calib_origin(gray)
    out = {}
    for name, col, digits in HUD_LAYOUT:
        gx = x0 + col * CELL                     # この値の glyph x
        val, minsc = _read_hex(gray, gx + CELL, y, digits)
        out[name] = (val, round(min(fconf, minsc), 3))
    return out


if __name__ == "__main__":
    import sys
    from PIL import Image
    for p in sys.argv[1:]:
        hud = read_hud(Image.open(p))
        widths = {name: digits for name, _col, digits in HUD_LAYOUT}
        parts = " ".join(
            "%s=%0*X(%.2f)" % (k, widths[k], hud[k][0], hud[k][1])
            for k in HUD_FIELDS)
        print("%s -> %s" % (p, parts))
