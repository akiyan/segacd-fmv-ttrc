#!/usr/bin/env python3
"""Report exact DEBUG HUD frames that exceed an upload-gate limit."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path


SCRIPT = Path(__file__).resolve()
REPO = SCRIPT.parents[4]
TOOLS = REPO / "tools"
sys.path.insert(0, str(TOOLS))
import av_config  # noqa: E402


GATE_COLUMNS = {
    "S": "slip",
    "D": "desync",
    "R": "resync",
    "C": "cd_wait",
    "M": "main_vblank_wait",
    "J": "prgbuf_jitter_peak_kib",
}

# S/D/R are cumulative counters and J is a sticky maximum.  Once they exceed
# a limit, repeating the same value on every later frame is state, not another
# event.  Report the transition into each new over-limit value instead.
TRANSITION_FIELDS = {"S", "D", "R", "J"}

HUD_COLUMNS = (
    ("P", "palette", 2),
    ("S", "slip", 2),
    ("D", "desync", 2),
    ("R", "resync", 2),
    ("L", "lead_256b", 2),
    ("C", "cd_wait", 2),
    ("W", "sub_wait_lines", 2),
    ("M", "main_vblank_wait", 2),
    ("A", "sub_adpcm_decode_units", 2),
    ("U", "main_pattern_ticks", 4),
    ("N", "cold_runs_low8", 2),
    ("J", "prgbuf_jitter_peak_kib", 2),
    ("V", "flip_vcounter", 2),
    ("O", "flip_interval_excess_ticks", 2),
    ("E", "pass2_entry_q4", 2),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tsv", type=Path)
    parser.add_argument("--gate-json", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="also write the Markdown report to this path",
    )
    args = parser.parse_args()
    if args.tsv.suffix.lower() != ".tsv":
        parser.error("HUD input must use the .tsv extension")
    return args


def as_int(row: dict[str, str], column: str) -> int:
    text = row.get(column, "").strip()
    if not text:
        return 0
    return int(round(float(text)))


def hex_value(value: int, digits: int = 2) -> str:
    return f"0x{value:0{max(digits, len(f'{value:X}'))}X}"


def load_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source, delimiter="\t")
        fields = list(reader.fieldnames or ())
        required = {
            "loop",
            "frame",
            "capture_first",
            *GATE_COLUMNS.values(),
        }
        missing = required - set(fields)
        if missing:
            raise SystemExit(f"HUD TSV lacks columns: {sorted(missing)}")
        rows = [row for row in reader if as_int(row, "loop") == 0]
    if not rows:
        raise SystemExit("HUD TSV contains no first-loop rows")
    frames = [as_int(row, "frame") for row in rows]
    if frames != list(range(len(rows))):
        raise SystemExit("first-loop HUD frames must be contiguous and start at zero")
    return rows, fields


def load_gate(path: Path) -> dict:
    gate = json.loads(path.read_text(encoding="utf-8"))
    for key in (
        "content_fps",
        "expected_frames",
        "observed_first_loop_frames",
        "limits",
        "maxima",
    ):
        if key not in gate:
            raise SystemExit(f"gate JSON lacks {key}")
    for key in GATE_COLUMNS:
        if key not in gate["limits"] or key not in gate["maxima"]:
            raise SystemExit(f"gate JSON lacks {key} limit or maximum")
    return gate


def validate(rows: list[dict[str, str]], gate: dict) -> None:
    frames = len(rows)
    if int(gate["expected_frames"]) != frames:
        raise SystemExit("gate expected_frames does not match HUD TSV")
    if int(gate["observed_first_loop_frames"]) != frames:
        raise SystemExit("gate observed_first_loop_frames does not match HUD TSV")
    for field, column in GATE_COLUMNS.items():
        actual = max(as_int(row, column) for row in rows)
        if actual != int(gate["maxima"][field]):
            raise SystemExit(
                f"gate {field} maximum {gate['maxima'][field]} "
                f"does not match TSV maximum {actual}"
            )


def displayed_vblanks(rows: list[dict[str, str]]) -> list[int | None]:
    starts = [as_int(row, "capture_first") for row in rows]
    values: list[int | None] = [None] * len(rows)
    for index in range(len(rows) - 1):
        span = starts[index + 1] - starts[index]
        if span <= 0:
            raise SystemExit("capture_first must increase between content frames")
        values[index] = span
    return values


def cadence_normal_vblanks(content_fps: float) -> int | None:
    """Return an exact integer cadence, or None for rates such as 24 fps."""
    expected = av_config.vsync_n_for_fps(content_fps)
    integer_rate = av_config.NTSC_VSYNC / expected
    playback_rate = av_config.playback_fps_for_content(content_fps)
    if math.isclose(playback_rate, integer_rate, abs_tol=1e-9):
        return expected
    return None


def gate_overage_events(
    rows: list[dict[str, str]],
    gate: dict,
) -> dict[int, list[tuple[str, str, int, str, int]]]:
    events: dict[int, list[tuple[str, str, int, str, int]]] = defaultdict(list)
    for field, column in GATE_COLUMNS.items():
        limit = int(gate["limits"][field])
        if int(gate["maxima"][field]) <= limit:
            continue
        previous: int | None = None
        for index, row in enumerate(rows):
            value = as_int(row, column)
            over = value > limit
            changed = previous is None or value != previous
            if over and (field not in TRANSITION_FIELDS or changed):
                severity = "WARNING" if field == "C" else "FAIL"
                events[index].append((severity, field, value, ">", limit))
            previous = value
    return dict(sorted(events.items()))


def render_markdown(
    rows: list[dict[str, str]],
    fields: list[str],
    gate: dict,
) -> str:
    vblanks = displayed_vblanks(rows)
    events = gate_overage_events(rows, gate)
    normal_vblanks = cadence_normal_vblanks(float(gate["content_fps"]))

    evaluated_vblanks = sum(value is not None for value in vblanks)
    vblank_warning_count = (
        sum(
            value is not None and value != normal_vblanks
            for value in vblanks
        )
        if normal_vblanks is not None
        else None
    )
    gate_warning_count = sum(
        severity == "WARNING"
        for triggers in events.values()
        for severity, *_rest in triggers
    )
    gate_failure_count = sum(
        severity == "FAIL"
        for triggers in events.values()
        for severity, *_rest in triggers
    )
    available_hud = [
        (name, column, digits)
        for name, column, digits in HUD_COLUMNS
        if column in fields
    ]
    summary = []
    if normal_vblanks is None:
        summary.append(
            f"VBLANK warning rule: deferred for "
            f"{float(gate['content_fps']):g} fps."
        )
    else:
        warning_rate = (
            100.0 * vblank_warning_count / evaluated_vblanks
            if evaluated_vblanks
            else 0.0
        )
        summary.append(
            f"VBLANK warning rate / count / total: "
            f"{warning_rate:.2f}% / {vblank_warning_count} / "
            f"{evaluated_vblanks} "
            f"(normal {hex_value(normal_vblanks)})."
        )
    summary.append(
        f"Gate conditions: {gate_warning_count} warning, "
        f"{gate_failure_count} failure."
    )
    if not events:
        return "\n".join(summary) + "\n"
    headers = [
        "F",
        "Warning / over limit",
        "VBLANK",
        *[name for name, _, _ in available_hud],
    ]
    lines = [
        *summary,
        "",
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    frame_digits = max(4, len(f"{len(rows) - 1:X}"))
    for index, triggers in events.items():
        row = rows[index]
        trigger_text = ", ".join(
            f"{severity} {name} {hex_value(value)} {operator} "
            f"{hex_value(reference)}"
            for severity, name, value, operator, reference in triggers
        )
        vblank = (
            "—"
            if vblanks[index] is None
            else hex_value(int(vblanks[index]))
        )
        values = [
            hex_value(as_int(row, column), digits)
            for _name, column, digits in available_hud
        ]
        lines.append(
            "| "
            + " | ".join(
                [
                    hex_value(as_int(row, "frame"), frame_digits),
                    trigger_text,
                    vblank,
                    *values,
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append(
        "VBLANK is derived from the next frame's capture start; "
        "the terminal hold is not reported."
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    rows, fields = load_rows(args.tsv)
    gate = load_gate(args.gate_json)
    validate(rows, gate)
    report = render_markdown(rows, fields, gate)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
