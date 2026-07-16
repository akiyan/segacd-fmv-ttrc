#!/usr/bin/env python3
"""Compare STL4 and MOSAIC-GM on evenly sampled real master frames."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from time import perf_counter

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from palette_algorithms import build_mosaic_palettes, score_palettes  # noqa: E402
from quantize_global4_tiles import build_palettes, tile_blocks  # noqa: E402


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


def rgb333_bayer(image):
    height, width, _channels = image.shape
    threshold = np.tile(
        (BAYER8 + 0.5) / 64.0,
        (height // 8 + 1, width // 8 + 1),
    )[:height, :width]
    scaled = image.astype(np.float32) * (7.0 / 255.0)
    base = np.floor(scaled)
    return np.clip(
        base + ((scaled - base) > threshold[..., None]), 0, 7
    ).astype(np.uint8)


def load_tiles(master_dir: Path, count: int):
    frames = sorted(master_dir.glob("*.png"))
    if not frames:
        raise SystemExit(f"no PNG frames under {master_dir}")
    selected = np.unique(np.linspace(0, len(frames) - 1, min(count, len(frames)), dtype=int))
    return np.concatenate([
        tile_blocks(rgb333_bayer(np.asarray(Image.open(frames[index]).convert("RGB"))))
        for index in selected
    ]), len(selected), len(frames)


def compare(label: str, master_dir: Path, frame_count: int):
    tiles, sampled, total = load_tiles(master_dir, frame_count)
    start = perf_counter()
    stl = build_palettes(tiles, n_pal=4)
    stl_s = perf_counter() - start
    stl_score = score_palettes(tiles, stl)

    start = perf_counter()
    mosaic, mosaic_stats = build_mosaic_palettes(tiles, return_stats=True)
    mosaic_s = perf_counter() - start
    mosaic_score = score_palettes(
        tiles, mosaic[:mosaic_stats["active_lines"]],
        core_colors=mosaic_stats["core_colors"],
    )

    print(f"\n{label}: sampled={sampled}/{total} tiles={len(tiles)}")
    print(
        f"  STL4      time={stl_s:.3f}s pixel={stl_score.summary()['pixel_error_per_pixel']:.6f} "
        f"map={stl_score.summary()['mapping_noise_per_pixel']:.6f} "
        f"lines={','.join(f'{value:.3f}' for value in stl_score.line_fraction)}"
    )
    print(
        f"  MOSAIC-GM time={mosaic_s:.3f}s pixel={mosaic_score.summary()['pixel_error_per_pixel']:.6f} "
        f"map={mosaic_score.summary()['mapping_noise_per_pixel']:.6f} "
        f"active={mosaic_stats['active_lines']} core={mosaic_stats['core_colors']} "
        f"lines={','.join(f'{value:.3f}' for value in mosaic_score.line_fraction)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument(
        "--case", action="append", nargs=2, metavar=("LABEL", "MASTER_DIR"),
        help="case label and master-frame directory; may be repeated",
    )
    args = parser.parse_args()
    cases = args.case or [
        ("Bad Apple H32", "videos/BadApple_H32_256x224_pcm13/master"),
        ("Sonic H32", "videos/sonic_H32_256x224_pcm13_geometry_pad_4by3/master"),
    ]
    for label, path in cases:
        compare(label, Path(path), args.frames)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
