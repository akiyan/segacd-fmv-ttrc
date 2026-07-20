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

TTRCレイアウト(v10): HEADER.DAT = Header(1sec) + PALTAB(全区間パレット
              n_seg×128B, boot時Main-RAM表へ) + [WR0/WR1/Main pattern preloads]
              + startup audio prefetch(1 sector/frame)
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
import ima_adpcm
import player_constants
import pattern_supply
import stream_schedule
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
AUDIO_PCM = 0
AUDIO_CONTROL = 0
STARTUP_AUDIO_FRAMES = 30
PACK_DEBUG = False
PACK_FILL = True
PCM_SYNC_LEAD = 0x3000
PCM_SYNC_MAX = 0x6800
PCM_WAVE_RING_END = 0x8000
PCM_STARTUP_MARGIN = 0x0200
# リング諸元は tools/av_config.py の単一真実源から取る(sim/pack/playerで二重管理しない)。
# RING_SIZE はプレイヤの実 .equ RING_SIZE と一致(ビルド時 check_player_ring.py が検証)。
# PrgBuf のスケジュール上限と sim の画質予算上限は RING_SIZE から導出する。
RING_SIZE_KB = av_config.RING_SIZE_KB
RING_CAP_KB = av_config.RING_CAP_KB
RING_CAP_PAT = RING_CAP_KB * 1024 // PAT

# --- デバッグブロック(control先頭ヘッダ直後・固定長) ---
DBG_NCAT = 7                 # カテゴリ数 [raw,same,near,coa,flbk,buf,miss]
DBG_RESERVED = 4             # 予約u16スロット(将来の16bitデバッグ値用)
DBG_LEN = (DBG_NCAT + DBG_RESERVED) * 2   # = 22B(偶数)
assert DBG_LEN == stream_schedule.DEBUG_BLOCK_BYTES
FEATURE_COLD_RUNS = ttrc_routing.FEATURE_COLD_RUNS
FEATURE_FIXED_N2 = ttrc_routing.FEATURE_FIXED_N2
FEATURE_ADPCM22 = ttrc_routing.FEATURE_ADPCM22
FEATURE_PATTERN_SUPPLY = ttrc_routing.FEATURE_PATTERN_SUPPLY
ADPCM_TABLE_SECTORS = math.ceil(ima_adpcm.FULL_TABLE_BYTES / SECTOR)
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
    global FPS, VSYNC_N, PLAYBACK_FPS, AUDIO_KIND, AUDIO_RATE
    global AUDIO_PCM, AUDIO_CONTROL
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
    AUDIO_CONTROL = int(audio.get(
        "control_bytes", audio.get("frame_bytes", log.get("audio_frame_bytes", 0))))
    AUDIO_PCM = int(audio.get(
        "pcm_bytes", log.get("audio_pcm_bytes", AUDIO_CONTROL)))
    if AUDIO_RATE <= 0 or AUDIO_CONTROL <= 0 or AUDIO_PCM <= 0:
        raise SystemExit("decision log has no valid audio rate/frame size")
    try:
        expected_rate, expected_pcm, expected_control = av_config.audio_frame_layout(
            AUDIO_KIND, FPS)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if (AUDIO_RATE, AUDIO_PCM, AUDIO_CONTROL) != (
            expected_rate, expected_pcm, expected_control):
        raise SystemExit(
            "decision log audio layout "
            f"rate={AUDIO_RATE} pcm={AUDIO_PCM} control={AUDIO_CONTROL} "
            f"disagrees with {AUDIO_KIND}/{FPS:g}fps "
            f"({expected_rate}, {expected_pcm}, {expected_control})")
    checkpoint_bytes = int(audio.get("checkpoint_bytes", 0))
    expected_checkpoint = (
        av_config.IMA_CHECKPOINT_BYTES if AUDIO_KIND == "adpcm22" else 0)
    if checkpoint_bytes != expected_checkpoint:
        raise SystemExit(
            f"decision log checkpoint_bytes={checkpoint_bytes} != "
            f"{expected_checkpoint} for {AUDIO_KIND}")

    sim_prg_buf = int(hardware.get(
        "prg_buf_kb",
        log.get("prg_buf_kb", log.get("tank_kb", RING_CAP_KB))))
    if sim_prg_buf != RING_CAP_KB:
        raise SystemExit(
            f"decision log prg_buf_kb={sim_prg_buf} != "
            f"hardware PrgBuf cap={RING_CAP_KB}; "
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
        raise SystemExit("pack v10: decision log has no segment palettes; re-run sim")
    for seg, pals in enumerate(seg_pals):
        a = np.asarray(pals, np.uint8)
        if a.shape != (4, 15, 3):
            raise SystemExit(
                f"pack v10: segment {seg} palette shape is {a.shape}, expected (4, 15, 3); "
                "re-run sim")
        brightness = a.astype(np.int16).sum(axis=2)
        if int(brightness[0, 0]) != int(brightness.min()):
            raise SystemExit(
                f"pack v10: decision log segment {seg} P0 index1 is not tied for globally "
                "darkest usable CRAM colour (RGB sum); re-run sim with the current encoder")
        if int(brightness[0, 14]) != int(brightness.max()):
            raise SystemExit(
                f"pack v10: decision log segment {seg} P0 index15 is not tied for globally "
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


def sourced_cold_runs(entries, colds, sources):
    """Return ``(slot, count, source)`` runs split on slot or source changes."""
    runs = []
    start = previous = source = None
    count = 0
    for entry, cold, item_source in zip(entries, colds, sources):
        if not cold:
            continue
        slot = (int(entry) & 0x07FF) - BASE
        item_source = int(item_source)
        if count and (slot != previous + 1 or item_source != source):
            runs.append((start, count, source))
            count = 0
        if not count:
            start = slot
            source = item_source
        previous = slot
        count += 1
    if count:
        runs.append((start, count, source))
    return runs


def run_stats(per, sources=None):
    """フレーム内cold tile数とplayer cold-run record数を返して表示する。"""
    runs_per_frame = np.zeros(len(per), np.int64)
    colds_per_frame = np.zeros(len(per), np.int64)
    if sources is None:
        sources = tuple(tuple(pattern_supply.SOURCE_PRG for _ in entries)
                        for _cells, entries, _colds in per)
    prg_per_frame = np.zeros(len(per), np.int64)
    wr_per_frame = np.zeros(len(per), np.int64)
    main_per_frame = np.zeros(len(per), np.int64)
    for i, ((cells, entries, colds), frame_sources) in enumerate(zip(per, sources)):
        runs = sourced_cold_runs(entries, colds, frame_sources)
        runs_per_frame[i] = len(runs)
        colds_per_frame[i] = sum(count for _slot, count, _source in runs)
        for _slot, count, source in runs:
            if source == pattern_supply.SOURCE_PRG:
                prg_per_frame[i] += count
            elif source == pattern_supply.SOURCE_WR:
                wr_per_frame[i] += count
            elif source == pattern_supply.SOURCE_MAIN:
                main_per_frame[i] += count
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
    print(f"  source patterns: Prg={int(prg_per_frame.sum())} "
          f"Wr0={int(wr_per_frame[::2].sum())} Wr1={int(wr_per_frame[1::2].sum())} "
          f"Main={int(main_per_frame.sum())}")
    # O_LOADS stores four bytes per run and only Prg patterns inline.  Wr/Main
    # runs point at their persistent preload instead of copying pattern bytes.
    loads_bytes = prg_per_frame * PAT + runs_per_frame * 4
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


def verify_sim_pattern_transfers(
        log, packed_tiles, packed_runs, supply_plan=None):
    """Require frozen sim transfer counts to match pack/player counts exactly.

    Old decision logs predate these fields and remain packable.  Every newly
    generated log carries them, turning a future run-grouping change into a
    pack-time failure instead of a misleading analysis meter.
    """
    frozen = log.get("pattern_transfers")
    if frozen is None:
        print("  pattern transfer照合: 旧decision logのため省略 (再simで有効化)")
        return False
    schema = int(frozen.get("schema_version", 0))
    if schema not in (1, 2):
        raise SystemExit(
            "pack: unsupported pattern_transfers schema "
            f"{frozen.get('schema_version')!r}")

    expected = {
        "tiles": np.asarray(packed_tiles, np.int64),
        "runs": np.asarray(packed_runs, np.int64),
    }
    if schema >= 2:
        if supply_plan is None:
            raise SystemExit(
                "pack: schema-2 pattern transfer verification requires "
                "the materialized supply plan")
        expected.update({
            "prg": np.asarray(supply_plan.prg_loads, np.int64),
            "wr0": np.asarray(supply_plan.wr0_loads, np.int64),
            "wr1": np.asarray(supply_plan.wr1_loads, np.int64),
            "main": np.asarray(supply_plan.main_loads, np.int64),
        })
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
    detail = "tiles/runs/sources" if schema >= 2 else "tiles/runs"
    print(f"  pattern transfer照合: {len(packed_runs)} frames {detail} exact")
    return True


def verify_sim_stream_schedule(log, packed_schedule):
    """Require the analysis BODY/RING trace to match the packed schedule."""
    frozen = log.get("stream_schedule")
    if frozen is None:
        raise SystemExit(
            "pack: decision log has no BODY delivery trace; re-run sim")
    if int(frozen.get("schema_version", 0)) != stream_schedule.STREAM_SCHEDULE_SCHEMA_VERSION:
        raise SystemExit(
            "pack: unsupported stream_schedule schema "
            f"{frozen.get('schema_version')!r}; re-run sim")

    expected = {
        "block_lengths": np.asarray(packed_schedule["blk_len"], np.int64),
        "ring_occupancy": np.asarray(
            packed_schedule["ring_occupancy"], np.int64),
        "payload_sectors": np.asarray(
            packed_schedule["n_pay_sec"], np.int64),
        "control_sectors": np.asarray(
            packed_schedule["n_ctrl_sec"], np.int64),
        "body_useful_payload_bytes": np.asarray(
            packed_schedule["body_useful_payload_bytes"], np.int64),
        "body_useful_control_bytes": np.asarray(
            packed_schedule["body_useful_control_bytes"], np.int64),
        "body_pad_bytes": np.asarray(
            packed_schedule["body_pad_bytes"], np.int64),
        "body_physical_bytes": np.asarray(
            packed_schedule["body_physical_bytes"], np.int64),
    }
    for name, actual in expected.items():
        simulated = np.asarray(frozen.get(name, ()), np.int64)
        if simulated.shape != actual.shape:
            raise SystemExit(
                f"pack: sim/pack {name} length mismatch: "
                f"sim={simulated.shape} pack={actual.shape}")
        mismatch = np.flatnonzero(simulated != actual)
        if mismatch.size:
            frame = int(mismatch[0])
            raise SystemExit(
                f"pack: sim/pack {name} mismatch at frame {frame}: "
                f"sim={int(simulated[frame])} pack={int(actual[frame])}. "
                "Control layout or delivery scheduling changed after simulation; "
                "re-run sim.")
    print(f"  BODY配送/RING照合: {len(expected['ring_occupancy'])} slots exact")
    return True


def verify_body_delivery_file(
        body_path, stream_ctrl, stream_pay, schedule, *, prebuf_patterns):
    """Check every written BODY slot against useful-byte and pad traces."""
    n_pay = np.asarray(schedule["n_pay_sec"], np.int64)
    n_ctrl = np.asarray(schedule["n_ctrl_sec"], np.int64)
    fsec = np.asarray(schedule["fsec"], np.int64)
    useful_pay = np.asarray(schedule["body_useful_payload_bytes"], np.int64)
    useful_ctrl = np.asarray(schedule["body_useful_control_bytes"], np.int64)
    pad = np.asarray(schedule["body_pad_bytes"], np.int64)
    cc = 0
    pc = int(prebuf_patterns) * PAT
    seen_pay = np.zeros(len(fsec), np.int64)
    seen_ctrl = np.zeros(len(fsec), np.int64)
    seen_pad = np.zeros(len(fsec), np.int64)
    with Path(body_path).open("rb") as body:
        for i in range(1, len(fsec)):
            ncb = int(n_ctrl[i]) * SECTOR
            npb = int(n_pay[i]) * SECTOR
            slot_size = int(fsec[i]) * SECTOR
            slot = body.read(slot_size)
            if len(slot) != slot_size:
                raise AssertionError(f"BODY.DAT slot {i} is truncated")

            ctrl_src = stream_ctrl[cc:cc + ncb]
            pay_src = stream_pay[pc:pc + npb]
            ctrl_area = slot[:ncb]
            pay_area = slot[ncb:ncb + npb]
            rate_area = slot[ncb + npb:]
            if ctrl_area[:len(ctrl_src)] != ctrl_src or any(ctrl_area[len(ctrl_src):]):
                raise AssertionError(f"BODY.DAT control bytes/pad mismatch at slot {i}")
            if pay_area[:len(pay_src)] != pay_src or any(pay_area[len(pay_src):]):
                raise AssertionError(f"BODY.DAT payload bytes/pad mismatch at slot {i}")
            if any(rate_area):
                raise AssertionError(f"BODY.DAT rate-match pad is nonzero at slot {i}")

            seen_ctrl[i] = len(ctrl_src)
            seen_pay[i] = len(pay_src)
            seen_pad[i] = slot_size - len(ctrl_src) - len(pay_src)
            cc += ncb
            pc += npb
        if body.read(1):
            raise AssertionError("BODY.DAT has bytes beyond the slot schedule")
    for name, actual, traced in (
            ("useful control", seen_ctrl, useful_ctrl),
            ("useful payload", seen_pay, useful_pay),
            ("pad", seen_pad, pad)):
        mismatch = np.flatnonzero(actual != traced)
        if mismatch.size:
            i = int(mismatch[0])
            raise AssertionError(
                f"BODY.DAT {name} trace mismatch at slot {i}: "
                f"file={int(actual[i])} trace={int(traced[i])}")
    print(
        f"  BODY.DAT slot照合: {len(fsec) - 1} slots exact; useful "
        f"control={int(seen_ctrl.sum())}B payload={int(seen_pay.sum())}B "
        f"pad={int(seen_pad.sum())}B")


def _read_audio_samples(audio_path):
    """Read the configured mono WAV/raw source as u8 bytes or s16 samples."""
    try:
        import wave as _wave
        with _wave.open(str(audio_path), "rb") as wav:
            if wav.getnchannels() != 1:
                raise ValueError(f"audio must be mono, got {wav.getnchannels()} channels")
            width = wav.getsampwidth()
            rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())
        if rate != AUDIO_RATE:
            raise ValueError(f"audio rate is {rate}, expected {AUDIO_RATE}")
    except (OSError, EOFError):
        raw = Path(audio_path).read_bytes()
        width = 1 if AUDIO_KIND == "pcm13" else 2
    expected_width = 1 if AUDIO_KIND == "pcm13" else 2
    if width != expected_width:
        raise ValueError(
            f"{AUDIO_KIND} source sample width is {width}, expected {expected_width}")
    if AUDIO_KIND == "pcm13":
        return bytes(raw)
    if len(raw) & 1:
        raise ValueError("s16 ADPCM source has an odd byte count")
    return np.frombuffer(raw, "<i2").copy()


def build_audio_chunks(audio_path, frame_count):
    """Return fixed on-disc chunks and their reconstructed RF5C164 PCM."""
    raw = _read_audio_samples(audio_path)
    target_samples = int(frame_count) * AUDIO_PCM
    if AUDIO_KIND == "pcm13":
        source_len = len(raw)
        raw = retime_pcm_u8(raw, target_samples)
        if len(raw) != source_len:
            print(f"  PCM retime: {source_len} -> {len(raw)} samples "
                  f"({AUDIO_PCM} B/frame x {frame_count} frames)")
        signmag = bytearray(len(raw))
        for index, value in enumerate(raw):
            sample = value - 128
            signmag[index] = (
                min(sample, 0x7F) if sample >= 0
                else (0x80 | min(-sample, 0x7E)))
        pcm_chunks = [
            bytes(signmag[i * AUDIO_PCM:(i + 1) * AUDIO_PCM])
            for i in range(frame_count)
        ]
        return list(pcm_chunks), pcm_chunks

    source_len = len(raw)
    pcm16 = ima_adpcm.retime_pcm_s16(raw, target_samples)
    if len(pcm16) != source_len:
        print(f"  PCM retime: {source_len} -> {len(pcm16)} samples "
              f"({AUDIO_PCM} samples/frame x {frame_count} frames)")
    control_chunks, pcm_chunks = ima_adpcm.encode_decode_chunks(
        pcm16, AUDIO_PCM)
    if any(len(chunk) != AUDIO_CONTROL for chunk in control_chunks):
        raise AssertionError("IMA control chunk size drift")
    if any(len(chunk) != AUDIO_PCM for chunk in pcm_chunks):
        raise AssertionError("IMA decoded PCM chunk size drift")
    return control_chunks, pcm_chunks


def build_control(log, per, n_upd, pal_w, audio_path, sources=None):
    """Build control blocks and return their reconstructed source PCM chunks."""
    seg_cram = [pals_to_bytes_128(p) for p in log["seg_pals"]]
    frame_seg = np.asarray(log["frame_seg"], np.int64)
    cats_list = log.get("cats")                     # per-frame [raw,same,near,coa,flbk,buf,miss]
    # デバッグ欄: decision log に固定された pack.debug を使う。
    dbg_on = bool(cats_list) and PACK_DEBUG
    audio_chunks, pcm_chunks = build_audio_chunks(audio_path, len(per))
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
    if sources is None:
        sources = tuple(tuple(pattern_supply.SOURCE_PRG for _ in entries)
                        for _cells, entries, _colds in per)
    blocks = []
    for i in range(len(per)):
        cells, entries, colds = per[i]
        frame_sources = sources[i]
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
        for e, cold, source in zip(entries, colds, frame_sources):
            sourced_entry = pattern_supply.encode_entry_source(
                e, source if cold else pattern_supply.SOURCE_PRG)
            body += struct.pack(">H", (0x8000 if cold else 0) | sourced_entry)
        body += audio_chunks[i]
        # Keep the legacy audio offset unchanged.  The suffix is aligned so the
        # 68000 can read its words directly; old players simply ignore it.
        if len(body) & 1:
            body += b"\0"
        runs = sourced_cold_runs(entries, colds, frame_sources)
        body += struct.pack(">H", len(runs))
        for slot, count, source in runs:
            body += struct.pack(">HH", slot, pattern_supply.encode_run_count(count, source))
        # total_len は「先頭2Bを含むブロック全長」。実機は apply_cur を total_len で進めるので
        # パディング込みの偶数にする(奇数だと1B/フレームずつ desync する)。
        total = len(body) + 2
        if total & 1:
            body += b"\0"
            total += 1
        blocks.append(struct.pack(">H", total) + bytes(body))
    return blocks, pcm_chunks


def control_audio_bounds(block):
    """Return the fixed-size on-disc audio slice in one control block."""
    n_upd = struct.unpack_from(">H", block, 4)[0]
    dbg = block[7]
    pos = 8 + (DBG_LEN if dbg else 0) + ((C_CELLS + 7) // 8) + n_upd * 2
    return pos, pos + AUDIO_CONTROL


def control_audio(block):
    """Return the fixed-size encoded/PCM chunk embedded in one control block."""
    start, end = control_audio_bounds(block)
    chunk = block[start:end]
    if len(chunk) != AUDIO_CONTROL:
        raise ValueError(
            f"control audio truncated: got {len(chunk)}, expected {AUDIO_CONTROL}")
    return chunk


def replace_control_audio(block, chunk):
    """Replace one on-disc audio chunk without changing the block length."""
    if len(chunk) != AUDIO_CONTROL:
        raise ValueError(
            f"replacement audio is {len(chunk)} bytes, expected {AUDIO_CONTROL}")
    start, end = control_audio_bounds(block)
    out = bytearray(block)
    if len(out[start:end]) != AUDIO_CONTROL:
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
        return stream_schedule.rate_deltas(nfr, FPS)
    except stream_schedule.ScheduleError as exc:
        raise SystemExit(f"pack: {exc}") from exc


def rate_match_fsec(n_pay_sec, n_ctrl_sec):
    """Apply the player's bounded CD-rate accumulator to a routing table."""
    try:
        return stream_schedule.rate_match_sectors(
            n_pay_sec, n_ctrl_sec, fps=FPS)
    except stream_schedule.ScheduleError as exc:
        raise SystemExit(f"pack: {exc}") from exc


def schedule(per, n_load, blocks):
    """Schedule control JIT and rate-shaped payload prefetch sectors."""
    blk_len = np.array([len(b) for b in blocks], np.int64)
    if len(per) != len(n_load) or len(per) != len(blocks):
        raise SystemExit("pack: schedule inputs have different frame counts")
    try:
        return stream_schedule.schedule_payload_ring(
            n_load,
            blk_len,
            fps=FPS,
            ring_capacity_patterns=RING_CAP_PAT,
            frame_sectors=FRAME_SECTORS,
            fill=PACK_FILL,
        )
    except (ValueError, stream_schedule.ScheduleError) as exc:
        raise SystemExit(f"pack: {exc}") from exc


def decode_verify(log, per, blocks, supply_plan, sc, compare_dir=None, sample_dir=None):
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
    prg_patterns = supply_plan.prg_patterns
    f0_ring = deque(prg_patterns[:nl0])
    ring = deque(prg_patterns[nl0:nl0 + B]); pc = nl0 + B; cc = 0
    word = [deque(supply_plan.wr0_patterns), deque(supply_plan.wr1_patterns)]
    main = deque(supply_plan.main_patterns)
    tile = [None] * (POOL + BASE + 2)
    nt_slot = np.zeros(C_CELLS, np.int64); nt_pal = np.zeros(C_CELLS, np.int64)
    diffs = []; ring_peak = len(ring); bad = 0
    for i in range(len(per)):
        add = int(n_pay_sec[i]) * PAT_PER_SEC
        prg_src = f0_ring if (f0h and i == 0) else ring
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
            cold = e >> 15
            source = pattern_supply.decode_entry_source(e)
            ent = e & pattern_supply.NAME_ENTRY_MASK
            nt_pal[c] = (ent >> 13) & 3
            nt_slot[c] = (ent & 0x07FF) - BASE
            if cold:
                if source == pattern_supply.SOURCE_PRG:
                    src = prg_src
                elif source == pattern_supply.SOURCE_WR:
                    src = word[i & 1]
                elif source == pattern_supply.SOURCE_MAIN:
                    src = main
                else:
                    src = ()
                if not src:
                    bad += 1
                else:
                    tile[int(nt_slot[c]) + BASE] = src.popleft()
        # BODY payload follows control and arms later frames.  Append it only
        # after the current block has consumed every cold entry.
        for k in range(pc, min(pc + add, len(prg_patterns))):
            ring.append(prg_patterns[k])
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
    cache_left = len(word[0]) + len(word[1]) + len(main)
    if cache_left:
        bad += cache_left
    print(f"decode: ring_peak {ring_peak*PAT/1024:.0f}KB "
          f"preload_left {cache_left} 未配信pop(表示破壊) {bad}")
    if diffs:
        da = np.array([x[1] for x in diffs])
        nd = int((da > 0).sum())
        print(f"sim preview一致: 比較{len(da)}枚 差分ありフレーム={nd} 画素最大差={int(da.max())}")
        if nd:
            print("  差分フレーム(先頭10):", [x[0] for x in diffs if x[1] > 0][:10])


def _decode_control_chunk(chunk):
    if AUDIO_KIND == "pcm13":
        return bytes(chunk)
    decoded, _state = ima_adpcm.decode_chunk(chunk, AUDIO_PCM)
    return ima_adpcm.pcm16_to_sign_magnitude(decoded)


def write_stream(path, log, per, blocks, source_pcm_chunks, supply_plan, sc, POOL):
    """Write the v10 split stream and a combined tooling container.

    HEADER.DAT:
      Header(1sec) | PALTAB | [ADPCM_TABLE] | [WR0] | [WR1] | [MAIN]
                   | STARTUP_AUDIO
                   | FRAME0(control+patterns)
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
    payload = b"".join(supply_plan.prg_patterns)
    wr0_blob = b"".join(supply_plan.wr0_patterns)
    wr1_blob = b"".join(supply_plan.wr1_patterns)
    main_blob = b"".join(supply_plan.main_patterns)
    wr0_sec = -(-len(wr0_blob) // SECTOR)
    wr1_sec = -(-len(wr1_blob) // SECTOR)
    main_sec = -(-len(main_blob) // SECTOR)

    # Queue the first N reconstructed PCM chunks from HEADER, then make each
    # live control carry the next future PCM or checkpointed ADPCM chunk.
    # The old duplicate-and-skip layout consumed the
    # entire startup reserve by frame N and left the writer next to the play
    # head. Shifting fixed-size chunks keeps block lengths and sector scheduling
    # unchanged while preserving the exact source sample order.
    safe_audio_prefetch = max(0, min(
        (PCM_SYNC_MAX - PCM_SYNC_LEAD) // max(1, AUDIO_PCM),
        (PCM_WAVE_RING_END - PCM_SYNC_LEAD - PCM_STARTUP_MARGIN)
        // max(1, AUDIO_PCM)))
    audio_prefetch_frames = (
        min(nfr, STARTUP_AUDIO_FRAMES, safe_audio_prefetch) if f0_header else 0)
    source_audio_chunks = [control_audio(block) for block in blocks]
    if AUDIO_KIND == "pcm13":
        silence_chunk = b"\0" * AUDIO_CONTROL
    else:
        silence_chunk, _state = ima_adpcm.encode_chunk(
            np.zeros(AUDIO_PCM, dtype=np.int16), ima_adpcm.State())
    disc_blocks = [
        replace_control_audio(
            block,
            source_audio_chunks[i + audio_prefetch_frames]
            if i + audio_prefetch_frames < nfr else silence_chunk)
        for i, block in enumerate(blocks)
    ]
    queued_pcm = (
        list(source_pcm_chunks[:audio_prefetch_frames])
        + [_decode_control_chunk(control_audio(block)) for block in disc_blocks]
    )
    if queued_pcm[:nfr] != list(source_pcm_chunks):
        raise AssertionError("startup audio prefetch changed reconstructed sample order")
    silence_pcm = b"\0" * AUDIO_PCM
    if any(chunk != silence_pcm for chunk in queued_pcm[nfr:]):
        raise AssertionError("startup audio prefetch tail is not silent")
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
    # One reconstructed PCM chunk per sector lets the Sub write each chunk without
    # cross-sector staging. Offset 58 now carries the RF5C164 frequency delta;
    # offset 60 tells the player how many HEADER sectors to queue before PCM starts.
    audio_preload = b"".join(
        source_pcm_chunks[i].ljust(SECTOR, b"\0")
        for i in range(audio_prefetch_frames)
    )
    audio_preload_sec = audio_prefetch_frames
    # v4: 可変フレーム(5セクタ固定paddingを廃止=各frameは n_pay+n_ctrl セクタ)＋ vsync/コマ N。
    # CDレート累積器が実際のfpsを決める。Nは整数VBlank cadenceのヒントで、24fpsのN2を
    # 29.97fpsへ丸める指定ではない。AUDIOも実効fps由来。FRAME_SECTORS(=5)は最大スロット。
    vsync_n = VSYNC_N                                  # N: 近似VBLANK間隔(30/24→2, 15→4)
    fps_int = int(round(FPS))                         # 名目fps。FEATURE_FIXED_N2時はplayerが1001/400を選ぶ
    audio_fd = av_config.rf5c164_fd(AUDIO_PCM, PLAYBACK_FPS)
    if not f0_header:
        raise SystemExit("pack v10 requires frame0 in HEADER.DAT")
    features = FEATURE_COLD_RUNS
    if av_config.uses_fixed_n2_cadence(FPS):
        features |= FEATURE_FIXED_N2
    if AUDIO_KIND == "adpcm22":
        features |= FEATURE_ADPCM22
    if supply_plan.enabled:
        features |= FEATURE_PATTERN_SUPPLY
    header = struct.pack(">4sHHHHHHHHH", MAGIC, VERSION, nfr, TCOLS, TROWS, C_CELLS,
                         POOL, BASE, FRAME_SECTORS, len(log["seg_pals"]))
    header += struct.pack(">LLLL", Bpat, routing_sec, prebuf_sec, ring_peak)
    header += bytes([_mode])                          # offset 38: display mode
    header += b"\0"                                   # offset 39: pad
    header += struct.pack(">LL", f0_ctrl_sec, f0_pat_sec)  # offset 40,44: frame0ブロック
    header += struct.pack(">L", paltab_sec)          # offset 48: PALTABセクタ数(v3)
    # offset 54 is always the decoded RF5C164 byte/sample count.  PCM stores
    # the same number in controls; FEATURE_ADPCM22 derives control bytes as
    # checkpoint(4) + AUDIO_PCM/2 without expanding the fixed 64-byte header.
    header += struct.pack(">HH", vsync_n, AUDIO_PCM)
    header += struct.pack(">H", fps_int)             # offset 56: 名目fps(レートマッチpadding用) (v4)
    header += struct.pack(">HH", audio_fd, audio_preload_sec)  # offset 58: RF5C164 FD; 60: prefetch sectors
    header += struct.pack(">H", features)          # offset 62: optional stream features
    header += b"\0" * (64 - len(header)) + seg0
    header += b"\0" * (SECTOR - len(header))
    header = bytearray(header)
    if supply_plan.enabled:
        player_constants.PATTERN_SUPPLY_STRUCT.pack_into(
            header, player_constants.PATTERN_SUPPLY_OFFSET,
            player_constants.PATTERN_SUPPLY_MAGIC,
            player_constants.PATTERN_SUPPLY_VERSION, 0,
            len(supply_plan.wr0_patterns),
            len(supply_plan.wr1_patterns),
            len(supply_plan.main_patterns),
            wr0_sec, wr1_sec, main_sec,
        )
    header = player_constants.stamp_header_sector(header)
    frame0_blk = (f0_ctrl.ljust(f0_ctrl_sec * SECTOR, b"\0")
                  + f0_pat.ljust(f0_pat_sec * SECTOR, b"\0"))
    adpcm_table_blob = (
        ima_adpcm.full_tables().ljust(ADPCM_TABLE_SECTORS * SECTOR, b"\0")
        if AUDIO_KIND == "adpcm22" else b"")
    header_blob = (header
                   + paltab.ljust(paltab_sec * SECTOR, b"\0")
                   + adpcm_table_blob
                   + wr0_blob.ljust(wr0_sec * SECTOR, b"\0")
                   + wr1_blob.ljust(wr1_sec * SECTOR, b"\0")
                   + main_blob.ljust(main_sec * SECTOR, b"\0")
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
    verify_body_delivery_file(
        body_path,
        stream_ctrl,
        stream_pay,
        sc,
        prebuf_patterns=Bpat,
    )

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
          f"startup_audio prefetch {audio_prefetch_frames}f "
          f"preload Wr0/Wr1/Main={len(supply_plan.wr0_patterns)}/"
          f"{len(supply_plan.wr1_patterns)}/{len(supply_plan.main_patterns)} "
          f"frame0 {f0_ctrl_sec}+{f0_pat_sec} "
          f"routing {routing_sec} prebuf {prebuf_sec} frames {frames_stream_sec}) "
          f"ring_peak {ring_peak*PAT/1024:.0f}KB  v10 N={vsync_n}"
          f"(={PLAYBACK_FPS:.3f}fps) AUDIO={AUDIO_KIND} "
          f"control={AUDIO_CONTROL}B pcm={AUDIO_PCM}B FD=0x{audio_fd:04X}")
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
    ap.add_argument("--pattern-supply", action=argparse.BooleanOptionalAction, default=True,
                    help="move selected existing cold runs to Wr0/Wr1/Main boot preloads")
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
    # The frozen PrgBuf capacity and the packer's physical schedule cap must be
    # identical. A mismatch means the stream was simulated against another
    # memory map.
    sim_prg_buf = log.get("prg_buf_kb", log.get("tank_kb"))
    sim_cold = log.get("max_cold")
    print(f"  encode params from sim: max_cold={sim_cold} "
          f"PrgBuf={sim_prg_buf}KB  "
          f"pack cap={RING_CAP_KB}KB (physical ring {RING_SIZE_KB}KB)  "
          f"{TCOLS*8}x{TROWS*8} {FPS:g}fps AUDIO={AUDIO_KIND} "
          f"control={AUDIO_CONTROL}B pcm={AUDIO_PCM}B DEBUG={int(PACK_DEBUG)}")
    # A configured build is always namespaced by the TOML filename.  The old
    # pack.output value remains readable in schema v1 decision logs, but it no
    # longer controls configured output and cannot mix two profiles in one dir.
    output = args.output or str(
        profile.pack_output if profile is not None else "out/movieplay/MOVIE.DAT")
    audio_path = args.audio
    if not audio_path:
        audio_name = ((log.get("config") or {}).get("audio") or {}).get("file")
        if not audio_name:
            audio_name = (
                "audio_13k3_u8_mono.wav" if AUDIO_KIND == "pcm13"
                else "audio_22k05_s16_mono.wav")
        candidate = dec_log.parent / str(audio_name)
        if not candidate.exists():
            raise SystemExit(
                f"decision audio is missing: {candidate}; re-run sim or pass --audio explicitly")
        audio_path = str(candidate)
    compare = args.compare or str(dec_log.parent / "preview")
    POOL = args.pool_slots or int(log["vram_tiles"])
    per, n_load, n_upd, pal_w, Plist, tearing = resolve(log, POOL, mode=args.alloc)
    print(f"resolve[{args.alloc}]: tearing={tearing} M(payload)={len(Plist)} frames={len(per)}")
    supply_enabled = bool(args.pattern_supply and FPS >= 24.0)
    if args.pattern_supply and not supply_enabled:
        print("  pattern supply: disabled below 24fps until the dense-poll player path is qualified")
    supply_plan = pattern_supply.plan_supply(
        log, per, Plist, enabled=supply_enabled)
    print(f"  pattern supply: enabled={int(supply_plan.enabled)} "
          f"Prg={len(supply_plan.prg_patterns)} "
          f"Wr0={len(supply_plan.wr0_patterns)}/{pattern_supply.WORD_BUF_PATTERNS} "
          f"Wr1={len(supply_plan.wr1_patterns)}/{pattern_supply.WORD_BUF_PATTERNS} "
          f"Main={len(supply_plan.main_patterns)}/{pattern_supply.MAIN_BUF_PATTERNS}")
    # 不変条件(単一真実源 av_config): 実配信(pack)の1コマ cold が drop-safe 上限を超えたら失敗。
    # sim のモデル cap が pack の連続スロット割当に対して高すぎる兆候(=解析は合うが実機で滑る)。
    # frame0(完全ロードのヘッダ)は除外。
    # realized == cap(共有 TileAllocator で構成上保証)。上限=cap を自動取得(手動env廃止)。
    stream_mode = str(
        (((log.get("config") or {}).get("video") or {}).get("mode"))
        or log.get("mode") or "H32")
    stream_active_tiles = int(
        (((log.get("config") or {}).get("video") or {}).get("active_tiles"))
        or C_CELLS)
    cold_qualification = av_config.cold_cap_qualification(
        FPS, stream_mode, stream_active_tiles)
    cold_ceiling = cold_qualification.cap
    realized_max = max([int(x) for x in n_load[1:]], default=0)
    if realized_max > cold_ceiling:
        raise SystemExit(
            f"pack: realized per-frame cold max={realized_max} > cap={cold_ceiling}. "
            f"共有 TileAllocator では realized=cap のはず=想定外。sim/pack の割り当て食い違いを疑う。")
    print(f"  realized cold: max={realized_max} <= {stream_mode}/{stream_active_tiles} "
          f"active tiles cap {cold_ceiling} (measured at "
          f"{cold_qualification.active_tiles} tiles, 共有割り当て)")
    packed_tiles, packed_runs = run_stats(per, supply_plan.sources)
    if not np.array_equal(packed_tiles, n_load):
        frame = int(np.flatnonzero(packed_tiles != n_load)[0])
        raise SystemExit(
            f"pack: internal cold tile mismatch at frame {frame}: "
            f"runs={int(packed_tiles[frame])} resolve={int(n_load[frame])}")
    verify_sim_pattern_transfers(log, packed_tiles, packed_runs, supply_plan)
    blocks, source_pcm_chunks = build_control(
        log, per, n_upd, pal_w, audio_path, supply_plan.sources)
    sc = schedule(per, supply_plan.prg_loads, blocks)
    if supply_plan.enabled and log.get("pattern_supply") is None:
        frozen_lengths = np.asarray(
            (log.get("stream_schedule") or {}).get("block_lengths", ()), np.int64)
        actual_lengths = np.asarray(sc["blk_len"], np.int64)
        if frozen_lengths.shape != actual_lengths.shape or not np.array_equal(
                frozen_lengths, actual_lengths):
            raise SystemExit(
                "pack: pattern supply changed control block lengths; source assignment "
                "must preserve complete cold runs")
        print("  BODY配送/RING照合: preloadでPrg需要を変更したためbaseline traceとの一致対象外; "
              "control lengths exact")
    else:
        verify_sim_stream_schedule(log, sc)
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
            f"pack: PrgBuf exceeds cap {RING_CAP_KB}KB "
            f"(prebuf={sc['prebuf_pat']*PAT/1024:.0f}KB, "
            f"peak={sc['ring_peak']*PAT/1024:.0f}KB)")
    if not sc["feasible"]:
        raise SystemExit(
            "pack: refusing to write an infeasible BODY schedule "
            f"(over={sc['over']} under={sc.get('under', 0)} "
            f"ready_min={sc['ready_min']} ctrl_min={sc['ctrl_min']} "
            f"rate_lead_end={sc.get('rate_lead_end', 0)})")
    if args.verify:
        decode_verify(log, per, blocks, supply_plan, sc, compare_dir=compare or None,
                      sample_dir=Path(output).parent / "decoded")
    if not args.no_write:
        write_stream(
            output, log, per, blocks, source_pcm_chunks, supply_plan, sc, POOL)


if __name__ == "__main__":
    main()
