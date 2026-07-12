# Reasoning Without an Advisor — A Field Method for Not Guessing

*The advisor was a stronger reviewer that saw the whole context and pushed back
before you committed to a wrong path. When it is gone, you have to be your own
reviewer. This is the discipline that replaces it. Every principle below is drawn
from the full-screen H40 investigation ([`H40_FULL_JOURNEY.md`](H40_FULL_JOURNEY.md)),
so the examples are real.*

## The core loop

**Observe → Hypothesize → Predict → Experiment → Decide.** Never patch on a
hunch. If you are about to change code because "it might be this," stop and turn
"might" into a prediction you can measure.

---

## 1. Step back before you patch （立ち戻る）

When a symptom keeps coming back, the reflex is to patch it again. Resist it.
Ask **which layer** the fault is in and go to the root. Prove each layer innocent
**in order** — data → logic → assembly → regression → time-bisect →
environment — and blame the environment *last*, not first.

> *H40:* the audio symptom kept mutating — garble, then rewind, then lag.
> Patching each one was a treadmill. Stepping back revealed a single root (the
> re-seek pause) behind all three.

## 2. Question your assumptions and your model （前提を疑う）

Write down what you **believe** is true as one plain sentence, then attack it.
The simulator is a *model* of the hardware. When the hardware will not match the
model no matter how you tune, suspect the model or the encode's over-reach — not
just one more knob.

> *H40:* "the drops come from under-draining the CD" → tested by draining more →
> it got **worse**. The belief was false and the whole direction flipped.

## 3. Verify with an experiment; change ONE thing （実験で確かめる）

Do not reason your way to a conclusion and ship it. Build the A/B and measure.
Change **exactly one** variable per test — if you change three, you have learned
nothing about which one mattered. Use the **same metric** across every build.

> *H40:* scanning `RING_CAP` = 300 / 350 / 380 against the *same* slip counter
> revealed a non-monotonic curve (300 was worse, not better) that pure reasoning
> had gotten backwards.

## 4. Make the failure observable — add a marker （デバッグマーカー）

You cannot debug what you cannot see. Add an on-screen counter, a coloured
border, a small detector script, a "freeze at frame N" mode. An objective signal
removes human-judgment noise: *"it looks cleaner"* is not data; *"`S` went
38 → 0"* is.

> *H40:* the slip marker (red border + an `S` count when a sector was dropped)
> was the single most valuable tool. It turned a subjective mess into
> A/B-testable numbers, and the user made it the sole pass/fail judge.

## 5. Quantify — write a detector, don't trust impressions

Eyeballing dithered or compressed frames misleads: a one-pixel sampling shift
flips half the dither pixels. Write a small detector and run it over recordings
of each build. A "dark isolated horizontal dash" detector once pinned a Word-RAM
DMA first-word bug in a single pass (3.2 defects/frame vs a 0.4 false-positive
floor).

## 6. Distrust a single stochastic run

Timing-dependent bugs vary run to run. One clean run is **not** proof. Look for
**margin**: a steady frame rate with no dips means headroom; a pass that barely
holds will break on a different run. A cap that gives `S=0` *with visible margin*
is trustworthy; one that gives `S=0` right at the edge is not.

## 7. Bisect in time and space

Freeze at frame N and binary-search N to find the **first** bad frame, instead of
staring at post-collapse garbage. Do the same in space: which cell, which sector,
which layer. Find the earliest point where reality diverges from intent.

## 8. Be your own skeptic — argue the opposite （アドバイザーの代役）

Before committing to an interpretation, ask: **"If I'm wrong, what would I
see?"** Then go look for exactly that. When your evidence and your plan disagree,
do not silently follow the plan — state the conflict out loud and settle it with
one more check. This adversarial pass is precisely what the advisor did for you.

## 9. Protect the deliverable before long or irreversible steps

Save, commit, or write the file **before** a long render, an upload, or anything
that could end the session. A durable result survives a crash; an in-memory one
does not. Make the artifact real, then do the slow thing.

## 10. Name the trade-off; let the owner decide

When two hard requirements are mutually impossible — *zero drops* **and** *zero
quality loss* — do **not** silently degrade one of them. Put the honest fork in
front of the owner, with data. Often the owner relaxing a single constraint by a
hair (here: "fewer new tiles per heavy frame is acceptable") unlocks everything
else. Silent degradation is a betrayal; a named trade-off is a decision.

## 11. Keep the experiments valid — respect the process rules

- **Shared machine:** never run the simulator and the emulator at the same time;
  they steal each other's cores and corrupt each other's timing. One recording at
  a time.
- **A failed run is a failed run:** a black or silent recording is *failed data*,
  not a result — check the log before you trust it. A crashed emulator's log ends
  abruptly; a healthy one ends with shutdown lines.
- **The recording is not pixel-exact:** verify pixel-level claims only through
  integer paths (native-resolution lossless capture), not scaled screenshots.

---

## The one sentence to keep

The advisor's real gift was never the answers — it was the **pause before
acting**. Keep the pause. Write the hypothesis, design the check, let the data
speak. Guessing is fast and usually wrong; this loop is a little slower and
usually right.

---
---

# アドバイザー無しで考える — あてずっぽうにならないための実地メソッド（日本語訳）

*アドバイザーは、全体の文脈を見て、あなたが誤った道に踏み込む前に押し戻してくれる、
より強力なレビュアーだった。それが無くなったら、自分が自分のレビュアーになるしかない。
これはその代わりになる規律である。以下の各原則は全画面H40の調査
（[`H40_FULL_JOURNEY.md`](H40_FULL_JOURNEY.md)）から取っており、例はすべて実話。*

## 中核のループ

**観察 → 仮説 → 予測 → 実験 → 判断。**勘でパッチしない。「たぶんこれだ」でコードを
変えようとしたら止まって、その「たぶん」を測れる予測に変える。

---

## 1. パッチする前に立ち戻る （立ち戻る）

症状が繰り返すと、また同じ所をパッチしたくなる。抑える。故障が**どの層**にあるかを
問い、根へ行く。各層を**順に**無罪証明する — データ → ロジック → アセンブリ →
リグレッション → 時間二分 → 環境 — 環境を疑うのは*最初*ではなく*最後*。

> *H40:* 音声の症状が変化し続けた — ガビガビ、次に巻き戻り、次に遅延。一つずつ潰すのは
> 賽の河原。立ち戻ると3つ全ての背後に単一の根（再シークの一時停止）があった。

## 2. 前提とモデルを疑う （前提を疑う）

自分が**真だと信じている**ことを平易な一文で書き出し、それを攻撃する。シミュレータは
実機の*モデル*。どう詰めても実機がモデルに合わないなら、疑うべきはモデルやエンコードの
超過であり、もう一つのつまみではない。

> *H40:*「落ちはCDの吸い不足から来る」→ もっと吸って検証 → **悪化**。前提は偽で、方針が
> 丸ごと反転した。

## 3. 実験で確かめる；変えるのは一つ （実験で確かめる）

論理だけで結論に達してそのまま出さない。A/Bを組んで測る。1回のテストで変える変数は
**ちょうど一つ**。3つ変えたら、どれが効いたか何も学べていない。全ビルドで**同じ指標**を
使う。

> *H40:* `RING_CAP` = 300 / 350 / 380 を*同じ*滑りカウンタで走査し、純粋な論理が逆に
> していた非単調カーブ（300は良くなく悪化）を明らかにした。

## 4. 故障を可視化する — マーカーを入れる （デバッグマーカー）

見えないものはデバッグできない。画面上のカウンタ、色付きの枠、小さな検出スクリプト、
「コマNで静止」モードを入れる。客観信号が人の判断ノイズを除く。*「きれいに見える」*は
データではない。*「`S` が 38 → 0 になった」*はデータだ。

> *H40:* 滑りマーカー（セクタ脱落時の赤枠＋`S`カウント）が最も価値ある道具だった。
> 主観的な混乱をA/B可能な数値に変え、ユーザーはこれを唯一の合否判定にした。

## 5. 定量する — 検出器を書き、印象を信じない

ディザや압縮のかかったコマの目視は誤らせる：1ピクセルのサンプリングずれでディザの半分が
反転する。小さな検出器を書き、各ビルドの録画に走らせる。「暗く孤立した横ダッシュ」検出器は、
Word-RAM DMA の先頭ワードバグを一発で特定した（3.2欠陥/コマ 対 誤検出下限0.4）。

## 6. 単発の確率的ランを疑う

タイミング依存のバグはラン毎に変わる。1回きれいでも**証明ではない**。**余裕**を探す：
dip の無い定常フレームレートは余裕を意味し、ぎりぎり通るものは別のランで壊れる。
*明確な余裕を伴う* `S=0` は信頼でき、閾値ぎりぎりの `S=0` は信頼できない。

## 7. 時間と空間で二分する

崩壊後のゴミを眺める代わりに、コマNで静止して N を二分探索し**最初の**不良コマを見つける。
空間でも同様：どのセル、どのセクタ、どの層。現実が意図から分岐する最早点を見つける。

## 8. 自分の懐疑家になる — 反対を論じる （アドバイザーの代役）

解釈を確定する前に問う：**「もし間違っているなら、何が見えるはずか？」**そしてまさに
それを探しに行く。証拠と計画が食い違うなら、黙って計画に従わず、対立を口に出してもう
一度の確認で決着させる。この敵対的な一手こそ、アドバイザーがあなたにしていたことだ。

## 9. 長い・不可逆な手の前に成果物を守る

長いレンダー、アップロード、セッションを終わらせ得る何かの**前に**、保存・コミット・
ファイル書き出しをする。永続化した成果はクラッシュを生き延び、メモリ上のものは死ぬ。
成果物を実体化してから、遅い作業をする。

## 10. トレードオフを名指しし、オーナーに選ばせる

二つの厳しい要求が両立不能なとき — *落ちゼロ* **かつ** *画質無劣化* — どちらかを
**黙って**劣化させない。正直な分岐を、データと共にオーナーの前に置く。しばしばオーナーが
一つの制約をわずかに緩める（ここでは「重いコマの新規タイルを減らすのは可」）ことで、
他の全てが解ける。黙った劣化は裏切りで、名指したトレードオフは意思決定だ。

## 11. 実験を有効に保つ — プロセス規則を守る

- **共有マシン：**シミュレータとエミュレータを同時に走らせない。互いのコアを奪い、互いの
  タイミングを壊す。録画は一度に一つ。
- **失敗ランは失敗ラン：**黒や無音の録画は*失敗データ*で結果ではない — 信じる前にログを
  見る。クラッシュしたエミュのログは唐突に終わり、健全なものは終了行で終わる。
- **録画はピクセル厳密ではない：**ピクセル単位の主張は整数パス（ネイティブ解像度の
  ロスレスキャプチャ）でのみ検証し、拡大スクショではしない。

---

## 覚えておく一文

アドバイザーの本当の贈り物は答えではなく、**行動の前の一拍**だった。その一拍を保て。
仮説を書き、確認を設計し、データに語らせる。あてずっぽうは速くて大抵間違い、この
ループは少し遅くて大抵正しい。
