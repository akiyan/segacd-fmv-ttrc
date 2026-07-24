#!/usr/bin/env python3
"""Compare edge-preserving source dedither filters on selected Sonic frames."""

from __future__ import annotations

import argparse
import csv
import math
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


FILTERS = {
    "source": "setsar=1",
    "old_hqdn3d_gblur": (
        "setsar=1,scale=iw*2:ih*2:flags=lanczos,"
        "hqdn3d=6:6:8:8,gblur=sigma=1.6,"
        "scale=iw/2:ih/2:flags=lanczos"
    ),
    "bilateral": "setsar=1,bilateral=sigmaS=1.0:sigmaR=0.06:planes=15",
    "guided": "setsar=1,guided=radius=1:eps=0.002:planes=15",
    "nlmeans_s2_5": "setsar=1,nlmeans=s=2.5:p=3:r=5",
    "nlmeans_s3_5": "setsar=1,nlmeans=s=3.5:p=3:r=5",
}

LUMA = np.array([0.299, 0.587, 0.114], np.float64)
BLUR3 = np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.float64) / 16.0
SOBEL_X = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], np.float64) / 8.0
SOBEL_Y = SOBEL_X.T
CHECKER = np.array([[1, -1, 0], [-1, 1, 0], [0, 0, 0]], np.float64) / 4.0


def parse_frames(value: str) -> list[int]:
    frames = [int(item.strip(), 0) for item in value.split(",") if item.strip()]
    if not frames or min(frames) < 0 or len(set(frames)) != len(frames):
        raise argparse.ArgumentTypeError(
            "frames must be unique non-negative decimal or 0x-prefixed values"
        )
    return frames


def parse_crop(value: str) -> tuple[int, int, int, int]:
    try:
        x, y, width, height = (int(item) for item in value.split(":"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("crop must be x:y:width:height") from exc
    if min(x, y) < 0 or min(width, height) <= 0:
        raise argparse.ArgumentTypeError("crop position must be non-negative and size positive")
    return x, y, width, height


def convolve3(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    padded = np.pad(image, ((1, 1), (1, 1)), mode="reflect")
    result = np.zeros_like(image, dtype=np.float64)
    for row in range(3):
        for column in range(3):
            result += (
                padded[row : row + image.shape[0], column : column + image.shape[1]]
                * kernel[row, column]
            )
    return result


def metrics(source: np.ndarray, filtered: np.ndarray) -> dict[str, float]:
    source_luma = source @ LUMA
    filtered_luma = filtered @ LUMA
    source_base = convolve3(source_luma, BLUR3)
    filtered_base = convolve3(filtered_luma, BLUR3)
    source_gradient = np.hypot(
        convolve3(source_base, SOBEL_X), convolve3(source_base, SOBEL_Y)
    )
    filtered_gradient = np.hypot(
        convolve3(filtered_base, SOBEL_X), convolve3(filtered_base, SOBEL_Y)
    )
    flat = source_gradient < 4.0
    edges = source_gradient > 12.0
    checker_energy = np.abs(convolve3(filtered_luma, CHECKER))[flat].mean()
    edge_ratio = filtered_gradient[edges].mean() / source_gradient[edges].mean()
    edge_correlation = np.corrcoef(
        source_gradient[edges], filtered_gradient[edges]
    )[0, 1]
    return {
        "change_mae": float(np.abs(filtered - source).mean()),
        "flat_checker": float(checker_energy),
        "edge_ratio": float(edge_ratio),
        "edge_correlation": float(edge_correlation),
    }


def extract(source: Path, output: Path, frames: list[int]) -> dict[str, float]:
    expression = "+".join(f"eq(n\\,{frame})" for frame in frames)
    elapsed = {}
    for name, source_filter in FILTERS.items():
        destination = output / name
        destination.mkdir()
        started = time.perf_counter()
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-vf",
                f"{source_filter},select='{expression}'",
                "-fps_mode",
                "passthrough",
                str(destination / "%05d.png"),
            ],
            check=True,
        )
        elapsed[name] = time.perf_counter() - started
        actual = len(list(destination.glob("*.png")))
        if actual != len(frames):
            raise RuntimeError(
                f"{name}: extracted {actual} frames, expected {len(frames)}"
            )
    return elapsed


def render_sheets(
    output: Path,
    frames: list[int],
    crop: tuple[int, int, int, int],
    scale: int,
) -> None:
    names = list(FILTERS)
    columns = 3
    rows = math.ceil(len(names) / columns)
    x, y, width, height = crop
    cell_width = width * scale
    cell_height = height * scale
    label_height = 24
    for frame_index, frame in enumerate(frames, 1):
        canvas = Image.new(
            "RGB",
            (columns * cell_width, rows * (cell_height + label_height)),
            (32, 32, 32),
        )
        draw = ImageDraw.Draw(canvas)
        for candidate_index, name in enumerate(names):
            image = Image.open(
                output / name / f"{frame_index:05d}.png"
            ).convert("RGB")
            if x + width > image.width or y + height > image.height:
                raise ValueError(
                    f"crop {crop} lies outside {image.width}x{image.height}"
                )
            image = image.crop((x, y, x + width, y + height)).resize(
                (cell_width, cell_height), Image.Resampling.NEAREST
            )
            column = candidate_index % columns
            row = candidate_index // columns
            left = column * cell_width
            top = row * (cell_height + label_height)
            canvas.paste(image, (left, top))
            draw.text((left + 4, top + cell_height + 4), name, fill="white")
        canvas.save(output / f"frame_{frame:04x}.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--frames", type=parse_frames, default=parse_frames("0x12b,0x232"))
    parser.add_argument("--crop", type=parse_crop, default=parse_crop("0:0:128:96"))
    parser.add_argument("--scale", type=int, default=4)
    args = parser.parse_args()

    if args.output.exists():
        raise SystemExit(f"output already exists: {args.output}")
    if args.scale <= 0:
        raise SystemExit("--scale must be positive")
    args.output.mkdir(parents=True)

    elapsed = extract(args.source, args.output, args.frames)
    rows = []
    for frame_index, frame in enumerate(args.frames, 1):
        source = np.asarray(
            Image.open(
                args.output / "source" / f"{frame_index:05d}.png"
            ).convert("RGB"),
            dtype=np.float64,
        )
        for name in FILTERS:
            filtered = np.asarray(
                Image.open(
                    args.output / name / f"{frame_index:05d}.png"
                ).convert("RGB"),
                dtype=np.float64,
            )
            rows.append(
                {
                    "frame": frame,
                    "frame_hex": f"{frame:04X}",
                    "filter": name,
                    "seconds": elapsed[name],
                    **metrics(source, filtered),
                }
            )

    with (args.output / "metrics.tsv").open("w", encoding="utf-8", newline="") as dst:
        writer = csv.DictWriter(
            dst,
            fieldnames=[
                "frame",
                "frame_hex",
                "filter",
                "seconds",
                "change_mae",
                "flat_checker",
                "edge_ratio",
                "edge_correlation",
            ],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    render_sheets(args.output, args.frames, args.crop, args.scale)
    print(args.output / "metrics.tsv")
    for frame in args.frames:
        print(args.output / f"frame_{frame:04x}.png")


if __name__ == "__main__":
    main()
