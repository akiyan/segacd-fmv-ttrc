#!/usr/bin/env python3
import argparse
from pathlib import Path


PATCH_OFFSET = 0x1010
ORIGINAL = bytes.fromhex("46071c15")
PATCH_START_ONLY = bytes.fromhex("1e3c0080")
PATCH_ALL_BUTTONS = bytes.fromhex("1e3c00ff")


def main():
    parser = argparse.ArgumentParser(description="Patch JP Sega CD BIOS for headless auto-start checks.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--mode",
        choices=("start-only", "all-buttons"),
        default="all-buttons",
        help="Forced controller state. all-buttons is more reliable for BIOS menu bypass.",
    )
    args = parser.parse_args()

    data = bytearray(args.input.read_bytes())
    current = bytes(data[PATCH_OFFSET : PATCH_OFFSET + len(ORIGINAL)])
    if current != ORIGINAL:
        raise SystemExit(
            f"unexpected bytes at {PATCH_OFFSET:#x}: {current.hex()} expected {ORIGINAL.hex()}"
        )

    patch = PATCH_START_ONLY if args.mode == "start-only" else PATCH_ALL_BUTTONS
    data[PATCH_OFFSET : PATCH_OFFSET + len(patch)] = patch
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)

    print(f"patched {args.output} at {PATCH_OFFSET:#x}: {ORIGINAL.hex()} -> {patch.hex()}")


if __name__ == "__main__":
    main()
