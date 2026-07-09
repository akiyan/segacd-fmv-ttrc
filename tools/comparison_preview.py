#!/usr/bin/env python3
"""Comparison フレーム(1920x1080)のレイアウトを『ダミー値』で1枚だけ描くプレビュー。

Analysis(layout_preview.py)と同じ流儀で、sim/ffmpeg を回さず秒で反復するためのもの。

構成:
  最上部 = 見出しタイトル + 諸元(mode/res/audio/fps ...) を横に並べる。右端に同期
           フレームカウンタ(左右はこの frame 番号で同期、F00000 起点)。
  中段  = 左右に2動画(内容は常に横長なのでパネル自体を 4:3 にして黒帯を出さない):
            左 = Real output (エミュレータ名, バージョン) ← 実機/エミュ録画
            右 = Encoder ideal output                              ← エンコーダの理想出力
          各動画の見出しは枠の上、音声どちらかのバッジは枠の左下内側。
          音声は2トラック想定(track1=Emulator が既定, track2=Encoder)。
  下段  = Analysis と共通のフッター(layout_preview.draw_footer):
          status帯(Req/Comp/Buff/DMA + パレット + 3段タイムライン) + カテゴリ合計バー。

レイアウトを固めたら、実データ版(render_comparison 相当)へこの描画関数を流用する。

出力: tmp/comparison_preview.png
usage: python3 tools/comparison_preview.py
"""
from pathlib import Path
from PIL import Image, ImageDraw
import layout_preview as L

# --- 最上部タイトル行 ---
TITLE_BASE = 42                                          # タイトル/諸元/フレームの共通ベースライン

# --- 中段: 左右2枠(4:3固定=黒帯なし) ---
SIDE = 40                                                # 左右マージン
GAP = 40                                                 # パネル間
PANEL_W = (L.CW - SIDE * 2 - GAP) // 2                   # 900
PANEL_H = PANEL_W * 3 // 4                               # 675 (4:3)
LABEL_GAP = 12                                           # パネル見出しベースライン↔枠上端
REGION_TOP = 66                                          # タイトル行の下
REGION_BOT = L.STATUS_XY[1] - 4                          # フッター開始の直前(978)
# 見出し+パネルのブロックを上下中央へ
_block_h = LABEL_GAP + 30 + PANEL_H                      # (見出し行の高さ約30 + 間 + パネル)
PANEL_TOP = REGION_TOP + (REGION_BOT - REGION_TOP - _block_h) // 2 + (LABEL_GAP + 30)
LABEL_BASE = PANEL_TOP - LABEL_GAP

CMP_L = (SIDE, PANEL_TOP, SIDE + PANEL_W, PANEL_TOP + PANEL_H)
CMP_R = (SIDE + PANEL_W + GAP, PANEL_TOP, SIDE + PANEL_W + GAP + PANEL_W, PANEL_TOP + PANEL_H)


def video_panel(cv, d, rect, title, meta, seed, audio_label, audio_muted):
    """1枠 = 4:3 の動画(黒帯なし・枠内いっぱい)。上に見出し(+meta)、左下に音声バッジ。"""
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    cv.paste(L.dummy_image(w, h, seed), (x0, y0))        # 4:3 の動画が枠を埋める
    d.rectangle([x0, y0, x1, y1], outline=L.COL_BORDER)

    # 見出し(枠の上・共通ベースライン)。パネル見出しは小さめ(f_lbl)。
    d.text((x0 + 2, LABEL_BASE), title, fill=L.COL_TXT, font=L.f_lbl, anchor="ls")
    if meta:
        d.text((x0 + 2 + L._w(L.f_lbl, title) + 10, LABEL_BASE), meta,
               fill=L.COL_DIM, font=L.f_leg, anchor="ls")

    # 音声バッジ(枠の左下内側)。既定トラックは明るく、非既定は暗く。
    col = L.COL_DIM if audio_muted else L.COL_TXT
    d.text((x0 + 8, y1 - 8), audio_label, fill=col, font=L.f_leg, anchor="ls")


def draw_top_title(d, data):
    """最上部: 見出しタイトル + 諸元 + 右端に同期フレームカウンタ。"""
    hx = SIDE
    title = "SEGA-CD Tile Texture Reuse Codec: Real vs Ideal"
    d.text((hx, TITLE_BASE), title, fill=L.COL_TXT, font=L.f_head, anchor="ls")
    specs = " / ".join([data["mode"], data["res"], data["audio"], "%dfps" % data["fps"]])
    d.text((hx + L._w(L.f_head, title) + 14, TITLE_BASE), specs,
           fill=L.COL_DIM, font=L.f_meta, anchor="ls")

    # 右端: 同期フレームカウンタ(左右はこの frame 番号で同期、F00000 起点)
    frame = data["frame"]
    lab = "sync Frame:"
    fw = L._w(L.f_meta, lab) + L._w(L.f_meta, str(frame).rjust(5, "0"))
    fx = L.CW - SIDE - fw
    fy = TITLE_BASE - L.f_meta.getmetrics()[0]
    d.text((fx, TITLE_BASE), lab, fill=L.COL_DIM, font=L.f_meta, anchor="ls")
    L.draw_padnum(d, fx + L._w(L.f_meta, lab), fy, frame, 5, L.f_meta, L.COL_TXT)


def main():
    L.load_fonts()
    data = L.dummy_data()                                # フッターはそのまま流用
    cv = Image.new("RGB", (L.CW, L.CH), L.BG)
    d = ImageDraw.Draw(cv)

    draw_top_title(d, data)
    video_panel(cv, d, CMP_L, "Real output", "(Genesis Plus GX 1.7.4)",
                seed=31, audio_label="audio 1 · Emulator (default)", audio_muted=False)
    video_panel(cv, d, CMP_R, "Encoder ideal output", None,
                seed=32, audio_label="audio 2 · Encoder ideal", audio_muted=True)
    L.draw_footer(cv, data)                              # Analysis と共通フッター

    out = Path("tmp/comparison_preview.png")
    out.parent.mkdir(exist_ok=True)
    cv.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
