#!/usr/bin/env python3
"""Add public timeline links to a bilingual description and sync YouTube."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import time


EN_HEADING = "Full-resolution codec timeline (PNG):"
JA_HEADING = "高解像度コーデックタイムライン (PNG):"
EN_PROJECT = "Project source:\n"
JA_PROJECT = "プロジェクトのソース:\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="YouTube ID, .ytid, or uploaded video path")
    parser.add_argument("--timeline-receipt", type=Path, required=True)
    parser.add_argument("--description-file", type=Path, required=True)
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="update the description file without calling YouTube",
    )
    return parser.parse_args()


def load_youtube_helper():
    path = Path(os.environ.get(
        "YOUTUBE_HELPER",
        Path.home() / ".claude" / "skills" / "youtube" / "youtube.py",
    )).resolve()
    if not path.is_file():
        raise SystemExit(f"YouTube helper does not exist: {path}")
    spec = importlib.util.spec_from_file_location("codec_youtube_helper", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def replace_or_insert_block(
    description: str,
    *,
    heading: str,
    gist_label: str,
    project_marker: str,
    raw_url: str,
    gist_url: str,
) -> str:
    block = f"{heading}\n{raw_url}\n{gist_label}\n{gist_url}\n\n"
    pattern = re.compile(
        rf"{re.escape(heading)}\n[^\n]+\n{re.escape(gist_label)}\n[^\n]+\n\n"
    )
    if pattern.search(description):
        return pattern.sub(block, description, count=1)
    if project_marker not in description:
        raise SystemExit(
            f"description lacks insertion marker: {project_marker.rstrip()}"
        )
    return description.replace(project_marker, block + project_marker, 1)


def prepare_description(path: Path, receipt_path: Path) -> str:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    raw_url = str(receipt.get("raw_url", ""))
    gist_url = str(receipt.get("gist_url", ""))
    if not receipt.get("public") or not raw_url or not gist_url:
        raise SystemExit("timeline receipt does not identify a public Gist image")
    if any(char in raw_url + gist_url for char in "<>"):
        raise SystemExit("YouTube description URLs must not contain angle brackets")

    description = path.read_text(encoding="utf-8")
    description = replace_or_insert_block(
        description,
        heading=EN_HEADING,
        gist_label="Public Gist:",
        project_marker=EN_PROJECT,
        raw_url=raw_url,
        gist_url=gist_url,
    )
    description = replace_or_insert_block(
        description,
        heading=JA_HEADING,
        gist_label="公開Gist:",
        project_marker=JA_PROJECT,
        raw_url=raw_url,
        gist_url=gist_url,
    )
    if len(description) > 5000:
        raise SystemExit(f"YouTube description exceeds 5000 characters: {len(description)}")
    path.write_text(description, encoding="utf-8")
    return description


def sync_youtube(video: str, description: str) -> str:
    helper = load_youtube_helper()
    video_id = helper.resolve_video_id(video)
    youtube = helper.build_youtube(require_full_scope=True)
    current = youtube.videos().list(part="snippet,status", id=video_id).execute()
    items = current.get("items", [])
    if not items:
        raise SystemExit(f"YouTube video is not visible to the authorized account: {video_id}")
    snippet = items[0]["snippet"]
    status = items[0]["status"]
    snippet["description"] = description
    youtube.videos().update(
        part="snippet,status",
        body={"id": video_id, "snippet": snippet, "status": status},
    ).execute()
    # The read endpoint can briefly return the pre-update snippet. Retry a few
    # times, while continuing to compare every character except YouTube's
    # stripped final line ending.
    for attempt in range(5):
        verified = youtube.videos().list(part="snippet", id=video_id).execute()
        verified_items = verified.get("items", [])
        verified_description = (
            verified_items[0]["snippet"].get("description", "")
            if verified_items else ""
        )
        if verified_description.rstrip("\r\n") == description.rstrip("\r\n"):
            break
        if attempt < 4:
            time.sleep(1)
    else:
        raise SystemExit(f"YouTube description verification failed: {video_id}")
    return video_id


def main() -> None:
    args = parse_args()
    description = prepare_description(args.description_file, args.timeline_receipt)
    print(f"DESCRIPTION_FILE={args.description_file}")
    print(f"DESCRIPTION_LENGTH={len(description)}")
    if args.local_only:
        return
    video_id = sync_youtube(args.video, description)
    print("YOUTUBE_DESCRIPTION_VERIFIED=1")
    print(f"YTURL=https://youtu.be/{video_id}")


if __name__ == "__main__":
    main()
