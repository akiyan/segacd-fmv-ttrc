#!/usr/bin/env python3
import argparse
import heapq
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image


MD_LEVELS = np.array([0, 36, 72, 108, 144, 180, 216, 252], dtype=np.uint8)
TILE_SIZE = 8
TILE_BYTES = 32
CHUNKS_PER_FRAME = 4


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


def rgb888_to_rgb333(rgb):
    return np.clip(np.rint(rgb.astype(np.float32) * (7.0 / 255.0)), 0, 7).astype(np.uint8)


def rgb333_to_rgb888(rgb333):
    return MD_LEVELS[rgb333]


def weighted_palette(unique_rgb333, counts, colors=15, iterations=10):
    order = np.argsort(counts)[::-1]
    initial = unique_rgb333[order[:colors]].astype(np.float32)

    if len(initial) < colors:
        pad = np.zeros((colors - len(initial), 3), dtype=np.float32)
        initial = np.vstack([initial, pad])

    centers = initial
    points = unique_rgb333.astype(np.float32)
    weights = counts.astype(np.float32)

    for _ in range(iterations):
        diff = points[:, None, :] - centers[None, :, :]
        labels = np.argmin(np.sum(diff * diff, axis=2), axis=1)
        next_centers = centers.copy()
        for i in range(colors):
            mask = labels == i
            if np.any(mask):
                next_centers[i] = np.average(points[mask], axis=0, weights=weights[mask])
        if np.allclose(next_centers, centers):
            break
        centers = next_centers

    palette = np.clip(np.rint(centers), 0, 7).astype(np.uint8)

    # Deduplicate after snapping to RGB333. Fill any gaps with frequent unused colors.
    result = []
    used = set()
    for color in palette:
        key = tuple(int(v) for v in color)
        if key not in used:
            used.add(key)
            result.append(color)
    for color in unique_rgb333[order]:
        key = tuple(int(v) for v in color)
        if key not in used:
            used.add(key)
            result.append(color)
        if len(result) == colors:
            break
    while len(result) < colors:
        result.append(np.array([0, 0, 0], dtype=np.uint8))
    return np.array(result[:colors], dtype=np.uint8)


def nearest_indices(rgb333, palette, ordered=False):
    working = rgb333.astype(np.int16)
    if ordered:
        # Gentle 4x4 Bayer dither in RGB333 space. This keeps temporal noise lower
        # than error diffusion while still breaking large flat bands.
        bayer = np.array(
            [
                [0, 8, 2, 10],
                [12, 4, 14, 6],
                [3, 11, 1, 9],
                [15, 7, 13, 5],
            ],
            dtype=np.int16,
        )
        h, w, _ = working.shape
        threshold = bayer[np.arange(h)[:, None] % 4, np.arange(w)[None, :] % 4]
        threshold = ((threshold - 7) // 5).astype(np.int16)
        working = np.clip(working + threshold[:, :, None], 0, 7)

    flat = working.reshape(-1, 3).astype(np.int16)
    pal = palette.astype(np.int16)
    diff = flat[:, None, :] - pal[None, :, :]
    labels = np.argmin(np.sum(diff * diff, axis=2), axis=1).astype(np.uint8)
    return (labels.reshape(rgb333.shape[:2]) + 1).astype(np.uint8)


def pack_4bpp(indices):
    h, w = indices.shape
    if w % 2 != 0:
        raise ValueError("width must be even for 4bpp packing")
    left = indices[:, 0::2]
    right = indices[:, 1::2]
    return ((left << 4) | right).astype(np.uint8).tobytes()


def pack_tiles_4bpp(indices, width, height):
    h, w = indices.shape
    if (w, h) != (width, height):
        raise ValueError(f"expected {width}x{height}, got {w}x{h}")
    if width % TILE_SIZE or height % TILE_SIZE:
        raise ValueError("width and height must be multiples of 8")

    out = bytearray()
    for ty in range(0, height, TILE_SIZE):
        for tx in range(0, width, TILE_SIZE):
            tile = indices[ty : ty + TILE_SIZE, tx : tx + TILE_SIZE]
            for row in tile:
                for x in range(0, TILE_SIZE, 2):
                    out.append((int(row[x]) << 4) | int(row[x + 1]))
    return bytes(out)


def md_cram_word(rgb333):
    r, g, b = (int(v) for v in rgb333)
    return (b << 9) | (g << 5) | (r << 1)


def write_palette(path, palette15):
    words = [0] + [md_cram_word(color) for color in palette15]
    with path.open("wb") as f:
        for word in words:
            f.write(word.to_bytes(2, "big"))


def process_frame(src, preview_path, idx_path, tile_path, pal_path, ordered=False):
    image = Image.open(src).convert("RGB")
    rgb = np.asarray(image, dtype=np.uint8)
    rgb333 = rgb888_to_rgb333(rgb)
    packed_colors = (
        (rgb333[:, :, 0].astype(np.uint16) << 6)
        | (rgb333[:, :, 1].astype(np.uint16) << 3)
        | rgb333[:, :, 2].astype(np.uint16)
    )
    flat = packed_colors.reshape(-1)
    values, counts = np.unique(flat, return_counts=True)
    unique_rgb333 = np.stack(
        [
            ((values >> 6) & 7),
            ((values >> 3) & 7),
            (values & 7),
        ],
        axis=1,
    ).astype(np.uint8)

    palette = weighted_palette(unique_rgb333, counts)
    indices = nearest_indices(rgb333, palette, ordered=ordered)

    scanline = pack_4bpp(indices)
    height, width = indices.shape
    tile_data = pack_tiles_4bpp(indices, width, height)
    idx_path.write_bytes(scanline)
    tile_path.write_bytes(tile_data)
    write_palette(pal_path, palette)

    full_palette = np.vstack([np.zeros((1, 3), dtype=np.uint8), palette])
    preview = rgb333_to_rgb888(full_palette[indices])
    Image.fromarray(preview, "RGB").save(preview_path)
    return tile_data, width, height


def huffman_code_lengths(frequencies):
    heap = []
    seq = 0
    for symbol, freq in enumerate(frequencies):
        if freq:
            heap.append((int(freq), seq, symbol))
            seq += 1

    if not heap:
        return [1] + [0] * 255
    if len(heap) == 1:
        lengths = [0] * 256
        lengths[heap[0][2]] = 1
        return lengths

    heapq.heapify(heap)
    parents = {}
    while len(heap) > 1:
        freq_a, _seq_a, node_a = heapq.heappop(heap)
        freq_b, _seq_b, node_b = heapq.heappop(heap)
        parent = ("n", seq)
        seq += 1
        parents[node_a] = parent
        parents[node_b] = parent
        heapq.heappush(heap, (freq_a + freq_b, seq, parent))
        seq += 1

    lengths = [0] * 256
    for symbol, freq in enumerate(frequencies):
        if not freq:
            continue
        node = symbol
        depth = 0
        while node in parents:
            node = parents[node]
            depth += 1
        lengths[symbol] = max(depth, 1)
    return lengths


def canonical_codes(lengths):
    pairs = sorted((length, symbol) for symbol, length in enumerate(lengths) if length)
    codes = {}
    code = 0
    prev_len = 0
    for length, symbol in pairs:
        code <<= length - prev_len
        codes[symbol] = (code, length)
        code += 1
        prev_len = length
    return codes


def huffman_encode(data, codes):
    out = bytearray()
    bit_buffer = 0
    bit_count = 0
    bits_written = 0

    for byte in data:
        code, length = codes[byte]
        bit_buffer = (bit_buffer << length) | code
        bit_count += length
        bits_written += length
        while bit_count >= 8:
            shift = bit_count - 8
            out.append((bit_buffer >> shift) & 0xFF)
            bit_count -= 8
            bit_buffer &= (1 << bit_count) - 1 if bit_count else 0

    if bit_count:
        out.append((bit_buffer << (8 - bit_count)) & 0xFF)
    return bytes(out), bits_written


def write_huffman_outputs(out_dir, frame_tiles, width, height):
    huff_dir = out_dir / "huff"
    prepare_dir(huff_dir, clean=True)

    tiles_per_frame = (width // TILE_SIZE) * (height // TILE_SIZE)
    if tiles_per_frame % CHUNKS_PER_FRAME:
        raise ValueError(f"{tiles_per_frame} tiles/frame is not divisible by {CHUNKS_PER_FRAME}")
    chunk_bytes = (tiles_per_frame // CHUNKS_PER_FRAME) * TILE_BYTES

    chunks = []
    frequencies = [0] * 256
    for frame in frame_tiles:
        if len(frame) != tiles_per_frame * TILE_BYTES:
            raise ValueError("bad tile frame size")
        for offset in range(0, len(frame), chunk_bytes):
            chunk = frame[offset : offset + chunk_bytes]
            chunks.append(chunk)
            counts = np.bincount(np.frombuffer(chunk, dtype=np.uint8), minlength=256)
            for i, count in enumerate(counts):
                frequencies[i] += int(count)

    lengths = huffman_code_lengths(frequencies)
    codes = canonical_codes(lengths)

    table_entries = []
    data_offset = 0
    compressed_stream = bytearray()
    compressed_bytes_total = 0
    compressed_bits_total = 0
    raw_bytes_total = 0

    for chunk in chunks:
        encoded, bit_count = huffman_encode(chunk, codes)
        table_entries.append((data_offset, len(encoded), bit_count))
        compressed_stream.extend(encoded)
        data_offset += len(encoded)
        compressed_bytes_total += len(encoded)
        compressed_bits_total += bit_count
        raw_bytes_total += len(chunk)

    (huff_dir / "code_lengths.bin").write_bytes(bytes(lengths))
    (huff_dir / "chunks.bin").write_bytes(bytes(compressed_stream))

    with (huff_dir / "chunks.tbl").open("wb") as f:
        for offset, byte_count, bit_count in table_entries:
            f.write(offset.to_bytes(4, "big"))
            f.write(byte_count.to_bytes(2, "big"))
            f.write(bit_count.to_bytes(2, "big"))

    ratio = compressed_bytes_total / raw_bytes_total if raw_bytes_total else 1.0
    stats = {
        "raw_bytes": raw_bytes_total,
        "compressed_bytes": compressed_bytes_total,
        "compressed_bits": compressed_bits_total,
        "ratio": ratio,
        "chunks": len(chunks),
        "chunk_bytes": chunk_bytes,
        "chunks_per_frame": CHUNKS_PER_FRAME,
        "width": width,
        "height": height,
        "tiles_per_frame": tiles_per_frame,
    }
    (huff_dir / "stats.txt").write_text(
        "\n".join(f"{key}={value}" for key, value in stats.items()) + "\n"
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description="Quantize a movie preview to Genesis-style 15-color CRAM1 frames.")
    parser.add_argument("--input", default="movies/disc1/061.mp4")
    parser.add_argument("--output-dir", default="out/video/061_md15")
    parser.add_argument("--start", default="0")
    parser.add_argument("--duration", default="60")
    parser.add_argument("--fps", default="15")
    parser.add_argument("--crop", default="320:144:0:38")
    parser.add_argument("--scale-width", type=int, default=320)
    parser.add_argument("--scale-height", type=int)
    parser.add_argument("--mode", choices=["nodither", "ordered"], default="nodither")
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.output_dir)
    work_dir = out_dir / "work"
    raw_dir = work_dir / "raw"
    preview_dir = work_dir / "preview"
    idx_dir = out_dir / "idx"
    tile_dir = out_dir / "tile"
    pal_dir = out_dir / "pal"

    prepare_dir(out_dir)
    for path in (raw_dir, preview_dir, idx_dir, tile_dir, pal_dir):
        prepare_dir(path, clean=True)

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
            str(input_path),
            "-vf",
            f"crop={args.crop}"
            + (f",scale={args.scale_width}:{args.scale_height}:flags=lanczos" if args.scale_height else "")
            + f",fps={args.fps}",
            str(raw_dir / "%05d.png"),
        ]
    )

    raw_frames = sorted(raw_dir.glob("*.png"))
    if not raw_frames:
        raise RuntimeError("ffmpeg did not produce any frames")

    ordered = args.mode == "ordered"
    frame_tiles = []
    frame_width = None
    frame_height = None
    for i, frame in enumerate(raw_frames):
        stem = f"{i:05d}"
        tile_data, width, height = process_frame(
            frame,
            preview_dir / f"{stem}.png",
            idx_dir / f"{stem}.idx",
            tile_dir / f"{stem}.tile",
            pal_dir / f"{stem}.pal",
            ordered=ordered,
        )
        if frame_width is None:
            frame_width = width
            frame_height = height
        elif (width, height) != (frame_width, frame_height):
            raise ValueError("frame size changed during conversion")
        frame_tiles.append(tile_data)
        if (i + 1) % 25 == 0 or i + 1 == len(raw_frames):
            print(f"processed {i + 1}/{len(raw_frames)} frames")

    huff_stats = write_huffman_outputs(out_dir, frame_tiles, frame_width, frame_height)

    preview_mp4 = out_dir / f"061_crop_md15_{args.mode}.mp4"
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
            str(preview_dir / "%05d.png"),
            "-ss",
            args.start,
            "-t",
            args.duration,
            "-i",
            str(input_path),
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

    manifest = out_dir / "manifest.txt"
    manifest.write_text(
        "\n".join(
            [
                f"input={input_path}",
                f"duration={args.duration}",
                f"fps={args.fps}",
                f"crop={args.crop}",
                f"size={frame_width}x{frame_height}",
                "palette=CRAM1 15 colors plus reserved index 0",
                f"mode={args.mode}",
                f"frames={len(raw_frames)}",
                f"preview={preview_mp4.name}",
                "idx_format=packed 4bpp, high nibble first, indices 1..15",
                f"tile_format=VDP tile order, {huff_stats['tiles_per_frame']} tiles/frame, 32 bytes/tile",
                "pal_format=16 big-endian words, word0 reserved, words1..15 are Genesis 0BGR CRAM words",
                "huff_codebook=huff/code_lengths.bin, 256 canonical Huffman code lengths",
                "huff_table=huff/chunks.tbl, entries are u32 offset + u16 byte_count + u16 bit_count",
                "huff_data=huff/chunks.bin",
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
    print(f"wrote {idx_dir}")
    print(f"wrote {tile_dir}")
    print(f"wrote {pal_dir}")
    print(f"wrote {out_dir / 'huff'}")


if __name__ == "__main__":
    main()
