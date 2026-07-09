#!/usr/bin/env python3
"""Build a transition-effect check sample from K1/046,047,048.

For each adjacent pair, insert 4 linearly-blended (crossfade) intermediate
frames. Originals hold 1.0s; each interpolated frame holds 0.05s. The whole
sequence is quantised to 4 fixed global Genesis palettes (60 colours) with
per-8x8-tile palette selection, then encoded to mp4.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quantize_md_video import (  # noqa: E402
    rgb333_to_rgb888, rgb888_to_rgb333, run,
    nearest_indices, pack_tiles_4bpp, md_cram_word,
)
from quantize_global4_tiles import (  # noqa: E402
    tile_blocks, build_palettes, TILE,
)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "images/disc1/K1"
OUT = ROOT / "out/transition_sample"
N_INTER = 4          # intermediate frames per transition
BASE_FPS = 20        # 1 frame = 0.05s
HOLD_FRAMES = 20     # 1.0s hold for originals
PALETTES = 4
TARGET_W = 240       # MD framebuffer width (re-quantised at this res)
TARGET_H = 208
# contain: base image PAR is 1:0.91 (real AR 1.570), frame dot is 1.25:1
# (real AR 1.442). Base is wider -> width-fit, letterbox top/bottom.
# Width fills 240; content height = 240*1.25/1.570 = ~191 -> snap to 192
# (8px black bars top+bottom, tile-aligned).
CONTENT_H = 192
# H32 dot ratio: display each pixel 1.25:1 (5:4). Burn in via integer
# neighbour upscale 5x horizontal / 4x vertical -> PAR 5:4 = 1.25:1.
PAR_HSCALE = 5
PAR_VSCALE = 4

originals = ["046", "047", "048"]


def main():
    prev_dir = OUT / "preview"
    seq_dir = OUT / "seq"
    for d in (prev_dir, seq_dir):
        d.mkdir(parents=True, exist_ok=True)
        for c in d.iterdir():
            c.unlink()

    y_off = (TARGET_H - CONTENT_H) // 2

    def load_contain(name):
        src = Image.open(SRC / f"{name}.png").convert("RGB").resize(
            (TARGET_W, CONTENT_H), Image.LANCZOS)
        canvas = Image.new("RGB", (TARGET_W, TARGET_H), (0, 0, 0))
        canvas.paste(src, (0, y_off))
        return np.asarray(canvas).astype(np.float32)

    imgs = [load_contain(n) for n in originals]
    h, w, _ = imgs[0].shape

    # Build distinct-frame list with hold durations (in base frames).
    frames = []   # (rgb_uint8, hold_frames, is_original)
    for i, img in enumerate(imgs):
        frames.append((img.astype(np.uint8), HOLD_FRAMES, True))
        if i < len(imgs) - 1:
            a, b = img, imgs[i + 1]
            for k in range(1, N_INTER + 1):
                t = k / (N_INTER + 1)
                blend = np.clip((1 - t) * a + t * b, 0, 255).astype(np.uint8)
                frames.append((blend, 1, False))

    print(f"{len(frames)} distinct frames @ {w}x{h}, "
          f"{(w//TILE)*(h//TILE)} tiles/frame")

    # Train 4 global palettes on all distinct frames (per-tile).
    train = np.concatenate(
        [tile_blocks(rgb888_to_rgb333(f[0])) for f in frames], axis=0)
    print("training 4 global palettes ...")
    pals = build_palettes(train, n_pal=PALETTES)

    pal_bytes = bytearray()
    for pl in pals:
        pal_bytes += (0).to_bytes(2, "big")
        for col in pl:
            pal_bytes += int(md_cram_word(col)).to_bytes(2, "big")
    (OUT / "palettes.bin").write_bytes(pal_bytes)

    # Quantise each distinct frame, then emit it `hold` times into the preview seq.
    print("quantising + expanding to base fps ...")
    out_idx = 0
    tcols = w // TILE
    counts = np.zeros(PALETTES, np.int64)
    for f_rgb, hold, is_org in frames:
        rgb = rgb888_to_rgb333(f_rgb)
        tiles = tile_blocks(rgb)
        err = np.stack([_tile_err(tiles, pl) for pl in pals], 1)
        assign = err.argmin(1).astype(np.uint8)
        for p in range(PALETTES):
            counts[p] += int((assign == p).sum())
        prev = np.zeros((h, w, 3), np.uint8)
        for t in range(tiles.shape[0]):
            ry, rx = (t // tcols) * TILE, (t % tcols) * TILE
            pl = pals[assign[t]]
            ti = nearest_indices(rgb[ry:ry+TILE, rx:rx+TILE], pl)
            full16 = np.vstack([np.zeros((1, 3), np.uint8), pl])
            prev[ry:ry+TILE, rx:rx+TILE] = rgb333_to_rgb888(full16[ti])
        img = Image.fromarray(prev, "RGB")
        for _ in range(hold):
            img.save(seq_dir / f"{out_idx:05d}.png")
            out_idx += 1

    print(f"  palette tile usage: {dict(enumerate(counts.tolist()))}")
    print(f"  {out_idx} base frames @ {BASE_FPS}fps = {out_idx/BASE_FPS:.2f}s")

    out_mp4 = OUT / f"transition_sample_{TARGET_W}x{TARGET_H}_par1_25_contain.mp4"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-framerate", str(BASE_FPS), "-i", str(seq_dir / "%05d.png"),
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
         "-vf", f"scale=iw*{PAR_HSCALE}:ih*{PAR_VSCALE}:flags=neighbor",
         str(out_mp4)])
    print(f"wrote {out_mp4} "
          f"(disp {TARGET_W*PAR_HSCALE}x{TARGET_H*PAR_VSCALE}, PAR 1.25:1)")


def _tile_err(tiles, pal):
    px = tiles.reshape(-1, 3).astype(int)
    d = np.abs(px[:, None, :] - pal[None, :, :].astype(int)).sum(2)
    return d.min(1).reshape(tiles.shape[0], 64).sum(1)


if __name__ == "__main__":
    main()
