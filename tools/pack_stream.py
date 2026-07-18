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

TTRCレイアウト(v8): HEADER.DAT = Header(1sec) + PALTAB(全区間パレット
              n_seg×128B, boot時Main-RAM表へ) + startup audio prefetch(1 sector/frame)
              + frame0(control+patterns) + routing(1B/frame: total<<3 | n_ctrl_sec)
              + prebuffer(payload先頭Bpat)
              BODY.DAT = frame1以降の [control][payload][rate pad]
MOVIE.DAT はツール互換用の HEADER.DAT || BODY.DAT 連結コンテナ。
control block: >H total_len >H frame_seq >H n_upd >B pal >B dbg [DEBUG if dbg]
               ceil(cells/8) bitmap n_upd*(>H entry) audio [even pad]
               >H n_runs n_runs*(>H slot_start >H count)
  pal = 区間番号+1(0=切替なし)。実機はMain-RAMのPALTAB表を引く(in-stream CRAM廃止)。
  dbg=1 のとき諸元ヘッダ直後に固定長DEBUGブロック(前方固定=新プレイヤーは固定offsetで一発読み):
  7×>H カテゴリ数[raw,same,near,coa,flbk,buf,miss] + 4×>H 予約 = 22B(偶数)。
  既定OFF。TOML の pack.debug=true でデバッグ時だけ載せる。
"""
import argparse
import math
import pickle
import struct
import sys
from pathlib import Path
from collections import deque
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import av_config
import player_constants
import ttrc_routing
from encode_config import load_profile
from cbr_paths import sim_work_dir
from quantize_global4_tiles import pals_to_bytes
from quantize_md_video import rgb333_to_rgb888
from tile_alloc import slot_runs

SECTOR = 2048
MAGIC = b"TTRC"             # Tile Texture Reuse Codec
VERSION = ttrc_routing.VERSION
BASE = 1                     # POOL_TILE_BASE (VRAM tile index = BASE+slot)
FRAME_SECTORS = ttrc_routing.FRAME_SECTORS
PAT = 32
PAT_PER_SEC = SECTOR // PAT  # 64
NTSC_VSYNC = av_config.NTSC_VSYNC
# These values are populated from the decision log by configure_from_log().
# They are intentionally not read from CBRSIM_*: the log is the frozen encoder
# contract, and an unrelated inherited shell must not change the packed disc.
TCOLS = TROWS = C_CELLS = 0
TILE = 8
PATTERN_BYTES = 32
FPS = 0.0
VSYNC_N = 0
PLAYBACK_FPS = 0.0
AUDIO_KIND = "pcm13"
AUDIO_RATE = 0
AUDIO = 0
STARTUP_AUDIO_FRAMES = 30
PACK_DEBUG = False
PACK_FILL = True
PCM_SYNC_LEAD = 0x3000
PCM_SYNC_MAX = 0x6800
PCM_WAVE_RING_END = 0x8000
PCM_STARTUP_MARGIN = 0x0200
# リング諸元は tools/av_config.py の単一真実源から取る(sim/pack/playerで二重管理しない)。
# RING_SIZE はプレイヤの実 .equ RING_SIZE と一致(ビルド時 check_player_ring.py が検証)。
# RING_CAP(スケジュール上限)と sim の TANK は RING_SIZE から導出され必ず一致する。
RING_SIZE_KB = av_config.RING_SIZE_KB
RING_CAP_KB = av_config.RING_CAP_KB
RING_CAP_PAT = RING_CAP_KB * 1024 // PAT

# --- デバッグブロック(control先頭ヘッダ直後・固定長) ---
DBG_NCAT = 7                 # カテゴリ数 [raw,same,near,coa,flbk,buf,miss]
DBG_RESERVED = 4             # 予約u16スロット(将来の16bitデバッグ値用)
DBG_LEN = (DBG_NCAT + DBG_RESERVED) * 2   # = 22B(偶数)
FEATURE_COLD_RUNS = ttrc_routing.FEATURE_COLD_RUNS
FEATURE_FIXED_N2 = ttrc_routing.FEATURE_FIXED_N2
ROUTING_MAX_FRAMES = ttrc_routing.MAX_FRAMES


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


def configure_from_log(log, *, debug=None, fill=None, startup_audio_frames=None):
    """Populate pack constants from one frozen decision log.

    Legacy logs are accepted through their existing top-level fields.  No
    CBRSIM_* value participates in this function.
    """
    global TCOLS, TROWS, C_CELLS, TILE, PATTERN_BYTES
    global FPS, VSYNC_N, PLAYBACK_FPS, AUDIO_KIND, AUDIO_RATE, AUDIO
    global STARTUP_AUDIO_FRAMES, PACK_DEBUG, PACK_FILL

    cfg = log.get("config") or {}
    video = cfg.get("video") or {}
    timing = cfg.get("timing") or {}
    audio = cfg.get("audio") or {}
    hardware = cfg.get("hardware") or {}
    pack = cfg.get("pack") or {}
    geom = log.get("geom")
    if geom is None:
        geom = (video.get("cols"), video.get("rows"), video.get("cells"), video.get("tile"))
    if not geom or any(value is None for value in geom):
        raise SystemExit("decision log has no complete geometry")
    TCOLS, TROWS, C_CELLS, TILE = map(int, geom)
    if TILE != 8 or C_CELLS != TCOLS * TROWS:
        raise SystemExit(
            f"invalid decision geometry: {TCOLS}x{TROWS} cells={C_CELLS} tile={TILE}")
    PATTERN_BYTES = 32

    FPS = float(timing.get("fps", log.get("fps", 0)))
    if FPS <= 0:
        raise SystemExit("decision log has no valid fps")
    expected_vsync_n = av_config.vsync_n_for_fps(FPS)
    VSYNC_N = int(timing.get("vsync_n", expected_vsync_n))
    if VSYNC_N != expected_vsync_n:
        raise SystemExit(
            f"decision log vsync_n={VSYNC_N} disagrees with fps={FPS} ({expected_vsync_n})")
    expected_playback_fps = av_config.playback_fps_for_content(FPS)
    PLAYBACK_FPS = float(timing.get("playback_fps", expected_playback_fps))
    if not math.isclose(PLAYBACK_FPS, expected_playback_fps, rel_tol=0, abs_tol=1e-9):
        raise SystemExit(
            f"decision log playback_fps={PLAYBACK_FPS} disagrees with fps={FPS} "
            f"({expected_playback_fps})")

    AUDIO_KIND = str(audio.get("kind", log.get("audio_kind", "pcm13")))
    AUDIO_RATE = int(audio.get("rate", log.get("audio_rate", 0)))
    AUDIO = int(audio.get("frame_bytes", log.get("audio_frame_bytes", 0)))
    if AUDIO_RATE <= 0 or AUDIO <= 0:
        raise SystemExit("decision log has no valid audio rate/frame size")
    expected_audio = (av_config.pcm_frame_bytes(FPS, AUDIO_RATE)
                      if AUDIO_KIND == "pcm13" else int(round(AUDIO_RATE / FPS)))
    if AUDIO != expected_audio:
        raise SystemExit(
            f"decision log audio_frame_bytes={AUDIO} disagrees with its own "
            f"{AUDIO_KIND}/{AUDIO_RATE}Hz/{FPS:g}fps settings ({expected_audio})")

    sim_tank = int(hardware.get("tank_kb", log.get("tank_kb", RING_CAP_KB)))
    if sim_tank != RING_CAP_KB:
        raise SystemExit(
            f"decision log tank_kb={sim_tank} != hardware RING_CAP_KB={RING_CAP_KB}; "
            "re-run sim with the current tools/av_config.py")
    PACK_DEBUG = bool(pack.get("debug", False)) if debug is None else bool(debug)
    PACK_FILL = bool(pack.get("fill", True)) if fill is None else bool(fill)
    startup = pack.get("startup_audio_frames", 30)
    if startup_audio_frames is not None:
        startup = startup_audio_frames
    STARTUP_AUDIO_FRAMES = max(0, int(startup))


def require_canonical_p0_debug_colours(log):
    """Reject stale logs without the fixed dark background and bright text."""
    seg_pals = log.get("seg_pals")
    if not seg_pals:
        raise SystemExit("pack v8: decision log has no segment palettes; re-run sim")
    for seg, pals in enumerate(seg_pals):
        a = np.asarray(pals, np.uint8)
        if a.shape != (4, 15, 3):
            raise SystemExit(
                f"pack v8: segment {seg} palette shape is {a.shape}, expected (4, 15, 3); "
                "re-run sim")
        brightness = a.astype(np.int16).sum(axis=2)
        if int(brightness[0, 0]) != int(brightness.min()):
            raise SystemExit(
                f"pack v8: decision log segment {seg} P0 index1 is not tied for globally "
                "darkest usable CRAM colour (RGB sum); re-run sim with the current encoder")
        if int(brightness[0, 14]) != int(brightness.max()):
            raise SystemExit(
                f"pack v8: decision log segment {seg} P0 index15 is not tied for globally "
                "brightest usable CRAM colour (RGB sum); re-run sim with the current encoder")


def pals_to_bytes_128(pal_4x15):
    b = pals_to_bytes([np.asarray(pal_4x15[p], np.uint8) for p in range(4)])
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
    """フレーム内cold tile数とplayer cold-run record数を返して表示する。"""
    runs_per_frame = np.zeros(len(per), np.int64)
    colds_per_frame = np.zeros(len(per), np.int64)
    for i, (cells, entries, colds) in enumerate(per):
        runs = cold_runs(entries, colds)
        runs_per_frame[i] = len(runs)
        colds_per_frame[i] = sum(count for _slot, count in runs)
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
    return colds_per_frame, runs_per_frame


def cold_runs(entries, colds):
    """Return the exact packed/player cold-run records for one frame."""
    return slot_runs(
        (int(entry) & 0x07FF) - BASE
        for entry, cold in zip(entries, colds)
        if cold
    )


def verify_sim_pattern_transfers(log, packed_tiles, packed_runs):
    """Require frozen sim transfer counts to match pack/player counts exactly.

    Old decision logs predate these fields and remain packable.  Every newly
    generated log carries them, turning a future run-grouping change into a
    pack-time failure instead of a misleading analysis meter.
    """
    frozen = log.get("pattern_transfers")
    if frozen is None:
        print("  pattern transfer照合: 旧decision logのため省略 (再simで有効化)")
        return False
    if int(frozen.get("schema_version", 0)) != 1:
        raise SystemExit(
            "pack: unsupported pattern_transfers schema "
            f"{frozen.get('schema_version')!r}")

    expected = {
        "tiles": np.asarray(packed_tiles, np.int64),
        "runs": np.asarray(packed_runs, np.int64),
    }
    for name, actual in expected.items():
        simulated = np.asarray(frozen.get(name, ()), np.int64)
        if simulated.shape != actual.shape:
            raise SystemExit(
                f"pack: sim/pack pattern {name} length mismatch: "
                f"sim={simulated.shape} pack={actual.shape}")
        mismatch = np.flatnonzero(simulated != actual)
        if mismatch.size:
            frame = int(mismatch[0])
            raise SystemExit(
                f"pack: sim/pack pattern {name} mismatch at frame {frame}: "
                f"sim={int(simulated[frame])} pack={int(actual[frame])}. "
                "TileAllocator/run grouping changed after simulation; re-run sim.")
    print(f"  pattern transfer照合: {len(packed_runs)} frames tiles/runs exact")
    return True


def build_control(log, per, n_upd, pal_w, audio_path):
    """毎フレームの control ブロック列(連続バイト用)を作る。"""
    seg_cram = [pals_to_bytes_128(p) for p in log["seg_pals"]]
    frame_seg = np.asarray(log["frame_seg"], np.int64)
    cats_list = log.get("cats")                     # per-frame [raw,same,near,coa,flbk,buf,miss]
    # デバッグ欄: decision log に固定された pack.debug を使う。
    dbg_on = bool(cats_list) and PACK_DEBUG
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
        source_len = len(raw)
        raw = retime_pcm_u8(raw, len(per) * AUDIO)
        if len(raw) != source_len:
            print(f"  PCM retime: {source_len} -> {len(raw)} samples "
                  f"({AUDIO} B/frame x {len(per)} frames)")
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
        # Keep the legacy audio offset unchanged.  The suffix is aligned so the
        # 68000 can read its words directly; old players simply ignore it.
        if len(body) & 1:
            body += b"\0"
        runs = cold_runs(entries, colds)
        body += struct.pack(">H", len(runs))
        for slot, count in runs:
            body += struct.pack(">HH", slot, count)
        # total_len は「先頭2Bを含むブロック全長」。実機は apply_cur を total_len で進めるので
        # パディング込みの偶数にする(奇数だと1B/フレームずつ desync する)。
        total = len(body) + 2
        if total & 1:
            body += b"\0"
            total += 1
        blocks.append(struct.pack(">H", total) + bytes(body))
    return blocks


def control_audio_bounds(block):
    """Return the fixed-size PCM slice embedded in one control block."""
    n_upd = struct.unpack_from(">H", block, 4)[0]
    dbg = block[7]
    pos = 8 + (DBG_LEN if dbg else 0) + ((C_CELLS + 7) // 8) + n_upd * 2
    return pos, pos + AUDIO


def control_audio(block):
    """Return the fixed-size PCM chunk embedded in one control block."""
    start, end = control_audio_bounds(block)
    chunk = block[start:end]
    if len(chunk) != AUDIO:
        raise ValueError(f"control audio truncated: got {len(chunk)}, expected {AUDIO}")
    return chunk


def replace_control_audio(block, chunk):
    """Replace one PCM chunk without changing the control block length."""
    if len(chunk) != AUDIO:
        raise ValueError(f"replacement audio is {len(chunk)} bytes, expected {AUDIO}")
    start, end = control_audio_bounds(block)
    out = bytearray(block)
    if len(out[start:end]) != AUDIO:
        raise ValueError("control audio replacement points outside the block")
    out[start:end] = chunk
    if len(out) != len(block):
        raise AssertionError("audio replacement changed the control block length")
    return bytes(out)


def retime_pcm_u8(raw, target_len):
    """Stretch mono u8 PCM evenly to the fixed-chunk playback length."""
    if target_len <= 0:
        return b""
    if not raw:
        return b"\x80" * target_len
    if len(raw) == target_len:
        return bytes(raw)
    src = np.frombuffer(raw, np.uint8).astype(np.float64)
    src_x = np.arange(len(src), dtype=np.float64)
    dst_x = np.linspace(0.0, float(len(src) - 1), target_len)
    out = np.rint(np.interp(dst_x, src_x, src)).clip(0, 255).astype(np.uint8)
    return out.tobytes()


def rate_deltas(nfr):
    """Return the CD-1x sector allowance for BODY frames 1..N-1.

    Frame 0 lives in HEADER.DAT, so its allowance is zero.  The accumulator is
    intentionally identical to the player and to the BODY writer. Nominal
    30fps fixed-N2 content uses the exact 1001/400 sectors needed by two NTSC
    VBlanks. The 24/15 fps paths retain their legacy 75/nominal-fps delivery
    schedule.
    """
    try:
        rate_num, rate_mod = av_config.cd_sector_rate(FPS)
    except ValueError as exc:
        raise SystemExit(f"pack: {exc}") from exc
    out = np.zeros(nfr, np.int64)
    acc = 0
    for i in range(1, nfr):
        acc += rate_num
        out[i] = acc // rate_mod
        acc -= int(out[i]) * rate_mod
    return out


def rate_match_fsec(n_pay_sec, n_ctrl_sec):
    """Apply the player's bounded CD-rate accumulator to a routing table."""
    ratedelta = rate_deltas(len(n_pay_sec))
    fsec = np.zeros(len(n_pay_sec), np.int64)
    lead_trace = np.zeros(len(n_pay_sec), np.int64)
    lead = 0
    for i in range(1, len(n_pay_sec)):
        actual = int(n_pay_sec[i]) + int(n_ctrl_sec[i])
        due = int(ratedelta[i]) - lead
        fsec[i] = max(actual, due)
        lead += int(fsec[i]) - int(ratedelta[i])
        if lead < 0:
            raise AssertionError("rate-match lead became negative")
        lead_trace[i] = lead
    return fsec, ratedelta, lead_trace


def schedule(per, n_load, blocks):
    """Schedule control JIT and rate-shaped payload prefetch sectors."""
    nfr = len(per)
    blk_len = np.array([len(b) for b in blocks], np.int64)
    # v6+ always arms frame 0 from HEADER.DAT.  Keeping a switch here would
    # produce a file whose layout disagrees with its version, so reject the
    # removed legacy mode explicitly instead of silently emitting it.
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
    # PACK_FILL(default ON): use only the sectors that CD 1x can deliver by this
    # frame, replacing rate padding with useful payload whenever ring space is
    # available.  The old forward fill used all five routing sectors even at
    # 30 fps, where the real-time allowance is only two or three sectors. Starting
    # with a full ring and immediately refilling every consumed pattern made the
    # first 30 Sonic frames carry 102 sectors instead of 75.  BODY is data-paced,
    # so that policy itself slowed startup and exhausted the PCM preload.
    #
    # A backwards minimum-delivery envelope still guarantees every future cold
    # deadline.  If a burst truly needs more than the current rate allowance,
    # the schedule may use up to FRAME_SECTORS and the normal bounded lead logic
    # repays that burst later.  Thus this remains forward prefetch, but never
    # creates an avoidable above-1x startup burst merely to keep a full ring full.
    if PACK_FILL:
        ring_sec = RING_CAP_PAT // PAT_PER_SEC             # リング上限(セクタ)
        # frame0はDAT冒頭の専用ヘッダブロックとしてboot中にVRAM直ロードする(=ストリーミングの
        # リングを一切経由しない)。よってframe0のcoldはストリームの累積d(=リングpop)から除外し、
        # プリバッファは frame1用の満タンリング(RING_CAP)だけにする。frame0の配信は0。
        # これでboot時リングは常にRING_CAP以下=back-pressureに触れず、かつframe1以降が
        # 満タンリングで始まる。frame0の大バーストによる後続枯渇(崩壊)を根絶。
        Bsec = int(min(ring_sec, M_sec))                  # frame1用プリバッファ=満タン(<=総量)

        # need[i] = the least cumulative payload sectors that must have arrived
        # by the end of BODY slot i.  Besides the immediate frame-i+1 deadline,
        # carry future demand backwards through each slot's hard 5-n_ctrl cap so
        # a large later burst is prefetched early enough.
        need = np.zeros(nfr, np.int64)
        if nfr:
            need[-1] = M_sec                              # emit the complete payload stream
        for i in range(nfr - 2, -1, -1):
            immediate = int(-(-int(d[i + 1]) // PAT_PER_SEC))
            future = int(need[i + 1]) - int(cap_sec[i + 1])
            need[i] = max(immediate, future, 0)
        if nfr and Bsec < int(need[0]):
            raise SystemExit(
                f"pack: prebuffer {Bsec} sectors cannot arm the payload schedule; "
                f"at least {int(need[0])} are required")

        ratedelta = rate_deltas(nfr)
        rate_lead = 0
        prev = Bsec
        if nfr:
            A[0] = Bsec                                   # frame0 is outside BODY
        for i in range(1, nfr):
            # Sectors due now after repaying any earlier above-rate burst.  A
            # control sector consumes the allowance first; payload occupies only
            # the remainder unless the backwards safety envelope requires more.
            due = int(ratedelta[i]) - rate_lead
            soft_pay = max(0, due - int(nc[i]))
            hi_ring = (int(d[i]) + RING_CAP_PAT) // PAT_PER_SEC
            hi = min(prev + int(cap_sec[i]), M_sec, int(hi_ring))
            lo = max(prev, int(need[i]))
            if lo > hi:
                raise SystemExit(
                    f"pack: rate-shaped payload schedule is impossible at frame {i}: "
                    f"minimum cumulative delivery {lo} sectors exceeds limit {hi}")
            a = max(lo, min(prev + soft_pay, hi))
            A[i] = a
            actual = (a - prev) + int(nc[i])
            fsec = max(actual, due)
            rate_lead += fsec - int(ratedelta[i])
            if rate_lead < 0:
                raise AssertionError("rate-shaped schedule lead became negative")
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
                f"pack v8 control-first invariant failed at frame {frame}: "
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
            f"pack v8 control completeness failed at frame {frame}: "
            f"{int(ctrl_delivered[frame])} bytes delivered, "
            f"{int(ctrl_need[frame])} bytes required")

    fsec, ratedelta, rate_lead_trace = rate_match_fsec(n_pay_sec, nc)
    rate_lead_peak = int(rate_lead_trace.max()) if nfr else 0
    rate_lead_end = int(rate_lead_trace[-1]) if nfr else 0
    feasible = ((n_pay_sec >= 0).all() and over == 0 and under == 0
                and ready_min >= 0 and ctrl_min >= 0 and rate_lead_end == 0)
    return dict(n_pay_sec=n_pay_sec, n_ctrl_sec=nc, feasible=feasible, over=over, under=under,
                prebuf_pat=Bsec * PAT_PER_SEC, ring_peak=int(occ.max()), ring_min=int(occ.min()),
                ready_min=ready_min, ctrl_min=ctrl_min, blk_len=blk_len, M=M,
                fsec=fsec, ratedelta=ratedelta, rate_lead_peak=rate_lead_peak,
                rate_lead_end=rate_lead_end,
                f0_header=F0_HEADER, f0_cold=int(n_load[0]), f0_ctrl_len=int(blk_len[0]))


def decode_verify(log, per, blocks, Plist, sc, compare_dir=None, sample_dir=None):
    """Simulate the current control-first player and compare it with sim output.

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
            img[c] = rgb333_to_rgb888(full16[nt_pal[c], idx].reshape(8, 8, 3))
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
    """Write the v8 split stream and a combined tooling container.

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
    if nfr > ROUTING_MAX_FRAMES:
        raise SystemExit(
            f"pack: {nfr} frames exceed the player's {ROUTING_MAX_FRAMES}-frame "
            "routing table; split or shorten the source")
    f0_header = bool(sc.get("f0_header", False))
    nl0 = int(sc.get("f0_cold", 0))
    f0_ctrl_len = int(sc.get("f0_ctrl_len", 0))
    payload = b"".join(Plist)

    # Queue the first N chunks from HEADER, then make each live control carry
    # the next future chunk. The old duplicate-and-skip layout consumed the
    # entire startup reserve by frame N and left the writer next to the play
    # head. Shifting fixed-size chunks keeps block lengths and sector scheduling
    # unchanged while preserving the exact source sample order.
    safe_audio_prefetch = max(0, min(
        (PCM_SYNC_MAX - PCM_SYNC_LEAD) // max(1, AUDIO),
        (PCM_WAVE_RING_END - PCM_SYNC_LEAD - PCM_STARTUP_MARGIN) // max(1, AUDIO)))
    audio_prefetch_frames = (
        min(nfr, STARTUP_AUDIO_FRAMES, safe_audio_prefetch) if f0_header else 0)
    source_audio_chunks = [control_audio(block) for block in blocks]
    silence_chunk = b"\0" * AUDIO
    disc_blocks = [
        replace_control_audio(
            block,
            source_audio_chunks[i + audio_prefetch_frames]
            if i + audio_prefetch_frames < nfr else silence_chunk)
        for i, block in enumerate(blocks)
    ]
    queued_audio = (source_audio_chunks[:audio_prefetch_frames]
                    + [control_audio(block) for block in disc_blocks])
    if queued_audio[:nfr] != source_audio_chunks:
        raise AssertionError("startup PCM prefetch changed the source sample order")
    if any(chunk != silence_chunk for chunk in queued_audio[nfr:]):
        raise AssertionError("startup PCM prefetch tail is not silent")
    if [len(block) for block in disc_blocks] != [len(block) for block in blocks]:
        raise AssertionError("startup PCM prefetch changed control block lengths")
    print(f"  audio prefetch: {audio_prefetch_frames} chunks queued; "
          f"source order verified for {nfr} playback chunks")

    control = b"".join(disc_blocks)
    # frame0の control/patterns をストリームから切り出す(ヘッダ側へ)
    if f0_header:
        f0_ctrl = control[:f0_ctrl_len]
        f0_pat = payload[:nl0 * PAT]
        stream_ctrl = control[f0_ctrl_len:]          # frames1+ の control連結
        stream_pay = payload[nl0 * PAT:]             # frames1+ の payload連結
        f0_ctrl_sec = -(-len(f0_ctrl) // SECTOR)
        f0_pat_sec = -(-len(f0_pat) // SECTOR)
        if f0_pat_sec * SECTOR > av_config.RING_JITTER_MARGIN_KB * 1024:
            raise SystemExit(
                f"pack: frame0 needs {f0_pat_sec} pattern sectors, beyond the "
                f"{av_config.RING_JITTER_MARGIN_KB}KB boot staging tail")
    else:
        f0_ctrl = f0_pat = b""
        stream_ctrl = control
        stream_pay = payload
        f0_ctrl_sec = f0_pat_sec = 0
    if len(n_pay_sec) != nfr or len(n_ctrl_sec) != nfr:
        raise AssertionError(
            f"routing array length mismatch: frames={nfr}, "
            f"pay={len(n_pay_sec)}, ctrl={len(n_ctrl_sec)}")
    routing = bytearray()
    for frame, (n_pay, n_ctrl) in enumerate(zip(n_pay_sec, n_ctrl_sec)):
        try:
            routing.append(ttrc_routing.encode_route(n_pay, n_ctrl))
        except ValueError as exc:
            raise SystemExit(f"pack: invalid routing at frame {frame}: {exc}") from exc
    routing_sec = ttrc_routing.routing_sector_count(nfr)
    routing_blob = bytes(routing).ljust(routing_sec * SECTOR, b"\0")
    try:
        ttrc_routing.validate_route_table(routing_blob, nfr, routing_sec)
    except ValueError as exc:
        raise AssertionError(f"packer produced an invalid routing table: {exc}") from exc
    prebuf_bytes = stream_pay[:Bpat * PAT]           # frame1用プリバッファ(RING_CAP)
    prebuf_sec = -(-len(prebuf_bytes) // SECTOR)
    ring_peak = int(sc["ring_peak"])
    # The sim decision log is the source of truth. Never let a changed shell
    # environment silently turn an H32 stream into H40.
    mode_name = str(log.get("mode") or (log.get("config") or {}).get("video", {}).get("mode", "")).strip().upper()
    if not mode_name:
        mode_name = "H40" if TCOLS == 40 else "H32"
    if mode_name not in {"H32", "H40", "MODE4"}:
        raise SystemExit(f"pack: unsupported display mode in decision log: {mode_name!r}")
    _mode = {"H32": 0, "H40": 1, "MODE4": 2}[mode_name]
    # PALTAB: 全区間パレットをヘッダ直後に一括配置(セクタ整列)。boot時にMain-RAM表へ。
    paltab = b"".join(pals_to_bytes_128(p) for p in log["seg_pals"])
    paltab_sec = -(-len(paltab) // SECTOR)
    # One prefetched PCM chunk per sector lets the Sub write each chunk without
    # cross-sector staging. Controls are already shifted ahead, so offset 58's
    # legacy duplicate-skip count is zero; offset 60 still tells the player how
    # many HEADER sectors to queue before PCM starts.
    audio_preload = b"".join(
        source_audio_chunks[i].ljust(SECTOR, b"\0")
        for i in range(audio_prefetch_frames)
    )
    audio_preload_frames = 0
    audio_preload_sec = audio_prefetch_frames
    # v4: 可変フレーム(5セクタ固定paddingを廃止=各frameは n_pay+n_ctrl セクタ)＋ vsync/コマ N。
    # CDレート累積器が実際のfpsを決める。Nは整数VBlank cadenceのヒントで、24fpsのN2を
    # 29.97fpsへ丸める指定ではない。AUDIOも実効fps由来。FRAME_SECTORS(=5)は最大スロット。
    vsync_n = VSYNC_N                                  # N: 近似VBLANK間隔(30/24→2, 15→4)
    fps_int = int(round(FPS))                         # 名目fps。FEATURE_FIXED_N2時はplayerが1001/400を選ぶ
    if not f0_header:
        raise SystemExit("pack v8 requires frame0 in HEADER.DAT")
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
    features = FEATURE_COLD_RUNS
    if av_config.uses_fixed_n2_cadence(FPS):
        features |= FEATURE_FIXED_N2
    header += struct.pack(">H", features)          # offset 62: optional stream features
    header += b"\0" * (64 - len(header)) + seg0
    header += b"\0" * (SECTOR - len(header))
    header = player_constants.stamp_header_sector(header)
    frame0_blk = (f0_ctrl.ljust(f0_ctrl_sec * SECTOR, b"\0")
                  + f0_pat.ljust(f0_pat_sec * SECTOR, b"\0"))
    header_blob = (header
                   + paltab.ljust(paltab_sec * SECTOR, b"\0")
                   + audio_preload
                   + frame0_blk
                   + routing_blob
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
    constants_path = out_path.with_name("player_constants.inc")
    player_constants.generate_include(header_path, constants_path)

    pc = Bpat * PAT; cc = 0
    fsec_schedule = sc["fsec"]
    with body_path.open("wb") as f:
        # v4 レートマッチpadding: 各frameを「CD 1x が1コマ時間に届けるセクタ数」までpaddingする。
        # CD 1x = 75セクタ/秒。FEATURE_FIXED_N2時は1001/400 sectors/frame、その他は75/fps_int。
        # この整数割り当てを累積器で出し、fsec=max(実データ, レート割当)として「ディスク
        # 読み速度=表示速度」になり、paddingを外したv4で起きた過剰配送→バッファ溢れ→CDCスリップ
        # を根絶する(15fpsでは5固定=v3と同一)。padセクタはプレイヤが読んで捨てる(累積器で同期)。
        # レートマッチpadding(有界累積器 sec_acc/lead)。1コマのCD 1xセクタ配分ratedeltaを
        # 累積器で整数化(15fps→5固定, 24fps→75/24, FIXED_N2→1001/400)。lead = CD 1x予定より
        # 先行しているセクタ数(≥0)。重いコマ(実データ超過)は lead を増やし、後続の軽いコマは pad を
        # lead ぶん減らして吸収する。fsec = max(実データ, ratedelta - lead)。総ディスク量が CD 1x 相当
        # 指定された実効表示rateに収束し、過剰配送(→バッファ溢れ→CDCスリップ)も過小配送も起きない。
        # プレイヤ(sp.s pump1)と同一の整数演算=ディスク上のフレーム境界が完全一致。
        # schedule() has already applied that accumulator while choosing useful
        # payload in place of padding, so write the proven result directly.
        fsec_list = []
        for i in range(nfr):
            if f0_header and i == 0:
                continue                              # frame0 は FRAMES に出さない(ヘッダ側)
            fsec = int(fsec_schedule[i])
            fsec_list.append(fsec)
            npb = int(n_pay_sec[i]) * SECTOR
            ncb = int(n_ctrl_sec[i]) * SECTOR
            # v6+ physical order: complete the current control first, then
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
          f"combined {out_path} {total}sec (mode {mode_name} paltab {paltab_sec} "
          f"startup_audio prefetch {audio_prefetch_frames}f/skip {audio_preload_frames}f "
          f"frame0 {f0_ctrl_sec}+{f0_pat_sec} "
          f"routing {routing_sec} prebuf {prebuf_sec} frames {frames_stream_sec}) "
          f"ring_peak {ring_peak*PAT/1024:.0f}KB  v8 N={vsync_n}(={PLAYBACK_FPS:.3f}fps) AUDIO={AUDIO}")
    print(f"  initial CRAM: {palette_path} ({len(seg0)}B, canonical segment {int(frame_seg[0])})")
    print(f"  player constants: {constants_path}")
    print(f"  実機定数: NUM_FRAMES={nfr} FRAME_SECTORS={FRAME_SECTORS}(最大スロット) PALTAB_SEC={paltab_sec} "
          f"F0_CTRL_SEC={f0_ctrl_sec} F0_PAT_SEC={f0_pat_sec} ROUTING_SEC={routing_sec} "
          f"PREBUF_SEC={prebuf_sec} PREBUF_PAT={Bpat} RING_PEAK_PAT={ring_peak} VSYNC_N={vsync_n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="per-source TOML profile (used to locate and authenticate decisions.pkl)")
    ap.add_argument("--dec-log", default="")
    ap.add_argument("--pool-slots", type=int, default=0)
    ap.add_argument("--alloc", choices=["lru", "contig"], default="contig",
                    help="スロット割当: contig=フレーム内cold連番(MD大DMA向け, 既定) / lru=旧方式")
    ap.add_argument("--output", default="")
    ap.add_argument("--audio", default="")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--compare", default="")
    ap.add_argument("--debug", action=argparse.BooleanOptionalAction, default=None,
                    help="override the frozen pack.debug value")
    ap.add_argument("--fill", action=argparse.BooleanOptionalAction, default=None,
                    help="override the frozen pack.fill value")
    ap.add_argument("--startup-audio-frames", type=int, default=None,
                    help="override the frozen startup prefetch depth")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    profile = None
    if args.config:
        try:
            profile = load_profile(args.config)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"invalid encode profile: {exc}") from exc
    dec_log = Path(args.dec_log) if args.dec_log else (
        profile.decision_log if profile else sim_work_dir() / "decisions.pkl")
    log = load_log(dec_log)
    if profile is not None:
        recorded = ((log.get("config") or {}).get("profile") or {})
        if not recorded:
            raise SystemExit(
                f"{dec_log}: decision log predates TOML profile authentication; re-run sim")
        if recorded.get("sha256") != profile.sha256:
            raise SystemExit(
                f"{dec_log}: profile hash mismatch; the TOML changed after sim. "
                "Re-run sim before packing.")
    configure_from_log(
        log, debug=args.debug, fill=args.fill,
        startup_audio_frames=args.startup_audio_frames)
    require_canonical_p0_debug_colours(log)
    # 単一真実源チェック: sim が焼いた tank と、この pack の RING_CAP は同じ実機リングを
    # モデルするので一致すべき。ズレていたら二重管理の兆候なので警告。
    sim_tank = log.get("tank_kb")
    sim_cold = log.get("max_cold")
    print(f"  encode params from sim: max_cold={sim_cold} tank_kb={sim_tank}  "
          f"pack RING_CAP_KB={RING_CAP_KB} (RING_SIZE {RING_SIZE_KB})  "
          f"{TCOLS*8}x{TROWS*8} {FPS:g}fps AUDIO={AUDIO} DEBUG={int(PACK_DEBUG)}")
    # A configured build is always namespaced by the TOML filename.  The old
    # pack.output value remains readable in schema v1 decision logs, but it no
    # longer controls configured output and cannot mix two profiles in one dir.
    output = args.output or str(
        profile.pack_output if profile is not None else "out/movieplay/MOVIE.DAT")
    audio_path = args.audio
    if not audio_path:
        audio_name = ((log.get("config") or {}).get("audio") or {}).get("file")
        if not audio_name:
            audio_name = "audio_13k3_u8_mono.wav" if AUDIO_KIND == "pcm13" else "audio_22k05_adpcm_mono.wav"
        candidate = dec_log.parent / str(audio_name)
        if not candidate.exists():
            raise SystemExit(
                f"decision audio is missing: {candidate}; re-run sim or pass --audio explicitly")
        audio_path = str(candidate)
    compare = args.compare or str(dec_log.parent / "preview")
    POOL = args.pool_slots or int(log["vram_tiles"])
    per, n_load, n_upd, pal_w, Plist, tearing = resolve(log, POOL, mode=args.alloc)
    print(f"resolve[{args.alloc}]: tearing={tearing} M(payload)={len(Plist)} frames={len(per)}")
    # 不変条件(単一真実源 av_config): 実配信(pack)の1コマ cold が drop-safe 上限を超えたら失敗。
    # sim のモデル cap が pack の連続スロット割当に対して高すぎる兆候(=解析は合うが実機で滑る)。
    # frame0(完全ロードのヘッダ)は除外。
    # realized == cap(共有 TileAllocator で構成上保証)。上限=cap を自動取得(手動env廃止)。
    stream_mode = str(
        (((log.get("config") or {}).get("video") or {}).get("mode"))
        or log.get("mode") or "H32")
    cold_ceiling = av_config.cold_realized_ceiling_for_fps(FPS, stream_mode)
    realized_max = max([int(x) for x in n_load[1:]], default=0)
    if realized_max > cold_ceiling:
        raise SystemExit(
            f"pack: realized per-frame cold max={realized_max} > cap={cold_ceiling}. "
            f"共有 TileAllocator では realized=cap のはず=想定外。sim/pack の割り当て食い違いを疑う。")
    print(f"  realized cold: max={realized_max} <= {stream_mode} cap {cold_ceiling} (共有割り当て)")
    packed_tiles, packed_runs = run_stats(per)
    if not np.array_equal(packed_tiles, n_load):
        frame = int(np.flatnonzero(packed_tiles != n_load)[0])
        raise SystemExit(
            f"pack: internal cold tile mismatch at frame {frame}: "
            f"runs={int(packed_tiles[frame])} resolve={int(n_load[frame])}")
    verify_sim_pattern_transfers(log, packed_tiles, packed_runs)
    blocks = build_control(log, per, n_upd, pal_w, audio_path)
    sc = schedule(per, n_load, blocks)
    st = ("OK" if sc["feasible"] else
          f"INFEASIBLE(over {sc['over']} under {sc.get('under',0)} "
          f"rate_lead_end {sc.get('rate_lead_end', 0)})")
    Pb = sum(len(b) for b in blocks)
    under = sc.get("under", 0)
    print(f"schedule[{st}] prebuf {sc['prebuf_pat']*PAT/1024:.0f}KB ring_peak {sc['ring_peak']*PAT/1024:.0f}KB "
          f"ring_min {sc.get('ring_min',0)*PAT/1024:.0f}KB (cap {RING_CAP_KB}KB)  under(枯渇) {under} "
          f"({100.0*under/max(1,len(per)):.1f}%)  n_pay_sec avg {sc['n_pay_sec'].mean():.2f}  "
          f"control-first ready_min {sc['ready_min']}pat ctrl_min {sc['ctrl_min']}B  "
          f"rate_lead peak/end {sc['rate_lead_peak']}/{sc['rate_lead_end']}sec")
    startup_end = min(len(per), 31)
    if startup_end > 1:
        startup_fsec = int(sc["fsec"][1:startup_end].sum())
        startup_rate = int(sc["ratedelta"][1:startup_end].sum())
        print(f"  startup BODY frames 1..{startup_end - 1}: {startup_fsec} sectors "
              f"(CD-1x allowance {startup_rate}, avoidable excess {startup_fsec - startup_rate})")
    if sc["prebuf_pat"] > RING_CAP_PAT or sc["ring_peak"] > RING_CAP_PAT:
        raise SystemExit(
            f"pack: payload ring exceeds cap {RING_CAP_KB}KB "
            f"(prebuf={sc['prebuf_pat']*PAT/1024:.0f}KB, "
            f"peak={sc['ring_peak']*PAT/1024:.0f}KB)")
    if not sc["feasible"]:
        raise SystemExit(
            "pack: refusing to write an infeasible BODY schedule "
            f"(over={sc['over']} under={sc.get('under', 0)} "
            f"ready_min={sc['ready_min']} ctrl_min={sc['ctrl_min']} "
            f"rate_lead_end={sc.get('rate_lead_end', 0)})")
    if args.verify:
        decode_verify(log, per, blocks, Plist, sc, compare_dir=compare or None,
                      sample_dir=Path(output).parent / "decoded")
    if not args.no_write:
        write_stream(output, log, per, blocks, Plist, sc, POOL)


if __name__ == "__main__":
    main()
