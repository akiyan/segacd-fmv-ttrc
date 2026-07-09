#!/usr/bin/env python3
import argparse
from pathlib import Path


def read_chunk_table(path):
    data = path.read_bytes()
    if len(data) % 8:
        raise ValueError("chunk table size is not a multiple of 8")
    entries = []
    for offset in range(0, len(data), 8):
        chunk_offset = int.from_bytes(data[offset : offset + 4], "big")
        byte_count = int.from_bytes(data[offset + 4 : offset + 6], "big")
        bit_count = int.from_bytes(data[offset + 6 : offset + 8], "big")
        entries.append((chunk_offset, byte_count, bit_count))
    return entries


def frame_video_sizes(entries, chunks_per_frame):
    if len(entries) % chunks_per_frame:
        raise ValueError("chunk count is not divisible by chunks per frame")
    sizes = []
    for i in range(0, len(entries), chunks_per_frame):
        sizes.append(sum(byte_count for _offset, byte_count, _bits in entries[i : i + chunks_per_frame]))
    return sizes


def simulate(frame_sizes, duration, audio_bytes, buffer_size, initial_fill, cd_bps, palette_bytes):
    frame_count = len(frame_sizes)
    frame_time = duration / frame_count
    audio_per_frame = audio_bytes / frame_count
    buffer = min(initial_fill, buffer_size)
    min_buffer = buffer
    max_buffer = buffer
    first_underflow = None

    for frame_index, video_size in enumerate(frame_sizes):
        buffer = min(buffer_size, buffer + cd_bps * frame_time)
        consume = video_size + palette_bytes + audio_per_frame
        buffer -= consume
        if buffer < min_buffer:
            min_buffer = buffer
        if buffer > max_buffer:
            max_buffer = buffer
        if buffer < 0 and first_underflow is None:
            first_underflow = frame_index

    return {
        "buffer_size": buffer_size,
        "initial_fill": initial_fill,
        "min_buffer": min_buffer,
        "max_buffer": max_buffer,
        "end_buffer": buffer,
        "first_underflow": first_underflow,
        "ok": first_underflow is None,
    }


def main():
    parser = argparse.ArgumentParser(description="Simulate Sega CD streaming ring buffer.")
    parser.add_argument("--root", default="out/video/061_full_15fps_288x112_nodither")
    parser.add_argument("--audio", default="out/video/audio_test/061_full_13k3_mono_adpcm.wav")
    parser.add_argument("--format", choices=["huff", "rle_huff"], default="huff")
    parser.add_argument("--duration", type=float, default=152.866667)
    parser.add_argument("--cd-bps", type=float, default=153600)
    parser.add_argument("--chunks-per-frame", type=int, default=4)
    parser.add_argument("--palette-bytes", type=int, default=32)
    parser.add_argument("--buffers-kib", default="256,384,416,512,608,768")
    args = parser.parse_args()

    root = Path(args.root)
    if args.format == "huff":
        entries = read_chunk_table(root / "huff" / "chunks.tbl")
        frame_sizes = frame_video_sizes(entries, args.chunks_per_frame)
    else:
        data = (root / "rle_huff" / "chunks.tbl").read_bytes()
        if len(data) % 12:
            raise ValueError("RLE chunk table size is not a multiple of 12")
        byte_counts = []
        for offset in range(0, len(data), 12):
            byte_counts.append(int.from_bytes(data[offset + 4 : offset + 6], "big"))
        if len(byte_counts) % args.chunks_per_frame:
            raise ValueError("chunk count is not divisible by chunks per frame")
        frame_sizes = [
            sum(byte_counts[i : i + args.chunks_per_frame])
            for i in range(0, len(byte_counts), args.chunks_per_frame)
        ]
    audio_bytes = Path(args.audio).stat().st_size
    total_video = sum(frame_sizes)
    total_palette = len(frame_sizes) * args.palette_bytes
    total = total_video + total_palette + audio_bytes
    cd_total = args.cd_bps * args.duration
    deficit = total - cd_total

    print(f"frames={len(frame_sizes)}")
    print(f"duration={args.duration:.6f}")
    print(f"cd_bps={args.cd_bps:.2f}")
    print(f"video_bytes={total_video}")
    print(f"palette_bytes={total_palette}")
    print(f"audio_bytes={audio_bytes}")
    print(f"total_bytes={total}")
    print(f"cd_total_bytes={cd_total:.0f}")
    print(f"deficit_bytes={deficit:.0f}")
    print()

    buffers = [int(value.strip()) * 1024 for value in args.buffers_kib.split(",") if value.strip()]
    for buffer_size in buffers:
        for initial_fill in (buffer_size, min(buffer_size, max(0, int(deficit + 64 * 1024)))):
            result = simulate(
                frame_sizes,
                args.duration,
                audio_bytes,
                buffer_size,
                initial_fill,
                args.cd_bps,
                args.palette_bytes,
            )
            first = result["first_underflow"]
            first_text = "none" if first is None else f"{first} ({first * args.duration / len(frame_sizes):.2f}s)"
            print(
                "buffer_kib={:.0f} initial_kib={:.1f} ok={} min_kib={:.1f} end_kib={:.1f} first_underflow={}".format(
                    buffer_size / 1024,
                    initial_fill / 1024,
                    result["ok"],
                    result["min_buffer"] / 1024,
                    result["end_buffer"] / 1024,
                    first_text,
                )
            )


if __name__ == "__main__":
    main()
