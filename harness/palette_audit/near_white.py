#!/usr/bin/env python3
"""Count near-white 666/777 tile patterns before and after the codec."""

from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np
from PIL import Image


BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42],
    [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38],
    [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41],
    [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37],
    [63, 31, 55, 23, 61, 29, 53, 21],
], dtype=np.float32)


def source_distribution(master_dir: Path, width: int, height: int, tile: int):
    threshold = np.tile(
        (BAYER8 + 0.5) / 64.0,
        (height // 8 + 1, width // 8 + 1),
    )[:height, :width]
    counts = np.zeros(65, dtype=np.int64)
    flat = np.zeros(65, dtype=np.int64)
    rows, cols = height // tile, width // tile
    files = sorted(master_dir.glob("*.png"))
    for path in files:
        image = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        scaled = image.astype(np.float32) * (7.0 / 255.0)
        base = np.floor(scaled)
        quant = np.clip(
            base + ((scaled - base) > threshold[..., None]), 0, 7
        ).astype(np.uint8)
        qtiles = quant.reshape(rows, tile, cols, tile, 3)
        qtiles = qtiles.transpose(0, 2, 1, 3, 4).reshape(rows * cols, tile * tile, 3)
        source_tiles = image.reshape(rows, tile, cols, tile, 3)
        source_tiles = source_tiles.transpose(0, 2, 1, 3, 4).reshape(rows * cols, tile * tile, 3)
        white = np.all(qtiles == 7, axis=2)
        gray = np.all(qtiles == 6, axis=2)
        only = np.all(white | gray, axis=1)
        gray_count = gray.sum(axis=1)
        for dots in range(1, 65):
            cells = np.flatnonzero(only & (gray_count == dots))
            counts[dots] += len(cells)
            flat[dots] += sum(
                int(source_tiles[cell].max()) - int(source_tiles[cell].min()) <= 3
                for cell in cells
            )
    return counts, flat, len(files)


def display_distribution(log, palettes, frame_seg, cells: int):
    keys = [None] * cells
    palette_index = np.zeros(cells, dtype=np.uint8)
    dot_count = np.zeros(cells, dtype=np.uint8)
    counts = np.zeros(65, dtype=np.int64)
    frames = np.zeros(65, dtype=np.int64)

    def classify(cell: int, segment: int):
        if keys[cell] is None:
            return
        full = np.vstack([
            np.zeros((1, 3), dtype=np.uint8),
            palettes[segment, palette_index[cell]],
        ])
        rgb = full[np.frombuffer(keys[cell], dtype=np.uint8)]
        white = np.all(rgb == 7, axis=1)
        gray = np.all(rgb == 6, axis=1)
        dot_count[cell] = int(gray.sum()) if np.all(white | gray) else 0

    previous_segment = -1
    for frame, updates in enumerate(log["frames"]):
        segment = int(frame_seg[frame])
        for cell, pal, key in updates:
            keys[cell] = key
            palette_index[cell] = pal
        if segment != previous_segment:
            for cell in range(cells):
                classify(cell, segment)
            previous_segment = segment
        else:
            for cell, _pal, _key in updates:
                classify(cell, segment)
        hist = np.bincount(dot_count, minlength=65)
        counts += hist
        frames += hist > 0
    counts[0] = 0
    frames[0] = 0
    return counts, frames


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decisions", type=Path)
    parser.add_argument("master_dir", type=Path)
    args = parser.parse_args()

    with args.decisions.open("rb") as src:
        log = pickle.load(src)
    palettes = np.asarray(log["seg_pals"], dtype=np.uint8)
    frame_seg = np.asarray(log["frame_seg"], dtype=np.int32)
    cols, rows, cells, tile = map(int, log["geom"])
    source, flat, source_frames = source_distribution(
        args.master_dir, cols * tile, rows * tile, tile
    )
    display, display_frames = display_distribution(log, palettes, frame_seg, cells)

    print(f"source_frames={source_frames} display_frames={len(log['frames'])}")
    print("gray_dots,source_tiles,source_flat_range_le_3,display_cell_frames,display_frames")
    for dots in range(1, 65):
        if source[dots] or display[dots]:
            print(
                f"{dots},{source[dots]},{flat[dots]},"
                f"{display[dots]},{display_frames[dots]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
