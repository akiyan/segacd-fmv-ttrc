#!/usr/bin/env python3
"""Pack quantize_global4_tiles output into a streamable PROBE.BIN for the
continuous-stream player.

Layout (all sector-aligned, SECTOR=2048):
  header  : 1 sector
            >4sHHHHHHHH = magic 'MPG4', version, frames, w_tiles, h_tiles,
                          tiles_per_frame, tile_bytes, frame_sectors, header_sectors
            offset 32   : 4 CRAM palette lines * 16 words = 128 bytes
  frames  : each padded to frame_sectors sectors:
            [ tile_bytes tile data ][ tiles_per_frame pmap bytes ][ zero pad ]

The Sub streams frames back to back with one continuous ROM_READN; the Main
reads tile data -> VRAM and pmap -> per-tile name-table palette bits.
"""
import argparse
import struct
from pathlib import Path

SECTOR = 2048
MAGIC = b"MPG4"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="quantize_global4_tiles output dir")
    ap.add_argument("--frames", type=int, default=0, help="0 = all frames found")
    ap.add_argument("--frame-sectors", type=int, default=5)
    ap.add_argument("--w-tiles", type=int, default=20)
    ap.add_argument("--h-tiles", type=int, default=10)
    ap.add_argument("--audio", default="", help="raw signed 8-bit mono PCM (13.3kHz)")
    ap.add_argument("--audio-rate", type=int, default=13300)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--per-frame", action="store_true",
                    help="include a 128-byte palette per frame from pal/NNNNN.pal")
    ap.add_argument("--overlay", action="store_true",
                    help="include a Plane B overlay block per frame from overlay/NNNNN.ovl")
    ap.add_argument("--overlay-cells", type=int, default=24,
                    help="overlay cells per frame (CBR); block = cells*(32+2) bytes")
    ap.add_argument("--output", default="out/disc/PROBE.BIN")
    args = ap.parse_args()
    PAL_BYTES = 4 * 16 * 2				# 128: per-frame palette block
    OVL_BYTES = args.overlay_cells * (32 + 2)		# 24*34 = 816: overlay block

    root = Path(args.root)
    pals = (root / "palettes.bin").read_bytes()
    if len(pals) != 4 * 16 * 2:
        raise SystemExit(f"palettes.bin must be 128 bytes, got {len(pals)}")

    tiles = sorted((root / "tile").glob("*.tile"))
    if args.frames:
        tiles = tiles[: args.frames]
    n = len(tiles)
    if n == 0:
        raise SystemExit("no tile frames found")

    tiles_per_frame = args.w_tiles * args.h_tiles
    tile_bytes = tiles_per_frame * 32

    # audio: signed 8-bit mono -> RF5C164 sign-magnitude, audio_bytes per frame
    audio_bytes = 0
    audio = b""
    if args.audio:
        audio_bytes = (args.audio_rate + args.fps - 1) // args.fps  # 13.3kHz/15fps -> 887
        raw = Path(args.audio).read_bytes()
        sm = bytearray(len(raw))
        for i, b in enumerate(raw):
            s = b - 256 if b >= 128 else b			# signed -128..127
            if s >= 0:
                sm[i] = min(s, 0x7F)				# +0..+127
            else:
                sm[i] = 0x80 | min(-s, 0x7E)			# sign-magnitude, avoid 0xFF
        audio = bytes(sm)

    pal_block = PAL_BYTES if args.per_frame else 0	# per-frame palette after pmap
    ovl_block = OVL_BYTES if args.overlay else 0	# Plane B overlay after palette
    audio_off = tile_bytes + tiles_per_frame + pal_block + ovl_block
    frame_payload = audio_off + audio_bytes		# tile + pmap + [pal] + [overlay] + audio
    frame_bytes = args.frame_sectors * SECTOR
    if frame_payload > frame_bytes:
        raise SystemExit(f"frame payload {frame_payload} > {frame_bytes} "
                         f"({args.frame_sectors} sectors); raise --frame-sectors")

    version = 4 if args.overlay else (3 if args.per_frame else 2)
    # header: magic, ver, frames, w, h, tpf, tile_bytes, frame_sectors, hdr_sectors,
    #         audio_off, audio_bytes  (ver 3 = per-frame palette, ver 4 = +Plane B overlay)
    header = struct.pack(">4sHHHHHHHHHH", MAGIC, version, n,
                         args.w_tiles, args.h_tiles, tiles_per_frame, tile_bytes,
                         args.frame_sectors, 1, audio_off, audio_bytes)
    header += b"\0" * (32 - len(header))
    header += pals
    header += b"\0" * (SECTOR - len(header))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        f.write(header)
        for i, tpath in enumerate(tiles):
            td = tpath.read_bytes()
            if len(td) != tile_bytes:
                raise SystemExit(f"{tpath.name}: tile size {len(td)} != {tile_bytes}")
            pm = (root / "pmap" / f"{tpath.stem}.pmap").read_bytes()
            if len(pm) != tiles_per_frame:
                raise SystemExit(f"{tpath.stem}.pmap size {len(pm)} != {tiles_per_frame}")
            frame = td + pm
            if args.per_frame:
                pal = (root / "pal" / f"{tpath.stem}.pal").read_bytes()
                if len(pal) != PAL_BYTES:
                    raise SystemExit(f"{tpath.stem}.pal size {len(pal)} != {PAL_BYTES}")
                frame += pal
            if args.overlay:
                ovl = (root / "overlay" / f"{tpath.stem}.ovl").read_bytes()
                if len(ovl) != OVL_BYTES:
                    raise SystemExit(f"{tpath.stem}.ovl size {len(ovl)} != {OVL_BYTES}")
                frame += ovl
            if audio_bytes:
                a = audio[i*audio_bytes:(i+1)*audio_bytes]
                a = a + b"\x00" * (audio_bytes - len(a))		# pad last frame's audio
                frame += a
            f.write(frame)
            f.write(b"\0" * (frame_bytes - len(frame)))

    total = SECTOR + n * frame_bytes
    print(f"wrote {out} ({n} frames, {args.frame_sectors} sectors/frame, "
          f"{total} bytes, {total//SECTOR} sectors)")


if __name__ == "__main__":
    main()
