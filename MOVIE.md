# MOVIE.DAT binary format (TTRC)

`MOVIE.DAT` is the on-disc stream of the **Tile Texture Reuse Codec** — the
current encoder/player path. It is written by `tools/pack_stream.py` from the
`tools/sim.py` decision log, and read by the Sega CD player (`boot/movieplay_sp.s`
streams it, `boot/movieplay_ip.s` displays it). This document describes the
on-disc byte layout exactly.

All multi-byte integers are **big-endian**. The stream is sector-aligned so the
Sub CPU can issue one continuous `ROM_READN` over the whole movie after the
header.

```
SECTOR         = 2048            (one Mode-1 CD sector)
MAGIC          = "TTRC"          (0x54545243; Tile Texture Reuse Codec)
VERSION        = 2               (2 = frame-0-in-header layout, the default and the
                                  only layout the current player supports; 1 = the
                                  legacy layout without the FRAME0 block, written
                                  only with CBRSIM_F0_HEADER=0)
FRAME_SECTORS  = 5               (each frame occupies exactly 5 sectors)
PAT            = 32              (one 8x8 4bpp tile pattern = 32 bytes)
AUDIO          = 887             (PCM bytes per frame; 13.3 kHz / 15 fps)
BASE           = 1               (POOL_TILE_BASE: VRAM tile index = BASE + slot)
```

## File layout

```
+--------------------------------------------------+  sector 0
| HEADER (1 sector, zero-padded)                   |
+--------------------------------------------------+  sector 1
| FRAME0 (f0_ctrl_sec + f0_pat_sec sectors)        |  frame 0 control + patterns
+--------------------------------------------------+
| ROUTING (routing_sec sectors)                    |  2 bytes per frame
+--------------------------------------------------+
| PREBUFFER (prebuf_sec sectors)                   |  first Bpat cold patterns
+--------------------------------------------------+
| FRAME 1  (FRAME_SECTORS = 5 sectors)             |
| FRAME 2  (5 sectors)                             |
| ...                                              |
| FRAME nfr-1 (5 sectors)                          |
+--------------------------------------------------+
```

**Frame 0 is not in the FRAMES region** (version 2). It lives in the dedicated
FRAME0 block right after the header: first its control block, zero-padded to
`f0_ctrl_sec` sectors, then its cold patterns (32 bytes each), zero-padded to
`f0_pat_sec` sectors. The player loads and displays it during boot, *without*
going through the streaming ring — so the ring never exceeds its cap at boot
and frame 1 starts with a full ring (this killed the frame-0 burst collapse).

The whole thing is one contiguous CD read; the player never re-seeks mid-movie
(except the rare slip-recovery re-seek described below).

## Header (1 sector = 2048 bytes)

First 22 bytes: `struct ">4sHHHHHHHHH"`.

| Off | Size | Field          | Meaning |
|-----|------|----------------|---------|
| 0   | 4    | magic          | `"TTRC"` |
| 4   | 2    | version        | format version (currently 2; see VERSION above) |
| 6   | 2    | frames         | total frame count (`nfr`) |
| 8   | 2    | tcols          | tile grid columns |
| 10  | 2    | trows          | tile grid rows |
| 12  | 2    | cells          | total cells = `tcols * trows` (`C_CELLS`) |
| 14  | 2    | pool           | VRAM tile-pool size (number of resident slots) |
| 16  | 2    | base           | `POOL_TILE_BASE`; VRAM tile index of slot `s` = `base + s` |
| 18  | 2    | frame_sectors  | sectors per frame = 5 |
| 20  | 2    | n_seg          | number of palette segments |

Next 16 bytes: `struct ">LLLL"`.

| Off | Size | Field        | Meaning |
|-----|------|--------------|---------|
| 22  | 4    | prebuf_pat   | `Bpat`: number of cold patterns pre-buffered before frame 1 |
| 26  | 4    | routing_sec  | sectors occupied by the routing table |
| 30  | 4    | prebuf_sec   | sectors occupied by the prebuffer |
| 34  | 4    | ring_peak    | peak PRG-RAM ring usage (patterns), for buffer sizing |

Then:

- byte 38: display mode. `0` = H32, `1` = H40, `2` = mode4 reserved for a
  future player path. If absent or zero in old streams, the player treats it as
  H32.
- byte 39: zero (pad);
- offset 40: `u32` **f0_ctrl_sec** — sectors of frame 0's control block in the
  FRAME0 region (version 2);
- offset 44: `u32` **f0_pat_sec** — sectors of frame 0's patterns in the FRAME0
  region (version 2);
- bytes 48..63 are zero;
- offset 64: 128 bytes = **`seg0`**, the CRAM palette (4 lines x 16 words) for the
  segment of frame 0, so the screen has correct colours before the first frame;
- remainder up to 2048 is zero.

A 128-byte CRAM block is 4 palette lines x 16 words; each word is
`0000BBB0GGG0RRR0` (Genesis colour). Only 15 of the 16 entries per line are
usable colour (entry 0 = transparent).

## Routing table

`routing_sec` sectors of **2 bytes per frame**: `[n_pay_sec, n_ctrl_sec]` (one
byte each). For frame `i` these say how many of its 5 sectors are payload
(cold tile patterns) and how many are control. The Sub CPU uses this to route
the continuous read into the PRG-RAM ring (payload) and the apply buffer
(control), staging each sector through Word RAM. `n_pay_sec + n_ctrl_sec <= 5`;
the rest is padding. The table covers all `nfr` frames including frame 0, but
in version 2 frame 0's entry is `(0, 0)` (it is delivered by the FRAME0 block,
not the FRAMES region).

## Prebuffer

`prebuf_sec` sectors holding the first `Bpat` cold patterns (32 bytes each) of
frames 1 onward (frame 0's patterns are in the FRAME0 block), loaded into the
ring before playback. `Bpat` is sized to fill the usable ring (`RING_CAP`), so
frame 1 starts with a full ring.

## Frame (exactly FRAME_SECTORS = 5 sectors; frames 1..nfr-1)

```
[ n_pay_sec sectors : payload  ]  cold tile patterns, 32 bytes each, back to back
[ n_ctrl_sec sectors : control ]  one control block (below)
[ pad to 5 sectors ]
```

**Payload** is a run of 32-byte tile patterns (the *cold* = newly loaded tiles
for this frame), consumed in order and DMA'd into ring slots. A pattern is
`pack_key`-encoded: 8 rows x 4 bytes, each byte = two 4bpp pixels
`(hi<<4)|lo`.

**Control block** (a variable-length block, byte layout):

| Size | Field       | Meaning |
|------|-------------|---------|
| 2    | total_len   | total block length **including these 2 bytes**; always even |
| 2    | frame_seq   | frame sequence number (low 16 bits). The player checks this against the frame it expects; a mismatch means the stream desynced (e.g. a dropped CD sector) — the frame's updates are discarded (previous frame held) and the desync counter increments. |
| 2    | n_upd       | number of cell updates this frame |
| 1    | pal         | 1 = reload CRAM this frame (segment change), else 0 |
| 1    | dbg         | 1 = a debug block follows immediately (see below), else 0 |
| 22   | debug       | **present only if `dbg==1`**: fixed-length debug block (below) |
| 128  | cram        | **present only if `pal==1`**: the new 4-line CRAM palette |
| ceil(cells/8) | bitmap | one bit per cell; 1 = this cell is updated this frame |
| n_upd x 2 | entries | one big-endian word per update, in cell order (see below) |
| 887  | audio       | RF5C164 sign-magnitude PCM for this frame |
| 0/1  | pad         | one zero byte if needed to make `total_len` even |

**Debug block** (a fixed 22 bytes, present only when `dbg==1`). It sits at a
**fixed position right after the 8-byte mini-header**, so a player reads it at a
constant offset — no `total_len` arithmetic needed. Every value is a big-endian
`u16`, clamped to `0xFFFF`. It is diagnostic only; the player skips it (advance
by 22) if it does not use it.

| Off | Size | Field     | Meaning |
|-----|------|-----------|---------|
| 0   | 2    | raw       | Raw cells this frame (fresh 32-byte pattern from CD) |
| 2   | 2    | same      | Same cells (unchanged or exact resident reuse, 0 B) |
| 4   | 2    | near      | Near cells (near-perfect resident reuse) |
| 6   | 2    | coa       | Coa cells (coarse resident reuse) |
| 8   | 2    | flbk      | Flbk cells (wide fallback resident reuse) |
| 10  | 2    | buf       | Buf cells (filled from the PRG-RAM prebuffer/tank, 0 CD) |
| 12  | 2    | miss      | Miss cells (changed but not updated this frame) |
| 14  | 8    | reserved  | 4 x u16 reserved for future 16-bit debug values (zero) |

The seven category counts always sum to the cell count. Whether the block is
emitted is an encoder setting (`tools/pack_stream.py`: off by default, on with
`CBRSIM_PACK_DEBUG=1`); release streams omit it to save CD bandwidth
(22 bytes/frame).

**Update entry** (2 bytes each), one per set bit in the bitmap, in ascending
cell order:

- bit 15 (`0x8000`) = **cold**: this cell loads a fresh pattern from this
  frame's payload (into a ring slot). If clear, the cell only re-points its
  name-table entry to a pattern already resident.
- bits 13..14 = the palette line (0..3).
- bits 0..10 = the VDP tile index = `base + slot` (the player recovers the ring
  slot as `(entry & 0x07FF) - base`). Bits 11..12 are zero.

The entry's low 15 bits are written to the VDP name table as-is (priority and
flip bits are unused). This is the core *tile texture reuse*: most cells cost
just this 2-byte entry.

**Audio**: 887 bytes/frame of RF5C164 sign-magnitude 8-bit PCM (positive =
`0..0x7F`, negative = `0x80 | magnitude`, magnitude clamped to `0x7E` so the
byte `0xFF` — the RF5C164 loop-stop marker — never appears), fed to the PCM
chip in sync.

## How the player advances (and the extension points)

The Sub CPU reads `total_len` at the start of each control block, copies exactly
`total_len` bytes out of the circular apply buffer (linearised into a Word-RAM
scratch area), and advances its cursor by `total_len` (`boot/movieplay_sp.s`).
It parses by length, not by scanning to a fixed end, so `total_len` must stay
even (odd desyncs by 1 byte per frame).

**Slip detection and recovery.** Right after copying a block the player checks
`frame_seq` against the frame it expects. A mismatch means the continuous CD read
dropped a sector (the ring and control streams shifted). The player detects this
at the source: `drain1` reads each sector's MSF header and, when it sees a gap
(non-consecutive MSF), re-seeks (`CDC_STOP`+`ROM_READN`) to the lost sector and
re-reads it — recovering the exact data with no quality loss. Slips are rare, so
the brief re-seek is cheap; the `frame_seq` check is the backstop (a mismatched
frame is dropped, holding the previous frame). The debug HUD shows `S` =
slip/re-seek count and `D` = residual desyncs (normally 0).

Two forward-compatible ways to extend the format:

1. **The `dbg` flag + debug block.** The debug block is opt-in per frame via the
   `dbg` byte and lives at a fixed offset right after the mini-header, so a
   player that wants the counts reads them at a constant position, and one that
   does not simply advances past them (`+22` when `dbg==1`). Its 8 reserved
   bytes (4 x u16) are the room to add more 16-bit debug metrics without moving
   anything.
2. **Appending within `total_len`.** Because the player advances by `total_len`,
   any bytes added after `audio` (still inside `total_len`, kept even) are
   skipped by a player that does not know them. Use this for a larger or
   variable-length future extension.

## Reconstruction (player)

The player keeps a VRAM **tile pool** of `pool` resident patterns (an
LRU/double-buffer-protected ring, `base + slot` in VRAM) and a name table. Per
frame: if `pal`, reload CRAM; DMA the payload's cold patterns into their slots;
for every set bit in the bitmap, apply its entry — cold entries having just
filled a slot, warm entries re-pointing the name table to an existing
`base + slot`. Audio is streamed to the PCM chip. A 1M/1M Word RAM double buffer
swaps at frame boundaries.
