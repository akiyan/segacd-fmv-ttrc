#!/usr/bin/env python3
"""Quantize a movie to 4 fixed global Genesis palettes with per-tile palette
selection, and export uncompressed VDP tile data for the continuous-stream
player.

Each 8x8 tile is assigned (per frame) to whichever of the 4 global 15-colour
palettes represents it best; its pixels are quantised to that palette (4bpp).
The 4 palettes are fixed for the whole clip and live in CRAM lines 0-3, so up to
4*15 = 60 colours can appear on one screen.

Outputs (under --output-dir):
  palettes.bin          4 * 16 big-endian CRAM words (line N = colour 0 black + 15)
  tile/NNNNN.tile       VDP-order 4bpp tile data, tiles_per_frame * 32 bytes
  pmap/NNNNN.pmap       1 byte per tile (0-3) = palette line for that tile
  preview/NNNNN.png     rendered frame (for the mp4)
  preview.mp4           visual check of the 4-palette per-tile result
"""
import argparse
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quantize_md_video import (  # noqa: E402
    prepare_dir,
    rgb333_to_rgb888,
    rgb888_to_rgb333,
    run,
    weighted_palette,
    nearest_indices,
    pack_tiles_4bpp,
    md_cram_word,
)

TILE = 8

# RGB333 has only 512 possible input colours.  Build nearest-colour tables when
# a palette changes, then score millions of pixels with compact table lookups.
# This is also the common hot-path foundation for STL4 and MOSAIC-GM.
_RGB333_GRID = np.stack([
    (np.arange(512, dtype=np.uint16) >> 6) & 7,
    (np.arange(512, dtype=np.uint16) >> 3) & 7,
    np.arange(512, dtype=np.uint16) & 7,
], axis=1).astype(np.int16)


def rgb333_keys(pixels):
    """Pack an RGB333 array ending in (...,3) into 9-bit colour keys."""
    value = np.asarray(pixels, dtype=np.uint16)
    return ((value[..., 0] << 6) | (value[..., 1] << 3) | value[..., 2])


def palette_lut(pal, squared=False):
    """Return nearest error/index tables for all 512 RGB333 colours.

    ``squared=False`` matches STL4's L1 training metric.  ``squared=True``
    matches the player's per-tile assignment and nearest-index metric.  NumPy's
    first-minimum tie behaviour is retained in the returned zero-based index.
    """
    palette = np.asarray(pal, dtype=np.int16)
    delta = _RGB333_GRID[:, None, :] - palette[None, :, :]
    distance = (delta * delta).sum(2) if squared else np.abs(delta).sum(2)
    index = distance.argmin(1).astype(np.uint8)
    error = distance[np.arange(512), index].astype(np.int16)
    return error, index


def extract_frames(args, work_dir):
    prepare_dir(work_dir, clean=True)
    vf = (f"crop={args.crop},scale={args.scale_width}:{args.scale_height}:flags=lanczos,"
          f"fps={args.fps}")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", str(args.start), "-t", str(args.duration),
         "-i", args.input, "-vf", vf, str(work_dir / "%05d.png")])
    frames = sorted(work_dir.glob("*.png"))
    if not frames:
        raise RuntimeError("ffmpeg produced no frames")
    return frames


def load_rgb333(path):
    return rgb888_to_rgb333(np.asarray(Image.open(path).convert("RGB")))


def tile_blocks(rgb333):
    """Return (n_tiles, 64, 3) RGB333 tiles in VDP order (row-major tiles)."""
    h, w, _ = rgb333.shape
    ty, tx = h // TILE, w // TILE
    blocks = []
    for r in range(ty):
        for c in range(tx):
            blocks.append(rgb333[r*TILE:(r+1)*TILE, c*TILE:(c+1)*TILE].reshape(64, 3))
    return np.array(blocks, dtype=np.uint8)


def hist(pixels, weights=None):
    """pixels (N,3) rgb333 -> (unique_rgb333, counts)。weights(N,)を渡すと各色の重み総和を数える。"""
    keys = (pixels[:, 0].astype(np.int64) << 6) | (pixels[:, 1] << 3) | pixels[:, 2]
    counts = np.bincount(keys, weights=weights, minlength=512)
    used = np.nonzero(counts)[0]
    rgb = np.stack([(used >> 6) & 7, (used >> 3) & 7, used & 7], axis=1).astype(np.uint8)
    return rgb, counts[used]


def edge_weights(tiles, alpha):
    """tiles (T,64,3) rgb333 -> 画素ごとの学習重み (T,64) = 1 + alpha*edge。edge=局所勾配(隣接画素との
    rgb絶対差の和)。線/輪郭の画素ほど大きい=面積が小さくてもパレットに拾われる。alpha<=0 なら None(無効)。
    平坦部は勾配0で重み1のまま=実写など滑らかな素材は自動的にほぼ無変化(自動バランス)。"""
    if alpha <= 0:
        return None
    T = tiles.shape[0]
    t = tiles.reshape(T, 8, 8, 3).astype(np.int32)
    e = np.zeros((T, 8, 8), np.float64)
    e[:, :, :-1] += np.abs(t[:, :, :-1] - t[:, :, 1:]).sum(3)   # 右隣との差
    e[:, :, 1:] += np.abs(t[:, :, 1:] - t[:, :, :-1]).sum(3)    # 左隣
    e[:, :-1, :] += np.abs(t[:, :-1, :] - t[:, 1:, :]).sum(3)   # 下隣
    e[:, 1:, :] += np.abs(t[:, 1:, :] - t[:, :-1, :]).sum(3)    # 上隣
    e_norm = e.reshape(T, 64) / 21.0        # 21=1方向で全chが最大差(0↔7)。強い線でe_norm≈2, 緩い勾配≈0.3
    return (1.0 + alpha * e_norm)


def palette15(pixels, colors=15, weights=None):
    """15-colour palette (rgb333) for a pixel set. CRAM line = black + these 15;
    nearest_indices() maps pixels onto these and returns indices 1..15。
    weights(N,)=画素ごとの学習重み(エッジ重み等)。渡すと頻度の代わりに重み総和で色を選ぶ。"""
    u, c = hist(pixels, weights)
    # 知覚重み(CBRSIM_PAL_SAT>0): 画素頻度だけだと小さくても目立つ鮮やか&明るい領域(例:光球の黄色)に
    # 色が回らずベタ化する。彩度×明度で重みを底上げし、そこへ色を割り当てて階調を出せるようにする。
    satw = float(os.environ.get("CBRSIM_PAL_SAT", "0"))
    if satw > 0:
        uf = u.astype(np.float64)
        chroma = uf.max(1) - uf.min(1)                       # 0..7 彩度
        val = uf.max(1)                                      # 0..7 明度
        c = c * (1.0 + satw * (chroma / 7.0) * (val / 7.0))  # 鮮やか&明るい色ほど重く
    return weighted_palette(u, c, colors=colors, iterations=16)		# (15,3)


def tile_errors(tiles, pal):
    """Min quantisation error per tile against one 15-colour palette. tiles (T,64,3)."""
    error, _index = palette_lut(pal, squared=False)
    keys = rgb333_keys(tiles).reshape(tiles.shape[0], 64)
    return error[keys].sum(1, dtype=np.int64)


def pals_to_bytes(pals):
    """n_pal palettes (each (15,3) rgb333) -> CRAM bytes: line = black + 15 colours."""
    b = bytearray()
    for pl in pals:
        b += (0).to_bytes(2, "big")
        for col in pl:
            b += int(md_cram_word(col)).to_bytes(2, "big")
    return bytes(b)


def build_overlay(tiles, pals, assign, n_ovl=24):
    """Plane B オーバーレイを作る(CBR=常に n_ovl 枠)。

    量子化が厳しいセル上位 n_ovl 個へ、4パレットのうち1つ(p_ov)を使う 8x8 タイルを
    重ねる。色0=透過なので画素ごとに背景(Plane A, 割当パレット)と上載せ(p_ov)の
    二乗誤差(nearest_indices と同じ指標)が小さい方を採る。
    戻り: (pattern_bytes n_ovl*32, desc_bytes n_ovl*2[cell,pal], render_list)。
    render_list = [(cell, p_ov, idx8x8(0=透過)), ...] (プレビュー用)。不足枠は cell=0xFF。
    """
    C = tiles.shape[0]
    flat = tiles.reshape(-1, 3).astype(np.int64)
    err = np.zeros((4, C, 64), np.int64)
    idx = np.zeros((4, C, 64), np.uint8)
    for p in range(4):
        pal = pals[p].astype(np.int64)				# (15,3)
        d = ((flat[:, None, :] - pal[None, :, :]) ** 2).sum(2)	# (C*64,15)
        j = d.argmin(1)
        idx[p] = (j + 1).reshape(C, 64).astype(np.uint8)	# 1..15
        err[p] = d[np.arange(len(flat)), j].reshape(C, 64)
    bg = assign.astype(int)
    bg_err = err[bg, np.arange(C)]				# (C,64)
    comb = np.minimum(bg_err[None], err)			# (4,C,64)
    comb_cell = comb.sum(2)					# (4,C)
    best_pov = comb_cell.argmin(0)				# (C,)
    improve = bg_err.sum(1) - comb_cell[best_pov, np.arange(C)]
    order = np.argsort(improve)[::-1]
    chosen = [int(c) for c in order if improve[c] > 0][:n_ovl]

    pat = bytearray()
    desc = bytearray()
    render = []
    for c in chosen:
        pov = int(best_pov[c])
        use_ov = err[pov, c] < bg_err[c]			# (64,) 上載せが有利な画素
        ti = np.where(use_ov, idx[pov, c], 0).astype(np.uint8).reshape(8, 8)
        from quantize_md_video import pack_tiles_4bpp as _pack
        pat += _pack(ti, 8, 8)
        desc += bytes((c, pov))
        render.append((c, pov, ti))
    for _ in range(n_ovl - len(chosen)):			# CBR padding
        pat += b"\x00" * 32
        desc += bytes((0xFF, 0))
    return bytes(pat), bytes(desc), render


def build_palettes(train_tiles, n_pal=4):
    """Lloyd over palettes: assign each tile to a palette, refit each palette。
    CBRSIM_EDGE_WEIGHT>0 で線/輪郭の画素を学習で重く扱う(アニメの線が面積小でも拾われる)。"""
    alpha = float(os.environ.get("CBRSIM_EDGE_WEIGHT", "3.0"))
    ew = edge_weights(train_tiles, alpha)                 # (T,64) or None
    # CBRSIM_GPU 時は重い tile_errors を GPU で(結果はビット一致)。palette学習はここが律速。
    try:
        import gpu_quant
        if gpu_quant.enabled():
            return gpu_quant.build_palettes(train_tiles, palette15, n_pal, edge_w=ew)
    except Exception as _e:   # noqa: BLE001  GPU不調時はCPUへ静かに退避
        print(f"[build_palettes] GPU退避: {_e}")

    def _w(mask):
        return None if ew is None else ew[mask].reshape(-1)
    means = train_tiles.reshape(train_tiles.shape[0], 64, 3).mean(1)
    order = np.argsort(means.sum(1))
    groups = np.array_split(order, n_pal)
    pals = [palette15(train_tiles[g].reshape(-1, 3), weights=_w(g)) for g in groups]
    for _ in range(6):
        err = np.stack([tile_errors(train_tiles, pl) for pl in pals], axis=1)	# (T,n_pal)
        assign = err.argmin(1)
        newpals = []
        for p in range(n_pal):
            sel = train_tiles[assign == p]
            newpals.append(pals[p] if len(sel) == 0 else palette15(sel.reshape(-1, 3), weights=_w(assign == p)))
        pals = newpals
    return pals								# list of (15,3)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default="movies/disc1/061.mp4")
    p.add_argument("--output-dir", default="out/video/061_160x80_global4")
    p.add_argument("--start", default="90")
    p.add_argument("--duration", default="50")
    p.add_argument("--fps", default="15")
    p.add_argument("--crop", default="320:152:0:34")
    p.add_argument("--scale-width", type=int, default=160)
    p.add_argument("--scale-height", type=int, default=80)
    p.add_argument("--palettes", type=int, default=4)
    p.add_argument("--per-frame", action="store_true",
                   help="quantise 4 palettes PER FRAME (else 4 fixed global palettes)")
    p.add_argument("--overlay", action="store_true",
                   help="generate Plane B overlay data (overlay/NNNNN.ovl) for the hardest cells")
    p.add_argument("--overlay-cells", type=int, default=24,
                   help="overlay cells per frame (CBR, fixed count)")
    p.add_argument("--train-stride", type=int, default=6,
                   help="use every Nth frame to train the global palettes")
    p.add_argument("--keep-work", action="store_true")
    args = p.parse_args()

    out_dir = Path(args.output_dir)
    work = out_dir / "work" / "src"
    tile_dir = out_dir / "tile"
    pmap_dir = out_dir / "pmap"
    prev_dir = out_dir / "preview"
    pal_dir = out_dir / "pal"
    ovl_dir = out_dir / "overlay"
    dirs = [tile_dir, pmap_dir, prev_dir]
    if args.per_frame:
        dirs.append(pal_dir)
    if args.overlay:
        dirs.append(ovl_dir)
    for d in dirs:
        prepare_dir(d, clean=True)

    print("extracting frames ...")
    frames = extract_frames(args, work)
    w, h = args.scale_width, args.scale_height
    print(f"  {len(frames)} frames @ {w}x{h} {args.fps}fps, {(w//TILE)*(h//TILE)} tiles/frame")

    pals = None
    if args.per_frame:
        print("per-frame mode: 4 palettes quantised independently each frame")
    else:
        print("training 4 global palettes (per-tile) ...")
        train = np.concatenate([tile_blocks(load_rgb333(f))
                                for f in frames[::args.train_stride]], axis=0)
        pals = build_palettes(train, n_pal=args.palettes)
        (out_dir / "palettes.bin").write_bytes(pals_to_bytes(pals))

    print("mapping frames ...")
    counts = np.zeros(args.palettes, np.int64)
    for i, f in enumerate(frames):
        rgb = load_rgb333(f)
        tiles = tile_blocks(rgb)					# (T,64,3)
        if args.per_frame:
            pals = build_palettes(tiles, n_pal=args.palettes)	# 4 palettes for THIS frame
            (pal_dir / f"{i:05d}.pal").write_bytes(pals_to_bytes(pals))
            if i == 0:						# header fallback palette
                (out_dir / "palettes.bin").write_bytes(pals_to_bytes(pals))
        err = np.stack([tile_errors(tiles, pl) for pl in pals], 1)	# (T,n_pal)
        assign = err.argmin(1).astype(np.uint8)				# (T,) palette per tile
        for p in range(args.palettes):
            counts[p] += int((assign == p).sum())
        # build full-frame index image quantised per tile to its palette
        idx = np.zeros((h, w), np.uint8)
        prev = np.zeros((h, w, 3), np.uint8)
        tcols = w // TILE
        for t in range(tiles.shape[0]):
            ry, rx = (t // tcols) * TILE, (t % tcols) * TILE
            pl = pals[assign[t]]
            ti = nearest_indices(rgb[ry:ry+TILE, rx:rx+TILE], pl)	# 1..15 into the line
            idx[ry:ry+TILE, rx:rx+TILE] = ti
            full16 = np.vstack([np.zeros((1, 3), np.uint8), pl])
            prev[ry:ry+TILE, rx:rx+TILE] = rgb333_to_rgb888(full16[ti])
        stem = f"{i:05d}"
        if args.overlay:
            pat, desc, render = build_overlay(tiles, pals, assign, args.overlay_cells)
            (ovl_dir / f"{stem}.ovl").write_bytes(pat + desc)
            for c, pov, ti8 in render:			# overlay onto preview
                ry, rx = (c // tcols) * TILE, (c % tcols) * TILE
                full16 = np.vstack([np.zeros((1, 3), np.uint8), pals[pov]])
                m = ti8 > 0
                cell = prev[ry:ry+TILE, rx:rx+TILE]
                cell[m] = rgb333_to_rgb888(full16[ti8])[m]
        (tile_dir / f"{stem}.tile").write_bytes(pack_tiles_4bpp(idx, w, h))
        (pmap_dir / f"{stem}.pmap").write_bytes(assign.tobytes())
        Image.fromarray(prev, "RGB").save(prev_dir / f"{stem}.png")

    print(f"  palette tile usage: {dict(enumerate(counts.tolist()))}")

    print("encoding preview mp4 ...")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-framerate", str(args.fps), "-i", str(prev_dir / "%05d.png"),
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
         "-vf", "scale=iw*4:ih*4:flags=neighbor",
         str(out_dir / "preview.mp4")])

    if not args.keep_work:
        prepare_dir(out_dir / "work", clean=True)
        (out_dir / "work").rmdir()
    print(f"wrote {out_dir} ({len(frames)} frames, palettes.bin + tile/ + pmap/ + preview.mp4)")


if __name__ == "__main__":
    main()
