#!/usr/bin/env python3
"""実機用の差分ストリーム(TTRC, B方式=セクタ間ストリーム分離)を決定ログから生成する。

唯一の真実源 = sim: simが CBRSIM_EMIT_DEC で吐く決定ログ(更新セル(cell,pal,key)＋
区間パレット)を再生してストリーム化。keyは64B(idx1..15)内包=32Bパターン復元可。

B方式の狙い: 連続CD読み(シーク無し=絶対ルール)を保ったまま、PRGリングへの書込を
**完全DMA(CDC_TRN)化**する(連続読み中のCPU-PRGバースト書込はSub-CPUを固める)。そのため
2ストリームをセクタ粒度でインタリーブ:
  payload: cold pattern(32B)連続 -> リングへDMA
  control: 毎フレーム apply-list+audio 可変長ブロック連続 -> apply-bufferへDMA(CPUはカーソルで処理)
control連続化でセクタ整列の無駄を回避 -> 149フル画質でPRGに収まる(A方式のセクタ整列は256/枚<消費で不可)。

TTRCレイアウト(v6): HEADER.DAT = Header(1sec) + PALTAB(全区間パレット
              n_seg×128B, boot時Main-RAM表へ) + startup audio(1 sector/frame)
              + frame0(control+patterns) + routing(2B/frame: n_pay_sec,n_ctrl_sec)
              + prebuffer(payload先頭Bpat)
              BODY.DAT = frame1以降の [control][payload][rate pad]
MOVIE.DAT はツール互換用の HEADER.DAT || BODY.DAT 連結コンテナ。
control block: >H total_len >H frame_seq >H n_upd >B pal >B dbg [DEBUG if dbg]
               ceil(cells/8) bitmap n_upd*(>H entry) 887 audio [pad偶数]
  pal = 区間番号+1(0=切替なし)。実機はMain-RAMのPALTAB表を引く(in-stream CRAM廃止)。
  dbg=1 のとき諸元ヘッダ直後に固定長DEBUGブロック(前方固定=新プレイヤーは固定offsetで一発読み):
  7×>H カテゴリ数[raw,same,near,coa,flbk,buf,miss] + 4×>H 予約 = 22B(偶数)。
  既定OFF(CBRSIM_PACK_DEBUG=1 で有効=デバッグ時だけ載せる)。
"""
import argparse
import os
import pickle
import struct
import sys
from pathlib import Path
from collections import deque
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim as sim
from sim import C_CELLS, TCOLS, TROWS, TILE, PATTERN_BYTES, AUDIO_RATE, FPS
from cbr_paths import sim_work_dir

SECTOR = 2048
MAGIC = b"TTRC"             # Tile Texture Reuse Codec
VERSION = 6
BASE = 1                     # POOL_TILE_BASE (VRAM tile index = BASE+slot)
FRAME_SECTORS = 5
PAT = 32
PAT_PER_SEC = SECTOR // PAT  # 64
# 1コマの音声バイトは fps由来。15fps=887(13.3kHz/15), 30fps=443(13.3kHz/30)。
# 旧: 887固定=15fps専用だった(30fpsで2倍消費し disc が CD 1x を8%超過する主因だった)。
AUDIO = int(round(AUDIO_RATE / FPS))   # AUDIO_RATE, FPS は sim から import
# PCM開始直後はエミュレータ/実機の立上がり位相で一時的にSubの供給が遅れる。先頭音声を
# ディスクのboot prefixへ複製し、PCMを開始する前にwave RAMへ並べておく。frame0を含む
# Requested startup arming depth. The player starts PCM at SYNC_LEAD (the same
# address used by the write pointer). Thirty chunks cover the frame-0 build and
# the first dense scene while the live writer catches up; the header keeps this
# explicit so older streams remain readable and experiments can override it.
# Leave the setting overridable for old stream/player combinations.
STARTUP_AUDIO_FRAMES = max(0, int(os.environ.get("CBRSIM_STARTUP_AUDIO_FRAMES", "30")))
PCM_SYNC_LEAD = 0x3000
PCM_SYNC_MAX = 0x6800
PCM_WAVE_RING_END = 0x8000
PCM_STARTUP_MARGIN = 0x0200
# リング諸元は tools/av_config.py の単一真実源から取る(sim/pack/playerで二重管理しない)。
# RING_SIZE はプレイヤの実 .equ RING_SIZE と一致(ビルド時 check_player_ring.py が検証)。
# RING_CAP(スケジュール上限)と sim の TANK は RING_SIZE から導出され必ず一致する。
import av_config
RING_SIZE_KB = av_config.RING_SIZE_KB
RING_CAP_KB = int(os.environ.get("CBRSIM_RING_CAP_KB", str(av_config.RING_CAP_KB)))
if RING_CAP_KB > av_config.BACKPRESSURE_KB:
    raise SystemExit(
        f"CBRSIM_RING_CAP_KB={RING_CAP_KB} exceeds player back-pressure "
        f"{av_config.BACKPRESSURE_KB}KB — the pump would stall and drop sectors. "
        f"Lower it (default {av_config.RING_CAP_KB} from av_config.py).")
RING_CAP_PAT = RING_CAP_KB * 1024 // PAT

# コールド上限(=1コマの新規タイル数の上限)は「エンコーダ=sim」だけの責務。
# ここ(pack段)で cap すると、(1)simが出す解析の絵とディスクの絵が食い違い、(2)cell昇順で
# 下段を打ち切り前コマ保持(stale)になって sim の賢い流用(reuse)より画質が劣る。
# よって pack では絶対に cap しない。指定されたら「simへ移せ」と明示エラーにする。
if os.environ.get("CBRSIM_PACK_MAXCOLD"):
    raise SystemExit(
        "CBRSIM_PACK_MAXCOLD is removed: the cold cap belongs to the encoder. "
        "Set CBRSIM_MAX_COLD in the sim and re-sim, so the analysis matches the "
        "disc and capped cells reuse (not go stale). See tools/av_config.py.")

# --- デバッグブロック(control先頭ヘッダ直後・固定長) ---
DBG_NCAT = 7                 # カテゴリ数 [raw,same,near,coa,flbk,buf,miss]
DBG_RESERVED = 4             # 予約u16スロット(将来の16bitデバッグ値用)
DBG_LEN = (DBG_NCAT + DBG_RESERVED) * 2   # = 22B(偶数)


def debug_block(cats):
    """カテゴリ数タプル(len==DBG_NCAT)+予約 を固定長DBG_LENへ。各値0xFFFFクランプ。"""
    vals = list(cats)[:DBG_NCAT] + [0] * DBG_RESERVED
    return struct.pack(">%dH" % (DBG_NCAT + DBG_RESERVED),
                       *[min(int(v), 0xFFFF) for v in vals])


def pack_key(key):
    a = np.frombuffer(key, np.uint8)
    out = bytearray()
    for y in range(8):
        for x in range(0, 8, 2):
            out.append((int(a[y * 8 + x]) << 4) | int(a[y * 8 + x + 1]))
    return bytes(out)


def load_log(path):
    with open(path, "rb") as f:
        return pickle.load(f)


def require_canonical_p0_index15(log):
    """Reject stale decision logs that cannot provide the fixed DEBUG colour."""
    seg_pals = log.get("seg_pals")
    if not seg_pals:
        raise SystemExit("pack v6: decision log has no segment palettes; re-run sim")
    for seg, pals in enumerate(seg_pals):
        a = np.asarray(pals, np.uint8)
        if a.shape != (4, 15, 3):
            raise SystemExit(
                f"pack v6: segment {seg} palette shape is {a.shape}, expected (4, 15, 3); "
                "re-run sim")
        brightness = a.astype(np.int16).sum(axis=2)
        if int(brightness[0, 14]) != int(brightness.max()):
            raise SystemExit(
                f"pack v6: decision log segment {seg} P0 index15 is not tied for globally "
                "brightest nonzero CRAM colour (RGB sum); re-run sim with the current encoder")


def pals_to_bytes_128(pal_4x15):
    b = sim.pals_to_bytes([np.asarray(pal_4x15[p], np.uint8) for p in range(4)])
    assert len(b) == 128, len(b)
    return b


def build_bitmap(cells):
    bm = bytearray((C_CELLS + 7) // 8)
    for c in cells:
        bm[c >> 3] |= 1 << (c & 7)
    return bytes(bm)


def resolve(log, POOL, mode="lru"):
    """検証済み LRU+ダブルバッファ保護スロットモデルで cold を検出。
       mode="contig": クロックハンド円環走査でフレーム内coldを昇順(なるべく連番)スロットへ
       割当 -> MD側が連続ランを少数の大DMAにまとめられる。
       per=[(cells,entries,colds)], n_load, n_upd, pal_w, P(消費順cold pattern 32B) を返す。"""
    frames = log["frames"]
    frame_seg = np.asarray(log["frame_seg"], np.int64)
    nfr = len(frames)
    from tile_alloc import TileAllocator
    alloc = TileAllocator(C_CELLS, POOL, BASE)   # 共有割り当て(連続)。sim も同一 = cap=realized
    per = []
    n_load = np.zeros(nfr, np.int64)
    n_upd = np.zeros(nfr, np.int64)
    pal_w = np.zeros(nfr, np.int64)
    Plist = []

    for i in range(nfr):
        fr = sorted(frames[i], key=lambda t: t[0])
        results = alloc.place_frame([(int(cell), key) for (cell, pal, key) in fr], i)
        pal_w[i] = 1 if (i == 0 or frame_seg[i] != frame_seg[i - 1]) else 0
        cells, entries, colds = [], [], []
        for (cell, pal, key), (slot, cold) in zip(fr, results):
            if cold:
                Plist.append(pack_key(key))
                n_load[i] += 1
            cells.append(int(cell))
            entries.append((int(pal) << 13) | (BASE + slot))
            colds.append(cold)
            n_upd[i] += 1
        per.append((cells, entries, colds))
        if (i + 1) % 400 == 0:
            print(f"  resolve {i+1}/{nfr}", flush=True)
    return per, n_load, n_upd, pal_w, Plist, alloc.tearing


def run_stats(per):
    """フレーム内coldスロット列(セル順)の連続ラン統計。MD側DMAまとめの効果見積り。"""
    runs_per_frame = np.zeros(len(per), np.int64)
    colds_per_frame = np.zeros(len(per), np.int64)
    for i, (cells, entries, colds) in enumerate(per):
        prev = None
        runs = 0
        nc = 0
        for e, c in zip(entries, colds):
            if not c:
                continue
            s = (e & 0x07FF) - BASE
            if prev is None or s != prev + 1:
                runs += 1
            prev = s
            nc += 1
        runs_per_frame[i] = runs
        colds_per_frame[i] = nc
    tot_c = int(colds_per_frame.sum())
    tot_r = int(runs_per_frame.sum())
    heavy = colds_per_frame >= 300
    msg = (f"run_stats: cold計{tot_c} run計{tot_r} 平均ラン長{tot_c / max(1, tot_r):.1f} "
           f"フレーム最大run数{int(runs_per_frame.max())}")
    if heavy.any():
        msg += (f"  重量フレーム(cold>=300, {int(heavy.sum())}枚): "
                f"平均run数{runs_per_frame[heavy].mean():.1f} "
                f"平均ラン長{(colds_per_frame[heavy].sum() / max(1, runs_per_frame[heavy].sum())):.1f}")
    print(msg)
    # SPのラン形式ロード領域(Word-RAM 0x84..0x7000=28540B)に収まるか:
    # 1ラン = slot(2)+count(2)+count*32B
    loads_bytes = colds_per_frame * PAT + runs_per_frame * 4
    O_LOADS_CAP = 0x9800 - 0x84
    if int(loads_bytes.max()) > O_LOADS_CAP:
        print(f"  !! loads領域あふれ: 最大{int(loads_bytes.max())}B > {O_LOADS_CAP}B "
              f"(frame {int(loads_bytes.argmax())})")
    else:
        print(f"  loads領域 最大{int(loads_bytes.max())}B / {O_LOADS_CAP}B")
    return runs_per_frame


def build_control(log, per, n_upd, pal_w, audio_path):
    """毎フレームの control ブロック列(連続バイト用)を作る。"""
    seg_cram = [pals_to_bytes_128(p) for p in log["seg_pals"]]
    frame_seg = np.asarray(log["frame_seg"], np.int64)
    cats_list = log.get("cats")                     # per-frame [raw,same,near,coa,flbk,buf,miss]
    # デバッグ欄: 既定OFF。CBRSIM_PACK_DEBUG=1 でデバッグ向けに載せる。
    dbg_on = bool(cats_list) and os.environ.get("CBRSIM_PACK_DEBUG", "0") == "1"
    aud = b""
    if audio_path:
        # WAV(pcm_u8, 中心=128)を読む。wave で data チャンクだけ取り出す(ヘッダを
        # サンプルに混ぜない)。生ファイルなら read_bytes にフォールバック。
        try:
            import wave as _wave
            _w = _wave.open(str(audio_path), "rb")
            raw = _w.readframes(_w.getnframes())        # u8 サンプル列(ヘッダ無し)
            _w.close()
        except Exception:
            raw = Path(audio_path).read_bytes()
        sm = bytearray(len(raw))
        for j, b in enumerate(raw):                     # u8(中心128) -> RF5C164 符号+絶対値
            s = b - 128                                 # 符号付き -128..127 (128=無音=0)
            if s > 0x7F:
                s = 0x7F
            sm[j] = s if s >= 0 else (0x80 | min(-s, 0x7E))   # 0xFF(ストップ印)を避ける
        aud = bytes(sm)
    # CRAM pre-load(PALTAB): パレット本体はヘッダ直後のPALTAB領域で一括配送し、実機は
    # boot時にMain-RAM表へコピー済み。ストリームのpalバイトは「区間番号+1」(0=切替なし)の
    # 参照だけにし、in-streamの128B CRAM payloadは廃止(切替コマの予算が空く+到着タイミング
    # 非依存=スリップ回復に強い)。区間数は av_config.PALTAB_MAX_SEG が上限(実機表の容量)。
    n_seg = len(seg_cram)
    cap_seg = min(int(av_config.PALTAB_MAX_SEG), 255)
    if n_seg > cap_seg:
        raise SystemExit(
            f"palette segments {n_seg} > PALTAB capacity {cap_seg} "
            f"(av_config.PALTAB_MAX_SEG — raise it and the player equ together)")
    blocks = []
    for i in range(len(per)):
        cells, entries, colds = per[i]
        body = bytearray()
        # 同期マーカー: frame_seq(下位16bit)。実機は control 読み出し時に期待フレーム番号と
        # 照合し、ズレたら desync 検知(CDCセクタ落ち等)して復帰できる。total_len に含む。
        body += struct.pack(">H", i & 0xFFFF)
        body += struct.pack(">H", int(n_upd[i]))
        pal_ref = (int(frame_seg[i]) + 1) if pal_w[i] else 0
        body += struct.pack(">BB", pal_ref, 1 if dbg_on else 0)
        if dbg_on:
            body += debug_block(cats_list[i])       # 固定長DEBUGブロック(Miss含む7カテゴリ+予約)
        body += build_bitmap(cells)
        for e, cold in zip(entries, colds):
            body += struct.pack(">H", (0x8000 if cold else 0) | e)
        a = aud[i * AUDIO:(i + 1) * AUDIO]
        body += a + b"\0" * (AUDIO - len(a))
        # total_len は「先頭2Bを含むブロック全長」。実機は apply_cur を total_len で進めるので
        # パディング込みの偶数にする(奇数だと1B/フレームずつ desync する)。
        total = len(body) + 2
        if total & 1:
            body += b"\0"
            total += 1
        blocks.append(struct.pack(">H", total) + bytes(body))
    return blocks


def control_audio(block):
    """Return the fixed-size PCM chunk embedded in one control block."""
    n_upd = struct.unpack_from(">H", block, 4)[0]
    dbg = block[7]
    pos = 8 + (DBG_LEN if dbg else 0) + ((C_CELLS + 7) // 8) + n_upd * 2
    chunk = block[pos:pos + AUDIO]
    if len(chunk) != AUDIO:
        raise ValueError(f"control audio truncated: got {len(chunk)}, expected {AUDIO}")
    return chunk


def schedule(per, n_load, blocks):
    """control JIT(前向き)で nc[i] を決め、payload は cap=(5-nc)*64 でセクタ単位の後ろ向き最小占有。"""
    nfr = len(per)
    blk_len = np.array([len(b) for b in blocks], np.int64)
    # v6 always arms frame 0 from HEADER.DAT.  Keeping a switch here would
    # produce a file whose layout disagrees with its version, so reject the
    # removed legacy mode explicitly instead of silently emitting it.
    if os.environ.get("CBRSIM_F0_HEADER", "1") == "0":
        raise SystemExit("pack v6 requires frame0 in HEADER.DAT; CBRSIM_F0_HEADER=0 is unsupported")
    F0_HEADER = True
    nc = np.zeros(nfr, np.int64)
    ctrl_deliv = 0
    ctrl_cur = 0
    for i in range(nfr):
        if F0_HEADER and i == 0:
            continue                                      # frame0 control はヘッダ側(ストリーム外)
        deficit = (ctrl_cur + int(blk_len[i])) - ctrl_deliv
        k = max(0, -(-deficit // SECTOR)) if deficit > 0 else 0
        nc[i] = k
        ctrl_deliv += k * SECTOR
        ctrl_cur += int(blk_len[i])
    cap_sec = np.maximum(FRAME_SECTORS - nc, 0)
    n_load_s = np.array(n_load, np.int64)
    if F0_HEADER:
        n_load_s[0] = 0                                   # frame0 patterns はヘッダ側(ストリーム外)
    d = np.cumsum(n_load_s)
    M = int(d[-1])
    d_sec = -(-d // PAT_PER_SEC)
    M_sec = int(-(-M // PAT_PER_SEC))
    A = np.zeros(nfr, np.int64)
    # PACK_FILL(既定ON): 「5セクタを使い切る」= 余った payload 帯域(pad)を軽いコマのうちに
    #   先読みしてリング予備を満たす(occ を RING_CAP まで積む)。重いシーンのパターンを前倒し
    #   供給し、リング枯渇(=ゴミ化)を防ぐ。旧= 最小配信(backward, 遅く届ける=pad浪費)。
    if os.environ.get("CBRSIM_PACK_FILL", "1") != "0":
        ring_sec = RING_CAP_PAT // PAT_PER_SEC             # リング上限(セクタ)
        # frame0はDAT冒頭の専用ヘッダブロックとしてboot中にVRAM直ロードする(=ストリーミングの
        # リングを一切経由しない)。よってframe0のcoldはストリームの累積d(=リングpop)から除外し、
        # プリバッファは frame1用の満タンリング(RING_CAP)だけにする。frame0の配信は0。
        # これでboot時リングは常にRING_CAP以下=back-pressure(416KB)に触れず、かつframe1以降が
        # 満タンリングで始まる。frame0の大バーストによる後続枯渇(崩壊)を根絶。
        Bsec = int(min(ring_sec, M_sec))                  # frame1用プリバッファ=満タン(<=総量)
        prev = Bsec
        for i in range(nfr):
            if i == 0:
                A[0] = Bsec                               # frame0はストリーム配信0(ヘッダで別ロード)
                prev = Bsec
                continue
            hi_ring = (int(d[i]) + RING_CAP_PAT) // PAT_PER_SEC   # occ<=RING_CAP 制約(frame1+)
            a = min(prev + int(cap_sec[i]), M_sec, int(hi_ring))
            if a < prev:
                a = prev                                  # 単調(未配信に戻さない)
            A[i] = a
            prev = a
    else:
        cumcap = np.cumsum(cap_sec)
        Bsec = int(max(0, np.max(d_sec - cumcap)))
        A[-1] = M_sec
        for i in range(nfr - 1, 0, -1):
            A[i - 1] = max(int(d_sec[i - 1]), int(A[i] - cap_sec[i]))
    n_pay_sec = np.empty(nfr, np.int64)
    n_pay_sec[0] = A[0] - Bsec
    n_pay_sec[1:] = A[1:] - A[:-1]
    occ = A * PAT_PER_SEC - d
    under = int((occ < 0).sum())                          # リング枯渇(飢餓)コマ数
    over = int((n_pay_sec + nc > FRAME_SECTORS).sum())

    # BODY.DAT is control-first.  The player may apply frame i immediately
    # after its control sectors arrive, before frame i's payload sectors.  All
    # cold patterns consumed through frame i must therefore have arrived by
    # the end of frame i-1.  Frame 0 is independent and comes from HEADER.DAT.
    if nfr > 1:
        ready_margin = A[:-1] * PAT_PER_SEC - d[1:]
        ready_bad = np.flatnonzero(ready_margin < 0)
        ready_min = int(ready_margin.min())
        if ready_bad.size:
            frame = int(ready_bad[0]) + 1
            raise SystemExit(
                f"pack v6 control-first invariant failed at frame {frame}: "
                f"only {int(A[frame - 1]) * PAT_PER_SEC} patterns delivered before control, "
                f"but {int(d[frame])} are consumed through that frame "
                f"(short by {-int(ready_margin[frame - 1])})")
    else:
        ready_min = 0

    # Independently prove that the cumulative control sectors routed through
    # each frame contain that frame's complete variable-length control block.
    # This also covers n_ctrl=0: the block must already fit in earlier sectors.
    ctrl_need_len = blk_len.copy()
    if nfr:
        ctrl_need_len[0] = 0                              # frame0 control is in HEADER.DAT
    ctrl_need = np.cumsum(ctrl_need_len)
    ctrl_delivered = np.cumsum(nc) * SECTOR
    ctrl_margin = ctrl_delivered - ctrl_need
    ctrl_bad = np.flatnonzero(ctrl_margin < 0)
    ctrl_min = int(ctrl_margin.min()) if nfr else 0
    if ctrl_bad.size:
        frame = int(ctrl_bad[0])
        raise SystemExit(
            f"pack v6 control completeness failed at frame {frame}: "
            f"{int(ctrl_delivered[frame])} bytes delivered, "
            f"{int(ctrl_need[frame])} bytes required")

    feasible = ((n_pay_sec >= 0).all() and over == 0 and under == 0
                and ready_min >= 0 and ctrl_min >= 0)
    return dict(n_pay_sec=n_pay_sec, n_ctrl_sec=nc, feasible=feasible, over=over, under=under,
                prebuf_pat=Bsec * PAT_PER_SEC, ring_peak=int(occ.max()), ring_min=int(occ.min()),
                ready_min=ready_min, ctrl_min=ctrl_min, blk_len=blk_len, M=M,
                f0_header=F0_HEADER, f0_cold=int(n_load[0]), f0_ctrl_len=int(blk_len[0]))


def decode_verify(log, per, blocks, Plist, sc, compare_dir=None, sample_dir=None):
    """Simulate the v6 control-first player and compare it with sim output.

    A frame consumes its already-armed cold patterns before that frame's BODY
    payload is appended to the ring.  This intentionally models the earliest
    legal apply time instead of relying on favorable CPU/CD overlap.
    """
    frame_seg = np.asarray(log["frame_seg"], np.int64)
    seg_pals = log["seg_pals"]
    n_pay_sec = sc["n_pay_sec"]; blk_len = sc["blk_len"]; B = sc["prebuf_pat"]
    ctrl = b"".join(blocks)
    POOL = int(log["vram_tiles"])
    cmp = Path(compare_dir) if compare_dir else None
    if sample_dir:
        sample_dir = Path(sample_dir); sample_dir.mkdir(parents=True, exist_ok=True)
    samples = set(range(0, len(per), max(1, len(per) // 6)))
    # v2 frame0ヘッダ: frame0のパターンはストリーミングのリングではなくヘッダのF0PATブロック
    # から供給される(実機の boot ロード)。よって decode_verify も frame0 は別deque(f0_ring)から
    # popし、リングは prebuffer(Plist[nl0:nl0+B])で種蒔く。ストリーム payload カーソルは nl0+B から。
    # (これを分けないと frame0 のパターンをリングから食い、末尾で nl0 個ぶん枯渇して見える。)
    f0h = bool(sc.get("f0_header", False))
    nl0 = int(sc.get("f0_cold", 0)) if f0h else 0
    f0_ring = deque(Plist[:nl0])
    ring = deque(Plist[nl0:nl0 + B]); pc = nl0 + B; cc = 0
    tile = [None] * (POOL + BASE + 2)
    nt_slot = np.zeros(C_CELLS, np.int64); nt_pal = np.zeros(C_CELLS, np.int64)
    diffs = []; ring_peak = len(ring); bad = 0
    for i in range(len(per)):
        add = int(n_pay_sec[i]) * PAT_PER_SEC
        src = f0_ring if (f0h and i == 0) else ring   # frame0はヘッダのf0patから, 以降はリング
        blk = ctrl[cc:cc + int(blk_len[i])]; cc += int(blk_len[i])
        p = 2                                         # skip total_len
        p += 2                                        # skip frame_seq(同期マーカー)
        nupd = struct.unpack(">H", blk[p:p + 2])[0]; p += 2
        palw = blk[p]; dbg = blk[p + 1]; p += 2
        if dbg:
            p += DBG_LEN                              # skip debug block
        # v3: palw = 区間番号+1 の参照のみ(in-stream CRAMは無い)。PALTAB表と一致するか検証。
        if palw and (palw - 1) != int(frame_seg[i]):
            print(f"  !! palref mismatch frame {i}: pal={palw - 1} != seg={int(frame_seg[i])}")
            bad += 1
        bmbytes = (C_CELLS + 7) // 8                   # bitmap = ceil(cells/8)(H32=72, H40full=140)
        bm = blk[p:p + bmbytes]; p += bmbytes
        cells = [c for c in range(C_CELLS) if bm[c >> 3] & (1 << (c & 7))]
        for c in cells:
            e = struct.unpack(">H", blk[p:p + 2])[0]; p += 2
            cold = e >> 15; ent = e & 0x7FFF
            nt_pal[c] = (ent >> 13) & 3
            nt_slot[c] = (ent & 0x07FF) - BASE
            if cold:
                if not src:
                    bad += 1
                else:
                    tile[int(nt_slot[c]) + BASE] = src.popleft()
        # BODY payload follows control and arms later frames.  Append it only
        # after the current block has consumed every cold entry.
        for k in range(pc, min(pc + add, len(Plist))):
            ring.append(Plist[k])
        pc += add
        ring_peak = max(ring_peak, len(ring))
        need_img = (cmp is not None) or (sample_dir is not None and i in samples)
        if not need_img:
            continue
        full16 = np.zeros((4, 16, 3), np.uint8)
        full16[:, 1:] = np.asarray(seg_pals[int(frame_seg[i])], np.uint8)
        img = np.zeros((C_CELLS, TILE, TILE, 3), np.uint8)
        for c in range(C_CELLS):
            pat = tile[int(nt_slot[c]) + BASE]
            if pat is None:
                continue
            a = np.frombuffer(pat, np.uint8); idx = np.zeros(64, np.uint8)
            idx[0::2] = a >> 4; idx[1::2] = a & 0xF
            img[c] = sim.rgb333_to_rgb888(full16[nt_pal[c], idx].reshape(8, 8, 3))
        fr = img.reshape(TROWS, TCOLS, TILE, TILE, 3).transpose(0, 2, 1, 3, 4).reshape(
            TROWS * TILE, TCOLS * TILE, 3)
        if sample_dir is not None and i in samples:
            Image.fromarray(fr, "RGB").save(sample_dir / f"decoded_{i:05d}.png")
        if cmp is not None:
            ref_p = cmp / f"{i:05d}.png"
            if ref_p.exists():
                ref = np.asarray(Image.open(ref_p).convert("RGB"))[:TROWS * TILE, :TCOLS * TILE]
                diffs.append((i, int(np.abs(fr.astype(np.int32) - ref.astype(np.int32)).max())))
        if (i + 1) % 400 == 0:
            print(f"  decode {i+1}/{len(per)}", flush=True)
    print(f"decode: ring_peak {ring_peak*PAT/1024:.0f}KB 未配信pop(表示破壊) {bad}")
    if diffs:
        da = np.array([x[1] for x in diffs])
        nd = int((da > 0).sum())
        print(f"sim preview一致: 比較{len(da)}枚 差分ありフレーム={nd} 画素最大差={int(da.max())}")
        if nd:
            print("  差分フレーム(先頭10):", [x[0] for x in diffs if x[1] > 0][:10])


def write_stream(path, log, per, blocks, Plist, sc, POOL):
    """Write the v6 split stream and a combined tooling container.

    HEADER.DAT:
      Header(1sec) | PALTAB | STARTUP_AUDIO | FRAME0(control+patterns)
                   | ROUTING(0..N-1,[0]=0,0) | PREBUF1(frame1用RING_CAP)
    BODY.DAT:
      FRAMES(1..N-1), each [control sectors][payload sectors][rate pad]
    MOVIE.DAT (``path``) is the off-disc HEADER.DAT || BODY.DAT container.

    frame0 はストリーミングのリングを経由せず boot 中に VRAM 直ロードするので、リングは
    常に RING_CAP 以下=back-pressure非接触。frame1以降が満タンリングで始まる。
    PALTAB = 全区間パレット(n_seg×128B, セクタ整列)。実機はboot時にWord-RAM経由で
    Main-RAM表へコピーし、以降のpalバイト(区間番号+1)で表を引く(in-stream CRAM廃止)。"""
    n_pay_sec = sc["n_pay_sec"]; n_ctrl_sec = sc["n_ctrl_sec"]
    Bpat = int(sc["prebuf_pat"])
    frame_seg = np.asarray(log["frame_seg"], np.int64)
    seg0 = pals_to_bytes_128(log["seg_pals"][int(frame_seg[0])])
    nfr = len(per)
    f0_header = bool(sc.get("f0_header", False))
    nl0 = int(sc.get("f0_cold", 0))
    f0_ctrl_len = int(sc.get("f0_ctrl_len", 0))
    payload = b"".join(Plist)
    control = b"".join(blocks)
    # frame0の control/patterns をストリームから切り出す(ヘッダ側へ)
    if f0_header:
        f0_ctrl = control[:f0_ctrl_len]
        f0_pat = payload[:nl0 * PAT]
        stream_ctrl = control[f0_ctrl_len:]          # frames1+ の control連結
        stream_pay = payload[nl0 * PAT:]             # frames1+ の payload連結
        f0_ctrl_sec = -(-len(f0_ctrl) // SECTOR)
        f0_pat_sec = -(-len(f0_pat) // SECTOR)
    else:
        f0_ctrl = f0_pat = b""
        stream_ctrl = control
        stream_pay = payload
        f0_ctrl_sec = f0_pat_sec = 0
    routing = bytearray()
    for i in range(nfr):
        routing += bytes([int(n_pay_sec[i]) & 0xFF, int(n_ctrl_sec[i]) & 0xFF])
    routing_sec = -(-len(routing) // SECTOR)
    prebuf_bytes = stream_pay[:Bpat * PAT]           # frame1用プリバッファ(RING_CAP)
    prebuf_sec = -(-len(prebuf_bytes) // SECTOR)
    ring_peak = int(sc["ring_peak"])
    # The sim decision log is the source of truth.  Falling back to the
    # environment keeps old logs readable, but never let a case mismatch or a
    # changed shell environment silently turn an H32 stream into H40.
    mode_name = str(log.get("mode") or os.environ.get("CBRSIM_MODE", "")).strip().upper()
    if not mode_name:
        mode_name = "H40" if TCOLS == 40 else "H32"
    if mode_name not in {"H32", "H40", "MODE4"}:
        raise SystemExit(f"pack: unsupported display mode in decision log: {mode_name!r}")
    _mode = {"H32": 0, "H40": 1, "MODE4": 2}[mode_name]
    # PALTAB: 全区間パレットをヘッダ直後に一括配置(セクタ整列)。boot時にMain-RAM表へ。
    paltab = b"".join(pals_to_bytes_128(p) for p in log["seg_pals"])
    paltab_sec = -(-len(paltab) // SECTOR)
    # v5 startup audio: one PCM chunk per sector. Sector-per-frame is deliberate:
    # the Sub CPU can drain one sector and write one complete chunk immediately,
    # without a cross-sector staging buffer. The data is a duplicate; controls stay
    # self-contained and older analysis/verification paths remain unchanged.
    safe_audio_preload = max(0, min(
        (PCM_SYNC_MAX - PCM_SYNC_LEAD) // max(1, AUDIO),
        (PCM_WAVE_RING_END - PCM_SYNC_LEAD - PCM_STARTUP_MARGIN) // max(1, AUDIO)))
    audio_preload_frames = min(nfr, STARTUP_AUDIO_FRAMES, safe_audio_preload) if f0_header else 0
    audio_preload = b"".join(
        control_audio(blocks[i]).ljust(SECTOR, b"\0")
        for i in range(audio_preload_frames)
    )
    audio_preload_sec = audio_preload_frames
    # v4: 可変フレーム(5セクタ固定paddingを廃止=各frameは n_pay+n_ctrl セクタ)＋ vsync/コマ N。
    # これで fps がセクタ境界から解放され、表示は N vsync/コマでペーシング(N4=14.985, N2=29.97)。
    # AUDIO も fps由来。FRAME_SECTORS(=5)は最大スロット(routingバイトの上限)としてのみ残す。
    NTSC_VSYNC = 59.94
    vsync_n = int(round(NTSC_VSYNC / FPS))            # N: 1コマの表示VBLANK数(30fps→2, 15fps→4)
    fps_int = int(round(FPS))                         # 名目fps(15/30)。レートマッチpadding用(下記)
    if not f0_header:
        raise SystemExit("pack v6 requires frame0 in HEADER.DAT")
    header = struct.pack(">4sHHHHHHHHH", MAGIC, VERSION, nfr, TCOLS, TROWS, C_CELLS,
                         POOL, BASE, FRAME_SECTORS, len(log["seg_pals"]))
    header += struct.pack(">LLLL", Bpat, routing_sec, prebuf_sec, ring_peak)
    header += bytes([_mode])                          # offset 38: display mode
    header += b"\0"                                   # offset 39: pad
    header += struct.pack(">LL", f0_ctrl_sec, f0_pat_sec)  # offset 40,44: frame0ブロック
    header += struct.pack(">L", paltab_sec)          # offset 48: PALTABセクタ数(v3)
    header += struct.pack(">HH", vsync_n, AUDIO)     # offset 52: N(vsync/コマ), 54: AUDIO(1コマ音声B) (v4)
    header += struct.pack(">H", fps_int)             # offset 56: 名目fps(レートマッチpadding用) (v4)
    header += struct.pack(">HH", audio_preload_frames, audio_preload_sec)  # offset 58,60 (v5)
    header += b"\0" * (64 - len(header)) + seg0
    header += b"\0" * (SECTOR - len(header))
    frame0_blk = (f0_ctrl.ljust(f0_ctrl_sec * SECTOR, b"\0")
                  + f0_pat.ljust(f0_pat_sec * SECTOR, b"\0"))
    header_blob = (header
                   + paltab.ljust(paltab_sec * SECTOR, b"\0")
                   + audio_preload
                   + frame0_blk
                   + bytes(routing).ljust(routing_sec * SECTOR, b"\0")
                   + prebuf_bytes.ljust(prebuf_sec * SECTOR, b"\0"))
    if len(header_blob) % SECTOR:
        raise AssertionError(f"HEADER.DAT is not sector aligned: {len(header_blob)} bytes")

    out_path = Path(path)
    if out_path.name.upper() in {"HEADER.DAT", "BODY.DAT"}:
        raise SystemExit(
            "--output names the combined tooling container; "
            "use a name other than HEADER.DAT/BODY.DAT")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header_path = out_path.with_name("HEADER.DAT")
    body_path = out_path.with_name("BODY.DAT")
    # The Main-IP binary embeds the initial CRAM image.  Keep that build input
    # beside the split stream and derive it from the same canonical decision
    # log, so a stale palettes.bin cannot disagree with HEADER.DAT's PALTAB.
    palette_path = out_path.with_name("palettes.bin")
    palette_path.write_bytes(seg0)
    with header_path.open("wb") as f:
        f.write(header_blob)

    pc = Bpat * PAT; cc = 0
    with body_path.open("wb") as f:
        # v4 レートマッチpadding: 各frameを「CD 1x が1コマ時間に届けるセクタ数」までpaddingする。
        # CD 1x = 75セクタ/秒。1コマ=75/fps_int セクタ(15fps→5, 30fps→2.5)。この整数割り当てを
        # 累積器で出す(30fpsは2,3,2,3…平均2.5)。fsec=max(実データ, レート割当)。これで「ディスク
        # 読み速度=表示速度」になり、paddingを外したv4で起きた過剰配送→バッファ溢れ→CDCスリップ
        # を根絶する(15fpsでは5固定=v3と同一)。padセクタはプレイヤが読んで捨てる(累積器で同期)。
        # レートマッチpadding(有界累積器 sec_acc/lead)。CD 1x = 75 sec/s。1コマの CD 1x セクタ配分
        # ratedelta を累積器で整数化(15fps→5固定, 30fps→2/3平均)。lead = ディスクが CD 1x 予定より
        # 先行しているセクタ数(≥0)。重いコマ(実データ超過)は lead を増やし、後続の軽いコマは pad を
        # lead ぶん減らして吸収する。fsec = max(実データ, ratedelta - lead)。総ディスク量が CD 1x 相当
        # (=nfr*75/fps_int)に収束し、過剰配送(→バッファ溢れ→CDCスリップ)も過小配送も起きない。
        # プレイヤ(sp.s pump1)と同一の整数演算=ディスク上のフレーム境界が完全一致。
        fsec_list = []
        sec_acc = 0
        lead = 0
        for i in range(nfr):
            if f0_header and i == 0:
                continue                              # frame0 は FRAMES に出さない(ヘッダ側)
            sec_acc += 75
            ratedelta = sec_acc // fps_int
            sec_acc -= ratedelta * fps_int
            delta = ratedelta - lead                  # このコマで追いつくべきセクタ数(先行時は負)
            actual = int(n_pay_sec[i]) + int(n_ctrl_sec[i])
            fsec = actual if actual > delta else delta   # max(実データ, delta)
            lead += fsec - ratedelta                  # lead は常に ≥0(padで0に戻る)
            fsec_list.append(fsec)
            npb = int(n_pay_sec[i]) * SECTOR
            ncb = int(n_ctrl_sec[i]) * SECTOR
            # v6 physical order: complete the current control first, then
            # carry only payload that has been armed for future frames.
            fr = stream_ctrl[cc:cc + ncb].ljust(ncb, b"\0"); cc += ncb
            fr += stream_pay[pc:pc + npb].ljust(npb, b"\0"); pc += npb
            fr = fr.ljust(fsec * SECTOR, b"\0")       # レートマッチpad(超過ぶんは捨てセクタ)
            f.write(fr)
    if cc < len(stream_ctrl):
        raise AssertionError(f"BODY.DAT omitted {len(stream_ctrl) - cc} control bytes")
    if pc < len(stream_pay):
        raise AssertionError(f"BODY.DAT omitted {len(stream_pay) - pc} payload bytes")
    frames_stream_sec = int(sum(fsec_list))
    if body_path.stat().st_size != frames_stream_sec * SECTOR:
        raise AssertionError("BODY.DAT size disagrees with frame sector schedule")

    # Preserve MOVIE.DAT for offline tools.  Derive it from the two physical
    # disc files so there cannot be a third, subtly different representation.
    with out_path.open("wb") as dst, header_path.open("rb") as src:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)
    with out_path.open("ab") as dst, body_path.open("rb") as src:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)

    header_sec = len(header_blob) // SECTOR
    total = header_sec + frames_stream_sec
    if out_path.stat().st_size != total * SECTOR:
        raise AssertionError("combined MOVIE.DAT size disagrees with HEADER.DAT + BODY.DAT")
    print(f"wrote {header_path} {header_sec}sec + {body_path} {frames_stream_sec}sec; "
          f"combined {out_path} {total}sec (mode {mode_name} paltab {paltab_sec} startup_audio {audio_preload_frames}f/"
          f"{audio_preload_sec}s frame0 {f0_ctrl_sec}+{f0_pat_sec} "
          f"routing {routing_sec} prebuf {prebuf_sec} frames {frames_stream_sec}) "
          f"ring_peak {ring_peak*PAT/1024:.0f}KB  v6 N={vsync_n}(={NTSC_VSYNC/vsync_n:.3f}fps) AUDIO={AUDIO}")
    print(f"  initial CRAM: {palette_path} ({len(seg0)}B, canonical segment {int(frame_seg[0])})")
    print(f"  実機定数: NUM_FRAMES={nfr} FRAME_SECTORS={FRAME_SECTORS}(最大スロット) PALTAB_SEC={paltab_sec} "
          f"F0_CTRL_SEC={f0_ctrl_sec} F0_PAT_SEC={f0_pat_sec} ROUTING_SEC={routing_sec} "
          f"PREBUF_SEC={prebuf_sec} PREBUF_PAT={Bpat} RING_PEAK_PAT={ring_peak} VSYNC_N={vsync_n}")


def main():
    ap = argparse.ArgumentParser()
    sim_dir = sim_work_dir()
    ap.add_argument("--dec-log", default=str(sim_dir / "decisions.pkl"))
    ap.add_argument("--pool-slots", type=int, default=0)
    ap.add_argument("--alloc", choices=["lru", "contig"], default="contig",
                    help="スロット割当: contig=フレーム内cold連番(MD大DMA向け, 既定) / lru=旧方式")
    ap.add_argument("--output", default="out/movieplay/MOVIE.DAT")
    ap.add_argument("--audio", default="")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--compare", default=str(sim_dir / "preview"))
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    log = load_log(args.dec_log)
    require_canonical_p0_index15(log)
    # 単一真実源チェック: sim が焼いた tank と、この pack の RING_CAP は同じ実機リングを
    # モデルするので一致すべき。ズレていたら二重管理の兆候なので警告。
    sim_tank = log.get("tank_kb")
    sim_cold = log.get("max_cold")
    if sim_tank is not None and sim_tank != RING_CAP_KB:
        print(f"  [warn] sim tank_kb={sim_tank} != pack RING_CAP_KB={RING_CAP_KB} "
              f"(should match: both model the usable ring, av_config.py)")
    print(f"  encode params from sim: max_cold={sim_cold} tank_kb={sim_tank}  "
          f"pack RING_CAP_KB={RING_CAP_KB} (RING_SIZE {RING_SIZE_KB})")
    POOL = args.pool_slots or int(log["vram_tiles"])
    per, n_load, n_upd, pal_w, Plist, tearing = resolve(log, POOL, mode=args.alloc)
    print(f"resolve[{args.alloc}]: tearing={tearing} M(payload)={len(Plist)} frames={len(per)}")
    # 不変条件(単一真実源 av_config): 実配信(pack)の1コマ cold が drop-safe 上限を超えたら失敗。
    # sim のモデル cap が pack の連続スロット割当に対して高すぎる兆候(=解析は合うが実機で滑る)。
    # frame0(完全ロードのヘッダ)は除外。
    # realized == cap(共有 TileAllocator で構成上保証)。上限=cap を自動取得(手動env廃止)。
    cold_ceiling = av_config.cold_realized_ceiling_for_fps(FPS)   # = cold_cap_for_fps(FPS)
    realized_max = max([int(x) for x in n_load[1:]], default=0)
    if realized_max > cold_ceiling:
        raise SystemExit(
            f"pack: realized per-frame cold max={realized_max} > cap={cold_ceiling}. "
            f"共有 TileAllocator では realized=cap のはず=想定外。sim/pack の割り当て食い違いを疑う。")
    print(f"  realized cold: max={realized_max} == cap {cold_ceiling} (共有割り当て)")
    run_stats(per)
    blocks = build_control(log, per, n_upd, pal_w, args.audio)
    sc = schedule(per, n_load, blocks)
    st = "OK" if sc["feasible"] else f"INFEASIBLE(over {sc['over']} under {sc.get('under',0)})"
    Pb = sum(len(b) for b in blocks)
    under = sc.get("under", 0)
    print(f"schedule[{st}] prebuf {sc['prebuf_pat']*PAT/1024:.0f}KB ring_peak {sc['ring_peak']*PAT/1024:.0f}KB "
          f"ring_min {sc.get('ring_min',0)*PAT/1024:.0f}KB (cap {RING_CAP_KB}KB)  under(枯渇) {under} "
          f"({100.0*under/max(1,len(per)):.1f}%)  n_pay_sec avg {sc['n_pay_sec'].mean():.2f}  "
          f"control-first ready_min {sc['ready_min']}pat ctrl_min {sc['ctrl_min']}B")
    if sc["ring_peak"] > RING_CAP_PAT:
        print(f"  !! ring_peak {sc['ring_peak']*PAT/1024:.0f}KB > cap {RING_CAP_KB}KB (PRG収容不可の恐れ)")
    if args.verify:
        decode_verify(log, per, blocks, Plist, sc, compare_dir=args.compare or None,
                      sample_dir=Path(args.output).parent / "decoded")
    if not args.no_write:
        write_stream(args.output, log, per, blocks, Plist, sc, POOL)


if __name__ == "__main__":
    main()
