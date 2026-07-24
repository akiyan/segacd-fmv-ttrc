#!/usr/bin/env python3
"""Render segmented CRAM palettes and exact displayed-colour usage."""

from __future__ import annotations

import argparse
import csv
import pickle
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


MD_LEVELS = np.array([0, 36, 72, 108, 144, 180, 216, 252], dtype=np.uint8)
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


def font(size: int, mono: bool = False, bold: bool = False) -> ImageFont.FreeTypeFont:
    family = "DejaVuSansMono" if mono else "DejaVuSans"
    suffix = "-Bold" if bold else ""
    path = Path(f"/usr/share/fonts/truetype/dejavu/{family}{suffix}.ttf")
    return ImageFont.truetype(str(path), size)


def rgb333_text(col) -> str:
    return "".join(str(int(v)) for v in col)


def rgb888(col) -> tuple[int, int, int]:
    return tuple(int(MD_LEVELS[int(v)]) for v in col)


def rgb888_text(col) -> str:
    return "#" + "".join(f"{v:02X}" for v in rgb888(col))


def cram_word(col) -> int:
    r, g, b = (int(v) for v in col)
    return (b << 9) | (g << 5) | (r << 1)


def fmt_time(seconds: float) -> str:
    value = int(round(seconds))
    minutes, sec = divmod(value, 60)
    return f"{minutes:02d}:{sec:02d}"


def load_log(path: Path):
    with path.open("rb") as src:
        log = pickle.load(src)
    palettes = np.asarray(log["seg_pals"], dtype=np.uint8)
    frame_seg = np.asarray(log["frame_seg"], dtype=np.int32)
    if palettes.ndim != 4 or palettes.shape[1:] != (4, 15, 3):
        raise SystemExit(f"unexpected segment palette shape: {palettes.shape}")
    if len(frame_seg) != len(log["frames"]):
        raise SystemExit("frame_seg and decision-frame counts differ")
    return log, palettes, frame_seg


def replay_usage(log, palettes, frame_seg):
    geom = tuple(log.get("geom", ()))
    cells = int(geom[2]) if len(geom) >= 3 else 1 + max(
        cell for frame in log["frames"] for cell, _pal, _key in frame)
    current_palette = np.zeros(cells, dtype=np.uint8)
    current_hist = np.zeros((cells, 16), dtype=np.int16)
    initialized = np.zeros(cells, dtype=bool)
    active = np.zeros((4, 16), dtype=np.int64)
    usage = np.zeros((len(palettes), 4, 16), dtype=np.int64)

    for frame, updates in enumerate(log["frames"]):
        for cell, pal, key in updates:
            if initialized[cell]:
                active[current_palette[cell]] -= current_hist[cell]
            hist = np.bincount(np.frombuffer(key, dtype=np.uint8), minlength=16)
            if hist[0]:
                raise SystemExit(f"frame {frame} cell {cell} uses reserved index 0")
            current_palette[cell] = pal
            current_hist[cell] = hist
            initialized[cell] = True
            active[pal] += hist
        if not initialized.all():
            raise SystemExit(f"not all display cells are initialized at frame {frame}")
        usage[int(frame_seg[frame])] += active
    return usage


def source_histogram(master_dir: Path | None, width: int, height: int):
    hist = np.zeros(512, dtype=np.int64)
    if master_dir is None:
        return hist, 0
    files = sorted(master_dir.glob("*.png"))
    threshold = np.tile(
        (BAYER8 + 0.5) / 64.0,
        (height // 8 + 1, width // 8 + 1),
    )[:height, :width]
    for path in files:
        image = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        if image.shape != (height, width, 3):
            raise SystemExit(f"{path}: shape {image.shape}, expected {(height, width, 3)}")
        scaled = image.astype(np.float32) * (7.0 / 255.0)
        base = np.floor(scaled)
        quant = np.clip(
            base + ((scaled - base) > threshold[..., None]), 0, 7
        ).astype(np.uint8)
        keys = (
            (quant[..., 0].astype(np.int16) << 6)
            | (quant[..., 1].astype(np.int16) << 3)
            | quant[..., 2]
        )
        hist += np.bincount(keys.ravel(), minlength=512)
    return hist, len(files)


def colour_key(col) -> int:
    r, g, b = (int(v) for v in col)
    return (r << 6) | (g << 3) | b


def key_colour(key: int) -> tuple[int, int, int]:
    return ((key >> 6) & 7, (key >> 3) & 7, key & 7)


def segment_ranges(frame_seg):
    starts = [0] + [
        frame for frame in range(1, len(frame_seg))
        if frame_seg[frame] != frame_seg[frame - 1]
    ]
    ends = starts[1:] + [len(frame_seg)]
    return list(zip(starts, ends))


def youtube_chapter_map(ranges, fps: float, upload_offset: float):
    separate = {}
    last = 0.0 if upload_offset > 0 else -10.0
    last_segment = None
    for segment, (start, _end) in enumerate(ranges):
        timestamp = upload_offset + start / fps
        if timestamp - last >= 10.0:
            separate[segment] = segment
            last = timestamp
            last_segment = segment
        else:
            separate[segment] = last_segment
    return separate


def collect_global(palettes, usage, source_hist):
    slot_count = defaultdict(int)
    pixel_count = defaultdict(int)
    locations = defaultdict(list)
    segments = defaultdict(set)
    for segment in range(len(palettes)):
        for pal in range(4):
            for index in range(1, 16):
                col = tuple(int(v) for v in palettes[segment, pal, index - 1])
                key = colour_key(col)
                slot_count[key] += 1
                locations[key].append(f"S{segment + 1:02d}/P{pal}/I{index:02d}")
                count = int(usage[segment, pal, index])
                if count:
                    pixel_count[key] += count
                    segments[key].add(segment + 1)
    keys = sorted(
        pixel_count,
        key=lambda key: (sum(key_colour(key)), key_colour(key)),
    )
    return keys, slot_count, pixel_count, locations, segments, source_hist


def draw_segment_chart(path, palettes, usage, frame_seg, fps, upload_offset):
    ranges = segment_ranges(frame_seg)
    chapter_map = youtube_chapter_map(ranges, fps, upload_offset)
    width = 2048
    top = 132
    block_h = 354
    height = top + block_h * len(ranges) + 24
    image = Image.new("RGB", (width, height), (15, 16, 21))
    draw = ImageDraw.Draw(image)
    title = font(34, bold=True)
    body = font(18)
    header = font(21, bold=True)
    mono = font(14, mono=True)
    mono_small = font(12, mono=True)

    draw.text((24, 18), "Bad Apple H32: current CRAM palettes by segment", font=title, fill=(245, 246, 250))
    draw.text(
        (24, 66),
        "P0-P3 are tile palette lines. Gold = referenced by displayed tiles; dim = table-only. Index 00 is transparent.",
        font=body,
        fill=(186, 191, 204),
    )
    draw.text(
        (24, 94),
        f"RGB333 777 is hardware maximum: CRAM 0x0EEE, digital #FCFCFC. "
        f"Upload chapter times include the {upload_offset:g} s startup offset.",
        font=body,
        fill=(186, 191, 204),
    )

    left = 122
    cell_w = 116
    row_h = 66
    for segment, (start, end) in enumerate(ranges):
        y = top + segment * block_h
        bg = (25, 27, 34) if segment % 2 == 0 else (21, 23, 29)
        draw.rectangle((12, y, width - 12, y + block_h - 8), fill=bg)
        content_time = start / fps
        upload_time = upload_offset + content_time
        kept = chapter_map[segment]
        chapter_note = (
            "YouTube chapter"
            if kept == segment
            else f"merged into S{kept + 1:02d} chapter"
        )
        draw.text(
            (24, y + 12),
            f"CRAM segment {segment + 1:02d}   F{start:04X}-F{end - 1:04X}   "
            f"content {fmt_time(content_time)}   upload {fmt_time(upload_time)}   {chapter_note}",
            font=header,
            fill=(234, 236, 242),
        )
        segment_total = max(1, int(usage[segment].sum()))
        for pal in range(4):
            row_y = y + 50 + pal * row_h
            row_total = int(usage[segment, pal].sum())
            draw.text((24, row_y + 9), f"P{pal}", font=header, fill=(234, 236, 242))
            draw.text(
                (24, row_y + 36),
                f"{row_total / segment_total:5.1%} px",
                font=mono_small,
                fill=(158, 164, 178),
            )
            full = np.vstack([np.zeros((1, 3), dtype=np.uint8), palettes[segment, pal]])
            for index, col in enumerate(full):
                x = left + index * cell_w
                used = index > 0 and int(usage[segment, pal, index]) > 0
                border = (244, 196, 73) if used else (61, 65, 77)
                fill = rgb888(col)
                draw.rectangle((x, row_y + 2, x + cell_w - 8, row_y + 34), fill=fill, outline=border, width=3 if used else 1)
                if not used:
                    draw.line((x + 3, row_y + 5, x + cell_w - 11, row_y + 31), fill=(72, 75, 86), width=1)
                tag = "T" if index == 0 else " "
                draw.text((x, row_y + 38), f"{index:02d}{tag} {rgb333_text(col)}", font=mono, fill=(218, 220, 227) if used else (117, 121, 133))
                draw.text((x, row_y + 53), rgb888_text(col), font=mono_small, fill=(174, 178, 190) if used else (92, 95, 105))
    image.save(path)


def draw_global_chart(path, global_data, source_frames):
    keys, slot_count, pixel_count, locations, segments, source_hist = global_data
    source_keys = set(int(key) for key in np.flatnonzero(source_hist))
    table_keys = set(slot_count)
    total = max(1, sum(pixel_count.values()))
    width = 2048
    top = 250
    row_h = 152
    height = top + row_h * len(keys) + 28
    image = Image.new("RGB", (width, height), (15, 16, 21))
    draw = ImageDraw.Draw(image)
    title = font(36, bold=True)
    body = font(20)
    row_title = font(24, mono=True, bold=True)
    mono = font(16, mono=True)

    neutral = sum(len(set(key_colour(key))) == 1 for key in keys)
    draw.text((28, 20), "Bad Apple H32: unique colours displayed across the full movie", font=title, fill=(245, 246, 250))
    draw.text(
        (28, 76),
        f"Displayed: {len(keys)} colours ({neutral} neutral gray + {len(keys) - neutral} off-gray).  "
        f"PALTAB union: {len(table_keys)}.  Bayer-quantised source union: {len(source_keys)} across {source_frames} frames.",
        font=body,
        fill=(205, 209, 220),
    )
    missing = sorted(source_keys - table_keys)
    missing_text = ", ".join(rgb333_text(key_colour(key)) for key in missing) or "none"
    draw.text(
        (28, 112),
        f"Source-only colours mapped away by palette quantisation: {missing_text}.",
        font=body,
        fill=(205, 209, 220),
    )
    draw.text(
        (28, 148),
        "RGB333 777 is the maximum Mega Drive colour: CRAM 0x0EEE and digital #FCFCFC. Counts are displayed pixel-frames.",
        font=body,
        fill=(205, 209, 220),
    )
    draw.text(
        (28, 188),
        "A single 15-colour line can hold the complete 10-colour Bayer source union for this encode.",
        font=font(23, bold=True),
        fill=(244, 196, 73),
    )

    for row, key in enumerate(keys):
        y = top + row * row_h
        col = key_colour(key)
        fill = rgb888(col)
        bg = (25, 27, 34) if row % 2 == 0 else (21, 23, 29)
        draw.rectangle((12, y, width - 12, y + row_h - 8), fill=bg)
        draw.rectangle((28, y + 22, 240, y + 118), fill=fill, outline=(104, 109, 124), width=2)
        draw.text(
            (274, y + 16),
            f"RGB333 {rgb333_text(col)}   CRAM 0x{cram_word(col):04X}   digital {rgb888_text(col)}",
            font=row_title,
            fill=(239, 241, 246),
        )
        count = pixel_count[key]
        source_count = int(source_hist[key])
        draw.text(
            (274, y + 55),
            f"displayed {count:,} ({count / total:.6%})   source-after-Bayer {source_count:,}   PALTAB slots {slot_count[key]}",
            font=mono,
            fill=(192, 197, 210),
        )
        segment_text = ",".join(str(value) for value in sorted(segments[key]))
        draw.text((274, y + 84), f"segments: {segment_text}", font=mono, fill=(166, 171, 184))
        loc = locations[key]
        shown = ", ".join(loc[:12]) + (f"  ... +{len(loc) - 12}" if len(loc) > 12 else "")
        draw.text((274, y + 111), shown, font=font(13, mono=True), fill=(139, 144, 157))
    image.save(path)


def write_tsvs(output, palettes, usage, frame_seg, fps, upload_offset, global_data):
    ranges = segment_ranges(frame_seg)
    with (output / "palette_slots.tsv").open("w", newline="") as dst:
        writer = csv.writer(dst, delimiter="\t", lineterminator="\n")
        writer.writerow([
            "segment", "start_frame", "end_frame", "content_start_s", "upload_start_s",
            "palette", "index", "reserved_transparent", "rgb333", "cram_word",
            "digital_rgb888", "displayed_pixel_frames",
        ])
        for segment, (start, end) in enumerate(ranges):
            for pal in range(4):
                full = np.vstack([np.zeros((1, 3), dtype=np.uint8), palettes[segment, pal]])
                for index, col in enumerate(full):
                    writer.writerow([
                        segment + 1, start, end - 1, f"{start / fps:.6f}",
                        f"{upload_offset + start / fps:.6f}", pal, index, int(index == 0),
                        rgb333_text(col), f"0x{cram_word(col):04X}", rgb888_text(col),
                        int(usage[segment, pal, index]),
                    ])

    keys, slot_count, pixel_count, locations, segments, source_hist = global_data
    total = max(1, sum(pixel_count.values()))
    with (output / "palette_global.tsv").open("w", newline="") as dst:
        writer = csv.writer(dst, delimiter="\t", lineterminator="\n")
        writer.writerow([
            "rgb333", "cram_word", "digital_rgb888", "displayed_pixel_frames",
            "displayed_fraction", "source_after_bayer_pixels", "paltab_slots",
            "segments", "locations",
        ])
        for key in keys:
            col = key_colour(key)
            writer.writerow([
                rgb333_text(col), f"0x{cram_word(col):04X}", rgb888_text(col),
                pixel_count[key], f"{pixel_count[key] / total:.12f}", int(source_hist[key]),
                slot_count[key], " ".join(map(str, sorted(segments[key]))),
                " ".join(locations[key]),
            ])


def write_summary(path, palettes, global_data, source_frames):
    keys, slot_count, pixel_count, _locations, segments, source_hist = global_data
    source_keys = set(int(key) for key in np.flatnonzero(source_hist))
    lines = [
        "Bad Apple H32 palette audit",
        f"CRAM segments: {len(palettes)}",
        f"Displayed unique colours: {len(keys)}",
        f"PALTAB unique colours: {len(slot_count)}",
        f"Bayer-quantised source unique colours: {len(source_keys)} ({source_frames} frames)",
        "",
        "Displayed colours:",
    ]
    total = max(1, sum(pixel_count.values()))
    for key in keys:
        col = key_colour(key)
        lines.append(
            f"  {rgb333_text(col)}  CRAM=0x{cram_word(col):04X}  "
            f"RGB={rgb888_text(col)}  displayed={pixel_count[key]} "
            f"({pixel_count[key] / total:.8%})  source={int(source_hist[key])}  "
            f"slots={slot_count[key]}  segments={','.join(map(str, sorted(segments[key])))}"
        )
    missing = sorted(source_keys - set(slot_count))
    lines.extend([
        "",
        "Source-only colours mapped away:",
        *(f"  {rgb333_text(key_colour(key))}  pixels={int(source_hist[key])}" for key in missing),
    ])
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decisions", type=Path)
    parser.add_argument("--master-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--upload-offset", type=float, default=0.0)
    args = parser.parse_args()

    log, palettes, frame_seg = load_log(args.decisions)
    fps = float(log.get("fps", 30.0))
    geom = tuple(log.get("geom", (32, 28, 896, 8)))
    width, height = int(geom[0] * geom[3]), int(geom[1] * geom[3])
    usage = replay_usage(log, palettes, frame_seg)
    source_hist, source_frames = source_histogram(args.master_dir, width, height)
    global_data = collect_global(palettes, usage, source_hist)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    draw_segment_chart(
        args.output_dir / "palette_by_segment.png", palettes, usage, frame_seg,
        fps, args.upload_offset,
    )
    draw_global_chart(
        args.output_dir / "palette_global.png", global_data, source_frames,
    )
    write_tsvs(
        args.output_dir, palettes, usage, frame_seg, fps, args.upload_offset,
        global_data,
    )
    write_summary(
        args.output_dir / "summary.txt", palettes, global_data, source_frames,
    )

    keys, slot_count, _pixel_count, _locations, _segments, source_hist = global_data
    print(f"segments={len(palettes)} displayed_unique={len(keys)} "
          f"paltab_unique={len(slot_count)} source_unique={np.count_nonzero(source_hist)}")
    for name in (
        "palette_by_segment.png", "palette_global.png", "palette_slots.tsv",
        "palette_global.tsv", "summary.txt",
    ):
        print(args.output_dir / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
