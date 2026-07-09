#!/usr/bin/env python3
import argparse
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from quantize_md_video import (
    CHUNKS_PER_FRAME,
    MD_LEVELS,
    TILE_BYTES,
    TILE_SIZE,
    md_cram_word,
    nearest_indices,
    pack_4bpp,
    pack_tiles_4bpp,
    prepare_dir,
    rgb333_to_rgb888,
    rgb888_to_rgb333,
    weighted_palette,
    write_palette,
    write_huffman_outputs,
)


def run(cmd):
    subprocess.run(cmd, check=True)


def rgb333_colors():
    colors = []
    for r in range(8):
        for g in range(8):
            for b in range(8):
                colors.append((r, g, b))
    return np.array(colors, dtype=np.uint8)


COLORS_333 = rgb333_colors()


def extract_frames(args, raw_dir):
    prepare_dir(raw_dir, clean=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            args.start,
            "-t",
            args.duration,
            "-i",
            args.input,
            "-vf",
            f"crop={args.crop},scale={args.scale_width}:{args.scale_height}:flags=lanczos,fps={args.fps}",
            str(raw_dir / "%05d.png"),
        ]
    )
    frames = sorted(raw_dir.glob("*.png"))
    if not frames:
        raise RuntimeError("ffmpeg did not produce any frames")
    return frames


def frame_rgb333_and_hist(path):
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
    rgb333 = rgb888_to_rgb333(rgb)
    packed = (
        (rgb333[:, :, 0].astype(np.uint16) << 6)
        | (rgb333[:, :, 1].astype(np.uint16) << 3)
        | rgb333[:, :, 2].astype(np.uint16)
    )
    hist = np.bincount(packed.reshape(-1), minlength=512).astype(np.float64)
    return rgb333, hist


def palette_error_table(palette):
    diff = COLORS_333.astype(np.int16)[:, None, :] - palette.astype(np.int16)[None, :, :]
    return np.min(np.sum(diff * diff, axis=2), axis=1).astype(np.float64)


def palette_from_hist(hist):
    values = np.flatnonzero(hist)
    if len(values) == 0:
        return np.zeros((15, 3), dtype=np.uint8)
    unique_rgb333 = COLORS_333[values]
    counts = hist[values].astype(np.int64)
    return weighted_palette(unique_rgb333, counts)


def build_global_palettes(hists, palette_count=4, iterations=8):
    # Deterministic initial split across the movie timeline.
    frame_count = len(hists)
    assignments = np.minimum(
        (np.arange(frame_count) * palette_count) // frame_count,
        palette_count - 1,
    ).astype(np.int32)

    palettes = []
    for _ in range(iterations):
        palettes = []
        for group in range(palette_count):
            mask = assignments == group
            if np.any(mask):
                hist = np.sum(hists[mask], axis=0)
            else:
                hist = np.sum(hists, axis=0)
            palettes.append(palette_from_hist(hist))

        error_tables = np.stack([palette_error_table(palette) for palette in palettes], axis=0)
        errors = hists @ error_tables.T
        next_assignments = np.argmin(errors, axis=1).astype(np.int32)
        if np.array_equal(assignments, next_assignments):
            break
        assignments = next_assignments

    # Rebuild once using final assignments.
    final_palettes = []
    for group in range(palette_count):
        mask = assignments == group
        hist = np.sum(hists[mask], axis=0) if np.any(mask) else np.sum(hists, axis=0)
        final_palettes.append(palette_from_hist(hist))
    return np.array(final_palettes, dtype=np.uint8), assignments


def write_palette_bank(path, palettes):
    with path.open("wb") as f:
        for palette in palettes:
            words = [0] + [md_cram_word(color) for color in palette]
            for word in words:
                f.write(word.to_bytes(2, "big"))


def process_frames(rgb333_frames, palettes, assignments, out_dir, mode_name):
    preview_dir = out_dir / "work" / "preview"
    idx_dir = out_dir / "idx"
    tile_dir = out_dir / "tile"
    pal_dir = out_dir / "pal"
    prepare_dir(preview_dir, clean=True)
    prepare_dir(idx_dir, clean=True)
    prepare_dir(tile_dir, clean=True)
    prepare_dir(pal_dir, clean=True)

    frame_tiles = []
    height = width = None
    for i, rgb333 in enumerate(rgb333_frames):
        stem = f"{i:05d}"
        palette = palettes[int(assignments[i])]
        indices = nearest_indices(rgb333, palette, ordered=False)
        h, w = indices.shape
        height = h if height is None else height
        width = w if width is None else width

        idx_dir.joinpath(f"{stem}.idx").write_bytes(pack_4bpp(indices))
        tile_data = pack_tiles_4bpp(indices, w, h)
        tile_dir.joinpath(f"{stem}.tile").write_bytes(tile_data)
        write_palette(pal_dir / f"{stem}.pal", palette)
        frame_tiles.append(tile_data)

        full_palette = np.vstack([np.zeros((1, 3), dtype=np.uint8), palette])
        preview = rgb333_to_rgb888(full_palette[indices])
        Image.fromarray(preview, "RGB").save(preview_dir / f"{stem}.png")

        if (i + 1) % 25 == 0 or i + 1 == len(rgb333_frames):
            print(f"processed {i + 1}/{len(rgb333_frames)} frames")

    return frame_tiles, width, height


def main():
    parser = argparse.ArgumentParser(description="Encode MD movie preview using global 15-color palette banks.")
    parser.add_argument("--input", default="movies/disc1/061.mp4")
    parser.add_argument("--output-dir", default="out/video/061_full_15fps_288x112_global4pal")
    parser.add_argument("--start", default="0")
    parser.add_argument("--duration", default="152.866667")
    parser.add_argument("--fps", default="15")
    parser.add_argument("--crop", default="320:144:0:38")
    parser.add_argument("--scale-width", type=int, default=288)
    parser.add_argument("--scale-height", type=int, default=112)
    parser.add_argument("--palette-count", type=int, default=4)
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.palette_count <= 16:
        raise SystemExit("--palette-count must be 1..16")

    out_dir = Path(args.output_dir)
    raw_dir = out_dir / "work" / "raw"
    prepare_dir(out_dir)
    frame_paths = extract_frames(args, raw_dir)

    rgb333_frames = []
    hists = []
    for i, frame_path in enumerate(frame_paths):
        rgb333, hist = frame_rgb333_and_hist(frame_path)
        rgb333_frames.append(rgb333)
        hists.append(hist)
        if (i + 1) % 100 == 0 or i + 1 == len(frame_paths):
            print(f"hist {i + 1}/{len(frame_paths)}")
    hists = np.stack(hists, axis=0)

    palettes, assignments = build_global_palettes(hists, palette_count=args.palette_count)
    write_palette_bank(out_dir / "global_palettes.bin", palettes)
    (out_dir / "frame_palette_ids.bin").write_bytes(assignments.astype(np.uint8).tobytes())

    mode_name = f"global{args.palette_count}pal"
    frame_tiles, width, height = process_frames(rgb333_frames, palettes, assignments, out_dir, mode_name)
    huff_stats = write_huffman_outputs(out_dir, frame_tiles, width, height)

    preview_mp4 = out_dir / f"061_crop_md15_{mode_name}.mp4"
    run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            args.fps,
            "-i",
            str(out_dir / "work" / "preview" / "%05d.png"),
            "-ss",
            args.start,
            "-t",
            args.duration,
            "-i",
            args.input,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "16",
            "-preset",
            "fast",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(preview_mp4),
        ]
    )

    unique, counts = np.unique(assignments, return_counts=True)
    assignment_summary = ",".join(f"{int(k)}:{int(v)}" for k, v in zip(unique, counts))
    (out_dir / "manifest.txt").write_text(
        "\n".join(
            [
                f"input={args.input}",
                f"duration={args.duration}",
                f"fps={args.fps}",
                f"crop={args.crop}",
                f"size={width}x{height}",
                f"palette={args.palette_count} global banks, 15 colors each plus reserved index 0",
                f"palette_assignments={assignment_summary}",
                f"frames={len(frame_paths)}",
                f"preview={preview_mp4.name}",
                f"palette_bank=global_palettes.bin, {args.palette_count} * 16 big-endian words",
                "frame_palette_ids=frame_palette_ids.bin, one u8 per frame",
                f"huff_chunk_bytes={huff_stats['chunk_bytes']}",
                f"huff_chunks_per_frame={CHUNKS_PER_FRAME}",
                f"huff_raw_bytes={huff_stats['raw_bytes']}",
                f"huff_compressed_bytes={huff_stats['compressed_bytes']}",
                f"huff_ratio={huff_stats['ratio']:.4f}",
            ]
        )
        + "\n"
    )

    if not args.keep_work:
        shutil.rmtree(raw_dir)

    print(f"wrote {preview_mp4}")
    print(f"wrote {out_dir / 'huff'}")


if __name__ == "__main__":
    main()
