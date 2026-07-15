#!/usr/bin/env python3
"""Prove the lossless global-brightest -> P0/index15 canonicalization."""

from __future__ import annotations

import argparse
import pickle
import struct
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import sim  # noqa: E402


SECTOR = 2048


def render_rgb333(pals, frame_seg, assigns, pidxs):
    out = []
    for i, (assign, idx) in enumerate(zip(assigns, pidxs)):
        full = np.zeros((4, 16, 3), np.uint8)
        full[:, 1:] = pals[int(frame_seg[i])]
        out.append(full[assign[:, None], idx])
    return out


def assert_canonical(pals, label):
    a = np.asarray(pals, np.uint8)
    if a.shape != (4, 15, 3):
        raise SystemExit(f"{label}: palette shape is {a.shape}, expected (4, 15, 3)")
    brightness = a.astype(np.int16).sum(axis=2)
    if int(brightness[0, 14]) != int(brightness.max()):
        raise SystemExit(f"{label}: P0/index15 is not globally brightest")
    cram = sim.pals_to_bytes([a[row] for row in range(4)])
    for row in range(4):
        if cram[row * 32:row * 32 + 2] != b"\0\0":
            raise SystemExit(f"{label}: reserved index0 is nonzero in row {row}")
    return cram


def prove_synthetic():
    # Two segments place their global maximum in different nonzero rows/slots.
    # The third is already canonical and must remain byte-identical.
    pals = []
    for bright_row, bright_slot, dest_tied in ((2, 2, False), (3, 8, False),
                                               (1, 4, True)):
        p = np.zeros((4, 15, 3), np.uint8)
        for row in range(4):
            for col in range(15):
                p[row, col] = ((col + row) % 7, (2 * col + row) % 7,
                               (3 * col + row) % 7)
        p[bright_row, bright_slot] = (7, 7, 7)
        p[0, 14] = (7, 7, 7) if dest_tied else (1, 2, 3)
        pals.append(p)

    frame_seg = np.array([0, 0, 1, 1, 2], np.int32)
    assigns = [
        np.array([0, 1, 2, 3], np.int8),
        np.array([2, 0, 1, 3], np.int8),
        np.array([0, 3, 2, 1], np.int8),
        np.array([3, 0, 1, 2], np.int8),
        np.array([1, 0, 3, 2], np.int8),
    ]
    pidxs = []
    for i, assign in enumerate(assigns):
        base = ((np.arange(len(assign) * 64, dtype=np.uint16).reshape(-1, 64)
                 + i * 5) % 15 + 1)
        pidxs.append(base.astype(np.uint8))

    original_pals = [p.copy() for p in pals]
    original_assign = [a.copy() for a in assigns]
    original_idx = [p.copy() for p in pidxs]
    before = render_rgb333(original_pals, frame_seg, assigns, original_idx)
    canonical, stats = sim.canonicalize_p0_index15(
        pals, frame_seg, assigns, pidxs)
    after = render_rgb333(canonical, frame_seg, assigns, pidxs)

    for i, (a, b) in enumerate(zip(before, after)):
        if not np.array_equal(a, b):
            raise SystemExit(f"synthetic frame {i}: RGB identity failed")
    for seg, (old, new) in enumerate(zip(original_pals, canonical)):
        assert_canonical(new, f"synthetic segment {seg}")
        old_codes = np.sort(((old[:, :, 0].astype(np.int16) << 6)
                             | (old[:, :, 1].astype(np.int16) << 3)
                             | old[:, :, 2].astype(np.int16)), axis=None)
        new_codes = np.sort(((new[:, :, 0].astype(np.int16) << 6)
                             | (new[:, :, 1].astype(np.int16) << 3)
                             | new[:, :, 2].astype(np.int16)), axis=None)
        if not np.array_equal(old_codes, new_codes):
            raise SystemExit(f"synthetic segment {seg}: 60-colour multiset changed")
    if stats["row_swapped_segments"] != 2 or stats["index_swapped_segments"] != 2:
        raise SystemExit(f"unexpected synthetic permutation counts: {stats}")
    if not np.array_equal(canonical[2], original_pals[2]):
        raise SystemExit("already-canonical tie segment was needlessly reordered")
    if not np.array_equal(assigns[4], original_assign[4]) or not np.array_equal(
            pidxs[4], original_idx[4]):
        raise SystemExit("already-canonical tie frame was needlessly remapped")
    return stats


def prove_font_upload():
    font_path = ROOT / "boot" / "dbgfont.bin"
    data = font_path.read_bytes()
    nibbles = np.concatenate((np.frombuffer(data, np.uint8) >> 4,
                              np.frombuffer(data, np.uint8) & 0x0F))
    if not set(int(x) for x in np.unique(nibbles)) <= {0, 1}:
        raise SystemExit("dbgfont.bin contains an index other than 0/1")

    # Mirror the player's one-time word expansion exactly: every 1 bit fans out
    # to the remaining three bits of its own nibble, producing index 15.
    expanded = bytearray()
    for off in range(0, len(data), 2):
        d0 = int.from_bytes(data[off:off + 2], "big")
        d2 = d0
        for _ in range(3):
            d2 = (d2 << 1) & 0xFFFF
            d0 |= d2
        expanded += d0.to_bytes(2, "big")
    out = np.frombuffer(expanded, np.uint8)
    out_nibbles = np.concatenate((out >> 4, out & 0x0F))
    if not set(int(x) for x in np.unique(out_nibbles)) <= {0, 15}:
        raise SystemExit("font upload expansion did not produce only index0/index15")
    if int((nibbles == 1).sum()) != int((out_nibbles == 15).sum()):
        raise SystemExit("font upload expansion changed the number of set pixels")
    return len(data) // 32


def check_decision_log(path):
    with Path(path).open("rb") as src:
        log = pickle.load(src)
    seg_pals = log.get("seg_pals")
    if not seg_pals:
        raise SystemExit(f"{path}: no seg_pals")
    for seg, pals in enumerate(seg_pals):
        assert_canonical(pals, f"decision segment {seg}")
    frame_seg = np.asarray(log.get("frame_seg"), np.int64)
    if frame_seg.size == 0 or int(frame_seg.min()) < 0 or int(frame_seg.max()) >= len(seg_pals):
        raise SystemExit(f"{path}: invalid frame_seg")
    return log


def _word_rgb_sum(word):
    return ((word >> 1) & 7) + ((word >> 5) & 7) + ((word >> 9) & 7)


def check_header(path, log=None):
    data = Path(path).read_bytes()
    if len(data) < 2 * SECTOR or data[:4] != b"TTRC":
        raise SystemExit(f"{path}: not a complete TTRC HEADER.DAT")
    nseg = struct.unpack_from(">H", data, 20)[0]
    paltab_sec = struct.unpack_from(">L", data, 48)[0]
    if paltab_sec * SECTOR < nseg * 128 or len(data) < SECTOR + paltab_sec * SECTOR:
        raise SystemExit(f"{path}: truncated PALTAB")
    blocks = [data[SECTOR + seg * 128:SECTOR + (seg + 1) * 128]
              for seg in range(nseg)]
    for seg, block in enumerate(blocks):
        words = np.frombuffer(block, dtype=">u2").astype(np.uint16).reshape(4, 16)
        if np.any(words[:, 0] != 0):
            raise SystemExit(f"HEADER segment {seg}: an index0 word is nonzero")
        sums = np.vectorize(_word_rgb_sum)(words[:, 1:])
        if int(sums[0, 14]) != int(sums.max()):
            raise SystemExit(f"HEADER segment {seg}: P0/index15 is not globally brightest")
    if log is not None:
        if nseg != len(log["seg_pals"]):
            raise SystemExit("HEADER/decision segment counts differ")
        for seg, pals in enumerate(log["seg_pals"]):
            expected = assert_canonical(pals, f"decision segment {seg}")
            if blocks[seg] != expected:
                raise SystemExit(f"HEADER PALTAB segment {seg} differs from decision log")
        seg0 = int(np.asarray(log["frame_seg"])[0])
        if data[64:192] != blocks[seg0]:
            raise SystemExit("HEADER seg0 copy differs from frame 0's PALTAB entry")
    return nseg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dec-log", help="optionally verify a current decisions.pkl")
    parser.add_argument("--header", help="optionally verify its packed HEADER.DAT")
    args = parser.parse_args()
    if args.header and not args.dec_log:
        log = None
    else:
        log = check_decision_log(args.dec_log) if args.dec_log else None

    stats = prove_synthetic()
    glyphs = prove_font_upload()
    extra = ""
    if args.header:
        nseg = check_header(args.header, log)
        extra = f", {nseg} packed segments"
    print("palette index15 proof: OK "
          f"({stats['segments']} synthetic segments, {stats['verified_pixels']} pixels, "
          f"frame0 included, {glyphs} font glyphs{extra})")


if __name__ == "__main__":
    main()
