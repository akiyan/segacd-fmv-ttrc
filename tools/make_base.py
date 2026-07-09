#!/usr/bin/env python3
"""比較動画の静的背景PNG(base.png)を生成する。高さ1080ネイティブ(文字くっきり)。

2カラム構成:
  左  = MEGA-CD sim output(実機画面, 4:3黒帯) + status帯 + 1行メタ
  右  = 3段: Source / カテゴリマップ / Miss+MissCarryマップ(いずれも黒帯なし)
右パネルの下端はメイン枠の下端(ガイドライン)に合わせる。カテゴリ/Miss段の凡例と
カウントは render_statusline が per-frame で描く(catleg/missleg)ので base には描かない。
座標は compose_cbr_delta.sh と共有。
"""
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
OUT = Path(os.environ.get("CBRSIM_OUT", "tmp/sim"))
CW, CH = 1920, 1080

# --- レイアウト(frame=枠 [x0,y0,x1,y1]、動画は枠の内側に PAD だけ余白) ---
# 1920x1080をフルに使う。左=大画面(4:3), 右=3段パネル, 下=status帯/パレット状態パネル。
# 端マージン40, 列間41, 上見出し, 下マージン16。座標は compose_cbr_delta.sh と共有。
PAD = 11
MAIN_FRAME = (40, 52, 1267, 978)     # 左大枠(4:3黒帯)。video (51,63) 1205x904
SRC_FRAME = (1308, 52, 1877, 336)    # 右1段(黒帯なし)。video (1319,63) 547x262
CAT_FRAME = (1308, 373, 1877, 657)   # 右2段。video (1319,384) 547x262。凡例=catleg overlay
MISS_FRAME = (1308, 694, 1877, 978)  # 右3段。下端=メイン枠と一致。video (1319,705) 547x262
STATUS_XY = (40, 988)                # status帯 1227x76 (左カラム下)
# 動的凡例(catleg/missleg)の overlay 位置: 各パネル枠の少し上
CATLEG_XY = (1308, 343)
MISSLEG_XY = (1308, 664)


def main():
    cv = Image.new("RGB", (CW, CH), (12, 12, 12))
    d = ImageDraw.Draw(cv)
    f_head = ImageFont.truetype(FONT, 33)
    f_meta = ImageFont.truetype(FONT, 20)

    def frame(rect, title=None):
        if title:
            d.text((rect[0] + 2, rect[1] - 42), title, fill=(235, 235, 235), font=f_head)
        d.rectangle(list(rect), outline=(200, 200, 200))

    src_label = os.environ.get("CBRSIM_SRCLABEL", "Source")
    frame(MAIN_FRAME, "MEGA-CD sim output")
    frame(SRC_FRAME, src_label)
    frame(CAT_FRAME)     # 見出し=catleg(per-frame)
    frame(MISS_FRAME)    # 見出し=missleg(per-frame)
    _ = f_meta           # meta行は廃止(下部はstatus帯とパレット状態パネルで使う)

    cv.save(OUT / "base.png")
    print("wrote", OUT / "base.png", cv.size)


if __name__ == "__main__":
    main()
