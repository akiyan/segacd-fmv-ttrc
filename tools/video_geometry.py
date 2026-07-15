#!/usr/bin/env python3
"""Source-to-Mega-Drive geometry helpers.

The two Mega Drive horizontal modes use different pixel widths.  Their dot
ratios compensate for that difference:

* H32: 256 pixels, HAR/PAR 8:7
* H40: 320 pixels, HAR/PAR 32:35

Both modes therefore describe the same visible NTSC aperture (64:49) when
224 lines are shown.  The default ``pad`` fit preserves every source pixel;
``crop`` is an explicit opt-in for inputs whose outer margins are known to be
black.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ModeGeometry:
    name: str
    har_num: int
    har_den: int
    default_width: int
    default_height: int

    @property
    def har(self) -> float:
        return self.har_num / self.har_den

    def display_aspect(self, width: int, height: int) -> float:
        return (width / height) * self.har


MODES = {
    "H32": ModeGeometry("H32", 8, 7, 256, 224),
    "H40": ModeGeometry("H40", 32, 35, 320, 224),
}


def mode_geometry(mode: str) -> ModeGeometry:
    key = mode.upper()
    if key not in MODES:
        raise ValueError(f"unsupported mode {mode!r}; choose H32 or H40")
    return MODES[key]


def parse_ratio(value: str | None) -> tuple[int, int]:
    if not value or value in {"N/A", "0:1"}:
        return 1, 1
    num, den = value.split(":", 1)
    return int(num), int(den)


def probe_source(src: str) -> tuple[int, int, int, int]:
    """Return coded width/height and the source sample-aspect ratio."""
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,sample_aspect_ratio", "-of", "json", src],
        text=True,
    )
    stream = json.loads(out)["streams"][0]
    sar_num, sar_den = parse_ratio(stream.get("sample_aspect_ratio"))
    return int(stream["width"]), int(stream["height"]), sar_num, sar_den


def probe_size(src: str) -> tuple[int, int]:
    """Return the first video stream's coded width and height."""
    width, height, _, _ = probe_source(src)
    return width, height


def _even(value: int) -> int:
    return max(2, value - (value & 1))


def center_crop(src_w: int, src_h: int, target_dar: float,
                src_sar: float = 1.0) -> tuple[int, int, int, int]:
    """Return an even centre crop matching target_dar after source SAR."""
    src_dar = (src_w / src_h) * src_sar
    if src_dar > target_dar + 1e-9:
        crop_h = src_h
        crop_w = _even(math.floor(src_h * target_dar / src_sar))
        crop_w = min(crop_w, src_w)
        return crop_w, crop_h, (src_w - crop_w) // 2, 0
    if src_dar < target_dar - 1e-9:
        crop_w = src_w
        crop_h = _even(math.floor(src_w * src_sar / target_dar))
        crop_h = min(crop_h, src_h)
        return crop_w, crop_h, 0, (src_h - crop_h) // 2
    return _even(src_w), _even(src_h), 0, 0


def geometry_plan(mode: str, width: int, height: int, src_w: int, src_h: int,
                  src_sar_num: int = 1, src_sar_den: int = 1,
                  fit: str = "pad") -> dict:
    g = mode_geometry(mode)
    if fit not in {"pad", "crop"}:
        raise ValueError("fit must be pad or crop")
    dar = g.display_aspect(width, height)
    src_sar = src_sar_num / src_sar_den
    cw, ch, cx, cy = center_crop(src_w, src_h, dar, src_sar)
    src_dar = (src_w / src_h) * src_sar
    if src_dar > dar:
        fit_w = width
        fit_h = _even(math.floor(width * g.har / src_dar))
    else:
        fit_h = height
        fit_w = _even(math.floor(height * src_dar / g.har))
    return {
        "mode": g.name,
        "har": f"{g.har_num}:{g.har_den}",
        "src": [src_w, src_h],
        "src_sar": f"{src_sar_num}:{src_sar_den}",
        "crop": [cw, ch, cx, cy],
        "fit": fit,
        "fit_size": [fit_w, fit_h],
        "out": [width, height],
        "display_aspect": dar,
    }


def source_filter(mode: str, width: int, height: int, src_w: int, src_h: int,
                  *, src_sar_num: int = 1, src_sar_den: int = 1,
                  fit: str = "pad",
                  denoise: bool = True) -> str:
    """Build the canonical ffmpeg filter, accounting for source SAR."""
    p = geometry_plan(mode, width, height, src_w, src_h,
                      src_sar_num, src_sar_den, fit)
    cw, ch, cx, cy = p["crop"]
    fw, fh = p["fit_size"]
    if fit == "crop":
        vf = ["setsar=1", f"crop={cw}:{ch}:{cx}:{cy}"]
        iw, ih = cw, ch
    else:
        # Normalize source SAR, then scale the complete source to the largest
        # raster with the target mode's displayed aspect. Padding never drops
        # source pixels; crop remains an explicit opt-in.
        vf = ["setsar=1"]
        iw, ih = fw, fh
    if denoise:
        # Keep the source's displayed shape during the denoise pass. Scaling
        # to the MD raster here would apply the HAR twice.
        vf += [f"scale={iw * 2}:{ih * 2}:flags=lanczos",
               "hqdn3d=6:6:8:8", "gblur=sigma=1.6"]
    vf.append(f"scale={iw}:{ih}:flags=lanczos")
    if fit == "pad":
        vf.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
    return ",".join(vf)


def raw_filter(mode: str, width: int, height: int, src_w: int, src_h: int,
               *, src_sar_num: int = 1, src_sar_den: int = 1,
               fit: str = "pad") -> str:
    return source_filter(mode, width, height, src_w, src_h,
                         src_sar_num=src_sar_num, src_sar_den=src_sar_den,
                         fit=fit,
                         denoise=False)


def _main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", required=True)
    ap.add_argument("--mode", choices=sorted(MODES), required=True)
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--fit", choices=("pad", "crop"), default="pad")
    ap.add_argument("--source-sar", help="override input SAR, e.g. 25:27 for a 576x400 file intended as 4:3")
    args = ap.parse_args()
    g = mode_geometry(args.mode)
    w = args.width or g.default_width
    h = args.height or g.default_height
    sw, sh, sar_num, sar_den = probe_source(args.src)
    if args.source_sar:
        sar_num, sar_den = parse_ratio(args.source_sar)
    p = geometry_plan(args.mode, w, h, sw, sh, sar_num, sar_den, args.fit)
    p["master_vf"] = source_filter(args.mode, w, h, sw, sh,
                                    src_sar_num=sar_num, src_sar_den=sar_den,
                                    fit=args.fit)
    p["raw_vf"] = raw_filter(args.mode, w, h, sw, sh,
                              src_sar_num=sar_num, src_sar_den=sar_den,
                              fit=args.fit)
    print(json.dumps(p, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _main()
