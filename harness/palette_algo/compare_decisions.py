#!/usr/bin/env python3
"""Compare segmented decision-log palettes on fixed real source frames."""

from __future__ import annotations

import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_sources import rgb333_bayer  # noqa: E402
from palette_algorithms import score_palettes  # noqa: E402
from quantize_global4_tiles import palette_lut, rgb333_keys, tile_blocks  # noqa: E402


def flatten_low_detail(tiles, threshold=0.12):
    values = tiles.astype(np.float64)
    detail = values.std(axis=1).mean(axis=1)
    result = tiles.copy()
    mask = detail < threshold
    result[mask] = np.round(values.mean(axis=1)[mask]).astype(np.uint8)[:, None, :]
    return result


def load_log(path: Path):
    with path.open("rb") as source:
        return pickle.load(source)


def quantize(tiles, palettes):
    keys = rgb333_keys(tiles)
    tables = [palette_lut(palette, squared=True) for palette in palettes]
    cost = np.stack([table[0] for table in tables])
    index = np.stack([table[1] for table in tables])
    assign = cost[:, keys].sum(2, dtype=np.int64).T.argmin(1)
    selected = index[assign[:, None], keys]
    return np.asarray(palettes)[assign[:, None], selected], assign


def seam_error(source, output, rows, cols):
    residual = (output.astype(np.int16) - source.astype(np.int16))
    image = residual.reshape(rows, cols, 8, 8, 3).transpose(0, 2, 1, 3, 4)
    image = image.reshape(rows * 8, cols * 8, 3)
    total = 0
    pairs = 0
    for x in range(8, cols * 8, 8):
        total += int(((image[:, x - 1] - image[:, x]) ** 2).sum())
        pairs += rows * 8
    for y in range(8, rows * 8, 8):
        total += int(((image[y - 1] - image[y]) ** 2).sum())
        pairs += cols * 8
    return total, pairs


def evaluate(label, log, frame_tiles, indices):
    palettes = np.asarray(log["seg_pals"], dtype=np.uint8)
    frame_seg = np.asarray(log["frame_seg"], dtype=np.int32)
    cols, rows, cells, _tile = map(int, log["geom"])
    if any(len(frame_tiles[index]) != cells for index in indices):
        raise SystemExit(f"{label}: source tile count differs from decision geometry")

    pixel_error = 0
    mapping_noise = 0
    seam = 0
    seam_pairs = 0
    line_count = np.zeros(4, dtype=np.int64)
    for segment in np.unique(frame_seg[indices]):
        selected_frames = [index for index in indices if frame_seg[index] == segment]
        tiles = np.concatenate([frame_tiles[index] for index in selected_frames])
        result = score_palettes(tiles, palettes[int(segment)])
        pixel_error += result.pixel_error
        mapping_noise += result.mapping_noise
        line_count += np.bincount(result.assign, minlength=4)
        for index in selected_frames:
            source = frame_tiles[index]
            output, _assign = quantize(source, palettes[int(segment)])
            value, pairs = seam_error(source, output, rows, cols)
            seam += value
            seam_pairs += pairs
    pixels = len(indices) * cells * 64
    print(
        f"{label}: segments={len(palettes)} sample_frames={len(indices)} "
        f"pixel={pixel_error / pixels:.9f} mapping={mapping_noise / pixels:.9f} "
        f"combined={(pixel_error + mapping_noise) / pixels:.9f} "
        f"seam={seam / max(1, seam_pairs):.9f} "
        f"lines={','.join(f'{value / line_count.sum():.3f}' for value in line_count)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master_dir", type=Path)
    parser.add_argument("decision", nargs="+", type=Path)
    parser.add_argument("--frames", type=int, default=240)
    args = parser.parse_args()
    logs = [load_log(path) for path in args.decision]
    total = min(len(log["frame_seg"]) for log in logs)
    frames = sorted(args.master_dir.glob("*.png"))[:total]
    if len(frames) != total:
        raise SystemExit(f"master has {len(frames)} frames, expected at least {total}")
    indices = np.unique(np.clip(
        ((np.arange(min(args.frames, total)) + 0.5) * total / min(args.frames, total)).astype(int),
        0, total - 1,
    ))
    frame_tiles = {
        int(index): flatten_low_detail(tile_blocks(rgb333_bayer(
            np.asarray(Image.open(frames[int(index)]).convert("RGB"))
        )))
        for index in indices
    }
    for path, log in zip(args.decision, logs):
        evaluate(path.stem + "@" + path.parent.name, log, frame_tiles, indices)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
