---
name: real-upload
description: Record the built disc's hardware playback (native resolution, lossless), trim to the movie via the debug HUD frame counter, upscale losslessly (integer nearest + PAR metadata, 60fps), and upload the standalone hardware recording to YouTube. Works for any display mode (H32/H40/mode4). Use for "実機録画をアップ", "hardware playback upload", or "/real-upload".
---

# real-upload — 実機再生そのものを録画してYouTubeへ

comparison ではなく **実機パス再生の単体映像** を、ドット無劣化・60fps で公開する
標準手順。画面モードが変わってもモード表を引くだけで同じ手順が使える。

## 前提

- `out/MOVIEPLAY.cue` が対象の `MOVIE.DAT` でビルド済みであること。
- **DEBUG=1 ビルドであること**（頭出しに HUD の F カウンタを使うため）。
  `make disc DEBUG=1`
- 共有マシン排他: 開始前に必ず確認し、**検出したら中断して待つ**
  （`ps ... | grep -iE "sim\.py|render_|retroarch|Xvfb|record_movie|run_headless" && { echo BUSY; exit 1; }`）。
  echoだけして続行する形は禁止（過去に違反事故）。

## モード表

| mode  | 録画サイズ(--record-size) | PAR(setsar) | 4x出力解像度 |
|-------|---------------------------|-------------|--------------|
| H32   | 256x224 (native H32 + 22-cell HUD) | 8/7  | 1024x896     |
| H40   | 320x224                   | 32/35       | 1280x896     |
| mode4 | 256x192                   | 7/6 (要実測) | 1024x768    |

- PAR はメタデータ(setsar)で保持し、**リサンプリングは一切しない**。
  H32はnative 256x224、H40はnative 320x224で録画する。HUDは両モードで
  同じ連続22セル（`FxxxxPxxSxxDxxRxxLxxxx`）を使う。

## 手順

1. **録画（ロスレス・ネイティブ解像度）**
   RetroArch は録画開始時(BIOS)のジオメトリに固定されるため、
   `--record-size` でモードのネイティブサイズを明示する:

   ```sh
   tools/run_headless.sh out/MOVIEPLAY.cue --tag real --shots 1 --interval 60 \
     --boot-wait 12 --record tmp/real.mkv --record-preset ffv1-flac \
     --record-size <モード表のサイズ>
   ```

   - `--interval` はヘッドレスの高速化(~7x)込みで全編が入る長さに
     （エミュ時間 ≈ boot~110s + 動画長 + 15s）。
   - 長時間タスクは割り込みで死ぬため `setsid nohup ... &` で切り離すこと。

2. **F0000（映画開始）のシーク時刻を実測**
   適当な2〜3点で HUD を読む: `-ss t` で1フレーム抽出 →
   `python3 tools/read_frameno.py <png>` → `t0 = t - F/15`。
   複数点で t0 が一致（線形）することを確認する。

3. **切り出し＋無劣化拡大エンコード（60fpsそのまま）**

   ```sh
   ffmpeg -ss <t0-0.1> -t <動画長+1.5> -i tmp/real.mkv \
     -vf "scale=<4x解像度>:flags=neighbor,setsar=<PAR>" \
     -c:v libx264 -crf 16 -preset slow -pix_fmt yuv420p \
     -c:a aac -b:a 192k videos/<stem>_emu.mp4
   ```

   - `<stem>` は AGENTS.md の規約: `<input>_<mode>_<WxH>_<audio>`
     （例 `machi_op_H40_320x144_pcm13`）。
   - フレームレートは触らない（59.94fpsのまま）。fpsフィルタ・-r 禁止。

4. **確認**: 1フレーム抽出して HUD が鮮明・アスペクトが 4:3 相当
   （ffprobe で `display_aspect_ratio` を見る）ことを確認。

5. **YouTubeアップ**（youtube スキル、常に新規URL・上書き禁止）

   ```sh
   "$PY" ~/.claude/skills/youtube/youtube.py upload videos/<stem>_emu.mp4 \
     --title "SEGA-CD FMV of <work> - <mode> <WxH>/<grid> <content-fps> <audio>, <video-fps> playback (<エミュ名>) <YYYYMMDD.eN.pM>" \
     --privacy unlisted --category 20 --desc "<下記構成>"
   ```

   タイトル規約(fpsは2つとも明記): 諸元部に**コンテンツfps**(例 15fps)、末尾に
   **動画fps**(=キャプチャfps, 例 60fps)を入れる。**"hardware"を名乗らない**
   (エミュ録画のため)エミュレータ名を括弧で明記。**末尾にビルド版
   `YYYYMMDD.eN.pM`** を付ける(`tools/av_version.txt`。e=エンコーダ, p=プレイヤー。
   出力に影響する変更で該当を上げ、番号は戻さない。MOVIE.DATヘッダ版とは別)。
   例: `... 40x18 15fps PCM, 60fps playback (Genesis Plus GX) 20260710.e1.p1`。

   説明文（英→日の順、`<`/`>` 文字は使わない）:
   1. 概要: プレイヤ実装パスの再生をエミュレータでロスレスキャプチャ、
      Nxニアレスト無劣化拡大、PARはメタデータ保持。動画fps(60fps)と
      コンテンツfps(例15fps)は**諸元側に**書く。
   2. デバッグHUDの読み方: F=フレーム番号 / P=パレット区間 (左上1行)。
   3. 出力/ソース諸元（modeバイト、グリッド、fps、PCM、CBR、tank、cold cap、
      CD連続読み、15秒静止ループ）。ビットレートはソース行に書かない。
   4. プロジェクトURL: https://github.com/akiyan/segacd-fmv-ttrc （英日両方に）。

6. 報告時は**最新URLを明示**する（過去URLはキャッシュと紛らわしいため）。
