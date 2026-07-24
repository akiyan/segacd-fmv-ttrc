#!/usr/bin/env python3
"""Stack matching codec and HUD timelines on one verified frame axis."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SCRIPT = Path(__file__).resolve()
REPO = SCRIPT.parents[4]
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))
import tmpfs_workspace  # noqa: E402


BG = (12, 12, 14)
TEXT = (230, 230, 234)
DIM = (158, 160, 169)
WARN = (246, 190, 72)
FAIL = (244, 87, 87)
SEPARATOR = (62, 64, 72)
HEADER_HEIGHT = 185


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)


def fmt_frame(frame_index: int, frames: int) -> str:
    width = max(3, len(f"{max(frames - 1, 0):X}"))
    return f"f0x{frame_index:0{width}X}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("timeline", type=Path)
    parser.add_argument("hudline", type=Path)
    parser.add_argument("--timeline-layout", type=Path)
    parser.add_argument("--hudline-layout", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--gap", type=int, default=0)
    return parser.parse_args()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_receipt(path: Path, kind: str) -> dict:
    if not path.is_file():
        raise SystemExit(f"{kind} layout receipt does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("kind") != kind:
        raise SystemExit(
            f"expected {kind} layout receipt, got {data.get('kind')!r}")
    return data


def validate_axis(timeline: dict, hudline: dict) -> None:
    for key in (
        "frames", "pixels_per_frame", "plot_left", "plot_width", "frame_x",
    ):
        if timeline.get(key) != hudline.get(key):
            raise SystemExit(
                f"frame-axis mismatch for {key}: "
                f"{timeline.get(key)!r} != {hudline.get(key)!r}")
    # TSV time deltas can preserve sub-microsecond decimal rounding that the
    # HUD's integer cadence does not. This tolerance is far below one frame
    # over a whole movie and does not permit a different playback cadence.
    if abs(float(timeline["fps"]) - float(hudline["fps"])) > 1e-6:
        raise SystemExit(
            f"frame-axis mismatch for fps: "
            f"{timeline['fps']} != {hudline['fps']}")


def validate_image(path: Path, receipt: dict, kind: str) -> None:
    recorded = receipt.get("image_sha256")
    if recorded and digest(path) != recorded:
        raise SystemExit(f"{kind} image hash does not match its layout receipt")


def main() -> None:
    args = parse_args()
    if args.gap < 0:
        raise SystemExit("gap must not be negative")
    timeline_path = args.timeline.resolve()
    hudline_path = args.hudline.resolve()
    timeline_layout_path = (
        args.timeline_layout.resolve()
        if args.timeline_layout
        else Path(str(args.timeline.absolute()) + ".json")
    )
    hudline_layout_path = (
        args.hudline_layout.resolve()
        if args.hudline_layout
        else Path(str(args.hudline.absolute()) + ".json")
    )
    timeline = load_receipt(timeline_layout_path, "timeline")
    hudline = load_receipt(hudline_layout_path, "hudline")
    validate_axis(timeline, hudline)
    validate_image(timeline_path, timeline, "timeline")
    validate_image(hudline_path, hudline, "hudline")

    with Image.open(timeline_path) as source:
        timeline_image = source.convert("RGB")
    with Image.open(hudline_path) as source:
        hudline_image = source.convert("RGB")
    if timeline_image.width != hudline_image.width:
        raise SystemExit(
            f"image width mismatch: "
            f"{timeline_image.width} != {hudline_image.width}")
    expected_width = (
        int(timeline["plot_left"]) + int(timeline["plot_width"]) + 45
    )
    if timeline_image.width != expected_width:
        raise SystemExit(
            f"image width {timeline_image.width} does not match frame layout "
            f"{expected_width}")

    timeline_plot_top = int(timeline["plot_top"])
    hudline_plot_top = int(hudline["plot_top"])
    if not 0 < timeline_plot_top < timeline_image.height:
        raise SystemExit("invalid timeline plot_top")
    if not 0 < hudline_plot_top < hudline_image.height:
        raise SystemExit("invalid hudline plot_top")
    timeline_rows = timeline.get("rows") or ()
    if not timeline_rows:
        raise SystemExit("timeline receipt lacks row geometry")
    timeline_plot_bottom = max(
        int(row["top"]) + int(row["height"])
        for row in timeline_rows
    )
    if not timeline_plot_top < timeline_plot_bottom <= timeline_image.height:
        raise SystemExit("invalid timeline row geometry")
    # The HUD panel owns the one shared horizontal scale and footer.  Crop the
    # codec panel at its final data row so its duplicate scale and explanation
    # disappear, then join the HUD panel directly below on the same x axis.
    upper = timeline_image.crop(
        (0, timeline_plot_top, timeline_image.width, timeline_plot_bottom))
    lower = hudline_image.crop(
        (0, hudline_plot_top, hudline_image.width, hudline_image.height))

    width = timeline_image.width
    height = HEADER_HEIGHT + upper.height + args.gap + lower.height
    combined = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(combined)
    title = timeline.get("label") or timeline_path.stem
    state = str(
        hudline.get(
            "status",
            "PASS" if hudline["gate_pass"] else "FAIL",
        )
    )
    if state == "WARN":
        state = "WARNING"
    state_color = {
        "PASS": DIM,
        "WARNING": WARN,
        "FAIL": FAIL,
    }.get(state, FAIL)
    draw.text((24, 16), title, fill=TEXT, font=font(36))
    draw.text(
        (width - 24, 18), state, fill=state_color, font=font(34), anchor="ra")
    draw.text(
        (24, 64),
        (
            f"Shared frame axis | {timeline['frames']} frames | "
            f"{float(hudline['fps']):g} fps | "
            f"{timeline['pixels_per_frame']} px/frame | "
            f"x = {timeline['plot_left']} + frame * "
            f"{timeline['pixels_per_frame']}"
        ),
        fill=DIM,
        font=font(20),
    )
    evaluation_end_raw = timeline.get("evaluation_end_frame")
    evaluation_end = (
        int(timeline["frames"])
        if evaluation_end_raw is None
        else int(evaluation_end_raw)
    )
    profile = (
        timeline.get("config_sha256")
        or hudline.get("profile_sha256")
        or ""
    )
    draw.text(
        (24, 96),
        (
            f"Codec /timeline | cold cap {timeline.get('cold_cap_tiles', '?')} "
            f"| EVAL {fmt_frame(0, int(timeline['frames']))}-"
            f"{fmt_frame(evaluation_end - 1, int(timeline['frames']))} "
            f"| profile {profile[:10]}"
        ),
        fill=DIM,
        font=font(19),
    )
    maxima = hudline["gate_maxima"]
    limits = hudline["gate_limits"]
    gate_text = "  ".join(
        f"{key} {int(maxima[key])}/{int(limits[key])}"
        for key in ("S", "D", "R", "C", "M", "J")
    )
    vblank_text = ""
    if hudline.get("display_vblank_warning_supported"):
        vblank_text = (
            f" | VB warn "
            f"{float(hudline['display_vblank_warning_rate_percent']):.2f}%/"
            f"{int(hudline['display_vblank_warning_count'])}/"
            f"{int(hudline['display_vblank_evaluated_total'])}"
        )
    draw.text(
        (24, 127),
        (
            f"Playback /hudline | gate maxima / limits  {gate_text} | "
            f"J normal {int(hudline['jitter_normal_kib'])} KiB | "
            f"OCR {float(hudline.get('ocr_confidence_min', 0.0)):.3f}"
            f"{vblank_text}"
        ),
        fill=state_color,
        font=font(19),
    )
    draw.line(
        (0, HEADER_HEIGHT - 1, width - 1, HEADER_HEIGHT - 1),
        fill=SEPARATOR,
        width=2,
    )

    upper_top = HEADER_HEIGHT
    combined.paste(upper, (0, upper_top))
    lower_top = upper_top + upper.height + args.gap
    combined.paste(lower, (0, lower_top))
    if args.gap:
        draw = ImageDraw.Draw(combined)
        y = upper_top + upper.height + args.gap // 2
        draw.line((0, y, width - 1, y), fill=SEPARATOR, width=2)

    output = (
        args.output
        or REPO / "videos" / f"{timeline_path.stem}_mixline.png"
    ).absolute()
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
                kind="mixline-png",
                key=f"{timeline_path.stem}-{hudline_path.stem}",
                required_bytes=max(width * height * 3, 128 * 1024 ** 2),
            )
        combined.save(actual_output, optimize=True)
        if lease is not None:
            tmpfs_workspace.publish_alias(output, actual_output)
    finally:
        if lease is not None:
            lease.release()

    receipt = {
        "schema_version": 1,
        "kind": "mixline",
        "image": str(output),
        "image_sha256": digest(output.resolve()),
        "timeline_image": str(timeline_path),
        "timeline_image_sha256": digest(timeline_path),
        "timeline_layout": str(timeline_layout_path),
        "timeline_layout_sha256": digest(timeline_layout_path),
        "hudline_image": str(hudline_path),
        "hudline_image_sha256": digest(hudline_path),
        "hudline_layout": str(hudline_layout_path),
        "hudline_layout_sha256": digest(hudline_layout_path),
        "frames": int(timeline["frames"]),
        "fps": float(timeline["fps"]),
        "status": state,
        "gate_pass": bool(hudline["gate_pass"]),
        "gate_status": str(hudline.get("gate_status", state)),
        "pixels_per_frame": int(timeline["pixels_per_frame"]),
        "plot_left": int(timeline["plot_left"]),
        "plot_width": int(timeline["plot_width"]),
        "frame_x": timeline["frame_x"],
        "gap": args.gap,
        "header_height": HEADER_HEIGHT,
        "panels": [
            {
                "kind": "header",
                "top": 0,
                "height": HEADER_HEIGHT,
            },
            {
                "kind": "timeline",
                "top": upper_top,
                "height": upper.height,
                "source_crop_top": timeline_plot_top,
                "source_crop_bottom": timeline_plot_bottom,
            },
            {"kind": "hudline", "top": lower_top, "height": lower.height},
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
