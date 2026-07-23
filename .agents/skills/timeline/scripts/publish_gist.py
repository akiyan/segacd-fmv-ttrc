#!/usr/bin/env python3
"""Publish one timeline PNG in a public, Git-backed GitHub Gist."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote


REPO = Path(__file__).resolve().parents[4]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--description", required=True)
    parser.add_argument("--gist-id", help="resume an already-created Gist")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, text=True, **kwargs)


def main() -> None:
    args = parse_args()
    requested_image = args.image.absolute()
    image = requested_image.resolve()
    if not image.is_file():
        raise SystemExit(f"timeline image does not exist: {image}")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    receipt_path = Path(str(image) + ".gist.json")
    receipt_alias = Path(str(requested_image) + ".gist.json")
    if receipt_path.exists() and not args.force:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("image_sha256") == digest:
            if receipt_alias != receipt_path:
                receipt_alias.write_text(
                    json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
            return

    if args.gist_id:
        fetched = run(
            ["gh", "api", f"gists/{args.gist_id}"], capture_output=True)
        gist = json.loads(fetched.stdout)
        if not gist.get("public"):
            raise SystemExit("refusing to resume a non-public Gist")
    else:
        payload = {
            "description": args.description,
            "public": True,
            "files": {
                "README.md": {
                    "content": (
                        f"# {args.description}\n\n"
                        f"Full-resolution diagnostic image: `{image.name}`\n"
                    )
                }
            },
        }
        created = run(
            ["gh", "api", "gists", "--method", "POST", "--input", "-"],
            input=json.dumps(payload), capture_output=True,
        )
        gist = json.loads(created.stdout)
    gist_id = gist["id"]
    owner = gist["owner"]["login"]
    with tempfile.TemporaryDirectory(prefix="codec-timeline-gist-") as tmp:
        checkout = Path(tmp) / gist_id
        run(["gh", "gist", "clone", gist_id, str(checkout)], capture_output=True)
        shutil.copy2(image, checkout / image.name)
        readme = checkout / "README.md"
        readme.write_text(
            f"# {args.description}\n\n"
            f"[Open the full-resolution PNG](./{image.name})\n\n"
            f"![Codec timeline](./{image.name})\n",
            encoding="utf-8",
        )
        author_name = run(
            ["git", "config", "user.name"], cwd=REPO,
            capture_output=True).stdout.strip()
        author_email = run(
            ["git", "config", "user.email"], cwd=REPO,
            capture_output=True).stdout.strip()
        if author_name != "akiyan" or not author_email or "@anthropic.com" in author_email:
            raise SystemExit("refusing non-owner or AI Gist commit attribution")
        run(["git", "config", "user.name", author_name], cwd=checkout)
        run(["git", "config", "user.email", author_email], cwd=checkout)
        run(["git", "add", "README.md", image.name], cwd=checkout)
        run(["git", "commit", "-m", "timeline画像を追加"], cwd=checkout,
            capture_output=True)
        run(["git", "push"], cwd=checkout, capture_output=True)
        commit = run(
            ["git", "rev-parse", "HEAD"], cwd=checkout,
            capture_output=True,
        ).stdout.strip()

    raw_url = (
        f"https://gist.githubusercontent.com/{owner}/{gist_id}/raw/"
        f"{commit}/{quote(image.name)}"
    )
    receipt = {
        "schema_version": 1,
        "gist_id": gist_id,
        "gist_url": gist["html_url"],
        "raw_url": raw_url,
        "filename": image.name,
        "image_sha256": digest,
        "commit": commit,
        "public": bool(gist["public"]),
    }
    receipt_text = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    receipt_path.write_text(receipt_text, encoding="utf-8")
    if receipt_alias != receipt_path:
        receipt_alias.write_text(receipt_text, encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
