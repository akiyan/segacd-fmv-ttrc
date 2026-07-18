---
name: compilation
description: Prepare and upload an existing, verified record lossless playback capture to YouTube. Bake the validated H32/H40 pixel aspect into a high-resolution square-pixel nearest-neighbor raster, add boot-aware CRAM chapters and project metadata, verify the result, and upload without recording, trimming, or using the DEBUG HUD for head cueing. Use for "実機録画をアップ", "playback recording upload", or "/compilation" after record has produced the latest capture.
---

# compilation — 録画済み再生映像をYouTubeへ

`record` が作成・検証した同期録画を、YouTube向けに整形して公開する。
エミュレータ録画を物理実機録画とは呼ばない。

## 役割境界

このスキルが担当するもの:

- 最新ビルドを収録した検証済みロスレスMKVの選択
- 表示モードに対応するPARのsquare-pixel高解像度化、nearest拡大、配信用エンコード
- 起動画面を考慮したCRAMチャプターとYouTubeメタデータ
- 最終ファイルの検証とアップロード

このスキルでは行わないもの:

- discのビルド、RetroArch起動、START入力、録画、同期検証
- DEBUGビルドの要求、HUD OCR、`F0000`探索
- `-ss` / `-t`による頭出しや映画部分だけの切り出し

録画が無い、またはコード・データより古い場合は、ここへ録画手順を複製せず
`record` を先に実行してから戻る。アップロードは常に最新成果物を使う。

## 入力

- `record` が作成したネイティブ解像度のロスレスMKV
- 同録画のRetroArchログ、音声JSON、タイミング確認結果
- 対応するsim出力ディレクトリ（CRAMチャプター用）
- `tools/av_version.txt` の現行ビルド版

DEBUG HUDは任意の診断表示であり、入力条件ではない。

## YouTube用square-pixel raster

| mode | 入力raster / PAR | 出力raster | nearest倍率 | 出力SAR |
|---|---:|---:|---:|---:|
| H32 | 256x224 / 8:7 | 2048x1568 | 横8倍・縦7倍 | 1:1 |
| H40 | 320x224 / 32:35 | 2048x1568 | 横6.4倍・縦7倍 | 1:1 |

H32とH40は異なるドット幅で同じ64:49の表示領域を表す。YouTubeへ非正方形
画素の拡大を任せず、2048x1568へnearestで変換してPARを画素数へ焼き込み、
`setsar=1`で渡す。H32は各入力画素が正確に8x7の同色ブロックになる。H40は
実用サイズでは完全な整数比にできないため、色を混ぜないnearestで6列/7列へ
振り分ける。`mode4` は推測値を足さず、geometry harnessでPARを検証してから
対応する。

## 手順

1. **入力を確認する**

   `ffprobe`で映像・音声、raster、約59.94fps、durationを確認する。対応する
   音声JSONとRetroArchログも確認し、壊れた録画や未検証の録画を使わない。
   `record`の既定である固定Replay高速録画は、要求されたpacket/decoded-frame数、
   正常終了、音声検査、代表フレーム確認を通ったFFV1/FLACなら正式な入力として使う。

2. **起動画面を残したまま配信用ファイルを作る**

   ```sh
   ffmpeg -i videos/INPUT_lossless.mkv \
     -vf "scale=2048:1568:flags=neighbor,setsar=1" \
     -c:v libx264 -crf 10 -preset slow -pix_fmt yuv420p \
     -c:a aac -b:a 192k -movflags +faststart \
     videos/STEM_emu.mp4
   ```

   `INPUT_lossless.mkv`と`STEM`は実値へ置き換える。nearest拡大そのものは
   新しい色を作らず、H32では8x7の完全な整数拡大になる。ただしYouTubeは必ず
   再エンコードするため、最終配信までロスレスとは呼ばない。CRF 10の高品質な
   入力を渡し、YouTube側の高解像度配信を使う。`-ss`、`-t`、fps filter、`-r`は
   追加しない。録画開始からのMega-CD起動画面、CD player、START遷移、映画、
   末尾をそのまま残す。

3. **起動画面込みのCRAMチャプターを作る**

   完成映像を普通に再生し、映画frame 0が見え始める時刻を秒単位で確認する。
   HUDやOCRは使わない。この時刻はチャプターだけをずらす値であり、映像は切らない。

   ```sh
   tools/python.sh tools/youtube_chapters.py SIM_OUT CONTENT_FPS \
     --content-offset MOVIE_START_SECONDS \
     --intro-label "Mega-CD startup"
   ```

   出力を説明文の先頭へ置く。`00:00 Mega-CD startup`の後に、映画開始時刻を
   加えたCRAM区間が並ぶ。

4. **最終ファイルを確認する**

   - 冒頭にMega-CD起動画面が残っている
   - 映像と音声があり、durationが入力とほぼ同じ
   - fpsが入力から変わっていない
   - rasterが2048x1568、SARが1:1、DARが64:49
   - 映画開始後の絵が縦長・横長になっていない

   `tools/extract_verification_frames.sh`で完成MP4から起動・本編・末尾を名前付き抽出する。
   出力先には`videos/<stem>/compilation_check`をbaseとして渡し、毎回新しく作られる
   source固有directoryの`manifest.tsv`とmontageだけを確認する。共有directoryの
   `*.png`をmontageせず、以前の録画・変換から残ったloose stillを混ぜない。

5. **YouTubeへアップロードする**

   タイトル、英語→日本語の説明、CRAMチャプター、公開範囲、カテゴリ、再アップロード
   の扱いは `AGENTS.md` の「YouTube Upload Style」を唯一の規約として使う。ここへ
   同じ規約を複製しない。アップロードはunlisted、category 20とし、説明文の英日
   両方に `https://github.com/akiyan/segacd-fmv-ttrc` を含める。

   ```sh
   PY="$HOME/.config/youtube/venv/bin/python"
   "$PY" "$HOME/.claude/skills/youtube/youtube.py" upload \
     videos/STEM_emu.mp4 \
     --title "$TITLE" --desc "$DESCRIPTION" \
     --privacy unlisted --category 20
   ```

   同じ成果物を再アップロードするときだけ、`AGENTS.md`に従って`--force`を足す。

6. **報告する**

   最新のYouTube URL、最終MP4のパス、duration、raster/SAR/DAR、音声の有無、
   起動画面を保持したことを明示する。
