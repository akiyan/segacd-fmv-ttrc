#!/usr/bin/env python3
"""実機/エミュ録画のデバッグHUD(左上端・1行)から各値を読む。

HUD はカテゴリ文字を描かず、boot/movieplay_ip.s の固定順で値だけを描く:
    H32/H40: xxxx xx xx xx xx xx xx xx xx xx xxxx xx xx
内部キー順は F/P/S/D/R/L/C/W/M/A/U/N/J。F は16進4桁、L は
音声リードの上位byte（256B単位）、P/S/D/R/C/W/M/A/N はlow byteの
16進2桁、U は16進4桁。U はMain pattern転送時間（Mega-CD stopwatchの
30.72 us tick）、N はcold-run数の下位byte、J は404 KiBを超えた
streamed PrgBuf占有量の再生中最大値（1 KiB単位、端数切り上げ）。
各8x8セルの上段バーコードを直接4-bitとして読み、下段の小型hex字形とのNCCで
信頼度を確認する。ネイティブ録画の原点(0,0)は即時判定し、位置がずれた画像だけ
先頭4桁で原点を探索する。

このモジュールの HUD_LAYOUT/HUD_H40_LAYOUT は boot/movieplay_ip.s の
prepare_dbg と一致させること(HUDレイアウトを変えたら両方直す)。

使い方:
    from read_frameno import read_frameno, read_hud
    n, conf = read_frameno(pil_img)              # 先頭4桁のフレーム番号のみ
    hud = read_hud(pil_img)                       # {'F':(v,conf), 'P':..., 'L':...}
"""
import numpy as np

import gen_debugfont


_T = {
    value: np.array([[1.0 if c == "#" else 0.0 for c in row]
                     for row in rows])
    for value, rows in enumerate(gen_debugfont.ORDER)
}

# --- HUDレイアウト(boot/movieplay_ip.s の prepare_dbg と一致させる) ---
CELL = 8                 # 1 HUDセル = 8px
HUD_ROW = 0              # inactive Plane A movie table's top row
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
    ("U", 4),
    ("N", 2),
    ("J", 2),
)
HUD_H40_FIELD_DIGITS = HUD_FIELD_DIGITS
# H40 DEBUG builds with HUD_FLIP_FIELDS append two flip-phase fields:
# V = V-counter at the previous accepted flip, O = that flip's lateness
# past the fixed-N2 arm point in 30.72us ticks (clamped to 0xFF).
HUD_H40_FLIP_FIELD_DIGITS = HUD_FIELD_DIGITS + (
    ("V", 2),
    ("O", 2),
)


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
HUD_H40_FLIP_LAYOUT, HUD_H40_FLIP_CELLS = _make_layout(HUD_H40_FLIP_FIELD_DIGITS)
HUD_H40_FLIP_FIELDS = tuple(name for name, _col, _digits in HUD_H40_FLIP_LAYOUT)
H40_NATIVE_WIDTH = 320


def hud_layout_for_width(width):
    """Return the native H32/H40 layout from the captured frame width.

    H32 and H40 deliberately use the same 30-cell layout. Separate layout
    objects remain for callers that retain native-mode metadata.
    """
    return HUD_H40_LAYOUT if width >= H40_NATIVE_WIDTH else HUD_LAYOUT


def _ncc(a, b):
    a = a - a.mean(); b = b - b.mean()
    d = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / d) if d > 1e-6 else -1.0


def _read_barcode(cell):
    """Decode the four two-pixel bars in row 0 and return value/confidence."""
    cell = cell.astype(float)
    low = float(np.percentile(cell, 10))
    high = float(np.percentile(cell, 90))
    span = high - low
    if span < 1.0:
        return 0, -1.0
    threshold = (low + high) * 0.5
    groups = cell[0, :8].reshape(4, 2).mean(axis=1)
    value = 0
    for group in groups:
        value = (value << 1) | int(group > threshold)
    margin = float(np.min(np.abs(groups - threshold)) / (span * 0.5))
    return value, min(1.0, margin)


def _read_cell(cell):
    value, barcode_conf = _read_barcode(cell)
    glyph_conf = _ncc(cell.astype(float), _T[value])
    return value, min(barcode_conf, glyph_conf)


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
                _value, score = _read_cell(cell)
                scores.append(score)
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
        bv, best = _read_cell(cell)
        val = val * 16 + bv
        minsc = min(minsc, best)
    return val, minsc


def _find_origin(gray, required_width):
    """Use the native (0,0) HUD directly; fall back to the movable-image scan."""
    if gray.shape[0] >= CELL and gray.shape[1] >= required_width:
        _value, score = _read_hex(gray, 0, 0, 4)
        if score >= 0.80:
            return 0, 0, score
    return _calib_origin(gray, required_width)


def read_frameno(img):
    """PIL Image または grayscale ndarray -> (frame_no, confidence)。F(先頭値)のみ。"""
    gray = _gray(img)
    x0, y, fconf = _find_origin(gray, 4 * CELL)
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
    x0, y, fconf = _find_origin(gray, cells * CELL)
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
