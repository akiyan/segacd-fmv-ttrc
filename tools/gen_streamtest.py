#!/usr/bin/env python3
"""Generate STREAM.DAT for the continuous-stream self-test.

The file is NUM_FRAMES "frames" of FRAME_SECTORS sectors each (sector-aligned).
Every byte of frame N holds the value (N & 0xFF), so the player can read any
byte of the currently displayed frame and recover its index. Played back in
order at 15 fps the recovered value counts up by 15 each second, which makes a
non-stop continuous read obvious at a glance (and a frozen / skipping read just
as obvious).
"""
import argparse

SECTOR_SIZE = 2048


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=256)
    ap.add_argument("--frame-sectors", type=int, default=5)
    ap.add_argument("--output", default="out/disc_streamtest/STREAM.DAT")
    args = ap.parse_args()

    frame_bytes = args.frame_sectors * SECTOR_SIZE
    with open(args.output, "wb") as f:
        for i in range(args.frames):
            f.write(bytes([i & 0xFF]) * frame_bytes)
    total = args.frames * args.frame_sectors
    print(f"wrote {args.output} ({args.frames} frames x {args.frame_sectors} "
          f"sectors = {total} sectors, {total*SECTOR_SIZE} bytes)")


if __name__ == "__main__":
    main()
