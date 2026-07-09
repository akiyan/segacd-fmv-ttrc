#!/usr/bin/env python3
"""Shared default paths for CBR sim outputs and derived video artifacts."""
import os
import re
from pathlib import Path


def _clean_part(value):
    text = str(value).strip() or "unknown"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "unknown"


def audio_tag(kind=None):
    kind = (kind or os.environ.get("CBRSIM_AUDIO", "adpcm22")).strip()
    return {"pcm13": "pcm", "pcm": "pcm", "adpcm22": "adpcm22", "adpcm": "adpcm"}.get(
        kind, _clean_part(kind))


def sim_stem(src=None, mode=None, width=None, height=None, audio=None):
    src = src or os.environ.get("CBRSIM_SRC", "movies/disc1/061.mp4")
    mode = mode or os.environ.get("CBRSIM_MODE", "H32")
    width = int(width or os.environ.get("CBRSIM_W", "256"))
    height = int(height or os.environ.get("CBRSIM_H", "144"))
    return "%s_%s_%dx%d_%s" % (
        _clean_part(Path(src).stem),
        _clean_part(mode),
        width,
        height,
        audio_tag(audio),
    )


def sim_work_dir():
    explicit = os.environ.get("CBRSIM_OUT")
    if explicit:
        return Path(explicit)
    return Path("videos") / sim_stem() / "tmp"


def artifact_path(suffix, ext="mp4", sim_dir=None):
    sim_dir = Path(sim_dir) if sim_dir is not None else sim_work_dir()
    if sim_dir.name == "tmp" and sim_dir.parent.name:
        stem = sim_dir.parent.name
    else:
        stem = sim_stem()
    return Path("videos") / f"{stem}_{suffix}.{ext}"
