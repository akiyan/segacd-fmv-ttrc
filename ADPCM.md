# 22.05 kHz IMA ADPCM playback

**Status: experimental, full-length emulator-qualified.** The current v9 path
decodes 22.05 kHz mono IMA ADPCM directly on the Sub CPU and writes the
reconstructed 8-bit samples to the RF5C164. H40 Sonic Jam completed all 2,714
frames in the lossless Genesis Plus GX recording with no CD slip, stream
desync, audio re-sync, or blocking CD pump. PCM 13.3 kHz remains the conservative
physical-hardware-qualified choice until this path is checked on a real console
and across the other supported rates and display modes.

The earlier 68000 and Z80 experiments remain useful negative results. The old
68000 player did not have enough end-to-end streaming margin, and the Z80 path
made the Main CPU stop the Z80 through BUSREQ for every refill. That feeding
contention produced periodic audio artifacts. The current design returns to the
straight Sub-CPU path after the video and streaming players were substantially
reduced in cost.

## Current format and codec

- Source audio is extracted as 22,050 Hz mono signed 16-bit PCM.
- The packer evenly retimes it to one fixed, even sample count per playback
  frame. H40 fixed-N2 uses 736 decoded samples per frame.
- IMA state is continuous across the movie. Every frame nevertheless begins
  with a four-byte checkpoint: signed 16-bit predictor, 8-bit step index, and a
  reserved zero byte. A chunk can therefore be decoded independently after a
  seek or control-ring recovery without resetting the codec.
- Two samples are packed per byte, low nibble first. An H40/N2 control carries
  `4 + 736/2 = 372` audio bytes and reconstructs 736 RF5C164 samples.
- `HEADER.DAT` startup audio is already reconstructed PCM, one chunk per sector.
  Timed control blocks carry future checkpointed ADPCM chunks so the wave-RAM
  write reserve remains persistent.
- Header offset 54 always means decoded RF5C164 samples per frame. Feature bit 2
  selects ADPCM and lets the player derive the smaller control size.
- Header offset 58 stores the RF5C164 frequency delta calculated from the fixed
  chunk size and actual playback cadence. This prevents wave-RAM lead drift.

The encoder and independent reference decoder are in `tools/ima_adpcm.py`.
`tools/pack_stream.py --verify` reconstructs every audio chunk and proves that
the startup prefix plus shifted controls reproduce the source chunk order for
the complete movie.

## Full lookup tables in both 1M Word-RAM banks

The decoder uses one 8,800-byte full table image:

| Table | Size | Contents |
|---|---:|---|
| next index | 2,848 B | 89 step rows x 16 nibbles, stored as `new_index * 32` |
| signed delta | 5,696 B | 89 x 16 precomputed signed 32-bit predictor deltas |
| output conversion | 256 B | reconstructed predictor high byte to RF5C164 byte |

The image is stored once on disc after PALTAB. At boot the Sub CPU stages it in
PRG-RAM, copies it to Word-RAM offset `+0x12800`, swaps banks, copies the second
physical bank, then swaps back. The same addresses are valid after every later
frame handoff, so timed playback performs no table copy and no bank-dependent
pointer adjustment.

The decoded PCM buffer starts at bank offset `+0x14C00`. It reserves 1,536 bytes
per bank, enough for the supported low-rate chunk maximum. The build check
proves that the table, buffer, control scratch, and resident routing copies do
not overlap.

## Sub-CPU hot path

Once the current control block is linear in Word RAM, the player:

1. loads the checkpoint;
2. decodes the two inlined nibbles of each packed byte through the full tables;
3. converts each reconstructed sample through the output lookup table;
4. sends the PCM buffer through the existing batched RF5C164 writer; and
5. continues with bitmap and cold-pattern expansion.

No codec state is carried in PRG-RAM. A malformed step index is clamped to 88,
and the fixed chunk size bounds every table and output-buffer access.

The DEBUG `Axx` HUD field measures decoder time. One displayed unit is four
30.72 microsecond Mega-CD stopwatch ticks, about 0.1229 ms. PCM builds report
zero.

## H40 Sonic Jam qualification

Profile: 320x224 H40, 40x28 tiles, 30 fps content on the fixed 29.970 fps N2
cadence, 2,714 frames.

| Result | Value |
|---|---:|
| Decoded audio | 736 samples/frame |
| ADPCM control audio | 372 B/frame, 50.5% of decoded PCM bytes |
| Full-table memory | 8,800 B in each physical Word-RAM bank |
| Decoder `A` | 62 minimum; 66 median, p95, p99, and maximum |
| Decoder time | 7.62 ms minimum; 8.11 ms typical/maximum |
| CD slip / stream desync | `S=0`, `D=0` for all 2,714 frames |
| Audio re-sync / blocking pumps | `R=0`, `C=0` for all 2,714 frames |
| Wave-RAM lead | 14,336 through 15,360 bytes |
| Offline IMA SNR on this source | 25.2 dB |
| Capture jump gate | 0 candidates at a 12,000 threshold; no clipped samples |

The same H40 Sonic PCM and ADPCM captures show no meaningful increase in the
Main CPU's short-wait HUD distribution: after excluding the V-counter wrap
values, mean `W` was 23.30 scanlines for PCM and 23.02 for ADPCM, with median 2
for both. This is expected because the CPUs are pipelined: most Sub work runs
while Main displays the preceding frame. The result proves this clip and build,
not a universal deadline bound.

The capture was checked programmatically and visually, but it has not been
human-listened in this environment. Physical Mega-CD playback is also still
unqualified.

## Why the retry fits when the old one did not

The codec cost per second has not disappeared. H40/N2 merely splits it into
smaller 736-sample frame jobs, and the measured decoder still consumes about
8.1 ms of every 33.4 ms frame. The meaningful change is the surrounding player:
control copying, cold-run expansion, CD pumping, Word-RAM handoff, Main pattern
transfer, and fixed-geometry work have all been reduced since the first ADPCM
attempt. The full bank-local tables also remove table transport and compact-table
arithmetic from the timed path.

The prior failure therefore remains a warning: static instruction counts are
not enough. BIOS calls, CDC readiness, Word-RAM access, and bank timing are not
captured by the decoder stopwatch. Every new profile still needs a full DEBUG
recording with stable `S`, `D`, `R`, `L`, `C`, `W`, and `A`.

## Main-CPU fallback

Main-side decode was considered but is not implemented in v9. In 1M/1M mode it
would have to decode a future chunk from the bank Main currently owns, return
the reconstructed PCM through a later handoff, and preserve the same startup
shift. That adds a one-frame ferry and competes with video DMA preparation.
Sub-direct is simpler and now passes the first full qualification, so Main
offload remains a fallback only if later physical-hardware or low-rate tests
show that the Sub path cannot hold its deadline.

## Remaining qualification

- listen to the complete lossless capture for codec texture and boundary clicks;
- run on physical Mega-CD hardware;
- record full H32/30, H40/24, and H40/15 profiles;
- confirm the RF5C164 frequency delta and stable wave-RAM lead for each cadence;
- keep PCM13 available as the fallback until those checks pass.
