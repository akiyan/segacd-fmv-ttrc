#!/usr/bin/env python3
"""Comparison フレーム(1920x1080)のレイアウトを『ダミー値』で1枚だけ描くプレビュー。

Analysis(layout_preview.py)と同じ流儀で、sim/ffmpeg を回さず秒で反復するためのもの。

構成:
  上 = 左右に2動画を並べる(下端はフッター開始に合わせる)
        左  = MEGA-CD Emulator output (エミュレータ名, バージョン) ← 実機/エミュ録画
        右  = Encoder ideal output                              ← エンコーダの理想出力
       左右は frame 番号で同期(F00000 起点)。右上に共通 Frame カウンタ。
       音声は2トラック作る想定(track1=Emulator が既定, track2=Encoder)。
       どちらの音声かを各パネル左下に小さく表示。
  下 = Analysis と共通のフッター(layout_preview.draw_footer):
       status帯(Req/Comp/Buff/DMA + パレット + 3段タイムライン) + カテゴリ合計バー。

レイアウトを固めたら、実データ版(render_comparison 相当)へこの描画関数を流用する。

出力: tmp/comparison_preview.png
usage: python3 tools/comparison_preview.py
"""
from pathlib import Path
from PIL import Image, ImageDraw
import layout_preview as L

# 上部2枠。下端はフッター開始(STATUS_XY[1]=982)の直前=Analysis のメイン枠下端(978)に合わせる。
TOP_Y0, TOP_Y1 = L.MAIN_FRAME[1], L.MAIN_FRAME[3]        # 52, 978
GAP = 40
PW = (L.CW - 40 * 2 - GAP) // 2                          # パネル幅(左右均等)
CMP_L = (40, TOP_Y0, 40 + PW, TOP_Y1)                    # 左: MEGA-CD Emulator output
CMP_R = (40 + PW + GAP, TOP_Y0, 40 + PW + GAP + PW, TOP_Y1)  # 右: Encoder ideal output

DISP_AR = 4.0 / 3.0                                      # 表示アスペクト(H32 256x224 → 4:3)


def video_panel(cv, d, rect, title, meta, seed, audio_label, audio_muted):
    """1枠: 見出し(+meta) + 表示アスペクトでレターボックスしたダミー動画 + 左下の音声バッジ。"""
    L.panel(d, rect)
    by = rect[1] - 10                                    # 見出しベースライン(Analysisと共通の流儀)
    d.text((rect[0] + 2, by), title, fill=L.COL_TXT, font=L.f_head, anchor="ls")
    if meta:
        d.text((rect[0] + 2 + L._w(L.f_head, title) + 12, by), meta,
               fill=L.COL_DIM, font=L.f_meta, anchor="ls")

    # 内側にレターボックス配置(黒地 + 中央にダミー映像)
    iw = rect[2] - rect[0] - 2 * L.PAD
    ih = rect[3] - rect[1] - 2 * L.PAD
    if iw / ih > DISP_AR:
        vh = ih; vw = int(round(ih * DISP_AR))
    else:
        vw = iw; vh = int(round(iw / DISP_AR))
    ox = rect[0] + L.PAD + (iw - vw) // 2
    oy = rect[1] + L.PAD + (ih - vh) // 2
    cv.paste(Image.new("RGB", (iw, ih), (0, 0, 0)), (rect[0] + L.PAD, rect[1] + L.PAD))
    cv.paste(L.dummy_image(vw, vh, seed), (ox, oy))

    # 音声バッジ(映像の左下角の内側)。既定トラックは明るく、非既定は暗く。
    col = L.COL_DIM if audio_muted else L.COL_TXT
    d.text((ox + 8, oy + vh - 8), audio_label, fill=col, font=L.f_leg, anchor="ls")


def draw_top(cv, d, data):
    """Comparison 上部: 左右2動画 + 同期フレームカウンタ。"""
    frame = data["frame"]
    video_panel(cv, d, CMP_L,
                "MEGA-CD Emulator output", "(Genesis Plus GX 1.7.4)",
                seed=31, audio_label="audio 1 · Emulator (default)", audio_muted=False)
    video_panel(cv, d, CMP_R,
                "Encoder ideal output", None,
                seed=32, audio_label="audio 2 · Encoder ideal", audio_muted=True)

    # 右上に共通フレームカウンタ(左右はこの frame 番号で同期。F00000 起点)。右端揃え。
    by = TOP_Y0 - 10
    lab = "sync Frame:"
    fw = L._w(L.f_meta, lab) + L._w(L.f_meta, str(frame).rjust(5, "0"))
    fx = CMP_R[2] - fw
    fy = by - L.f_meta.getmetrics()[0]
    d.text((fx, by), lab, fill=L.COL_DIM, font=L.f_meta, anchor="ls")
    L.draw_padnum(d, fx + L._w(L.f_meta, lab), fy, frame, 5, L.f_meta, L.COL_TXT)


def main():
    L.load_fonts()
    data = L.dummy_data()                                # フッターはそのまま流用
    cv = Image.new("RGB", (L.CW, L.CH), L.BG)
    d = ImageDraw.Draw(cv)

    draw_top(cv, d, data)
    L.draw_footer(cv, data)                              # Analysis と共通フッター

    out = Path("tmp/comparison_preview.png")
    out.parent.mkdir(exist_ok=True)
    cv.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
