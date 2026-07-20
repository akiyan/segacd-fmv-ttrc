#!/usr/bin/env python3
"""実機/エミュ録画のデバッグHUD(左上端・1行)から各値を読む。

HUD はカテゴリ文字を描かず、boot/movieplay_ip.s の固定順で値だけを描く:
    H32: xxxx xx xx xx xx xx xx xx xx xx
    H40: xxxx xx xx xx xx xx xx xx xx xx xxxx xx
内部キー順は従来どおり F/P/S/D/R/L/C/W/M/A/U/N。F は16進4桁、L は
音声リードの上位byte（256B単位）、P/S/D/R/C/W/M/A/N はlow byteの
16進2桁、U は16進4桁。U はMain pattern転送時間（Mega-CD stopwatchの
30.72 us tick）、N はcold-run数の下位byte。
8x8セルをテンプレート(gen_debugfont.py と同じ字形)と正規化相互相関(NCC)で照合。
背景映像に強いよう NCC(明暗オフセット不変) + 先頭4桁で原点自動較正。

このモジュールの HUD_LAYOUT/HUD_H40_LAYOUT は boot/movieplay_ip.s の
prepare_dbg と一致させること(HUDレイアウトを変えたら両方直す)。

使い方:
    from read_frameno import read_frameno, read_hud
    n, conf = read_frameno(pil_img)              # 先頭4桁のフレーム番号のみ
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

# --- HUDレイアウト(boot/movieplay_ip.s の prepare_dbg と一致させる) ---
CELL = 8                 # 1 HUDセル = 8px
HUD_ROW = 0              # Window planeの最上段
HUD_FIELD_DIGITS = (     # 値のみ。field間の空けはない
    ("F", 4),
    ("P", 2),
    ("S", 2),
    ("D", 2),
    ("R", 2),
    ("L", 2),
    ("C", 2),
    ("W", 2),
    ("M", 2),
    ("A", 2),
)
HUD_H40_FIELD_DIGITS = HUD_FIELD_DIGITS + (("U", 4), ("N", 2))


def _make_layout(field_digits):
    col = 0
    fields = []
    for name, digits in field_digits:
        fields.append((name, col, digits))
        col += digits
    return tuple(fields), col


HUD_LAYOUT, HUD_CELLS = _make_layout(HUD_FIELD_DIGITS)
HUD_FIELDS = tuple(name for name, _col, _digits in HUD_LAYOUT)
HUD_H40_LAYOUT, HUD_H40_CELLS = _make_layout(HUD_H40_FIELD_DIGITS)
HUD_H40_FIELDS = tuple(name for name, _col, _digits in HUD_H40_LAYOUT)
H40_NATIVE_WIDTH = 320


def hud_layout_for_width(width):
    """Return the native H32/H40 layout from the captured frame width.

    The values-only H40 row now fits inside 256 pixels, so row length can no
    longer identify the mode.  OCR input is expected to retain the emulator's
    native 256- or 320-pixel width.
    """
    return HUD_H40_LAYOUT if width >= H40_NATIVE_WIDTH else HUD_LAYOUT


def _ncc(a, b):
    a = a - a.mean(); b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 1e-6 else -1.0


def _gray(img):
    if hasattr(img, "convert"):
        return np.asarray(img.convert("L"))
    g = np.asarray(img)
    return g.mean(axis=2) if g.ndim == 3 else g


def _calib_origin(gray, required_width=4 * CELL):
    """先頭4桁のhex glyph列を左上窓で探し、その (x, y) を原点として返す。
    HUD は左上端(col0,row0)。"""
    best, bx, by = -2.0, 0, HUD_ROW * CELL
    h, w = gray.shape[:2]
    max_x = max(0, w - required_width)
    for y in range(0, min(17, h - 7)):
        for x in range(0, min(16, max_x) + 1):
            scores = []
            for digit in range(4):
                cell = gray[y:y + 8, x + digit * CELL:x + (digit + 1) * CELL].astype(float)
                scores.append(max(_ncc(cell, template) for template in _T.values()))
            s = min(scores)
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
    val, minsc = _read_hex(gray, x0, y)
    return val, min(fconf, minsc)


def read_hud(img, layout=None):
    """Read the values-only HUD, optionally using an explicit native layout.

    Pass ``HUD_H40_LAYOUT`` when the image has already been cropped narrower
    than its native 320-pixel frame; the 28-cell H40 row itself also fits in an
    H32-width crop and therefore cannot identify the mode.
    """
    gray = _gray(img)
    if layout is None:
        layout = hud_layout_for_width(gray.shape[1])
    cells = max(col + digits for _name, col, digits in layout)
    x0, y, fconf = _calib_origin(gray, cells * CELL)
    out = {}
    for name, col, digits in layout:
        gx = x0 + col * CELL
        val, minsc = _read_hex(gray, gx, y, digits)
        out[name] = (val, round(min(fconf, minsc), 3))
    return out


if __name__ == "__main__":
    import sys
    from PIL import Image
    for p in sys.argv[1:]:
        image = Image.open(p)
        hud = read_hud(image)
        layout = hud_layout_for_width(image.width)
        fields = tuple(name for name, _col, _digits in layout)
        widths = {name: digits for name, _col, digits in layout}
        parts = " ".join(
            "%s=%0*X(%.2f)" % (k, widths[k], hud[k][0], hud[k][1])
            for k in fields)
        print("%s -> %s" % (p, parts))
