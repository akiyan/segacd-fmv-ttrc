#!/usr/bin/env python3
"""Convert a 160x80 movie frame (20x10 8x8 tiles) into Mega-CD Graphics-ASIC
stamp data + stamp map for a 2x upscale to 320x160.

A 16x16 stamp = 2x2 source tiles, so the 20x10 source becomes a 10x5 grid of
stamps (50 stamps). Stamp number 0 is reserved as blank; stamps are numbered
1..50 row-major. The stamp map is a 16x16 entry grid (256x256px, 16x16 stamps).

Cell order inside a 16x16 stamp (4 x 8x8 cells, each standard Genesis tile of
32 bytes): default column-major TL, BL, TR, BR (override with --cell-order).
Verify against megadev/hardware; adjust if the ASIC output is scrambled.

Outputs (raw):
  <out>/stamp_data.bin  (51*128 bytes: stamp 0 blank + 50 stamps)
  <out>/stamp_map.bin   (16*16*2 = 512 bytes, row-major words)
"""
import argparse
from pathlib import Path

SRC_W_TILES = 20
SRC_H_TILES = 10
STAMP_W = 10          # 16x16 stamps across (20 tiles / 2)
STAMP_H = 5           # 16x16 stamps down  (10 tiles / 2)
MAP_DIM = 16          # 256x256px stamp map = 16x16 stamps
TILE_BYTES = 32
STAMP_BYTES = 128     # 16x16 4bpp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiles", required=True, help="raw 200x32B 8x8 tile data")
    ap.add_argument("--out", required=True, help="output dir")
    ap.add_argument("--cell-order", default="TL,BL,TR,BR",
                    help="order of the 4 8x8 cells within a 16x16 stamp")
    ap.add_argument("--pmap", help="200B pmap for the output name table")
    ap.add_argument("--pal", help="128B palettes.bin")
    ap.add_argument("--dat", help="write combined ASIC.DAT to this path")
    args = ap.parse_args()

    tiles = Path(args.tiles).read_bytes()
    if len(tiles) != SRC_W_TILES * SRC_H_TILES * TILE_BYTES:
        raise SystemExit(f"tiles must be {SRC_W_TILES*SRC_H_TILES*TILE_BYTES} bytes")
    order = args.cell_order.split(",")

    def tile(tr, tc):
        k = tr * SRC_W_TILES + tc
        return tiles[k * TILE_BYTES:(k + 1) * TILE_BYTES]

    # stamp data: index 0 blank, then 50 stamps row-major
    stamp_data = bytearray(STAMP_BYTES)            # stamp 0 = blank
    for sr in range(STAMP_H):
        for sc in range(STAMP_W):
            cells = {
                "TL": tile(2 * sr,     2 * sc),
                "TR": tile(2 * sr,     2 * sc + 1),
                "BL": tile(2 * sr + 1, 2 * sc),
                "BR": tile(2 * sr + 1, 2 * sc + 1),
            }
            for name in order:
                stamp_data += cells[name]

    # stamp map: 16x16 words, row-major; place 10x5 region of stamps 1..50
    smap = bytearray(MAP_DIM * MAP_DIM * 2)
    for sr in range(STAMP_H):
        for sc in range(STAMP_W):
            num = sr * STAMP_W + sc + 1            # 1..50
            idx = sr * MAP_DIM + sc
            smap[idx * 2] = (num >> 8) & 0xFF
            smap[idx * 2 + 1] = num & 0xFF

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "stamp_data.bin").write_bytes(bytes(stamp_data))
    (out / "stamp_map.bin").write_bytes(bytes(smap))
    print(f"stamp_data.bin = {len(stamp_data)} bytes ({len(stamp_data)//STAMP_BYTES} stamps incl blank)")
    print(f"stamp_map.bin  = {len(smap)} bytes ({MAP_DIM}x{MAP_DIM} entries)")

    # Combined ASIC.DAT: pmap@0x000, pal@0x100, stamp_data@0x180, stamp_map@0x1B00
    if args.dat:
        pmap = Path(args.pmap).read_bytes()
        pal = Path(args.pal).read_bytes()
        buf = bytearray()
        buf += pmap;        buf += b"\0" * (0x100 - len(buf))
        buf += pal;         buf += b"\0" * (0x180 - len(buf))
        sd_off = len(buf);  buf += stamp_data
        sm_off = len(buf);  buf += smap
        buf += b"\0" * ((2048 - len(buf) % 2048) % 2048)
        Path(args.dat).write_bytes(bytes(buf))
        print(f"{args.dat} = {len(buf)}B  stamp_data@{hex(sd_off)} stamp_map@{hex(sm_off)}")


if __name__ == "__main__":
    main()
