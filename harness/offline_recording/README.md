# Offline lossless recording

## Goal

This harness qualified a faster `/record` path that records the same native
FFV1/FLAC output without waiting for wall time. After the exact A/B and repeat
gates passed, the high-level recorder adopted this fixed-Replay path as its
default. It must preserve the complete Mega-CD startup, CD player, START
transition, DEBUG movie playback, and tail.

The offline path is acceptable only when its decoded video, decoded audio, and
packet timing are exactly equal to a realtime FFV1/FLAC run driven by the same
input Replay.

## Method

`tools/record_movie.sh` orchestrates the default path. `--offline-record`
remains an explicit, backward-compatible spelling of the same mode:

1. Build a DEBUG disc unless `--no-build` is explicit.
2. Record a RetroArch input Replay when none is supplied. The Replay is 120
   emulator frames longer than the main recording window.
3. Play that Replay with a fixed `--max-frames` limit.
4. Keep RetroArch's SDL dummy audio path initialized, but disable audio sync,
   audio rate control, and video vsync so emulation can run uncapped.
5. Record native FFV1 video and FLAC audio. Lossy presets and arbitrary FFmpeg
   recorder configurations are rejected in offline mode.
6. Exit RetroArch naturally, require normal runtime/core unload, reject Replay
   EOF, and require both recorded packet count and decoded-frame count to equal
   `--max-frames`.
7. Produce the same bounded lossless MKV and verification preview as the normal
   high-level recorder. `ffprobe` must find a non-empty audio stream; waveform
   thresholds are not a recording gate.

The fixed Replay matters. Recording the Replay-generation run itself is not an
equivalent baseline: in testing, RetroArch's Replay initial state shifted that
boundary by one stereo PCM sample. Both sides of an A/B must *play* the same
saved Replay.

## Commands

One-command default capture:

```sh
OUTDIR="$PWD/videos" tools/record_movie.sh \
  --config configs/ps2-sakura-op-h32.toml \
  --seconds 140 \
  --tag offline_record --record-size 256x224 --display :299 \
  --out videos/offline_record_preview.mp4
```

The command prints `REPLAY=...`. Reuse that exact file for the realtime
baseline and the repeat offline capture:

```sh
REPLAY=tmp/ps2-sakura-op-h32/record/offline_record_input.replay

OUTDIR="$PWD/videos" tools/record_movie.sh \
  --disc out/ps2-sakura-op-h32.cue --no-build \
  --seconds 140 --realtime-lossless --preset ffv1-flac \
  --input-replay "$REPLAY" \
  --tag realtime_baseline --record-size 256x224 --display :300 \
  --out videos/realtime_baseline_preview.mp4

OUTDIR="$PWD/videos" tools/record_movie.sh \
  --disc out/ps2-sakura-op-h32.cue --no-build \
  --seconds 140 --input-replay "$REPLAY" \
  --tag offline_ab --record-size 256x224 --display :301 \
  --out videos/offline_ab_preview.mp4

tools/python.sh tools/compare_recordings.py \
  videos/realtime_baseline_lossless.mkv \
  videos/offline_ab_lossless.mkv \
  --json videos/realtime_vs_offline.json
```

When requalifying, repeat the offline command with a new tag, then compare the
two offline bounded MKVs with the same comparator. Routine captures use their
built-in frame/packet, audio-stream, log, and visual gates without rerunning
the three-capture qualification.

## Exact gates

`tools/compare_recordings.py` performs a whole-file comparison with no trim,
seek, or automatic alignment:

- FFV1 pixel format, raster, frame rate, and stream metadata;
- every decoded video-frame hash and total frame count;
- every decoded stereo `s16le` PCM sample, SHA-256, and sample-frame count;
- every original Matroska packet PTS, DTS, and present duration;
- strict monotonic packet timestamps and equal total durations.

Routine recording deliberately has no RMS, clipping, or adjacent-sample jump
threshold. Those checks were content-dependent and could reject legitimate
source transients or artifacts introduced only by the lossy preview. Exact
same-Replay qualification still compares every decoded PCM sample.

## Measured environment

- Date: 2026-07-17
- RetroArch: 1.22.2, Git `b2ceb50`
- Core: system Genesis Plus GX libretro binary, SHA-256
  `40791618c03ea3f1fa04d925835b10671c8429c5ff9919ef58401303c57df920`
- Branch implementation: `251801413082431a7fd766c170d5773322afd242`
- Profile: H32 256x224, 30 fps movie, DEBUG HUD, PCM audio
- Requested bounded result: 140 seconds, with startup and tail
- Raw fixed run: 9000 emulator frames, 150.150 seconds of video time

The same 9120-frame Replay drove realtime, offline, and repeat-offline runs.

## Results

| Run | Main RetroArch wall | Main speed | High-level wall | Bounded size |
|---|---:|---:|---:|---:|
| Realtime FFV1/FLAC | 149.347 s | 1.01x | 178.759 s | 571,303,591 B |
| Offline FFV1/FLAC | 11.157 s | 13.46x | 40.592 s | 571,303,591 B |
| Offline repeat | 11.167 s | 13.45x | 40.567 s | 571,303,591 B |

The equal-setup high-level path was about **4.40x faster**. A separate
one-command offline run that also rebuilt the disc and generated its Replay
took 48.79 seconds (`PIPELINE_WALL_SECONDS=48.504`), still about **3.67x
shorter** than the 178.84-second realtime command. The final equal-setup
`/usr/bin/time -v` measurements were:

| Run | User CPU | System CPU | Reported CPU | Max RSS |
|---|---:|---:|---:|---:|
| Realtime | 186.27 s | 9.73 s | 109% | 900,988 KiB |
| Offline | 90.55 s | 2.89 s | 229% | 900,704 KiB |

Realtime versus offline and offline versus repeat both passed all exact gates:

- bounded duration: 140.016 seconds;
- decoded video: 8390 frames;
- video hash-sequence SHA-256:
  `22fb4307df8ed7f501b5d84475307baca1b8a34aec9cb7a3d557488576fd8207`;
- decoded audio: 6,174,720 stereo sample frames, 24,698,880 PCM bytes;
- PCM SHA-256:
  `2c0f18a7fe2b099ebc9617a3f7888e00c6f33056bb1378958ff3d464979b6887`;
- all video/audio packet timelines and compared stream metadata equal;
- archived diagnostic values from this qualification were RMS 869.948, peak
  12,724, and maximum adjacent-sample jump 4109. These are measurements, not
  current pass/fail gates.

A visual contact sheet confirmed the Mega-CD logo, CD player, licence screen,
DEBUG HUD movie playback through the full source, and post-movie tail.

The explicit realtime-lossless fallback also passed a wall-clock smoke run:
8.05 seconds of bounded 256x224 FFV1/FLAC, normal runtime/core unload, readable
trailer, a non-empty audio stream, and a successful preview transcode.

## Limits and decisions

- RetroArch 1.22.2 exposes no FFV1 recorder-FIFO wait counter. This work does
  not claim a FIFO wait duration. Exact 9000-packet/9000-decoded-frame raw
  counts, readable trailers, normal unload, and whole-content equality prove
  that no recorded frames were lost.
- FFV1 remains at two encoder threads. The measured 13.45x main-run speed
  already exceeds the target, so no thread-count change was needed.
- The qualified fixed-Replay path is now the high-level default. Use
  `--realtime-lossless --preset ffv1-flac` only for requalification or paced
  diagnosis; `--preset realtime` remains the separate H.264 4:2:0 check path.
- Existing post-processing remains sequential. The overall target was exceeded
  without adding parallel decode/transcode complexity.
- A Replay is tied to its disc, core binary, core options, and harness setup.
  Regenerate it after any of those change. A Replay must extend beyond the
  fixed run; reaching EOF is an error.
- Automatic Replay creation sends a short burst of START presses on wall-clock
  intervals. A newly generated Replay can therefore choose a slightly different
  emulator input frame on another host/run. Once saved, the Replay fixes those
  input frames exactly; all equality and repeatability tests must reuse that
  same file.
- Matroska container bytes may differ because of muxer metadata even when every
  decoded frame, PCM sample, and packet timestamp is equal. Use the comparator,
  not a whole-file checksum, as the content gate.
- No human listening pass was performed. The qualification establishes exact
  equality with realtime PCM, not an audible-quality claim.
