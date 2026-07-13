# Flexible fps (15 / 29.97 / …) — vsync-paced streaming

## The problem with the current player

The player is **CD-delivery-gated**: each frame occupies a fixed `FRAME_SECTORS = 5`
sector slot on disc, the player drains 5 sectors, processes, displays. So the frame
rate is `CD_1x / (FRAME_SECTORS x 2048)` = 15fps, locked. Padding fills the unused
part of each 5-sector slot. `AUDIO = 887 B/frame` is likewise a 15fps constant.

Can fps be freely variable? The blocker is **sector alignment**, not audio:

| fps | sectors/frame (75/fps) | integer? |
|---|---|---|
| 15 | 5.000 | yes |
| 25 | 3.000 | yes |
| 30 | 2.500 | **no** |
| 29.97 | 2.503 | no |
| 24 | 3.125 | no |

Only 15 and 25 pack into integer fixed slots. 30/29.97/24 cannot — so a fixed
per-frame sector slot can never do them. (25fps is rejected by the project.)

## The fix: decouple display timing from sector delivery

**Vsync-pace the display**: show a new frame every **N VBlanks** (NTSC 59.94 Hz),
with the CD streaming continuously into buffers (the ring + apply buffer absorb
per-frame data variation, since the *average* rate is within CD 1x). Then N — not
the sector count — sets the frame rate, and it lands on exact NTSC-locked rates:

| N (VBlanks/frame) | fps |
|---|---|
| 4 | 14.985 (“15”) |
| 3 | 19.980 (“20”) |
| 2 | **29.970 (“30”)** |

So **“30fps” is really 29.97** (N=2) — which is exactly the NTSC-correct rate we
wanted anyway, and each frame shows for exactly 2 vsyncs (no judder). “15fps” is
14.985 (N=4). This single mechanism gives 15 and 30 now, 29.97 for free (= N2),
20 (N3), and 24 later via 3:2 pulldown (N alternating 2/3 — judder, do last).

## Audio

At 13.3 kHz the per-frame audio chunk = `13300 x N / 59.94`: N=4 → 887.5 B,
N=2 → 443.8 B — **never a clean integer** (59.94 is fractional), and nudging the
rate (13320, 13290) doesn't fix it. That's fine: audio is a **continuous PCM
stream**, delivered in per-frame chunks that round ±1 to track the average, and
the existing write-ahead **SYNC** (lead / resync) already absorbs the sub-byte
drift. So AUDIO becomes **fps-derived** (`round(audio_rate x N / 59.94)`, ±1 per
frame to average), not a fixed 887. The sample rate can stay ~13.3 kHz; a small
adjustment is optional, not required.

## Implementation plan (branch `fps-flex`)

Minimal work items (the "1 and 2" from the discussion):

1. **pack** (`tools/pack_stream.py`):
   - Header carries **N (vsyncs/frame)** derived from the encode fps.
   - Stream frames so the average delivery matches the N-paced consumption (drop
     the rigid 5-sector-per-frame padding; size/pack to the fps data rate so the
     buffer never runs dry at N-paced display). Keep sector-aligned reads, but the
     per-frame *slot* is no longer the frame clock.
   - `AUDIO` fps-derived (per-frame chunk = `round(audio_rate x N / 59.94)`, ±1).
2. **player** (`boot/movieplay_*.s`):
   - Display advances every **N VBlanks** (N from header) instead of on
     sector-drain. The pump keeps streaming in the background between displays.
   - `AUDIO_BYTES` per-frame taken from the stream/header, not the 887 constant.
   - Keep the swap handshake once per frame.

Support order: **15 (N4) and 30/29.97 (N2) first**, then 24 (3:2 pulldown).

## Risks

The current sector-gated timing was hard-won (slip recovery, desync). Moving the
frame clock to VBlanks while the pump streams underneath is delicate: the buffer
must stay ahead at N-paced consumption, and the slip/desync recovery must still
work. Verify on ares/hardware (GPGX is lenient), starting with the cold-capped
Sonic H32 (256x208, N2 = 29.97) — the first real 30fps disc.
