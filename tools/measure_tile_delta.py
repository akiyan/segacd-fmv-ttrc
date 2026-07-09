#!/usr/bin/env python3
import argparse
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from quantize_md_video import (
    MD_LEVELS,
    TILE_BYTES,
    TILE_SIZE,
    rgb888_to_rgb333,
    nearest_indices,
    pack_tiles_4bpp,
    weighted_palette,
)


def run(cmd):
    subprocess.run(cmd, check=True)


def prepare_dir(path, clean=False):
    path.mkdir(parents=True, exist_ok=True)
    if clean:
        for child in path.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()


def extract_frames(input_path, raw_dir, duration, fps, crop, width, height):
    prepare_dir(raw_dir, clean=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0",
            "-t",
            str(duration),
            "-i",
            str(input_path),
            "-vf",
            f"crop={crop},scale={width}:{height}:flags=lanczos,fps={fps}",
            str(raw_dir / "%05d.png"),
        ]
    )
    frames = sorted(raw_dir.glob("*.png"))
    if not frames:
        raise RuntimeError("no frames extracted")
    return frames


def rgb333_frame(path):
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    return rgb888_to_rgb333(rgb)


def frame_palette(rgb333_frames):
    stacked = np.concatenate([f.reshape(-1, 3) for f in rgb333_frames], axis=0)
    packed = (
        (stacked[:, 0].astype(np.uint16) << 6)
        | (stacked[:, 1].astype(np.uint16) << 3)
        | stacked[:, 2].astype(np.uint16)
    )
    values, counts = np.unique(packed, return_counts=True)
    unique_rgb333 = np.stack(
        [
            ((values >> 6) & 7),
            ((values >> 3) & 7),
            (values & 7),
        ],
        axis=1,
    ).astype(np.uint8)
    return weighted_palette(unique_rgb333, counts)


def tiles_from_rgb333(rgb333, palette):
    indices = nearest_indices(rgb333, palette, ordered=False)
    h, w = indices.shape
    tile_data = pack_tiles_4bpp(indices, w, h)
    return np.frombuffer(tile_data, dtype=np.uint8).reshape(-1, TILE_BYTES).copy()


def measure(frames_rgb333, group_size):
    prev = None
    total_tiles = 0
    changed_tiles = 0
    changed_by_frame = []

    for start in range(0, len(frames_rgb333), group_size):
        group = frames_rgb333[start : start + group_size]
        if group_size == 1:
            palettes = [frame_palette([frame]) for frame in group]
        else:
            palette = frame_palette(group)
            palettes = [palette] * len(group)

        for rgb333, palette in zip(group, palettes):
            tiles = tiles_from_rgb333(rgb333, palette)
            frame_tiles = tiles.shape[0]
            if prev is None:
                changed = frame_tiles
            else:
                changed = int(np.any(tiles != prev, axis=1).sum())
            total_tiles += frame_tiles
            changed_tiles += changed
            changed_by_frame.append(changed)
            prev = tiles

    changed_arr = np.array(changed_by_frame, dtype=np.int32)
    return {
        "group_size": group_size,
        "frames": len(frames_rgb333),
        "tiles_per_frame": int(total_tiles // len(frames_rgb333)),
        "total_tiles": int(total_tiles),
        "changed_tiles": int(changed_tiles),
        "unchanged_tiles": int(total_tiles - changed_tiles),
        "changed_ratio": float(changed_tiles / total_tiles),
        "avg_changed_tiles": float(changed_arr.mean()),
        "median_changed_tiles": float(np.median(changed_arr)),
        "p90_changed_tiles": float(np.percentile(changed_arr, 90)),
        "max_changed_tiles": int(changed_arr.max()),
        "changed_raw_bytes": int(changed_tiles * TILE_BYTES),
        "bitmask_bytes": int(((total_tiles // len(frames_rgb333) + 7) // 8) * len(frames_rgb333)),
        "palette_bytes": int(32 * ((len(frames_rgb333) + group_size - 1) // group_size)),
    }


def main():
    parser = argparse.ArgumentParser(description="Measure tile delta potential for MD movie frames.")
    parser.add_argument("--input", default="movies/disc1/061.mp4")
    parser.add_argument("--duration", default="152.866667")
    parser.add_argument("--fps", default="15")
    parser.add_argument("--crop", default="320:144:0:38")
    parser.add_argument("--width", type=int, default=288)
    parser.add_argument("--height", type=int, default=112)
    parser.add_argument("--output-dir", default="out/video/061_full_15fps_288x112_nodither/delta_measure")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    raw_dir = out_dir / "raw"
    prepare_dir(out_dir)
    frame_paths = extract_frames(args.input, raw_dir, args.duration, args.fps, args.crop, args.width, args.height)
    frames_rgb333 = [rgb333_frame(path) for path in frame_paths]

    results = [measure(frames_rgb333, group_size) for group_size in (1, 4, 8)]
    duration = float(args.duration)

    lines = []
    for result in results:
        total_with_mask_pal = result["changed_raw_bytes"] + result["bitmask_bytes"] + result["palette_bytes"]
        lines.extend(
            [
                f"group_size={result['group_size']}",
                f"frames={result['frames']}",
                f"tiles_per_frame={result['tiles_per_frame']}",
                f"changed_tiles={result['changed_tiles']}",
                f"changed_ratio={result['changed_ratio']:.6f}",
                f"avg_changed_tiles={result['avg_changed_tiles']:.2f}",
                f"median_changed_tiles={result['median_changed_tiles']:.2f}",
                f"p90_changed_tiles={result['p90_changed_tiles']:.2f}",
                f"max_changed_tiles={result['max_changed_tiles']}",
                f"changed_raw_bytes={result['changed_raw_bytes']}",
                f"bitmask_bytes={result['bitmask_bytes']}",
                f"palette_bytes={result['palette_bytes']}",
                f"total_with_mask_pal={total_with_mask_pal}",
                f"changed_raw_bps={result['changed_raw_bytes'] / duration:.2f}",
                f"total_with_mask_pal_bps={total_with_mask_pal / duration:.2f}",
                "",
            ]
        )

    report = "\n".join(lines)
    (out_dir / "report.txt").write_text(report)
    print(report)
    shutil.rmtree(raw_dir)


if __name__ == "__main__":
    main()

