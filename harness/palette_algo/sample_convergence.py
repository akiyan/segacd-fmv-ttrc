#!/usr/bin/env python3
"""Measure MOSAIC-GM sample-count convergence on a fixed validation set."""

from __future__ import annotations

import argparse
import gc
import sys
from pathlib import Path
from time import perf_counter

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_sources import rgb333_bayer  # noqa: E402
from palette_algorithms import build_mosaic_palettes, score_palettes  # noqa: E402
from quantize_global4_tiles import tile_blocks  # noqa: E402


def spaced_indices(total: int, count: int, half_step: bool):
    count = min(total, count)
    if count == total:
        return np.arange(total, dtype=np.int64)
    offset = 0.5 if half_step else 0.0
    return np.unique(np.clip(
        ((np.arange(count, dtype=np.float64) + offset) * total / count).astype(np.int64),
        0, total - 1,
    ))


def load_tiles(frames, indices):
    return np.concatenate([
        tile_blocks(rgb333_bayer(np.asarray(Image.open(frames[index]).convert("RGB"))))
        for index in indices
    ])


def release_gpu_pool():
    try:
        import gpu_quant
        if gpu_quant.enabled():
            gpu_quant.cupy().get_default_memory_pool().free_all_blocks()
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master_dir", type=Path)
    parser.add_argument("--counts", default="30,60,120,240,480,960,1920")
    parser.add_argument("--validation-frames", type=int, default=240)
    args = parser.parse_args()

    frames = sorted(args.master_dir.glob("*.png"))
    if not frames:
        raise SystemExit(f"no PNG frames under {args.master_dir}")
    counts = [int(value) for value in args.counts.split(",") if value.strip()]
    validation_indices = spaced_indices(len(frames), args.validation_frames, half_step=True)
    validation = load_tiles(frames, validation_indices)
    print(
        f"source={args.master_dir} total_frames={len(frames)} "
        f"validation_frames={len(validation_indices)} validation_tiles={len(validation)}"
    , flush=True)
    print(
        "train_frames,time_s,active,core,pixel_per_px,map_per_px,score_per_px,delta_percent",
        flush=True,
    )
    previous = None
    for requested in counts:
        train_indices = spaced_indices(len(frames), requested, half_step=False)
        training = load_tiles(frames, train_indices)
        start = perf_counter()
        palettes, stats = build_mosaic_palettes(training, return_stats=True)
        elapsed = perf_counter() - start
        active = int(stats["active_lines"])
        result = score_palettes(
            validation, palettes[:active], core_colors=int(stats["core_colors"]))
        summary = result.summary()
        score = summary["score_per_pixel"]
        delta = float("nan") if previous is None else (previous - score) / previous * 100.0
        print(
            f"{len(train_indices)},{elapsed:.3f},{active},{stats['core_colors']},"
            f"{summary['pixel_error_per_pixel']:.9f},"
            f"{summary['mapping_noise_per_pixel']:.9f},"
            f"{score:.9f},{delta:.4f}"
        , flush=True)
        previous = score
        del training, palettes, result
        gc.collect()
        release_gpu_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
