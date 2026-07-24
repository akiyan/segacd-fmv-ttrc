#!/usr/bin/env python3
"""Render a frame-aligned whole-movie timeline from DEBUG HUD OCR data."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


SCRIPT = Path(__file__).resolve()
REPO = SCRIPT.parents[4]
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))
import av_config  # noqa: E402
import layout_preview as layout  # noqa: E402
import tmpfs_workspace  # noqa: E402


BG = (12, 12, 14)
PANEL = (20, 21, 25)
TEXT = (230, 230, 234)
DIM = (158, 160, 169)
GRID = (52, 54, 62)
MAJOR_GRID = (75, 77, 88)
WARN = (246, 190, 72)
FAIL = (244, 87, 87)
NORMAL = (136, 139, 149)
PASS_GUIDE = (84, 204, 139)
LIMIT = (248, 174, 58)
NORMAL_LIMIT = (246, 220, 96)


@dataclass(frozen=True)
class RowSpec:
    key: str
    label: str
    unit: str
    maximum: float
    gate_key: str | None = None
    eight_bit_scale: bool = False
    normal_value: float | None = None
    height: int = 46
    show_unit: bool = True
    show_zero: bool = False


GATE_COLUMN = {
    "S": "slip",
    "D": "desync",
    "R": "resync",
    "C": "cd_wait",
    "M": "main_vblank_wait",
    "J": "prgbuf_jitter_peak_kib",
}

HEX_COLUMNS = {
    "flip_vcounter",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tsv", type=Path)
    parser.add_argument("--gate-json", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label", default="")
    parser.add_argument("--pixels-per-frame", type=int)
    args = parser.parse_args()
    if args.tsv.suffix.lower() != ".tsv":
        parser.error("HUD input must use the .tsv extension")
    return args


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(layout.FONT, size)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_value(key: str, text: str) -> float:
    value = text.strip()
    if not value:
        return 0.0
    if key in HEX_COLUMNS:
        return float(int(value, 16))
    return float(value)


def load_tsv(path: Path) -> tuple[list[dict[str, str]], dict[str, np.ndarray], list[str]]:
    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        fields = list(reader.fieldnames or ())
        missing = {"loop", "frame", *GATE_COLUMN.values()} - set(fields)
        if missing:
            raise SystemExit(f"HUD TSV lacks columns: {sorted(missing)}")
        all_rows = list(reader)
    rows = [row for row in all_rows if int(row["loop"]) == 0]
    if not rows:
        raise SystemExit("HUD TSV contains no first-loop rows")
    frames = np.asarray([int(row["frame"]) for row in rows], np.int64)
    if not np.array_equal(frames, np.arange(len(rows))):
        raise SystemExit("first-loop HUD frames must be contiguous and start at zero")
    arrays: dict[str, np.ndarray] = {"frame": frames}
    for key in fields:
        if key in {"loop", "frame", "frame_hex", "lead_hex", "r_transition"}:
            continue
        try:
            arrays[key] = np.asarray(
                [parse_value(key, row[key]) for row in rows], np.float64)
        except ValueError:
            continue
    return rows, arrays, fields


def load_gate(path: Path) -> dict:
    gate = json.loads(path.read_text(encoding="utf-8"))
    for key in (
        "expected_frames", "observed_first_loop_frames", "maxima", "limits",
        "pass", "recording", "recording_size", "recording_mtime_ns",
        "content_fps", "profile_sha256",
    ):
        if key not in gate:
            raise SystemExit(f"gate JSON lacks {key}")
    status = gate.get("status", "PASS" if gate["pass"] else "FAIL")
    if status not in {"PASS", "WARNING", "FAIL"}:
        raise SystemExit(f"invalid gate status: {status!r}")
    if bool(gate["pass"]) != (status != "FAIL"):
        raise SystemExit("gate pass boolean disagrees with status")
    gate["status"] = status
    return gate


def validate(
    tsv_path: Path,
    gate_path: Path,
    config_path: Path | None,
    rows: list[dict[str, str]],
    data: dict[str, np.ndarray],
    gate: dict,
) -> None:
    frames = len(rows)
    if int(gate["expected_frames"]) != frames:
        raise SystemExit(
            f"gate expected {gate['expected_frames']} frames, TSV has {frames}")
    if int(gate["observed_first_loop_frames"]) != frames:
        raise SystemExit(
            "gate observed_first_loop_frames does not match the HUD TSV")
    for gate_key, column in GATE_COLUMN.items():
        actual = int(round(float(data[column].max(initial=0))))
        recorded = int(gate["maxima"][gate_key])
        if actual != recorded:
            raise SystemExit(
                f"gate {gate_key} maximum {recorded} != TSV maximum {actual}")
    if config_path is not None:
        if digest(config_path) != str(gate["profile_sha256"]):
            raise SystemExit("profile SHA does not match gate JSON")
    recording = Path(str(gate["recording"]))
    if recording.exists():
        stat = recording.stat()
        if stat.st_size != int(gate["recording_size"]):
            raise SystemExit("recording size does not match gate JSON")
        if stat.st_mtime_ns != int(gate["recording_mtime_ns"]):
            raise SystemExit("recording mtime does not match gate JSON")
    if not tsv_path.is_file() or not gate_path.is_file():
        raise SystemExit("HUD inputs disappeared while validating")


def derive_display_vblanks(
    data: dict[str, np.ndarray],
    content_fps: float,
) -> tuple[np.ndarray, int | None]:
    """Return displayed VBlanks per content frame from capture-frame starts.

    F is published atomically with the displayed movie frame.  The distance
    between consecutive first sightings therefore measures how many captured
    VBlanks the earlier content frame remained visible.  The final movie frame
    has no next F transition and is deliberately left unknown so the terminal
    hold cannot contaminate cadence statistics.
    """
    starts = data.get("capture_first")
    if starts is None:
        raise SystemExit("HUD TSV lacks capture_first for displayed VBlank timing")
    if len(starts) != len(data["frame"]):
        raise SystemExit("capture_first length does not match HUD frame count")
    displayed = np.full(len(starts), np.nan, dtype=np.float64)
    if len(starts) > 1:
        spans = np.diff(starts.astype(np.int64))
        if np.any(spans <= 0):
            raise SystemExit("capture_first must increase between content frames")
        displayed[:-1] = spans
    expected = av_config.vsync_n_for_fps(content_fps)
    integer_rate = av_config.NTSC_VSYNC / expected
    playback_rate = av_config.playback_fps_for_content(content_fps)
    normal = (
        expected
        if math.isclose(playback_rate, integer_rate, abs_tol=1e-9)
        else None
    )
    return displayed, normal


def row_specs(
    data: dict[str, np.ndarray],
    gate: dict,
    display_vblank_expected: int | None,
) -> list[RowSpec]:
    limits = {key: float(value) for key, value in gate["limits"].items()}
    lead_max = max(
        0x68,
        int(math.ceil(float(data.get("lead_256b", np.zeros(1)).max(initial=0)))))
    display_vblanks = data["display_vblanks"]
    finite_vblanks = display_vblanks[np.isfinite(display_vblanks)]
    capacity_floor = (
        float(display_vblank_expected * 2)
        if display_vblank_expected is not None
        else 1.0
    )
    display_vblank_max = float(
        finite_vblanks.max(initial=capacity_floor))
    rows = [
        RowSpec(
            "display_vblanks",
            "VBLANK",
            "display VBlanks/frame",
            max(capacity_floor, display_vblank_max),
            normal_value=(
                float(display_vblank_expected)
                if display_vblank_expected is not None
                else None
            ),
        ),
        RowSpec(
            "slip", "S  SLIP", "cumulative", max(1, limits["S"]),
            "S", height=23, show_unit=False,
        ),
        RowSpec(
            "desync", "D  DESYNC", "cumulative", max(1, limits["D"]),
            "D", height=23, show_unit=False,
        ),
        RowSpec(
            "resync", "R  RESYNC", "cumulative", max(1, limits["R"]),
            "R", height=23, show_unit=False,
        ),
        RowSpec("cd_wait", "C  CD WAIT", "sectors/frame",
                max(1, limits["C"], float(data["cd_wait"].max(initial=0))),
                "C"),
        RowSpec("main_vblank_wait", "M  MAIN WAIT", "VBlanks/frame",
                max(1, limits["M"], float(data["main_vblank_wait"].max(initial=0))),
                "M"),
        RowSpec("prgbuf_jitter_peak_kib", "J  PRG JITTER", "sticky peak KiB",
                max(
                    1,
                    limits["J"],
                    float(data["prgbuf_jitter_peak_kib"].max(initial=0)),
                    float(gate.get("jitter_headroom_kib", 0))),
                "J"),
        RowSpec("lead_256b", "L  AUDIO LEAD", "256-byte units", lead_max),
        RowSpec("sub_wait_lines", "W  SUB HANDOFF", "approx. scanlines", 255,
                eight_bit_scale=True),
        RowSpec("sub_adpcm_decode_units", "A  ADPCM", "0.12288 ms units", 255,
                eight_bit_scale=True),
        RowSpec("main_pattern_ms", "U  PATTERN", "ms, 12-bit wrap", 125.83),
        RowSpec("cold_runs_low8", "N  COLD RUNS", "runs, low byte", 255,
                eight_bit_scale=True, show_zero=False),
    ]
    optional = (
        ("flip_vcounter", "V  FLIP", "VDP line, frame F-1"),
        ("flip_interval_excess_ticks", "O  INTERVAL", "30.72 us ticks, F-1"),
        ("pass2_entry_q4", "E  PASS2 ENTRY", "4 ticks"),
    )
    for key, label, unit in optional:
        if key in data:
            rows.append(
                RowSpec(
                    key,
                    label,
                    unit,
                    255,
                    eight_bit_scale=True,
                    show_zero=False,
                )
            )
    return rows


def value_color(value: float, spec: RowSpec, gate: dict) -> tuple[int, int, int]:
    if spec.normal_value is not None:
        if value <= 0:
            return FAIL
        if math.isclose(value, spec.normal_value, abs_tol=0.01):
            return NORMAL
        return WARN
    if spec.gate_key is None:
        return NORMAL
    limit = float(gate["limits"][spec.gate_key])
    if value > limit:
        return FAIL
    return NORMAL


def fmt_hex(value: float) -> str:
    return f"0x{max(0, int(round(value))):02X}"


def fmt_frame(frame_index: int, frames: int) -> str:
    width = max(3, len(f"{max(frames - 1, 0):X}"))
    return f"f0x{frame_index:0{width}X}"


def draw_scale(
    draw: ImageDraw.ImageDraw,
    left: int,
    right: int,
    top: int,
    height: int,
    maximum: float,
) -> None:
    compact = height <= 23
    scale_font = font(10 if compact else 13)
    edge_offset = 6 if compact else 9
    y = top
    draw.line((left, y, right, y), fill=GRID, width=1)
    draw.text(
        (left - 10, y + edge_offset),
        fmt_hex(maximum),
        fill=(185, 187, 196),
        font=scale_font,
        anchor="rm",
    )


def draw_rows(
    image: Image.Image,
    data: dict[str, np.ndarray],
    specs: list[RowSpec],
    gate: dict,
    *,
    left: int,
    top: int,
    ppf: int,
) -> int:
    draw = ImageDraw.Draw(image)
    frames = len(data["frame"])
    plot_width = frames * ppf
    right = left + plot_width - 1
    label_font = font(16)
    unit_font = font(13)
    y0 = top
    for row_index, spec in enumerate(specs):
        row_height = spec.height
        y1 = y0 + row_height - 1
        draw.rectangle((left, y0, right, y1), fill=PANEL, outline=GRID)
        values = data.get(spec.key, np.zeros(frames))
        for frame_index, raw in enumerate(values):
            value = float(raw)
            if not math.isfinite(value):
                continue
            value = max(0.0, value)
            x0 = left + frame_index * ppf
            x1 = x0 + ppf - 1
            clipped = min(value, spec.maximum)
            bar = int(round((row_height - 1) * clipped / max(spec.maximum, 1e-9)))
            if bar:
                draw.rectangle(
                    (x0, y1 - bar + 1, x1, y1),
                    fill=value_color(value, spec, gate),
                )
            if value > spec.maximum:
                draw.line((x0, y0, x1, y0), fill=FAIL, width=2)
        draw_scale(
            draw,
            left,
            right,
            y0,
            row_height,
            spec.maximum,
        )
        draw.text(
            (18, y0 + (1 if row_height <= 23 else 3)),
            spec.label,
            fill=TEXT,
            font=label_font,
        )
        if spec.show_unit:
            draw.text((18, y0 + 25), spec.unit, fill=DIM, font=unit_font)

        if spec.gate_key is not None:
            limit = float(gate["limits"][spec.gate_key])
            limit_y = y1 - int(round(
                (row_height - 1) * min(limit, spec.maximum)
                / max(spec.maximum, 1e-9)))
            draw.line((left, limit_y, right, limit_y), fill=LIMIT, width=2)
            draw.text(
                (right - 4, limit_y - 2),
                f"limit {fmt_hex(limit)}",
                fill=LIMIT,
                font=font(13),
                anchor="rb",
            )
            if spec.gate_key == "J":
                normal = float(gate.get("jitter_headroom_kib", 0))
                normal_y = y1 - int(round(
                    (row_height - 1) * min(normal, spec.maximum)
                    / max(spec.maximum, 1e-9)))
                draw.line(
                    (left, normal_y, right, normal_y),
                    fill=NORMAL_LIMIT,
                    width=1,
                )
                draw.text(
                    (right - 4, normal_y - 2),
                    f"normal {fmt_hex(normal)}",
                    fill=NORMAL_LIMIT,
                    font=font(13),
                    anchor="rb",
                )

        if spec.normal_value is not None:
            normal = float(spec.normal_value)
            normal_y = y1 - int(round(
                (row_height - 1) * min(normal, spec.maximum)
                / max(spec.maximum, 1e-9)))
            draw.line(
                (left, normal_y, right, normal_y),
                fill=PASS_GUIDE,
                width=2,
            )
            draw.text(
                (right - 4, normal_y - 2),
                f"normal {fmt_hex(normal)}",
                fill=PASS_GUIDE,
                font=font(13),
                anchor="rb",
            )
        y0 += row_height

    bottom = y0
    fps = float(gate["content_fps"])
    duration = frames / fps
    for second in range(0, math.ceil(duration) + 1):
        frame_index = min(round(second * fps), frames - 1)
        x = left + frame_index * ppf
        major = second % 5 == 0
        draw.line(
            (x, top, x, bottom),
            fill=MAJOR_GRID if major else (38, 40, 47),
            width=1,
        )
        if major:
            draw.text((x + 3, bottom + 9), f"{second}s", fill=DIM, font=font(18))
            draw.text(
                (x + 3, bottom + 32),
                fmt_frame(frame_index, frames),
                fill=(115, 117, 126),
                font=font(15),
            )

    palette = data.get("palette", np.zeros(frames)).astype(np.int64)
    switches = np.flatnonzero(np.r_[False, palette[1:] != palette[:-1]])
    for frame_index in switches:
        x = left + int(frame_index) * ppf
        draw.line((x, top, x, bottom), fill=(130, 132, 145), width=2)
        draw.text(
            (x + 3, top + 3),
            f"P{palette[frame_index]:02d}",
            fill=TEXT,
            font=font(14),
        )
    return bottom


def main() -> None:
    args = parse_args()
    tsv_path = args.tsv.resolve()
    gate_path = args.gate_json.resolve()
    config_path = args.config.resolve() if args.config else None
    rows, data, _fields = load_tsv(tsv_path)
    gate = load_gate(gate_path)
    validate(tsv_path, gate_path, config_path, rows, data, gate)
    display_vblanks, display_vblank_expected = derive_display_vblanks(
        data,
        float(gate["content_fps"]),
    )
    data["display_vblanks"] = display_vblanks
    finite_display_vblanks = display_vblanks[np.isfinite(display_vblanks)]
    display_vblank_warning_count = (
        int(np.count_nonzero(
            finite_display_vblanks != display_vblank_expected
        ))
        if display_vblank_expected is not None
        else None
    )
    display_vblank_total = int(len(finite_display_vblanks))
    display_vblank_warning_rate = (
        100.0 * display_vblank_warning_count / display_vblank_total
        if display_vblank_warning_count is not None and display_vblank_total
        else None
    )

    frames = len(rows)
    ppf = args.pixels_per_frame or max(1, min(4, math.ceil(4200 / frames)))
    if ppf <= 0:
        raise SystemExit("pixels per frame must be positive")
    specs = row_specs(data, gate, display_vblank_expected)
    left = 220
    timeline_top = 172
    plot_width = frames * ppf
    width = left + plot_width + 45
    height = timeline_top + sum(spec.height for spec in specs) + 82
    output = (
        args.output
        or REPO / "videos" / f"{tsv_path.stem}_hudline.png"
    ).absolute()

    image = Image.new("RGBA", (width, height), BG + (255,))
    draw = ImageDraw.Draw(image)
    title = args.label or tsv_path.stem
    state = str(gate["status"])
    if state == "PASS" and display_vblank_warning_count:
        state = "WARNING"
    state_color = {
        "PASS": DIM,
        "WARNING": WARN,
        "FAIL": FAIL,
    }[state]
    maxima = gate["maxima"]
    limits = gate["limits"]
    max_text = "  ".join(
        f"{key} {int(maxima[key])}/{int(limits[key])}"
        for key in ("S", "D", "R", "C", "M", "J")
    )
    confidence = data.get("confidence", np.ones(frames))
    sample_count = data.get("sample_count", np.ones(frames))
    cadence_text = (
        f"VBlank warn {display_vblank_warning_rate:.2f}% / "
        f"{display_vblank_warning_count} / {display_vblank_total}, "
        if display_vblank_expected is not None
        else "VBlank warning rule deferred, "
    )
    draw.text((24, 16), title, fill=TEXT, font=font(36))
    draw.text((width - 24, 18), state, fill=state_color, font=font(34), anchor="ra")
    draw.text(
        (24, 64),
        (
            f"Complete DEBUG HUD timeline | {frames} frames | "
            f"{float(gate['content_fps']):g} fps | {ppf} px/frame"
        ),
        fill=DIM,
        font=font(20),
    )
    draw.text(
        (24, 96),
        f"Gate maxima / limits  {max_text}",
        fill=DIM,
        font=font(19),
    )
    draw.text(
        (24, 127),
        (
            f"J normal interval {int(gate.get('jitter_headroom_kib', 0))} KiB; "
            f"{cadence_text}"
            f"range {int(finite_display_vblanks.min())}-"
            f"{int(finite_display_vblanks.max())}; "
            f"OCR confidence min {confidence.min():.3f}; "
            f"samples {int(sample_count.sum())}; "
            f"profile {str(gate['profile_sha256'])[:10]}"
        ),
        fill=DIM,
        font=font(17),
    )
    bottom = draw_rows(
        image,
        data,
        specs,
        gate,
        left=left,
        top=timeline_top,
        ppf=ppf,
    )
    draw = ImageDraw.Draw(image)
    draw.text(
        (left, bottom + 64),
        (
            "VBLANK is derived from consecutive F capture starts; the terminal hold is excluded. "
            "F is the x-axis. V/O on frame F describe the flip for F-1; "
            "E belongs to F. Orange lines are gate limits; J also shows the yellow normal jitter interval."
        ),
        fill=DIM,
        font=font(16),
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    lease = None
    actual_output = output
    try:
        videos = (REPO / "videos").absolute()
        try:
            output.relative_to(videos)
        except ValueError:
            pass
        else:
            actual_output, lease = tmpfs_workspace.allocate_file(
                output,
                kind="hudline-png",
                key=f"{tsv_path.stem}-{digest(tsv_path)[:10]}",
                required_bytes=max(width * height * 4, 128 * 1024 ** 2),
            )
        image.convert("RGB").save(actual_output, optimize=True)
        if lease is not None:
            tmpfs_workspace.publish_alias(output, actual_output)
    finally:
        if lease is not None:
            lease.release()

    receipt = {
        "schema_version": 2,
        "kind": "hudline",
        "label": title,
        "image": str(output),
        "image_sha256": digest(output.resolve()),
        "tsv": str(tsv_path),
        "tsv_sha256": digest(tsv_path),
        "gate_json": str(gate_path),
        "gate_json_sha256": digest(gate_path),
        "recording": gate["recording"],
        "recording_size": gate["recording_size"],
        "recording_mtime_ns": gate["recording_mtime_ns"],
        "profile_sha256": gate["profile_sha256"],
        "frames": frames,
        "fps": float(gate["content_fps"]),
        "pixels_per_frame": ppf,
        "plot_left": left,
        "plot_top": timeline_top,
        "plot_width": plot_width,
        "base_row_height": 46,
        "frame_x": "plot_left + frame * pixels_per_frame",
        "frame_label_format": "f0xHEX",
        "gate_pass": bool(gate["pass"]),
        "gate_status": str(gate["status"]),
        "status": state,
        "gate_maxima": maxima,
        "gate_limits": limits,
        "jitter_normal_kib": int(gate.get("jitter_headroom_kib", 0)),
        "display_vblank_expected": display_vblank_expected,
        "display_vblank_warning_count": display_vblank_warning_count,
        "display_vblank_warning_rate_percent": display_vblank_warning_rate,
        "display_vblank_evaluated_total": display_vblank_total,
        "display_vblank_warning_supported": (
            display_vblank_expected is not None
        ),
        "display_vblank_min": int(finite_display_vblanks.min()),
        "display_vblank_max": int(finite_display_vblanks.max()),
        "display_vblank_average": float(finite_display_vblanks.mean()),
        "display_vblank_terminal_hold_excluded": True,
        "ocr_confidence_min": float(confidence.min()),
        "ocr_sample_count": int(sample_count.sum()),
        "rows": [
            {
                "key": spec.key,
                "label": spec.label,
                "unit": spec.unit,
                "maximum": spec.maximum,
                "gate_key": spec.gate_key,
                "eight_bit_scale": spec.eight_bit_scale,
                "normal_value": spec.normal_value,
                "height": spec.height,
                "show_unit": spec.show_unit,
                "show_zero": spec.show_zero,
            }
            for spec in specs
        ],
    }
    receipt_path = Path(str(output) + ".json")
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output)
    print(receipt_path)


if __name__ == "__main__":
    main()
