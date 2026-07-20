# 22.05 kHz IMA ADPCM playback

**Status: implementation complete. H40 Sonic is full-length emulator- and
listening-qualified. H40/15 Machi OP and Machi ED, and the v10 four-supply
H40/30 Bad Apple profile, completed their full recording, HUD, stream, and
replay-equivalence checks.** The ADPCM22 path introduced in v9 and retained by
the current v10 stream decodes 22.05 kHz mono IMA ADPCM directly on the Sub CPU
and writes the reconstructed 8-bit samples to the
RF5C164. H40 Sonic Jam completed all 2,714 frames in the lossless Genesis Plus
GX recording with no CD slip, stream desync, audio re-sync, or blocking CD
pump. The corrected sim audio uses the same reconstructed IMA/RF5C164 sample
path, and listening comparison with captured playback was accepted. PCM 13.3
kHz remains the conservative physical-console-qualified choice until ADPCM22
is also cross-checked on a real console and across the other supported rates
and display modes.

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

The sim analysis and straight sim video use that same shared encode/decode
path. Their audio waveform and mux therefore contain the reconstructed IMA
signal after RF5C164 8-bit output conversion, rather than the clean signed-16
source WAV. The preview WAV is timed at one decoded chunk per source-video
frame; the physical player still uses the header's RF5C164 frequency delta and
the actual NTSC playback cadence. This distinction makes codec texture audible
without pretending that emulator clock behavior is part of the offline model.

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

At 15 fps the N4 chunk contains 1,472 decoded samples and its table decode runs
for about 16 ms. That is longer than the 13.3 ms CD-sector interval, so the
low-rate decoder also performs a non-blocking CDC poll at most every 512 packed
bytes. This prevents a long uninterrupted decode from letting a ready sector
age out. The profile-specialized 24/30 fps decoder emits no polling counter or
call in this loop, preserving the qualified Sonic N2 path.

No codec state is carried in PRG-RAM. A malformed step index is clamped to 88,
and the fixed chunk size bounds every table and output-buffer access.

The DEBUG `Axx` HUD field measures the decode phase, including an opportunistic
CDC pump on low-rate profiles. One displayed unit is four
30.72 microsecond Mega-CD stopwatch ticks, about 0.1229 ms. PCM builds report
zero. In the Machi OP qualification, `A` was typically about `BC` because the
low-rate measurement also includes real sector draining performed by these
polls.

## H40 Sonic Jam qualification

Profile: 320x224 H40, 40x28 tiles, 30 fps content on the fixed 29.970 fps N2
cadence, 2,714 frames.

| Result | Value |
|---|---:|
| Timed cold cap | 178 patterns/frame |
| Decoded audio | 736 samples/frame |
| ADPCM control audio | 372 B/frame, 50.5% of decoded PCM bytes |
| Full-table memory | 8,800 B in each physical Word-RAM bank |
| Decoder `A` | 62 minimum; 66 median, p95, p99, and maximum |
| Decoder time | 7.62 ms minimum; 8.11 ms typical/maximum |
| CD slip / stream desync | `S=0`, `D=0` for all 2,714 frames |
| Audio re-sync / blocking pumps | `R=0`, `C=0` for all 2,714 frames |
| Wave-RAM lead | 14,336 through 15,360 bytes |
| Display cadence | all 2,713 timed intervals exactly two VBlanks; no extra scanout |
| Main pattern transfer | 17.69 ms maximum; at most one VBlank wait |
| Offline IMA SNR on this source | 25.2 dB |
| Sim model | shared packer-reference IMA decode plus RF5C164 8-bit conversion |
| Listening | corrected sim reconstruction and captured playback accepted |

The same H40 Sonic PCM and ADPCM captures show no meaningful increase in the
Main CPU's short-wait HUD distribution: after excluding the V-counter wrap
values, mean `W` was 23.30 scanlines for PCM and 23.02 for ADPCM, with median 2
for both. This is expected because the CPUs are pipelined: most Sub work runs
while Main displays the preceding frame. The result proves this clip and build,
not a universal deadline bound.

The capture was checked programmatically and visually. After the sim stopped
muxing clean source PCM and began reproducing the IMA decode plus RF5C164 8-bit
conversion, the reconstructed sim audio and captured playback were also checked
by listening and accepted. This closes the ADPCM22 implementation. Physical
Mega-CD playback remains a separate portability qualification.

The 178 cold limit is also a delivery qualification, not a DMA ceiling. A cap
of 179 kept the DEBUG slip counter at zero but inserted one extra scanout
between frames 30 and 31. A cap of 200 still completed Main pattern transfer in
19.81 ms, but exhausted payload delivery margin and held `S=2` from frame 2,126
onward. This distinguishes the Sub/CD delivery margin from Main DMA time.

## H40/15 Machi OP qualification

Profile: 320x224 H40, 40x28 tiles, 15 fps content on the N4 cadence, 2,293
movie frames. This qualification was added after the original recording showed
long holds at `F0107`, `F0166`, and `F0391`.

| Result | Before p56 | p56 low-rate CDC polling |
|---|---:|---:|
| Decoded audio | 1,472 samples/frame | 1,472 samples/frame |
| ADPCM control audio | 740 B/frame | 740 B/frame |
| Final CD slip / stream desync / audio re-sync | `S3 D0 R0` | `S0 D0 R0` |
| Main VBlank waits | maximum `M2` | maximum `M2` |
| `F0107` capture hold at about 59.94 fps | 16 display frames | 3 display frames |

The old `F0107` hold was therefore not a Main-CPU four-VBlank stall. The N4
decode ran longer than a CD-sector interval without reaching a CDC poll, after
which the Sub CPU had to recover the missed delivery. Player p56 polls inside
that decode, and the full replacement recording completed with no slips,
desyncs, or audio re-syncs. Startup, movie start, `F0107`, middle, and tail were
also checked visually. No waveform-threshold gate is used by the current
recorder. No new listening comparison was made; the accepted listening
qualification remains the H40 Sonic result above.

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

## H40/30 Bad Apple v10 qualification

The current v10 four-supply path was qualified with 6,576 Bad Apple frames at
320x224 H40, 1,120 active tiles, 30 fps, cold cap 178, and ADPCM22. The packer
matched every frozen pattern source and run, the independent replay matched
every reconstructed VRAM cell, PrgBuf stayed at or above 19 ready patterns,
and the physical schedule had no under-run.

Across all 6,575 timed DEBUG groups, `S`, `D`, `R`, and `C` stayed zero. Main
VBlank wait `M` stayed at most one, Main transfer time `U` stayed at most 549
stopwatch ticks, run count `N` stayed at most 69, and ADPCM decode `A` stayed
between 62 and 66 units. The same Replay produced identical 14,801 video-frame
hashes, 10,893,312 decoded stereo PCM sample frames, packet timing, and stream
metadata in realtime, offline, and repeated-offline recordings. This proves
deterministic capture equivalence and stream integrity; no new listening claim
was made.

## Main-CPU fallback

Main-side decode was considered but is not implemented in the current v10
player. In 1M/1M mode it
would have to decode a future chunk from the bank Main currently owns, return
the reconstructed PCM through a later handoff, and preserve the same startup
shift. That adds a one-frame ferry and competes with video DMA preparation.
Sub-direct is simpler and now passes the first full qualification, so Main
offload remains a fallback only if later physical-hardware or low-rate tests
show that the Sub path cannot hold its deadline.

## Broader qualification (not implementation blockers)

- run on physical Mega-CD hardware;
- record full H32/30 and H40/24 profiles; repeat H40/15 on more sources;
- confirm the RF5C164 frequency delta and stable wave-RAM lead for each cadence;
- keep PCM13 available when a physical-console-qualified fallback is required.
