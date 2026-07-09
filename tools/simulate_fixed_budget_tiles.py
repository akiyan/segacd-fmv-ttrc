#!/usr/bin/env python3
from pathlib import Path
import argparse
import json

import numpy as np
from PIL import Image, ImageDraw


TILE_BYTES = 32
SECTOR_SIZE = 2048


def read_cram_palette(path):
    data = path.read_bytes()
    colors = []
    for pos in range(0, len(data), 2):
        word = int.from_bytes(data[pos : pos + 2], "big")
        r = ((word >> 1) & 7) * 36
        g = ((word >> 5) & 7) * 36
        b = ((word >> 9) & 7) * 36
        colors.append((r, g, b))
    return colors


def tile_bytes_to_indices(tile_data, width_tiles, height_tiles):
    width = width_tiles * 8
    height = height_tiles * 8
    indices = np.zeros((height, width), dtype=np.uint8)
    pos = 0
    for ty in range(height_tiles):
        for tx in range(width_tiles):
            for row in range(8):
                for col_pair in range(4):
                    byte = tile_data[pos]
                    pos += 1
                    x = tx * 8 + col_pair * 2
                    y = ty * 8 + row
                    indices[y, x] = (byte >> 4) & 0x0F
                    indices[y, x + 1] = byte & 0x0F
    return indices


def render_tiles(tile_data, palette, width_tiles, height_tiles):
    indices = tile_bytes_to_indices(tile_data, width_tiles, height_tiles)
    rgb = np.zeros((height_tiles * 8, width_tiles * 8, 3), dtype=np.uint8)
    pal = np.array(palette, dtype=np.uint8)
    rgb[:, :] = pal[indices]
    return Image.fromarray(rgb, "RGB")


def tile_diff_score(a, b):
    aa = np.frombuffer(a, dtype=np.uint8).astype(np.int16)
    bb = np.frombuffer(b, dtype=np.uint8).astype(np.int16)
    return int(np.sum(np.abs(aa - bb)))


def simulate(root, frames, width_tiles, height_tiles, chunk_frames, chunk_sectors, out_dir, preload_first_frame, global_palette):
    tile_count = width_tiles * height_tiles
    budget = chunk_sectors * SECTOR_SIZE
    frame_header_bytes = 4
    chunk_header_bytes = 16
    palette_bytes = 32
    update_record_bytes = 2 + TILE_BYTES
    palette_overhead = 0 if global_palette else palette_bytes
    frame_payload_budget = (
        budget - chunk_header_bytes - chunk_frames * (palette_overhead + frame_header_bytes)
    ) // chunk_frames
    max_updates_per_frame = max(0, frame_payload_budget // update_record_bytes)

    tiles = []
    palettes = []
    for frame in range(frames):
        stem = f"{frame:05d}"
        tile_data = (root / "tile" / f"{stem}.tile").read_bytes()
        if len(tile_data) != tile_count * TILE_BYTES:
            raise SystemExit(f"unexpected tile size for {stem}: {len(tile_data)}")
        tiles.append([tile_data[i * TILE_BYTES : (i + 1) * TILE_BYTES] for i in range(tile_count)])
        palettes.append(read_cram_palette(root / "pal" / f"{stem}.pal"))

    out_dir.mkdir(parents=True, exist_ok=True)
    recon = [bytes(TILE_BYTES) for _ in range(tile_count)]
    stats = []
    first_stream_frame = 0
    if preload_first_frame and frames:
        recon = list(tiles[0])
        image = render_tiles(b"".join(recon), palettes[0], width_tiles, height_tiles)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 128, 12), fill=(0, 0, 0))
        draw.text((2, 0), "f0 preloaded key", fill=(255, 255, 255))
        image.save(out_dir / "00000.png")
        stats.append(
            {
                "frame": 0,
                "chunk": 0,
                "changed_tiles": tile_count,
                "updated_tiles": tile_count,
                "dropped_tiles": 0,
                "frame_payload_budget": tile_count * update_record_bytes,
                "preloaded": True,
            }
        )
        first_stream_frame = 1

    for chunk_start in range(first_stream_frame, frames, chunk_frames):
        for frame in range(chunk_start, min(chunk_start + chunk_frames, frames)):
            changed = []
            for tile_index, tile in enumerate(tiles[frame]):
                if tile != recon[tile_index]:
                    changed.append((tile_diff_score(recon[tile_index], tile), tile_index, tile))
            changed.sort(reverse=True)

            selected = changed[:max_updates_per_frame]
            for _score, tile_index, tile in selected:
                recon[tile_index] = tile

            reconstructed = b"".join(recon)
            image = render_tiles(reconstructed, palettes[frame], width_tiles, height_tiles)
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, 112, 12), fill=(0, 0, 0))
            draw.text((2, 0), f"f{frame} upd{len(selected)}/{len(changed)}", fill=(255, 255, 255))
            image.save(out_dir / f"{frame:05d}.png")

            stats.append(
                {
                    "frame": frame,
                    "chunk": chunk_start // chunk_frames,
                    "changed_tiles": len(changed),
                    "updated_tiles": len(selected),
                    "dropped_tiles": len(changed) - len(selected),
                    "frame_payload_budget": frame_payload_budget,
                    "preloaded": False,
                }
            )

    (out_dir / "stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Simulate fixed-sector tile-delta probe video.")
    parser.add_argument("--root", default="out/video/061_probe_137s_15s_15fps_272x96_nodither")
    parser.add_argument("--frames", type=int, default=32)
    parser.add_argument("--width-tiles", type=int, default=34)
    parser.add_argument("--height-tiles", type=int, default=12)
    parser.add_argument("--chunk-frames", type=int, default=4)
    parser.add_argument("--chunk-sectors", type=int, default=18)
    parser.add_argument("--output-dir", default="out/video/fixed_budget_probe")
    parser.add_argument("--preload-first-frame", action="store_true")
    parser.add_argument("--global-palette", action="store_true")
    args = parser.parse_args()

    stats = simulate(
        Path(args.root),
        args.frames,
        args.width_tiles,
        args.height_tiles,
        args.chunk_frames,
        args.chunk_sectors,
        Path(args.output_dir),
        args.preload_first_frame,
        args.global_palette,
    )
    print(f"frames={len(stats)}")
    print(f"chunk_sectors={args.chunk_sectors}")
    print(f"chunk_frames={args.chunk_frames}")
    print(f"max_updates_per_frame={stats[-1]['frame_payload_budget'] // (2 + TILE_BYTES)}")
    print(f"avg_updated_tiles={np.mean([s['updated_tiles'] for s in stats]):.2f}")
    print(f"avg_dropped_tiles={np.mean([s['dropped_tiles'] for s in stats]):.2f}")
    print(f"max_dropped_tiles={max(s['dropped_tiles'] for s in stats)}")


if __name__ == "__main__":
    main()
