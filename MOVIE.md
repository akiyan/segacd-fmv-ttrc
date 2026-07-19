# HEADER.DAT / BODY.DAT binary format (TTRC)

`HEADER.DAT` and `BODY.DAT` are the two on-disc files of the **Tile Texture
Reuse Codec**. They are written by `tools/pack_stream.py` from the `tools/sim.py`
decision log and read by the Sega CD player (`boot/movieplay_sp.s` streams them,
`boot/movieplay_ip.s` displays them). This document describes their byte layout
exactly.

The packer also writes `MOVIE.DAT = HEADER.DAT || BODY.DAT` as an off-disc
compatibility container for analysis and regression tools. `make disc` places
only `HEADER.DAT` and `BODY.DAT` on the disc.

All multi-byte integers are **big-endian**. Every region is sector-aligned. The
Sub CPU reads all of `HEADER.DAT`, prepares frame 0 with no timed read active,
then issues one continuous `ROM_READN` for `BODY.DAT`.

```
SECTOR         = 2048            (one Mode-1 CD sector)
MAGIC          = "TTRC"          (0x54545243; Tile Texture Reuse Codec)
VERSION        = 9               (bump for an incompatible stream interpretation)
FRAME_SECTORS  = 5               (routing-byte maximum; v4+ frames are variable)
PAT            = 32              (one 8x8 4bpp tile pattern = 32 bytes)
AUDIO          = decoded header field (PCM: 888 B at N4 / 444 B at N2;
                                      ADPCM22: 1472 / 736 samples)
BASE           = 1               (POOL_TILE_BASE: VRAM tile index = BASE + slot)
```

Version history: **v2** moved frame 0 out of the frame stream into a dedicated
block right after the header (loaded during boot, bypassing the streaming
ring). **v3** added the **PALTAB** (all segment palettes pre-loaded at boot
into a Main-RAM table) and dropped the per-frame in-stream CRAM payload. **v4**
added rate-matched variable frame sectors plus per-stream VBlank, audio-byte and
nominal-fps fields.
**v5** added a boot-prefix PCM preload. Legacy streams duplicated the first
audio chunks into one sector each and skipped their control writes. Current
streams use the same fields for a persistent prefetch: the prefix holds the
source start and controls carry future chunks.
**v6** split the boot prefix and timed stream into `HEADER.DAT` and `BODY.DAT`,
put frame 0 entirely in `HEADER.DAT`, and changed each timed frame from
payload-first to control-first. The `[n_pay_sec, n_ctrl_sec]` routing-byte order
did not change. Header feature bit 0 is an optional v6 extension that appends
cold-slot run descriptors after audio without moving any legacy field.
**v7** packed each routing entry from two bytes into one byte containing the
control-sector count and total-sector count. This doubled the resident-table
limit from 8192 to 16384 frames without changing the BODY sector order.
**v8** retained that one-byte routing layout and every header-field offset, but
defined feature bit 1 (`FEATURE_FIXED_N2`) as the authoritative fixed-cadence
contract. When set, the Main CPU flips once every two VBlanks and the Sub CPU
assigns 1001 sectors per 400 frames. This changes the rate-padding boundaries
in `BODY.DAT`, so an old v7 player must not read a v8 stream. The current packer
sets the bit only for rates classified by `uses_fixed_n2_cadence`; 24fps and
15fps leave it clear and retain their delivery-paced `75 / fps_int` schedule.
**v9** adds feature bit 2 (`FEATURE_ADPCM22`) for checkpointed continuous IMA
ADPCM. Header offset 54 remains the decoded RF5C164 sample count, while live
controls store a four-byte checkpoint plus one nibble per sample. Offset 58 is
repurposed from the obsolete duplicate-skip count to the RF5C164 frequency
delta. A feature-selected full decoder-table region follows PALTAB and is copied
into both physical 1M Word-RAM banks at boot. ADPCM22 is a supported v9 feature;
the table region is required whenever its feature bit is set.

## File layout

```
HEADER.DAT
+--------------------------------------------------+  sector 0
| HEADER (1 sector, zero-padded)                   |
+--------------------------------------------------+  sector 1
| PALTAB (paltab_sec sectors)                      |  all n_seg palettes, 128 B each
+--------------------------------------------------+
| ADPCM_TABLE (5 sectors when feature bit 2 set)   |  8,800 B full lookup image
+--------------------------------------------------+
| STARTUP_AUDIO (audio_preload_sec sectors)        |  one PCM chunk per sector
+--------------------------------------------------+
| FRAME 0 (f0_ctrl_sec + f0_pat_sec sectors)       |  control, then patterns
+--------------------------------------------------+
| ROUTING (routing_sec sectors)                    |  v7+: 1 byte per frame, max 16384 frames
+--------------------------------------------------+
| PREBUFFER (prebuf_sec sectors)                   |  frame-1 ring prefill (Bpat patterns)
+--------------------------------------------------+
                                                     end of HEADER.DAT

BODY.DAT
+--------------------------------------------------+  sector 0
| FRAME 1  (control, future payload, rate pad)     |
| ...                                              |
| FRAME nfr-1                                      |
+--------------------------------------------------+
```

The player drains all of `HEADER.DAT`, writing STARTUP_AUDIO to wave RAM while
PCM is stopped. After the header request has ended, it expands and prepares
frame 0, starts `BODY.DAT` at its own first sector, and fully pre-drains frame 1.
Only then does it release frame 0 to the Main CPU for display. PCM stays stopped
until the Main CPU confirms that display; timed playback then begins while the
`BODY.DAT` read remains continuous until the movie ends. Frame 0 therefore has
no time budget and never competes with frame 1 delivery, while the ring starts
frame 1 pre-filled to `RING_CAP`.

During boot, frame 0's patterns temporarily occupy the 40 KiB jitter reserve
between the 388 KiB usable `RING_CAP` and the 428 KiB physical ring; the largest
H40 frame needs 36 KiB after sector rounding. The on-disc routing table is
staged in the not-yet-active APPLY ring,
then copied identically into the final 16 KiB of both 1M Word-RAM banks after
`HEADER.DAT` has been drained. Frame 0 is expanded before `BODY.DAT` can reuse
its temporary PRG-RAM bytes. Duplicating routing is required because the bank
owned by the Sub CPU follows the display handoff, while delivery may run ahead
by more than one frame.

When feature bit 2 is set, the Sub also stages the 8,800-byte ADPCM table after
PALTAB and copies it to offset `+0x12800` of both physical banks. The decoded
PCM buffer is bank-local at `+0x14C00`. Two boot-time copies avoid every
per-frame table transfer or pointer change after a bank handoff.

## Header (1 sector = 2048 bytes)

First 22 bytes: `struct ">4sHHHHHHHHH"`.

| Off | Size | Field          | Meaning |
|-----|------|----------------|---------|
| 0   | 4    | magic          | `"TTRC"` |
| 4   | 2    | version        | format version (3 = fixed frames; 4 = rate-matched variable frames; 5 = startup PCM preload; 6 = split files and control-first body; 7 = packed routing entries; 8 = feature-controlled fixed-N2 cadence; 9 = checkpointed ADPCM and RF5C164 frequency field) |
| 6   | 2    | frames         | total frame count (`nfr`) |
| 8   | 2    | tcols          | tile grid columns |
| 10  | 2    | trows          | tile grid rows |
| 12  | 2    | cells          | total cells = `tcols * trows` (`C_CELLS`) |
| 14  | 2    | pool           | VRAM tile-pool size (number of resident slots) |
| 16  | 2    | base           | `POOL_TILE_BASE`; VRAM tile index of slot `s` = `base + s` |
| 18  | 2    | frame_sectors  | max sectors per frame = 5 (routing-byte cap; v4 frame size is `fsec`, see Routing) |
| 20  | 2    | n_seg          | number of palette segments |

Next 16 bytes: `struct ">LLLL"`.

| Off | Size | Field        | Meaning |
|-----|------|--------------|---------|
| 22  | 4    | prebuf_pat   | `Bpat`: number of cold patterns pre-buffered before frame 1 |
| 26  | 4    | routing_sec  | sectors occupied by the routing table |
| 30  | 4    | prebuf_sec   | sectors occupied by the prebuffer |
| 34  | 4    | ring_peak    | peak PRG-RAM ring usage (patterns), for buffer sizing |

The player reserves the final 16 KiB of each 1M Word-RAM bank for an identical
ROUTING copy. A v7+ stream uses one byte per frame and may therefore contain at
most 16384 frames. The packer rejects a longer stream before the table can
exceed either resident copy.

Then:

- byte 38: display mode. `0` = H32, `1` = H40, `2` = mode4 reserved for a
  future player path. If absent or zero in old streams, the player treats it as
  H32.
- byte 39: zero (pad);
- offset 40: `u32 f0_ctrl_sec` — sectors of frame 0's control block in
  `HEADER.DAT` (v2+);
- offset 44: `u32 f0_pat_sec` — sectors of frame 0's cold patterns in
  `HEADER.DAT` (v2+);
- offset 48: `u32 paltab_sec` — sectors of the PALTAB region (v3; 0 = none);
- offset 52: `u16 vsync_n` (v4) — nearest display-VBlank interval
  `N = round(59.94/fps)` (15fps→4, 24/30fps→2). This is a cadence/performance
  hint, not a request to round delivery-paced 24fps to 29.97fps. `0` in v2/v3
  streams (player defaults to 4 = 15fps). In v8 this remains a hint when
  `FEATURE_FIXED_N2` is clear. When that feature is set, it is authoritative
  and the Main CPU forces N=2 even if this hint is stale. A 24fps stream stores
  `N = 2` but leaves the feature clear, so it remains delivery-paced;
- offset 54: `u16 audio_bytes` (v4) — decoded RF5C164 samples per effective
  playback frame. PCM13 uses 888 at 15fps, 555 at 24fps, and 444 at 30fps.
  ADPCM22 uses an even count, normally 1472 at 15fps and 736 at 30fps; its live
  control size is derived as `4 + audio_bytes / 2`.
  `0` in v2/v3 (player defaults to 887);
- offset 56: `u16 fps_int` (v4) — nominal fps (15/24/30). When
  `FEATURE_FIXED_N2` is clear, the Sub CPU uses this as the modulus of the
  delivery-paced 75/fps sector schedule (see Routing/Frame). The fixed-N2
  feature instead selects 1001/400. Historical v2/v3 streams stored `0`; the
  v8+ players require a nonzero nominal fps and reject `0`;
- offset 58: `u16 audio_fd` (v9) — RF5C164 frequency delta derived from the
  fixed decoded chunk size and effective playback cadence. Older formats used
  this word as a duplicate-skip count; v9 requires a nonzero frequency value;
- offset 60: `u16 audio_preload_sec` (v5+) — sectors in STARTUP_AUDIO. v5+
  uses one decoded PCM chunk per sector. The requested value is clamped by
  wave-RAM capacity and the decoded chunk size;
- offset 62: `u16 features` (v6+ optional extensions). Bit 0
  (`FEATURE_COLD_RUNS`) means every control block appends the cold-slot run
  suffix described below. Bit 1 (`FEATURE_FIXED_N2`, v8) is the authoritative
  two-VBlank timing flag: it makes the Main CPU force N=2 and the Sub CPU use
  the 1001/400 sector accumulator. The packer derives it with
  `uses_fixed_n2_cadence`; 24fps leaves it clear even though its nearest
  `vsync_n` hint is also 2. Bit 2 (`FEATURE_ADPCM22`, v9) means live control
  audio is a checkpointed IMA chunk and the boot table region is present.
  Unknown bits must not move any legacy field;
- offset 64: 128 bytes = **`seg0`**, the CRAM palette (4 lines x 16 words) for the
  segment of frame 0, so the screen has correct colours before the first frame;
- offset 192: `u32 player_signature` — CRC-32 of bytes 0 through 63. The build
  generates `player_constants.inc` from this same sector and bakes the signature
  into both player objects. The Sub CPU compares it before accepting the stream,
  so combining a player with another profile's `HEADER.DAT` stops with a
  diagnostic instead of silently using the wrong immediate values;
- remainder up to 2048 is zero.

The player reads byte 38 while preparing frame 0, before entering `play_loop`.
It sets VDP H32/H40, the screen-column origin, and the matching VBlank DMA
budget once. The per-frame loop uses the cached values; it does not reread or
branch on the mode, so carrying the mode in the header adds no playback-loop
overhead.

A 128-byte CRAM block is 4 palette lines x 16 words; each word is
`0000BBB0GGG0RRR0` (Genesis colour). Only 15 of the 16 entries per line are
usable colour (entry 0 = transparent).

Every current encoder palette reserves two existing RGB333 colours before frame
quantisation. Among the 60 usable entries, the globally darkest colour (smallest
`R + G + B`) is moved to palette line 0, index 1 and the globally brightest is
moved to palette line 0, index 15. Only positions change: the complete 60-colour
multiset is identical, and index 0 in all four lines stays zero. Frames are then
quantised against that final grouping. This is a representation invariant, not
another stream-layout version.

## PALTAB (v3)

`paltab_sec` sectors right after the first sector of `HEADER.DAT`: all `n_seg`
segment palettes,
128 bytes each, back to back (16 per sector), zero-padded to a sector boundary.
At boot the Sub CPU stages this region into Word-RAM (same bank as frame 0) and
the Main CPU copies it **once** into a Main-RAM table (8 KB, capacity
`PALTAB_MAX_SEG` = 64 segments — see `tools/av_config.py`, asserted against the
player at build time). Every later palette switch just indexes this table via
the control block's `pal` byte, so palettes are independent of stream delivery
timing: a CD slip or re-seek can never corrupt the colours of a segment.
The PALTAB already contains the canonical line/slot order described above; the
packer rejects a stale decision log unless P0/index1 is tied for the global
minimum and P0/index15 is tied for the global maximum. These fixed entries let
a DEBUG player upload an opaque dark background and bright font once. Palette
switches require no colour search, glyph rewrite, font DMA, or extra VBlank wait.

## ADPCM_TABLE (v9, feature bit 2)

When `FEATURE_ADPCM22` is set, five sectors follow PALTAB. The first 8,800
bytes are the immutable big-endian decoder image and the remaining 1,440 bytes
are zero padding:

| Offset | Size | Contents |
|---:|---:|---|
| 0 | 2,848 B | `u16 next_index_x32[89][16]` |
| 2,848 | 5,696 B | `s32 signed_delta[89][16]` |
| 8,544 | 256 B | predictor-high-byte to RF5C164 output lookup |

At boot the Sub CPU stages this image in PRG-RAM and copies exactly 2,200 longs
to Word-RAM offset `+0x12800` in each physical 1M bank. The table is absent when
feature bit 2 is clear.

## STARTUP_AUDIO (v5)

Each STARTUP_AUDIO sector in `HEADER.DAT` begins with one decoded `audio_bytes`
PCM chunk and is zero-padded to 2048 bytes. This remains PCM for an ADPCM22
stream so boot requires no separate compressed staging. During boot the Sub CPU drains one
sector, appends its chunk to
wave RAM at `SYNC_LEAD`, and repeats while PCM is stopped. Current streams put
as many leading source chunks here as fit below the wave-RAM safety limit, then
put the next chunk in frame 0's control and continue the shifted order. This
preserves exact source sample order and a persistent write reserve. Historical v5/v6 players
could instead duplicate leading chunks and skip their live writes; the v7+
player deliberately removes that obsolete path. PCM starts at the prefetched source prefix after frame 0
is displayed, so the first audio sample remains aligned with the first visible
movie frame rather than starting during the Mega-CD boot screen.

## Routing table

In v7 through v9, `routing_sec` sectors in `HEADER.DAT` hold **one byte per frame**. Each
byte has this layout:

| Bits | Field | Meaning |
|------|-------|---------|
| 0-2  | `n_ctrl_sec` | control sectors at the start of this BODY frame slot |
| 3-5  | `total_sec` | control plus payload sectors stored in this slot |
| 6-7  | reserved | must be zero |

The encoded byte is `(total_sec << 3) | n_ctrl_sec`, and the payload count is
recovered as `n_pay_sec = total_sec - n_ctrl_sec`. The player validates that
the reserved bits are zero and `n_ctrl_sec <= total_sec <= FRAME_SECTORS` before
using an entry. It also requires the header field to be exactly
`routing_sec = ceil(nfr / 2048)`; unused bytes in the final sector are zero.
Frame 0's routing byte is zero because its control and patterns live entirely
in `HEADER.DAT`, not in `BODY.DAT`; the player validates this zero entry too.

The final 16 KiB of each 1M Word-RAM bank holds an identical resident copy, so
v7+ supports at most 16384 frames. For comparison, v6 used two bytes per frame,
`[n_pay_sec, n_ctrl_sec]`, and the same 16 KiB allocation limited it to 8192
frames. v6 already stored and read the `n_ctrl_sec` control sectors first in
each `BODY.DAT` frame slot despite the historical payload-first byte order.

In v7+, the first `n_ctrl_sec` sectors contain control data. The following
`n_pay_sec` sectors refill the PRG-RAM payload ring, and any sectors through the
frame's rate-matched `fsec` are padding.

The control and payload data are each continuous byte streams split at sector
boundaries. A frame slot's sectors therefore do not necessarily belong only to
that numbered frame: one control sector can finish several future control
blocks, and payload is normally a forward prefetch for later frames. The packer
guarantees both of these before writing v6+:

- after frame `i`'s complete control-sector prefix has arrived, control block
  `i` is present in the apply ring; `n_ctrl_sec = 0` is valid when an earlier
  sector already carried that block;
- before frame `i`'s control prefix is read, PREBUFFER plus payload sectors from
  earlier frame slots already contain every cold pattern frame `i` consumes.

The table covers all `nfr` frames. In the historical v6 layout, frame 0's entry
was the two-byte pair `(0, 0)`; in v7+ it is the single zero byte described
above.

**Rate-matched frame size (v4+).** A frame's total on-disc sectors is
`fsec = max(n_pay_sec + n_ctrl_sec, ratedelta - lead)`. `ratedelta` is the number
of sectors CD 1x delivers in one frame's display time. Both packer and player
generate the same integer sequence with a numerator/modulus accumulator.

For a v8+ stream with `FEATURE_FIXED_N2` set, the Main CPU forces N=2 and flips
every exactly two VBlanks; the Sub CPU rate accumulator uses numerator 1001,
modulus 400:
`acc += 1001; ratedelta = acc // 400; acc %= 400`. Every complete 400-frame
cycle therefore contains 199 two-sector frames and 201 three-sector frames,
for exactly 1001 sectors total. When the feature is clear, including current
24fps and 15fps streams, v8+ retains the earlier delivery-paced accumulator:
`acc += 75; ratedelta = acc // fps_int;
acc %= fps_int`. This gives 3.125 sectors/frame at 24fps and a constant 5 at
15fps. v4-v7 used that same 75/fps rule for all supported nominal rates,
including the old 2.5-sector average at 30fps.

`lead` starts at zero and increases by
`fsec - ratedelta`. A data-heavy frame can therefore run long, while following
light frames omit padding until that temporary lead is repaid. The complete
stream converges to the CD 1x display-rate total without overflowing the ring.
Historical v2/v3 players defaulted `fps_int = 0` to 15, yielding the constant 5
and reproducing the old fixed-slot behaviour. The current player accepts only v9 and
rejects a zero nominal fps before entering this schedule.

The v6-and-later packer first spends that frame's allowance on control, then replaces
otherwise-unused rate padding with future payload while ring space is
available. It exceeds the allowance only when a backwards deadline proof says
a later cold-pattern burst cannot otherwise be armed within the five-sector
routing cap. The normal `lead` repayment then removes padding from following
light frames. In particular, a full startup ring is not refilled with all five
sectors merely to keep it full; with `FEATURE_FIXED_N2`, an ordinary light
region remains on the 1001/400 two-or-three-sector sequence.

## Prebuffer

The final `prebuf_sec` sectors of `HEADER.DAT` hold the first `Bpat` cold
patterns (32 bytes each) of frames 1 onward. Frame 0's patterns are in the
earlier FRAME 0 region. The prebuffer is loaded into the ring before playback
and is sized to fill the usable ring (`RING_CAP`), so frame 1 starts fully
armed.

## Frame (`fsec` sectors, rate-matched; frames 1..nfr-1)

```
[ n_ctrl_sec sectors : control ]  next bytes of the continuous control stream
[ n_pay_sec sectors : payload  ]  next bytes of the future cold-pattern stream
[ pad to fsec sectors ]
```

**Control** comes first so the Sub CPU can begin the current frame as soon as
its complete control prefix has arrived, without waiting for the future payload
refill in the same slot. Readiness is based on all `n_ctrl_sec` sectors, not
merely the first control sector.

**Payload** is a run of 32-byte tile patterns (the *cold* = newly loaded tiles),
consumed in order and DMA'd into ring slots. These sectors replenish later
frames; patterns for the current frame were armed by PREBUFFER or earlier body
slots. A pattern is `pack_key`-encoded: 8 rows x 4 bytes, each byte = two 4bpp
pixels `(hi<<4)|lo`.

**Control block** (a variable-length block, byte layout):

| Size | Field       | Meaning |
|------|-------------|---------|
| 2    | total_len   | total block length **including these 2 bytes**; always even |
| 2    | frame_seq   | frame sequence number (low 16 bits). The player checks this against the frame it expects; a mismatch means the stream desynced (e.g. a dropped CD sector) — the frame's updates are discarded (previous frame held) and the desync counter increments. |
| 2    | n_upd       | number of cell updates this frame |
| 1    | pal         | v3: `segment index + 1` = switch CRAM this frame to that entry of the pre-loaded PALTAB; `0` = no change. (v1/v2 used a 0/1 flag followed by a 128-byte in-stream CRAM payload.) |
| 1    | dbg         | 1 = a debug block follows immediately (see below), else 0 |
| 22   | debug       | **present only if `dbg==1`**: fixed-length debug block (below) |
| ceil(cells/8) | bitmap | one bit per cell; 1 = this cell is updated this frame |
| n_upd x 2 | entries | one big-endian word per update, in cell order (see below) |
| PCM: audio_bytes; ADPCM22: 4 + audio_bytes/2 | audio | PCM stores RF5C164 sign-magnitude bytes directly. ADPCM22 stores `s16 predictor, u8 step_index, u8 reserved_zero`, then low-nibble-first IMA codes. Current prefetched streams carry this logical source chunk `audio_preload_sec` frames ahead. |
| 0/1  | audio pad   | zero byte when needed to align the optional suffix to a word boundary and keep the legacy block end even |
| 2    | n_runs      | present when header feature bit 0 is set; number of cold-slot runs |
| n_runs x 4 | cold runs | present when feature bit 0 is set; repeated `u16 slot_start, u16 count` pairs in payload-consumption order |

The suffix repeats information already encoded by the cold entry flag and tile
index. For a 30 fps frame with at most 1024 updates, the current player can copy
each consecutive cold run directly without walking all update entries a second
time. The sum of all run counts is the number of cold entries. Each run stays
within the header's `pool` slots. Legacy players still advance by `total_len`
and ignore these trailing bytes; new players use the entry scan when bit 0 is
clear or when their proven fast path does not cover the frame.

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

- bit 15 (`0x8000`) = **cold**: this cell loads a fresh pattern from the
  already-prefetched payload stream into a ring slot. If clear, the cell only
  re-points its name-table entry to a pattern already resident.
- bits 13..14 = the palette line (0..3).
- bits 0..10 = the VDP tile index = `base + slot` (the player recovers the ring
  slot as `(entry & 0x07FF) - base`). Bits 11..12 are zero.

The entry's low 15 bits are written to the VDP name table as-is (priority and
flip bits are unused). This is the core *tile texture reuse*: most cells cost
just this 2-byte entry.

**Audio**: PCM streams carry `audio_bytes` per frame of RF5C164 sign-magnitude 8-bit PCM (positive
= `0..0x7F`, negative = `0x80 | magnitude`, magnitude clamped to `0x7E` so the
byte `0xFF` — the RF5C164 loop-stop marker — never appears), fed to the PCM chip
in sync. ADPCM22 controls carry the smaller checkpointed chunk described above;
the Sub CPU reconstructs exactly `audio_bytes` RF5C164 samples into its bank-local
buffer before calling the same wave-RAM writer.

## How the player advances (and the extension points)

The Sub CPU reads `total_len` at the start of each control block, copies exactly
`total_len` bytes out of the circular apply buffer (linearised into a Word-RAM
scratch area), and advances its cursor by `total_len` (`boot/movieplay_sp.s`).
It parses by length, not by scanning to a fixed end, so `total_len` must stay
even (odd desyncs by 1 byte per frame).

**Slip detection and recovery.** Right after copying a block the player checks
`frame_seq` against the frame it expects. A mismatch means the continuous
`BODY.DAT` read dropped a sector (the ring and control streams shifted). The player detects this
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
2. **Appending within `total_len`.** Feature bit 0 uses this extension point for
   the cold-run suffix. Because the player advances by `total_len`, trailing
   bytes remain skippable by a player that does not know them. Any future
   suffix must stay inside `total_len`, preserve the legacy audio position, and
   keep the complete block even.

## Reconstruction (player)

The player keeps a VRAM **tile pool** of `pool` resident patterns (an
LRU/double-buffer-protected ring, `base + slot` in VRAM) and a name table. Per
frame: if `pal`, reload CRAM from the Main-RAM PALTAB table (entry `pal - 1`);
DMA the payload's cold patterns into their slots; for every set bit in the
bitmap, apply its entry — cold entries having just filled a slot, warm entries
re-pointing the name table to an existing `base + slot`. Audio is streamed to
the PCM chip. A 1M/1M Word RAM double buffer swaps at frame boundaries.
