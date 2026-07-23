#!/usr/bin/env python3
"""Quantize a movie to a single global Genesis palette and write a preview mp4.

Colours are snapped to the Genesis 512-colour space (3 bits/channel) and then
reduced to one global palette of N colours (weighted k-means over every frame).
Unlike quantize_md_video.py (per-frame 15-colour CRAM banks) this produces one
fixed palette for the whole clip - useful for previewing how a small frame looks
in a limited global palette.

Example:
  python3 tools/quantize_global_preview.py \
    --input movies/disc1/061.mp4 --scale-width 144 --scale-height 112 \
    --fps 15 --colors 58 \
    --output out/video/061_144x112_58col/061_144x112_58col.mp4
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quantize_md_video import (  # noqa: E402
    MD_LEVELS,
    prepare_dir,
    rgb333_to_rgb888,
    rgb888_to_rgb333,
    run,
    weighted_palette,
    nearest_indices,
)


def extract_frames(args, work_dir):
    prepare_dir(work_dir, clean=True)
    vf = (
        f"crop={args.crop},"
        f"scale={args.scale_width}:{args.scale_height}:flags=lanczos,"
        f"fps={args.fps}"
    )
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(args.start), "-t", str(args.duration),
        "-i", args.input, "-vf", vf,
        str(work_dir / "%05d.png"),
    ])
    frames = sorted(work_dir.glob("*.png"))
    if not frames:
        raise RuntimeError("ffmpeg produced no frames")
    return frames


def global_histogram(frames):
    """Count every RGB333 colour over all frames -> (unique_rgb333, counts)."""
    counts = np.zeros(512, dtype=np.int64)
    for f in frames:
        rgb = np.asarray(Image.open(f).convert("RGB"))
        r, g, b = (rgb888_to_rgb333(rgb)[..., i].astype(np.int64) for i in range(3))
        keys = (r << 6) | (g << 3) | b
        counts += np.bincount(keys.reshape(-1), minlength=512)
    used = np.nonzero(counts)[0]
    rgb333 = np.stack([(used >> 6) & 7, (used >> 3) & 7, used & 7], axis=1).astype(np.uint8)
    return rgb333, counts[used]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default="movies/disc1/061.mp4")
    p.add_argument("--output", default="out/video/061_144x112_58col/061_144x112_58col.mp4")
    p.add_argument("--start", default="0")
    p.add_argument("--duration", default="152.866667")
    p.add_argument("--fps", default="15")
    p.add_argument("--crop", default="320:144:0:38")
    p.add_argument("--scale-width", type=int, default=144)
    p.add_argument("--scale-height", type=int, default=112)
    p.add_argument("--colors", type=int, default=58)
    p.add_argument("--audio", action="store_true",
                   help="mux the input audio degraded through <rate>Hz mono target format")
    p.add_argument("--audio-rate", default="22050")
    p.add_argument("--audio-bitrate", default="64k")
    p.add_argument("--keep-work", action="store_true")
    args = p.parse_args()

    out_path = Path(args.output)
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work" / "src"
    prev = out_dir / "work" / "preview"

    print("extracting frames ...")
    frames = extract_frames(args, work)
    print(f"  {len(frames)} frames @ {args.scale_width}x{args.scale_height} {args.fps}fps")

    print("building global palette ...")
    unique_rgb333, counts = global_histogram(frames)
    print(f"  {len(unique_rgb333)} distinct MD colours in source")
    palette = weighted_palette(unique_rgb333, counts, colors=args.colors, iterations=24)
    actual = len(np.unique(palette, axis=0))
    print(f"  palette: {actual} unique MD colours (requested {args.colors})")

    print("mapping frames ...")
    prepare_dir(prev, clean=True)
    full_palette = np.vstack([np.zeros((1, 3), dtype=np.uint8), palette])
    for i, f in enumerate(frames):
        rgb = np.asarray(Image.open(f).convert("RGB"))
        rgb333 = rgb888_to_rgb333(rgb)
        idx = nearest_indices(rgb333, palette)
        out = rgb333_to_rgb888(full_palette[idx])
        Image.fromarray(out, "RGB").save(prev / f"{i:05d}.png")

    print("encoding mp4 ...")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", str(args.fps), "-i", str(prev / "%05d.png"),
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(out_path),
    ])

    if args.audio:
        # Degrade the source audio through <rate>Hz mono 4-bit IMA ADPCM so the
        # preview carries the target sound, then store it as AAC for mp4 player
        # compatibility (the container can't hold IMA ADPCM cleanly). The .wav is
        # the raw ADPCM-degraded reference and is kept alongside the mp4.
        acodec, alabel, atag = "adpcm_ima_wav", "4-bit IMA ADPCM", "adpcm"
        print(f"adding audio ({args.audio_rate}Hz mono {alabel} -> AAC) ...")
        wav = out_dir / f"audio_{args.audio_rate}_mono_{atag}.wav"
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(args.start), "-t", str(args.duration), "-i", args.input,
            "-vn", "-ac", "1", "-ar", str(args.audio_rate),
            "-c:a", acodec, str(wav),
        ])
        final = out_dir / (out_path.stem + ".withaudio.mp4")
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(out_path), "-i", str(wav),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", args.audio_bitrate,
            "-movflags", "+faststart", "-shortest", str(final),
        ])
        final.replace(out_path)
        print(f"  reference wav: {wav}")

    if not args.keep_work:
        prepare_dir(out_dir / "work", clean=True)
        (out_dir / "work").rmdir()

    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
