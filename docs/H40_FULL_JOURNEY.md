# The Journey to a Full-Screen H40 Sega CD FMV

*How the full-screen H40 (320×224, 40×28 = 1120 tiles) `machi` (街) ending was
made to stream from disc and play back cleanly — video and audio — with no
resolution or audio quality lost.*

Final result: **`S`(slip)=0 for the whole movie, steady 15 fps, clean and
synced audio, frame 0 a complete load.**
Build `20260711.e8.p3` — https://youtu.be/5C6t5pvUo-Y (unlisted).

---

## The system, in one paragraph

The codec is the **Tile Texture Reuse Codec (TTRC)**. `tools/sim.py` decides, per
cell, whether it is a *cold* tile (a fresh 32-byte pattern read from CD) or a
*reuse* of a pattern already resident in VRAM. `tools/pack_stream.py` turns those
decisions into `MOVIE.DAT`. On the console the **Sub CPU** (`movieplay_sp.s`)
streams the disc continuously and decodes each frame into a Word-RAM output
buffer; the **Main CPU** (`movieplay_ip.s`) DMAs that buffer to VRAM and displays
it. A 1M/1M Word-RAM double buffer swaps at each frame boundary. The disc is one
continuous read (`ROM_READN`, no seeking) so the Sub never stalls.

Full-screen H40 pushes every budget at once: 1120 cells updated per frame, big
control blocks, and a heavy CD delivery rate. It kept collapsing. Each chapter
below is one real cause that was found and fixed.

---

## Chapter 1 — Frame 0 was total noise

The first frame's patterns and control block were loaded by transferring CD
sectors **directly** into PRG/Word-RAM. Retries during that load let CD sectors
*slip* (a dropped sector), and the whole frame came up as garbage.

**Fix:** stage the load. Read each sector into a Word-RAM scratch (`PAD_SCR`),
then CPU-copy it to its destination (`drain_lin_staged`). The direct
CD-transfer-to-PRG path is what slipped; staging through Word-RAM removed it.

## Chapter 2 — Frame 1 was garbled

While frame 0 was being decoded (~40 ms), the Sub was busy and **stopped pumping
the CD**. The CD kept delivering; the CDC buffer overflowed and dropped ~3
sectors; frame 1's control block was then misaligned.

**Fix:** establish the streaming state (`drain_frame=1`, the apply buffer)
*before* frame 0 is decoded, and keep pumping the CD *during* the decode so the
CDC never overflows.

## Chapter 3 — Frame 0's patterns collided with the ring

Frame 0's patterns had been placed inside the streaming ring, so they overlapped
live streaming data and corrupted the occupancy math.

**Fix:** put frame 0's patterns in a dedicated Word-RAM scratch (`F0PAT_SCR`),
outside the ring. The ring's head/occupancy stays honest.

## Chapter 4 — The movie collapsed permanently mid-way

On heavy frames a CD sector would slip (an **MSF gap** — the sector header's
minute:second:frame counter jumps). Both the pattern ring and the control stream
then shifted, and the two desynced into a cascading freeze. Simply raising the
transfer-retry limit did **not** help — the sectors were genuinely lost.

**The user's idea, and the decisive fix.** Add a **sync header** to each control
block — a `frame_seq` number — so the player can *detect* desync. Then, at the
source, make `drain1` read every sector's MSF header and check it is
consecutive; on a gap, **re-seek** (`CDC_STOP` + `ROM_READN`) to the lost sector
and re-read it exactly. This recovers the precise data with no quality loss. 34
slips over the movie → all recovered, desync 0, full completion. The
continuous-read rule is technically broken, but only rarely, and the `frame_seq`
check is the backstop.

## Chapter 5 — The audio was garbled ("gabigabi")

Video now matched the encoder ideal, but the PCM was scrambled. The root cause
was a **data bug in the packer**, not the streaming: the audio WAV is *unsigned*
8-bit (silence = 128), but the packer converted it as *signed* 8-bit **and** fed
the WAV file header in as if it were audio samples. About 15% of samples were
wrong.

**Fix:** strip the header (read via the `wave` module) and convert
unsigned-8-bit (centre 128) to RF5C164 sign-magnitude correctly (silence →
`0x00`). Verified: sample garble 15% → 0%.

## Chapter 6 — The audio "rewound", then "lagged"

With the data fixed, the audio revealed a **rewind** (a chunk replaying) and, on
one attempted fix, a growing **lag**. The important realization: **every audio
symptom traced back to the re-seek recovery's *pause*.** Each re-seek freezes the
Sub for a moment; over the run these pauses slow the movie by ~9 s; the
fixed-rate PCM cannot follow a slowing picture, so it either *catches up and
skips* (the rewind) or, if we slow the playback frequency to match (an FD-follow
experiment), it *lags*. The user correctly reported the lag was worse than the
occasional glitch.

So the audio could not be truly fixed by touching the audio. **The drops
themselves had to go.**

## Chapter 7 — Driving the drops to zero

The user set the rule: **judge by the objective marker** (a red screen border and
an `S` counter when `slip_count > 0`), not by ear. Then every hypothesis was
tested against that marker.

- **Drain the CD more aggressively** (per-sector-type pump throttle) → drops got
  **worse** (more bus contention). Under-draining was not the cause.
- **Skip-realign recovery** (skip the lost sector, no pause) → **froze at F273**.
  The control stream is length-prefixed (`total_len`), so a hole in a dropped
  *control* sector makes the parser desync permanently. Only safe for *payload*
  holes.
- **Hybrid** (skip payload holes, re-seek only for control) → completed, but
  **more** drops (59 vs 38): the re-seek's `CDC_STOP` actually resets the CDC and
  suppresses cascade drops, which skip-realign loses.
- **Ring jitter margin (`RING_CAP`)** is **non-monotonic**: 300 → 80 drops
  (too little prebuffer starves the heavy early cluster), 350 → 38, **380 → 32**
  (more prebuffer, still under the back-pressure threshold, `under=0`). 380 was
  adopted — a free, no-quality-loss improvement.

The residual drops were not ring jitter at all — they were the **Sub-CPU load at
the scene-cut frames** (the opening montage's back-to-back full-screen
refreshes). No zero-quality-loss lever could remove them.

**The user relaxed exactly one constraint:** keep resolution and audio; keep
frame 0 a complete load; but it is acceptable to reduce the per-frame *raw*
(cold/new-tile) count. That pointed straight at the pack-level per-frame cold
cap.

**The final fix.** `pack_stream.py`'s `CBRSIM_PACK_MAXCOLD` caps how many new
tiles a frame may load, on the *existing* sim decisions (no re-sim, so
resolution/audio are byte-identical). Cells beyond the cap hold the previous
frame for one frame (a residual). Added a `i > 0` guard so **frame 0 is never
capped** (it stays a full load, per the user's requirement).

Measured against the marker:

| Cold cap | Slip marker `S` | Notes |
|---|---|---|
| none (≈262 peak) | 38 (re-seek) / 32 (RING_CAP 380) | drops + slowdown → audio compromise |
| 230 | 10 | drops return in the early montage |
| **200** | **0** | steady 15 fps, clear margin (no rate dips) |
| 150 | 0 | even more margin, lower quality |

**200 is the maximum-quality clean point.** With it: `S = 0` all the way, a
steady 15 fps, and — because there is no slowdown — the fixed-rate PCM stays
perfectly synced. No re-seeks, no rewind, no lag. Frame 0 remains a full load.
Resolution and audio are unchanged.

Canonical build (reproducible):

```sh
CBRSIM_W=320 CBRSIM_H=224 CBRSIM_MODE=H40 \
CBRSIM_RING_CAP_KB=380 CBRSIM_PACK_MAXCOLD=200 \
python3 tools/pack_stream.py \
  --dec-log videos/machi_ed_H40_320x224_pcm13/decisions.pkl \
  --audio  videos/machi_ed_H40_320x224_pcm13/audio_13k3_u8_mono.wav \
  --output out/movieplay/MOVIE.DAT
make disc DEBUG=1
```

> **Historical note (e9):** `CBRSIM_PACK_MAXCOLD` was later removed — the cold
> cap moved into the encoder (`CBRSIM_MAX_COLD` in `tools/sim.py`), with the
> realized ceiling asserted at pack time via `tools/av_config.py`
> (`COLD_CAP_REALIZED`). The command above no longer runs as-is; it is kept as
> the record of the fix as shipped at the time.

---

## What actually mattered

1. **An objective marker beat opinion.** The red slip counter turned "does it
   look OK?" into a number we could A/B every build against. Most wrong turns
   were killed by watching `S` go up.
2. **The sim is a model of the hardware.** When the hardware could not reproduce
   the sim no matter how we tuned, the honest answer was that the encode was
   over-reaching the hardware's real streaming capacity — not that we needed one
   more clever patch.
3. **Follow a symptom to its single root.** Three different audio symptoms
   (garble, rewind, lag) had one root: the re-seek pause. Fixing the root (remove
   the drops) fixed all three at once.
4. **Name the real trade-off and let the owner choose.** "Zero drops" and "zero
   quality loss" were both required and, at full scene-cut density, mutually
   impossible. Surfacing that honestly — instead of silently degrading — let the
   owner authorize the one small, well-scoped concession (fewer new tiles per
   heavy frame) that made everything else work.

---
---

# 全画面H40 セガCD FMV への旅路（日本語訳）

*全画面H40（320×224、40×28＝1120タイル）の `machi`（街）ED を、ディスクから
ストリーミングし、映像も音声もきれいに再生できるようにするまで。解像度も音声も
落とさずに達成した記録。*

最終結果：**通しで `S`（滑り）=0、定常15fps、音声はクリーンで同期、frame0 は完全
ロード。** ビルド `20260711.e8.p3` — https://youtu.be/5C6t5pvUo-Y （限定公開）。

---

## システムを一段落で

コーデックは **Tile Texture Reuse Codec（TTRC）**。`tools/sim.py` が各セルごとに、
*cold*（CDから新規に読む32Bのタイル）か *reuse*（VRAM常駐タイルの流用）かを決める。
`tools/pack_stream.py` がその決定を `MOVIE.DAT` にする。実機では **Sub CPU**
（`movieplay_sp.s`）がディスクを連続で読み続け、各コマを Word-RAM 出力バッファへ展開。
**Main CPU**（`movieplay_ip.s`）がそれを VRAM へ DMA して表示する。1M/1M の
Word-RAM ダブルバッファをコマ境界で入替。ディスクは1本の連続読み（`ROM_READN`、
シーク無し）なので Sub は止まらない。

全画面H40はあらゆる予算を同時に攻める：1コマ1120セル更新、大きな control ブロック、
重いCD供給レート。何度も崩壊した。以下の各章が、見つけて直した「本当の原因」ひとつずつ。

---

## 第1章 — frame0 が全面ノイズ

先頭コマのパターンと control を、CDセクタを PRG/Word-RAM へ**直接**転送して読んで
いた。読み込み中のリトライでCDセクタが*滑り*（1セクタ脱落）、コマ全体が化けた。

**修正：**一旦 Word-RAM のスクラッチ（`PAD_SCR`）へ受けてから CPU コピーする
（`drain_lin_staged`）。直接 CD→PRG 転送が滑りの元。Word-RAM 経由で解消。

## 第2章 — frame1 が化ける

frame0 の展開中（約40ms）、Sub が忙しく**CDを吸うのを止めて**いた。CDは供給を続け、
CDCバッファが溢れて約3セクタを落とし、frame1 の control がズレた。

**修正：**ストリーミング状態（`drain_frame=1`、apply バッファ）を frame0 展開の
*前*に確立し、展開*中*もCDを吸い続けてCDCを溢れさせない。

## 第3章 — frame0 のパターンがリングと衝突

frame0 のパターンをストリーミングのリング内に置いていたため、生きた配信データと
重なり、占有量計算を壊した。

**修正：**frame0 のパターンをリング外の専用 Word-RAM スクラッチ（`F0PAT_SCR`）へ。
リングの head/占有量が正しく保たれる。

## 第4章 — 中盤で永久崩壊

重いコマでCDセクタが滑る（**MSFギャップ**＝セクタヘッダの分秒フレーム番号が飛ぶ）。
パターンリングと control ストリームの両方がズレ、連鎖して凍結。転送リトライ上限を
上げても直らなかった＝本当にセクタが失われている。

**ユーザーの発案が決定打。**各 control ブロックに**同期ヘッダ**（`frame_seq` 番号）を
入れ、プレイヤが desync を*検知*できるようにする。さらに大元で、`drain1` が各セクタの
MSFヘッダを読んで連番かを検査し、ギャップ時に失セクタへ**再シーク**（`CDC_STOP` +
`ROM_READN`）して厳密に読み直す。品質無劣化の正確な回復。通しで34回の滑り→全て回復、
desync 0 で完走。連続読み原則は技術的には破るが稀で、`frame_seq` 検査がバックストップ。

## 第5章 — 音声がガビガビ

映像はエンコーダ理想と一致したが、PCMが壊れていた。真因はストリーミングではなく
**packのデータバグ**：音声WAVは*符号なし*8bit（無音=128）なのに、packが*符号あり*
8bitとして変換し、**かつ**WAVファイルのヘッダを音声サンプルとして食わせていた。
約15%のサンプルが誤り。

**修正：**ヘッダを剥がし（`wave` モジュールで読む）、符号なし8bit（中心128）を
RF5C164 のサイン・マグニチュードへ正しく変換（無音→`0x00`）。実測：乱れ15%→0%。

## 第6章 — 音声が「巻き戻り」、次に「遅延」

データを直すと、音声に**巻き戻り**（塊の再生）が露見し、ある修正試行では増大する
**遅延**が出た。重要な気づき：**音声の症状は全て再シーク回復の*一時停止*に由来する。**
各再シークは一瞬Subを止め、通しで映像を約9秒遅らせる。固定レートPCMは遅れる絵に追従
できず、*追いついて飛ぶ*（巻き戻り）か、再生周波数を合わせて遅くすると（FD追従の実験）
*遅延する*。ユーザーの「遅延の方が時々のグリッチより悪い」は正しかった。

つまり音声側をいじっても真には直らない。**落とし自体を消すしかない。**

## 第7章 — 落としをゼロへ

ユーザーがルールを定めた：**客観マーカーで判定**（`slip_count > 0` で赤枠＋`S`
カウンタ）、耳ではなく。以降、全仮説をそのマーカーで検証。

- **CDをもっと積極的に吸う**（セクタ種別別の pump throttle）→ 落ちが**悪化**（バス
  競合増）。吸い不足が原因ではなかった。
- **skip-realign 回復**（失セクタを飛ばし一時停止なし）→ **F273 で凍結**。control
  ストリームは長さ前置き（`total_len`）なので、*control* セクタの穴で parser が永久
  desync。*payload* の穴にしか使えない。
- **ハイブリッド**（payload の穴は skip、control のみ再シーク）→ 完走するが落ち増
  （59 対 38）：再シークの `CDC_STOP` は実は CDC をリセットして連鎖落ちを抑えており、
  skip-realign はそれを失う。
- **リングのジッタ余裕（`RING_CAP`）**は**非単調**：300 → 80（prebuffer 不足で重い
  序盤が starve）、350 → 38、**380 → 32**（prebuffer 増、back-pressure 閾値の下、
  `under=0`）。380 採用＝無劣化の無料改善。

残る落ちはリングのジッタではなく、**シーン転換コマの Sub-CPU 負荷**（冒頭モンタージュの
連続全画面リフレッシュ）。無劣化のレバーでは消せなかった。

**ユーザーが緩めた制約はただ一つ：**解像度と音声は維持、frame0 は完全ロード維持、
ただし1コマの *raw*（cold＝新規タイル）数を減らすのは可。これが pack 段の per-frame
cold cap を直接指した。

**最終修正。**`pack_stream.py` の `CBRSIM_PACK_MAXCOLD` が、*既存の* sim 決定に対して
（再sim不要＝解像度・音声はバイト単位で不変）、1コマが読める新規タイル数を制限。上限
超過のセルは1コマ前の絵を保持（残像）。`i > 0` ガードを追加し **frame0 は決して cap
しない**（完全ロード維持、ユーザー要件）。

マーカーでの実測：

| cold cap | 滑りマーカー `S` | 備考 |
|---|---|---|
| なし（ピーク≈262） | 38（再シーク）/ 32（RING_CAP 380） | 落ち＋減速→音声妥協 |
| 230 | 10 | 序盤モンタージュで落ち再発 |
| **200** | **0** | 定常15fps、明確な余裕（rate dip なし） |
| 150 | 0 | さらに余裕大・画質は下 |

**200 が画質最優先で通る最大点。**これで通し `S = 0`、定常15fps、そして減速が無いので
固定レートPCMが完全同期。再シークなし、巻き戻りなし、遅延なし。frame0 は完全ロード維持。
解像度・音声は不変。

再現ビルド（canonical）：

```sh
CBRSIM_W=320 CBRSIM_H=224 CBRSIM_MODE=H40 \
CBRSIM_RING_CAP_KB=380 CBRSIM_PACK_MAXCOLD=200 \
python3 tools/pack_stream.py \
  --dec-log videos/machi_ed_H40_320x224_pcm13/decisions.pkl \
  --audio  videos/machi_ed_H40_320x224_pcm13/audio_13k3_u8_mono.wav \
  --output out/movieplay/MOVIE.DAT
make disc DEBUG=1
```

> **後日注（e9）:** `CBRSIM_PACK_MAXCOLD` はその後撤去された。コールド上限は
> エンコーダ側（`tools/sim.py` の `CBRSIM_MAX_COLD`）へ移り、実現値の上限は
> `tools/av_config.py`（`COLD_CAP_REALIZED`）を単一の真実源として pack 時に
> 検証される。上のコマンドはそのままでは動かないが、当時の修正の記録として残す。

---

## 本当に効いたこと

1. **客観マーカーが意見に勝つ。**赤い滑りカウンタが「良さそうに見える？」を、毎ビルド
   A/B できる数値に変えた。多くの誤りは `S` が増えるのを見て潰した。
2. **sim は実機のモデル。**どう詰めても実機が sim を再現できないなら、正直な答えは
   「エンコードが実機の実ストリーミング能力を超過している」であり、もう一つ賢いパッチ
   ではない。
3. **症状を単一の根へ辿る。**3つの異なる音声症状（ガビガビ・巻き戻り・遅延）の根は
   一つ、再シークの一時停止。根（落ちの除去）を直すと3つ同時に直った。
4. **本当のトレードオフを名指しし、オーナーに選ばせる。**「落ちゼロ」と「画質無劣化」は
   両方要求され、フルなシーン転換密度では両立不能だった。黙って劣化させるのではなく
   正直に示したことで、全てを成立させる一つの小さく限定された譲歩（重いコマの新規タイル
   数を減らす）をオーナーが承認できた。
