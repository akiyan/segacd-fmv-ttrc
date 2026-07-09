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

TTRCレイアウト: Header(1sec) + routing(2B/frame: n_pay_sec,n_ctrl_sec) + prebuffer(payload先頭Bpat)
              + frames(各5sec = [n_pay_sec payload][n_ctrl_sec control][pad])
control block: >H total_len >H n_upd >B pal >B dbg [DEBUG if dbg] [128 CRAM if pal]
               ceil(cells/8) bitmap n_upd*(>H entry) 887 audio [pad偶数]
  dbg=1 のとき諸元ヘッダ直後に固定長DEBUGブロック(前方固定=新プレイヤーは固定offsetで一発読み):
  7×>H カテゴリ数[raw,same,near,coa,flbk,buf,miss] + 4×>H 予約 = 22B(偶数)。
  既定ON(CBRSIM_PACK_DEBUG=0 で無効=リリース時は載せない)。
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

SECTOR = 2048
MAGIC = b"TTRC"             # Tile Texture Reuse Codec
VERSION = 1
BASE = 1                     # POOL_TILE_BASE (VRAM tile index = BASE+slot)
FRAME_SECTORS = 5
PAT = 32
PAT_PER_SEC = SECTOR // PAT  # 64
AUDIO = 887                  # 13.3kHz/15fps ≒ 886.7 -> 887固定
RING_CAP_KB = 464            # PRGに収める上限(usable~480 - apply/routing/program)
RING_CAP_PAT = RING_CAP_KB * 1024 // PAT

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
    key_slot = {}
    slot_key = [None] * POOL
    slot_lastuse = np.full(POOL, -1, np.int64)
    slot_refs = np.zeros(POOL, np.int32)
    free = list(range(POOL - 1, -1, -1))
    prev_slot = np.full(C_CELLS, -1, np.int64)
    cur_slot = np.full(C_CELLS, -1, np.int64)
    per = []
    n_load = np.zeros(nfr, np.int64)
    n_upd = np.zeros(nfr, np.int64)
    pal_w = np.zeros(nfr, np.int64)
    Plist = []
    tearing = 0
    hand = 0

    def evict(s):
        k = slot_key[s]
        if k is not None:
            key_slot.pop(k, None)
            slot_key[s] = None

    for i in range(nfr):
        prev_protect = np.zeros(POOL, bool)
        ps = prev_slot[prev_slot >= 0]
        prev_protect[ps] = True

        def alloc_slot():
            nonlocal tearing
            if free:
                return free.pop()
            cand = np.where((slot_refs == 0) & (~prev_protect))[0]
            if cand.size == 0:
                tearing += 1
                cand = np.where(slot_refs == 0)[0]
                if cand.size == 0:
                    cand = np.arange(POOL)
            s = int(cand[np.argmin(slot_lastuse[cand])])
            evict(s)
            return s

        def alloc_slot_contig():
            nonlocal hand, tearing
            for _ in range(POOL):
                s = hand
                hand = (hand + 1) % POOL
                if slot_refs[s] == 0 and not prev_protect[s]:
                    evict(s)
                    return s
            tearing += 1
            for _ in range(POOL):
                s = hand
                hand = (hand + 1) % POOL
                if slot_refs[s] == 0:
                    evict(s)
                    return s
            s = int(np.argmin(slot_lastuse))
            evict(s)
            return s

        if mode == "contig":
            alloc_slot = alloc_slot_contig

        pal_w[i] = 1 if (i == 0 or frame_seg[i] != frame_seg[i - 1]) else 0
        cells, entries, colds = [], [], []
        for (cell, pal, key) in sorted(frames[i], key=lambda t: t[0]):
            cold = key not in key_slot
            if cold:
                slot = alloc_slot()
                key_slot[key] = slot
                slot_key[slot] = key
                Plist.append(pack_key(key))
                n_load[i] += 1
            else:
                slot = key_slot[key]
            oldc = cur_slot[cell]
            if oldc >= 0:
                slot_refs[oldc] -= 1
            slot_refs[slot] += 1
            slot_lastuse[slot] = i
            cur_slot[cell] = slot
            cells.append(int(cell))
            entries.append((int(pal) << 13) | (BASE + slot))
            colds.append(cold)
            n_upd[i] += 1
        per.append((cells, entries, colds))
        prev_slot[:] = cur_slot
        if (i + 1) % 400 == 0:
            print(f"  resolve {i+1}/{nfr}", flush=True)
    return per, n_load, n_upd, pal_w, Plist, tearing


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
    # SPのラン形式ロード領域(Word-RAM 0x84..0x5000=20348B)に収まるか:
    # 1ラン = slot(2)+count(2)+count*32B
    loads_bytes = colds_per_frame * PAT + runs_per_frame * 4
    O_LOADS_CAP = 0x5000 - 0x84
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
    # デバッグ欄: 既定ON(catsがあれば)。CBRSIM_PACK_DEBUG=0 でリリース向けに省略(実効Band節約)。
    dbg_on = bool(cats_list) and os.environ.get("CBRSIM_PACK_DEBUG", "1") != "0"
    aud = b""
    if audio_path:
        raw = Path(audio_path).read_bytes()
        sm = bytearray(len(raw))
        for j, b in enumerate(raw):                     # s8 -> RF5C164 sign-magnitude
            s = b - 256 if b >= 128 else b
            sm[j] = min(s, 0x7F) if s >= 0 else (0x80 | min(-s, 0x7E))
        aud = bytes(sm)
    blocks = []
    for i in range(len(per)):
        cells, entries, colds = per[i]
        body = bytearray()
        body += struct.pack(">H", int(n_upd[i]))
        body += struct.pack(">BB", int(pal_w[i]), 1 if dbg_on else 0)
        if dbg_on:
            body += debug_block(cats_list[i])       # 固定長DEBUGブロック(Miss含む7カテゴリ+予約)
        if pal_w[i]:
            body += seg_cram[int(frame_seg[i])]
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


def schedule(per, n_load, blocks):
    """control JIT(前向き)で nc[i] を決め、payload は cap=(5-nc)*64 でセクタ単位の後ろ向き最小占有。"""
    nfr = len(per)
    blk_len = np.array([len(b) for b in blocks], np.int64)
    nc = np.zeros(nfr, np.int64)
    ctrl_deliv = 0
    ctrl_cur = 0
    for i in range(nfr):
        deficit = (ctrl_cur + int(blk_len[i])) - ctrl_deliv
        k = max(0, -(-deficit // SECTOR)) if deficit > 0 else 0
        nc[i] = k
        ctrl_deliv += k * SECTOR
        ctrl_cur += int(blk_len[i])
    cap_sec = np.maximum(FRAME_SECTORS - nc, 0)
    d = np.cumsum(n_load)
    M = int(d[-1])
    d_sec = -(-d // PAT_PER_SEC)
    M_sec = int(-(-M // PAT_PER_SEC))
    cumcap = np.cumsum(cap_sec)
    Bsec = int(max(0, np.max(d_sec - cumcap)))
    A = np.zeros(nfr, np.int64)
    A[-1] = M_sec
    for i in range(nfr - 1, 0, -1):
        A[i - 1] = max(int(d_sec[i - 1]), int(A[i] - cap_sec[i]))
    n_pay_sec = np.empty(nfr, np.int64)
    n_pay_sec[0] = A[0] - Bsec
    n_pay_sec[1:] = A[1:] - A[:-1]
    occ = A * PAT_PER_SEC - d
    over = int((n_pay_sec + nc > FRAME_SECTORS).sum())
    feasible = (n_pay_sec >= 0).all() and over == 0
    return dict(n_pay_sec=n_pay_sec, n_ctrl_sec=nc, feasible=feasible, over=over,
                prebuf_pat=Bsec * PAT_PER_SEC, ring_peak=int(occ.max()), blk_len=blk_len, M=M)


def decode_verify(log, per, blocks, Plist, sc, compare_dir=None, sample_dir=None):
    """プレイヤをシミュレート: payloadをセクタ単位でリングへ, control を apply-buffer カーソルで
    処理, cold entryでリングpop, レンダして sim preview と画素比較。"""
    frame_seg = np.asarray(log["frame_seg"], np.int64)
    seg_pals = log["seg_pals"]
    n_pay_sec = sc["n_pay_sec"]; blk_len = sc["blk_len"]; B = sc["prebuf_pat"]
    ctrl = b"".join(blocks)
    POOL = int(log["vram_tiles"])
    cmp = Path(compare_dir) if compare_dir else None
    if sample_dir:
        sample_dir = Path(sample_dir); sample_dir.mkdir(parents=True, exist_ok=True)
    samples = set(range(0, len(per), max(1, len(per) // 6)))
    ring = deque(Plist[:B]); pc = B; cc = 0
    tile = [None] * (POOL + BASE + 2)
    nt_slot = np.zeros(C_CELLS, np.int64); nt_pal = np.zeros(C_CELLS, np.int64)
    diffs = []; ring_peak = len(ring); bad = 0
    for i in range(len(per)):
        add = int(n_pay_sec[i]) * PAT_PER_SEC
        for k in range(pc, min(pc + add, len(Plist))):
            ring.append(Plist[k])
        pc += add
        ring_peak = max(ring_peak, len(ring))
        blk = ctrl[cc:cc + int(blk_len[i])]; cc += int(blk_len[i])
        p = 2                                         # skip total_len
        nupd = struct.unpack(">H", blk[p:p + 2])[0]; p += 2
        palw = blk[p]; dbg = blk[p + 1]; p += 2
        if dbg:
            p += DBG_LEN                              # skip debug block
        if palw:
            p += 128
        bm = blk[p:p + 72]; p += 72
        cells = [c for c in range(C_CELLS) if bm[c >> 3] & (1 << (c & 7))]
        for c in cells:
            e = struct.unpack(">H", blk[p:p + 2])[0]; p += 2
            cold = e >> 15; ent = e & 0x7FFF
            nt_pal[c] = (ent >> 13) & 3
            nt_slot[c] = (ent & 0x07FF) - BASE
            if cold:
                if not ring:
                    bad += 1
                else:
                    tile[int(nt_slot[c]) + BASE] = ring.popleft()
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
        fr = img.reshape(TROWS, TCOLS, TILE, TILE, 3).transpose(0, 2, 1, 3, 4).reshape(144, 256, 3)
        if sample_dir is not None and i in samples:
            Image.fromarray(fr, "RGB").save(sample_dir / f"decoded_{i:05d}.png")
        if cmp is not None:
            ref_p = cmp / f"{i:05d}.png"
            if ref_p.exists():
                ref = np.asarray(Image.open(ref_p).convert("RGB"))[:144, :256]
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
    """TTRC MOVIE.DAT を書き出す。"""
    n_pay_sec = sc["n_pay_sec"]; n_ctrl_sec = sc["n_ctrl_sec"]
    Bpat = int(sc["prebuf_pat"])
    frame_seg = np.asarray(log["frame_seg"], np.int64)
    seg0 = pals_to_bytes_128(log["seg_pals"][int(frame_seg[0])])
    nfr = len(per)
    payload = b"".join(Plist)
    control = b"".join(blocks)
    routing = bytearray()
    for i in range(nfr):
        routing += bytes([int(n_pay_sec[i]) & 0xFF, int(n_ctrl_sec[i]) & 0xFF])
    routing_sec = -(-len(routing) // SECTOR)
    prebuf_bytes = payload[:Bpat * PAT]
    prebuf_sec = -(-len(prebuf_bytes) // SECTOR)
    ring_peak = int(sc["ring_peak"])
    # Display mode (offset 38): 0=H32 / 1=H40 / 2=mode4. Prefer CBRSIM_MODE;
    # fall back to tcols (40 columns means H40). Reserved zero remains H32 for
    # backward compatibility with existing streams.
    _mode = {"H32": 0, "H40": 1, "mode4": 2}.get(
        os.environ.get("CBRSIM_MODE", "").strip(), 1 if TCOLS == 40 else 0)
    header = struct.pack(">4sHHHHHHHHH", MAGIC, VERSION, nfr, TCOLS, TROWS, C_CELLS,
                         POOL, BASE, FRAME_SECTORS, len(log["seg_pals"]))
    header += struct.pack(">LLLL", Bpat, routing_sec, prebuf_sec, ring_peak)
    header += bytes([_mode])                       # offset 38: display mode
    header += b"\0" * (64 - len(header)) + seg0
    header += b"\0" * (SECTOR - len(header))
    pc = Bpat * PAT; cc = 0
    with Path(path).open("wb") as f:
        f.write(header)
        f.write(bytes(routing).ljust(routing_sec * SECTOR, b"\0"))
        f.write(prebuf_bytes.ljust(prebuf_sec * SECTOR, b"\0"))
        for i in range(nfr):
            npb = int(n_pay_sec[i]) * SECTOR
            ncb = int(n_ctrl_sec[i]) * SECTOR
            fr = payload[pc:pc + npb].ljust(npb, b"\0"); pc += npb
            fr += control[cc:cc + ncb].ljust(ncb, b"\0"); cc += ncb
            fr = fr.ljust(FRAME_SECTORS * SECTOR, b"\0")
            f.write(fr)
    total = 1 + routing_sec + prebuf_sec + nfr * FRAME_SECTORS
    print(f"wrote {path}  {total}sec (routing {routing_sec} prebuf {prebuf_sec}) "
          f"ring_peak {ring_peak*PAT/1024:.0f}KB")
    print(f"  実機定数: NUM_FRAMES={nfr} FRAME_SECTORS={FRAME_SECTORS} ROUTING_SEC={routing_sec} "
          f"PREBUF_SEC={prebuf_sec} PREBUF_PAT={Bpat} RING_PEAK_PAT={ring_peak}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dec-log", default="tmp/sim/decisions.pkl")
    ap.add_argument("--pool-slots", type=int, default=0)
    ap.add_argument("--alloc", choices=["lru", "contig"], default="contig",
                    help="スロット割当: contig=フレーム内cold連番(MD大DMA向け, 既定) / lru=旧方式")
    ap.add_argument("--output", default="out/movieplay/MOVIE.DAT")
    ap.add_argument("--audio", default="")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--compare", default="tmp/sim/preview")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    log = load_log(args.dec_log)
    POOL = args.pool_slots or int(log["vram_tiles"])
    per, n_load, n_upd, pal_w, Plist, tearing = resolve(log, POOL, mode=args.alloc)
    print(f"resolve[{args.alloc}]: tearing={tearing} M(payload)={len(Plist)} frames={len(per)}")
    run_stats(per)
    blocks = build_control(log, per, n_upd, pal_w, args.audio)
    sc = schedule(per, n_load, blocks)
    st = "OK" if sc["feasible"] else f"INFEASIBLE(over {sc['over']})"
    Pb = sum(len(b) for b in blocks)
    print(f"schedule[{st}] prebuf {sc['prebuf_pat']*PAT/1024:.0f}KB ring_peak {sc['ring_peak']*PAT/1024:.0f}KB "
          f"(cap {RING_CAP_KB}KB)  control {Pb/1024:.0f}KB  n_pay_sec avg {sc['n_pay_sec'].mean():.2f}")
    if sc["ring_peak"] > RING_CAP_PAT:
        print(f"  !! ring_peak {sc['ring_peak']*PAT/1024:.0f}KB > cap {RING_CAP_KB}KB (PRG収容不可の恐れ)")
    if args.verify:
        decode_verify(log, per, blocks, Plist, sc, compare_dir=args.compare or None,
                      sample_dir=Path(args.output).parent / "decoded")
    if not args.no_write:
        write_stream(args.output, log, per, blocks, Plist, sc, POOL)


if __name__ == "__main__":
    main()
