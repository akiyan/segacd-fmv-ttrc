#!/usr/bin/env python3
"""Extract per-frame Pass2/CD workload from a packed TTRC stream.

For every timed frame this emits the quantities that bound the fixed
two-VBlank cadence at 30 fps: cell updates, physical pattern loads by
source (Prg/Wr/Dic), cold-run descriptor structure (count, lengths,
short runs), the Main-CPU Pass2 word total, the palette-switch flag,
and the CD slot schedule (control/payload sectors, rate lead).

Supports TTRC v10 (legacy entries) and v12 (indexed Dic runs, optional
completed shadow lists).  The cold-run suffix is located from the block
tail by solving `n_runs` (the suffix is `[u16 n_runs][n_runs x 4]` at
the very end of the block) and validated against the update entries;
the low byte of `n_runs` can additionally be cross-checked against the
DEBUG HUD `N` column of a recording of the same stream.

Usage:
  tools/python.sh harness/cold_cap_model/extract_frames.py \
      out/sonic-jam-op-h40 --csv /tmp/frames_175.csv
"""

from __future__ import annotations

import argparse
import csv
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

SECTOR = 2048
ROUTING_TOTAL_MAX = 5
FEATURE_COLD_RUNS = 0x0001
FEATURE_FIXED_N2 = 0x0002
FEATURE_ADPCM22 = 0x0004
FEATURE_PATTERN_SUPPLY = 0x0008
FEATURE_SHADOW_UPDATE_LISTS = 0x0010
FEATURE_VRAM_RAW_PREFETCH = 0x0020
ADPCM_TABLE_SECTORS = 5
PATTERN_SUPPLY_OFFSET = 196
SHADOW_UPDATE_LIST_TAG = 0x8000
SHADOW_UPDATE_COUNT_MASK = 0x7FFF
WORDS_PER_PATTERN = 16
SHORT_RUN_MAX_WORDS = 32

SOURCE_NAMES = ("prg", "wr", "dic")


@dataclass
class FrameRow:
    frame: int
    n_upd: int
    use_list: bool
    pal_switch: int          # 0 = none, else segment index + 1
    cold_entries: int        # legacy entries with bit15 (0 for list frames)
    n_runs: int
    loads_total: int         # sum of run counts (= physical pattern loads)
    loads_prg: int
    loads_wr: int
    loads_dic: int
    pass2_words: int         # loads_total * 16
    short_runs: int          # runs of <= SHORT_RUN_MAX_WORDS words
    max_run_words: int
    control_bytes: int
    n_ctrl_sec: int
    n_pay_sec: int
    slot_sec: int            # actual physical sectors in this frame's slot
    rated_sec: int           # nominal CD-1x allowance this frame
    lead_sec: int            # cumulative delivery lead after this frame


def die(msg: str) -> None:
    raise AssertionError(msg)


def pattern_supply_sectors(header: bytes, version: int, features: int) -> int:
    if version < 10 or not features & FEATURE_PATTERN_SUPPLY:
        return 0
    values = struct.unpack_from(">4s8H", header, PATTERN_SUPPLY_OFFSET)
    magic, supply_version, reserved = values[:3]
    if magic != b"PSUP" or supply_version not in (1, 2) or reserved:
        die(f"invalid pattern-supply extension: {values!r}")
    return sum(values[-3:])


def decode_routes(routing: bytes, nframes: int) -> list[tuple[int, int]]:
    if not nframes or routing[0] != 0:
        die("v7+ frame 0 routing entry must be zero")
    routes = []
    for frame, packed in enumerate(routing[:nframes]):
        if packed & 0xC0:
            die(f"frame {frame}: routing reserved bits set in 0x{packed:02X}")
        n_ctrl = packed & 0x07
        total = (packed >> 3) & 0x07
        if total > ROUTING_TOTAL_MAX or n_ctrl > total:
            die(f"frame {frame}: bad routing entry 0x{packed:02X}")
        routes.append((total - n_ctrl, n_ctrl))
    return routes


def decode_run_words(raw: bytes, pos: int, k: int, pool: int,
                     seq: int) -> list[tuple[int, int, int, int]] | None:
    """Decode k descriptors at pos; None when any descriptor is invalid."""
    runs = []
    for i in range(k):
        w0, w1 = struct.unpack_from(">HH", raw, pos + i * 4)
        slot = w0 & 0x07FF
        count = w1 & 0x07FF
        source = (w1 >> 14) & 0x3
        dic_idx = ((w0 >> 11) & 0x1F) << 3 | ((w1 >> 11) & 0x7)
        if source == 3 or count == 0 or slot + count > pool:
            return None
        if source != 2 and dic_idx:
            return None
        runs.append((slot, count, source, dic_idx))
    return runs


def parse_runs_tail(raw: bytes, seq: int, pool: int, updates_end: int,
                    cold_entries: int | None,
                    check_count: bool) -> tuple[list, int]:
    """Solve the cold-run suffix from the block tail.

    Returns (runs, audio_span) where audio_span is the byte distance from
    the end of the updates region to the start of the suffix (fixed
    per-frame audio chunk plus its optional pad byte).  Used only for
    frames whose legacy entries pin the load count; positional parsing
    with a known audio_span handles the rest.
    """
    total_len = len(raw)
    candidates = []
    for k in range(0, (total_len - updates_end - 2) // 4 + 1):
        pos = total_len - 2 - 4 * k
        if pos < updates_end:
            break
        if struct.unpack_from(">H", raw, pos)[0] != k:
            continue
        runs = decode_run_words(raw, pos + 2, k, pool, seq)
        if runs is None:
            continue
        if check_count and cold_entries is not None \
                and sum(r[1] for r in runs) != cold_entries:
            continue
        candidates.append((k, runs, pos - updates_end))
    if len(candidates) != 1:
        die(f"frame {seq}: cold-run suffix not unique "
            f"(candidates {[c[0] for c in candidates]})")
    k, runs, audio_span = candidates[0]
    return runs, audio_span


def parse_runs_at(raw: bytes, seq: int, pool: int,
                  suffix_pos: int) -> list[tuple[int, int, int, int]]:
    """Decode the suffix at a known position and require an exact fit."""
    k = struct.unpack_from(">H", raw, suffix_pos)[0]
    if suffix_pos + 2 + 4 * k != len(raw):
        die(f"frame {seq}: suffix at {suffix_pos} with n_runs={k} "
            f"does not end the {len(raw)}-byte block")
    runs = decode_run_words(raw, suffix_pos + 2, k, pool, seq)
    if runs is None:
        die(f"frame {seq}: invalid run descriptor in positional parse")
    return runs


def parse_frame(raw: bytes, seq: int, cells: int, pool: int,
                features: int, audio_span: int | None) -> tuple[FrameRow, int]:
    total_len, packed_seq, raw_count = struct.unpack_from(">HHH", raw)
    if total_len != len(raw):
        die(f"frame {seq}: total_len {total_len} != {len(raw)}")
    if packed_seq != seq & 0xFFFF:
        die(f"frame {seq}: packed sequence is {packed_seq}")
    n_upd = raw_count & SHADOW_UPDATE_COUNT_MASK
    use_list = bool(raw_count & SHADOW_UPDATE_LIST_TAG)
    pal = struct.unpack_from(">H", raw, 6)[0]
    pos = 8

    cold_entries: int | None = None
    if use_list:
        pos += n_upd * 4
    else:
        bitmap_len = (cells + 7) // 8
        entries = struct.unpack_from(f">{n_upd}H", raw, pos + bitmap_len)
        cold_entries = sum(1 for e in entries if e & 0x8000)
        pos += bitmap_len + n_upd * 2

    check_count = (not use_list
                   and not features & FEATURE_VRAM_RAW_PREFETCH)
    if audio_span is None:
        if not check_count:
            die(f"frame {seq}: cannot solve audio span on a list/prefetch "
                f"frame; a legacy frame must come first")
        runs, audio_span = parse_runs_tail(
            raw, seq, pool, pos, cold_entries, check_count)
    else:
        runs = parse_runs_at(raw, seq, pool, pos + audio_span)

    loads = [0, 0, 0]
    short_runs = 0
    max_run_words = 0
    for _slot, count, source, _idx in runs:
        loads[source] += count
        words = count * WORDS_PER_PATTERN
        max_run_words = max(max_run_words, words)
        if words <= SHORT_RUN_MAX_WORDS:
            short_runs += 1
    loads_total = sum(loads)
    row = FrameRow(
        frame=seq, n_upd=n_upd, use_list=use_list, pal_switch=pal,
        cold_entries=cold_entries if cold_entries is not None else -1,
        n_runs=len(runs), loads_total=loads_total,
        loads_prg=loads[0], loads_wr=loads[1], loads_dic=loads[2],
        pass2_words=loads_total * WORDS_PER_PATTERN,
        short_runs=short_runs, max_run_words=max_run_words,
        control_bytes=total_len, n_ctrl_sec=0, n_pay_sec=0,
        slot_sec=0, rated_sec=0, lead_sec=0,
    )
    return row, audio_span


def read_pack(pack_dir: Path) -> tuple[list[FrameRow], dict]:
    header = (pack_dir / "HEADER.DAT").read_bytes()
    body = (pack_dir / "BODY.DAT").read_bytes()
    magic, version, nfr, cols, rows, cells, pool = struct.unpack_from(
        ">4sHHHHHH", header)
    if magic != b"TTRC" or version not in (10, 11, 12):
        die(f"expected TTRC v10-v12, got {magic!r} v{version}")
    if cols * rows != cells:
        die(f"grid {cols}x{rows} != {cells} cells")
    routing_sec = struct.unpack_from(">L", header, 26)[0]
    prebuf_sec = struct.unpack_from(">L", header, 30)[0]
    f0_ctrl_sec, f0_pat_sec, paltab_sec = struct.unpack_from(">LLL", header, 40)
    vsync_n = struct.unpack_from(">H", header, 52)[0]
    fps = struct.unpack_from(">H", header, 56)[0] or 15
    audio_preload_sec = struct.unpack_from(">H", header, 60)[0]
    features = struct.unpack_from(">H", header, 62)[0]
    if not features & FEATURE_COLD_RUNS:
        die("stream has no cold-run suffix; nothing to extract")
    table_sec = ADPCM_TABLE_SECTORS if features & FEATURE_ADPCM22 else 0
    supply_sec = pattern_supply_sectors(header, version, features)

    frame0_offset = (
        1 + paltab_sec + table_sec + supply_sec + audio_preload_sec) * SECTOR
    frame0_len = struct.unpack_from(">H", header, frame0_offset)[0]
    row0, audio_span = parse_frame(
        header[frame0_offset:frame0_offset + frame0_len], 0, cells, pool,
        features, None)
    rows_out = [row0]

    routing_offset = frame0_offset + (f0_ctrl_sec + f0_pat_sec) * SECTOR
    routes = decode_routes(
        header[routing_offset:routing_offset + routing_sec * SECTOR], nfr)

    if not (version >= 8 and features & FEATURE_FIXED_N2):
        die("only fixed-N2 v8+ rate accumulation is supported")
    rate_numerator, rate_modulus = 1001, 400

    accumulator = 0
    lead = 0
    body_pos = 0
    control_stream = bytearray()
    schedule = [(0, 0, 0, 0, 0)]
    for seq in range(1, nfr):
        n_pay, n_ctrl = routes[seq]
        accumulator += rate_numerator
        rated, accumulator = divmod(accumulator, rate_modulus)
        actual = n_pay + n_ctrl
        sectors = max(actual, rated - lead)
        lead += sectors - rated
        schedule.append((n_pay, n_ctrl, sectors, rated, lead))
        slot = body[body_pos:body_pos + sectors * SECTOR]
        if len(slot) != sectors * SECTOR:
            die(f"frame {seq}: BODY.DAT slot is truncated")
        control_stream += slot[:n_ctrl * SECTOR]
        body_pos += sectors * SECTOR
    if body_pos != len(body):
        die(f"BODY.DAT has {len(body) - body_pos} unrouted trailing bytes")

    control_pos = 0
    for seq in range(1, nfr):
        block_len = struct.unpack_from(">H", control_stream, control_pos)[0]
        if block_len < 8 or block_len & 1:
            die(f"frame {seq}: invalid control length {block_len}")
        row, span = parse_frame(
            bytes(control_stream[control_pos:control_pos + block_len]),
            seq, cells, pool, features, audio_span)
        if span != audio_span:
            die(f"frame {seq}: audio span drifted {audio_span} -> {span}")
        (row.n_pay_sec, row.n_ctrl_sec, row.slot_sec, row.rated_sec,
         row.lead_sec) = schedule[seq]
        rows_out.append(row)
        control_pos = block_len + control_pos

    meta = dict(version=version, nframes=nfr, cells=cells, pool=pool,
                fps=fps, vsync_n=vsync_n, features=features)
    return rows_out, meta


def cross_check_hud(rows: list[FrameRow], hud_csv: Path) -> None:
    """Verify parsed n_runs low bytes against a HUD OCR series (column N)."""
    by_frame = {}
    with hud_csv.open() as fh:
        for rec in csv.DictReader(fh):
            if rec["loop"] != "0":
                continue
            by_frame[int(rec["frame"])] = int(rec["cold_runs_low8"])
    mismatches = []
    checked = 0
    for row in rows:
        hud_n = by_frame.get(row.frame)
        if hud_n is None:
            continue
        checked += 1
        if row.n_runs & 0xFF != hud_n:
            mismatches.append((row.frame, row.n_runs, hud_n))
    if mismatches:
        for frame, n_runs, hud_n in mismatches[:10]:
            print(f"  MISMATCH frame {frame}: parsed n_runs={n_runs} "
                  f"HUD N={hud_n}", file=sys.stderr)
        die(f"{len(mismatches)}/{checked} HUD N mismatches against {hud_csv}")
    print(f"HUD cross-check OK: {checked} frames match column N ({hud_csv})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pack_dir", type=Path,
                    help="directory containing HEADER.DAT + BODY.DAT")
    ap.add_argument("--csv", type=Path, required=True,
                    help="output CSV path")
    ap.add_argument("--hud-csv", type=Path,
                    help="optional HUD OCR csv of the same stream; "
                         "validates parsed n_runs against column N")
    args = ap.parse_args()

    rows, meta = read_pack(args.pack_dir)
    print(f"{args.pack_dir}: TTRC v{meta['version']} frames={meta['nframes']} "
          f"cells={meta['cells']} pool={meta['pool']} "
          f"features=0x{meta['features']:04X}")

    if args.hud_csv:
        cross_check_hud(rows, args.hud_csv)

    fields = [f for f in FrameRow.__dataclass_fields__]
    with args.csv.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        for row in rows:
            writer.writerow([getattr(row, f) for f in fields])
    print(f"wrote {len(rows)} rows -> {args.csv}")


if __name__ == "__main__":
    main()
