# 22.05 kHz IMA ADPCM playback

TTRC v15 uses 22.05 kHz mono IMA ADPCM as its only audio format. The Sub CPU
decodes each timed control chunk and writes the reconstructed 8-bit
sign-magnitude samples to the RF5C164.

The earlier 68000 and Z80 experiments remain useful negative results. The old
68000 path did not have enough end-to-end streaming margin, while the Z80 path
made the Main CPU stop the Z80 through BUSREQ for every refill. That feeding
contention produced periodic audio artifacts. The current player keeps decode
and RF5C164 output on the Sub CPU.

## Format and codec

- Source audio is extracted as 22,050 Hz mono signed 16-bit PCM.
- The packer evenly retimes it to one fixed, even decoded-sample count per
  playback frame.
- IMA state is continuous across the movie. Every frame begins with a four-byte
  checkpoint: signed 16-bit predictor, 8-bit step index, and a reserved zero
  byte. A chunk can therefore be decoded independently after a seek or
  control-ring recovery.
- Two samples are packed per byte, low nibble first.
- `HEADER.DAT` startup audio is already reconstructed RF5C164 data, one chunk
  per sector. Timed control blocks carry future checkpointed ADPCM chunks so
  the wave-RAM write reserve remains persistent.
- Header offset 54 is the decoded RF5C164 sample count per frame. TTRC v15
  derives the control size as `4 + decoded_samples / 2`.
- Header offset 58 stores the RF5C164 frequency delta calculated from the fixed
  chunk size and actual playback cadence.

The encoder and independent reference decoder are in `tools/ima_adpcm.py`.
`tools/pack_stream.py --verify` reconstructs every audio chunk and proves that
the startup prefix plus shifted controls reproduce the source chunk order for
the complete movie.

The sim analysis and straight sim video use the same shared encode/decode path.
Their waveform and muxed audio therefore contain the reconstructed IMA signal
after RF5C164 8-bit conversion, rather than the clean signed-16 source WAV.
The preview WAV is timed at one decoded chunk per source-video frame; the
physical player uses the header's RF5C164 frequency delta and the actual NTSC
playback cadence.

## Full lookup tables in both 1M Word-RAM banks

The decoder uses one 8,800-byte full table image:

| Table | Size | Contents |
|---|---:|---|
| next index | 2,848 B | 89 step rows x 16 nibbles, stored as `new_index * 32` |
| signed delta | 5,696 B | 89 x 16 precomputed signed 32-bit predictor deltas |
| output conversion | 256 B | reconstructed predictor high byte to RF5C164 byte |

The image is stored once on disc after the boot stage. At boot the Sub CPU
stages it in PRG-RAM, copies it to Word-RAM offset `+0x12800`, swaps banks,
copies the second physical bank, then swaps back. The same addresses are valid
after every later frame handoff, so timed playback performs no table copy and
no bank-dependent pointer adjustment.

The decoded output buffer starts at bank offset `+0x14C00`. It reserves 1,536
bytes per bank, enough for the supported low-rate chunk maximum. The build
check proves that the table, buffer, control scratch, and resident routing
copies do not overlap.

## Sub-CPU hot path

Once the current control block is linear in Word RAM, the player:

1. loads the checkpoint;
2. decodes the two inlined nibbles of each packed byte through the full tables;
3. converts each reconstructed sample through the output lookup table;
4. sends the decoded buffer through the batched RF5C164 writer; and
5. continues with bitmap and cold-pattern expansion.

At low frame rates one decode chunk can run longer than a CD-sector interval.
The low-rate decoder therefore performs a non-blocking CDC poll at most every
512 packed bytes. Higher-rate specialized builds omit this polling counter and
call from the decode loop.

No codec state is carried in PRG-RAM. A malformed step index is clamped to 88,
and the fixed chunk size bounds every table and output-buffer access.

The DEBUG `Axx` HUD field measures the decode phase, including an opportunistic
CDC pump on low-rate profiles. One displayed unit is four 30.72 microsecond
Mega-CD stopwatch ticks, about 0.1229 ms.

## Qualification scope

The codec implementation, complete-stream reconstruction, player build matrix,
and emulator recording gate are part of the normal `/run` workflow. Physical
Mega-CD playback and additional display-mode or cadence combinations remain
broader portability checks rather than alternate-codec fallbacks.
