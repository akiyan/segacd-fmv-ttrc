---
name: record
description: Build a DEBUG Sega CD disc by default, then make a fast fixed-Replay FFV1/FLAC recording with RetroArch and Genesis Plus GX from emulator launch through the Mega-CD startup screens and playback, preserving synchronized A/V and producing a native-resolution lossless MKV plus a verification preview. Use for "record it", "capture playback as video", "record the OP", "verify the recording", or "/record". Build release or use realtime pacing only when explicitly requested. Use DEBUG HUD OCR only for requested diagnostics, never for default head cueing. This skill records and verifies; compilation produces the final upload MP4 and publishes it.
---

# record: Sega CD Playback Recording

Record the emulator's own synchronized video and build-generated audio from launch through
the Mega-CD BIOS/CD player, START transition, movie, and tail. Keep the startup sequence by
default. "Offline" here means unpaced emulation, not replacement with an offline audio source.

Run every command from the repository root.

## Role boundary

Use this skill to:

- build a DEBUG disc by default, or release only when explicitly requested;
- launch RetroArch, send START, and record synchronized A/V;
- validate timing, video, audio, logs, and optional diagnostic counters;
- return the raw lossless MKV, sidecars, and verification preview.

Do not apply upload PAR/upscaling, create YouTube metadata, or upload here. Pass the verified
lossless MKV to `compilation`. Do not locate `F0000` or trim to the movie unless the user
explicitly asks for a movie-only clip.

## Preconditions

Require `retroarch`, the Genesis Plus GX libretro core, `Xvfb`, `xdotool`, ImageMagick,
`ffmpeg`, `ffprobe`, and the locked `.venv`. Keep the Sega CD BIOS in RetroArch's system directory.
Use these overrides only when needed:

```sh
CORE=/path/to/genesis_plus_gx_libretro.so
SYSTEM_DIR=/path/to/retroarch/system
OUTDIR=/home/akiyan/segacd-novel/videos
```

Before recording, run the shared-machine exclusion check from `AGENTS.md`. Wait while any
sim/render or emulator capture is active. Never kill another session's process and never run
two captures together.

## Standard capture

Use `tools/record_movie.sh`, which owns the high-level recording workflow:

```sh
tools/record_movie.sh [--config TOML | --disc CUE --no-build] [--out MP4] [--seconds N] \
  [--trim SEC | --auto-audio-trim] [--tag NAME] [--display :N] \
  [--preset realtime|ffv1-flac] [--record-size WxH] [--no-build] \
  [--release-build] [--offline-record | --realtime-lossless] [--input-replay FILE]
```

Defaults and rules:

- Pass the same `--config configs/PROFILE.toml` used by sim and pack. The
  harness derives `out/PROFILE.cue` from the TOML filename.
- Use an explicit `--disc CUE --no-build` only for a previously verified image.
- Build with `DEBUG=1` by default. The Window HUD is part of the normal recording artifact.
- Use `--release-build` only when the user explicitly asks for a release build. It changes the
  harness build to `make disc CONFIG=configs/PROFILE.toml DEBUG=0`.
- Keep the startup sequence. The default is `--trim 0`; omitting `--trim` has the same result.
- Treat `--seconds` as the final duration from emulator launch. Include enough time for the
  startup screens, the full movie, and a short tail.
- Use `--trim SEC` or `--auto-audio-trim` only when the user explicitly requests a
  movie-only clip. Neither mode may be used for a normal `compilation` input.
- Use `--no-build` only after confirming in the current work that the disc represents the
  requested code/data and build mode. Unless release was explicitly requested, it must be a
  `DEBUG=1` disc; do not trust an unknown pre-existing image.
- Use `--record-size 256x224` for H32 and `--record-size 320x224` for H40.
- Use an unused display such as `--display :269`.
- Keep the preview MP4 under `videos/`. `OUTDIR` selects the raw MKV and sidecar directory;
  the high-level harness defaults it to `videos/`.
- A direct `tools/run_headless.sh out/PROFILE.cue` call defaults its screenshots,
  logs, PID files, and raw diagnostic capture to `tmp/PROFILE/record/`; do not
  put multiple profile runs directly in the shared `tmp/` root.
- `ffv1-flac` is the pixel-lossless default and the only normal input to `compilation`.
  The high-level default records it uncapped through a fixed Replay. Explicit
  `--preset realtime` uses wall-clock-paced H.264 with 4:2:0 chroma, writes `_native.mkv`
  rather than `_lossless.mkv`, and must not feed an upload compilation.
- `--offline-record` remains as an explicit spelling of the default. Offline always uses
  FFV1/FLAC; lossy presets and arbitrary low-level recorder configurations are rejected.
- `--realtime-lossless` selects the paced FFV1/FLAC fallback used to requalify or diagnose
  the offline path. It is not required for routine recordings.
- `--input-replay FILE` reuses an existing input Replay for an exact-frame paced or offline
  run. Reuse it only with the disc, libretro core, core options, and harness configuration
  that created it.

Canonical full capture for later upload:

```sh
OUTDIR="$PWD/videos" tools/record_movie.sh \
  --config configs/PROFILE.toml --seconds 180 \
  --tag STEM_emu --preset ffv1-flac --record-size 256x224 \
  --display :269 --out videos/STEM_emu_preview.mp4
```

Replace `STEM` and the mode-specific size. The harness records with a safety tail, then
stream-copies the requested launch-to-tail duration into the native lossless input at
`videos/STEM_emu_lossless.mkv`. This bounds the tail without seeking past the startup.
Do not delete it before `compilation` finishes.

For a short boot/playback check:

```sh
tools/record_movie.sh --config configs/PROFILE.toml \
  --seconds 30 --tag rec_check --display :269 \
  --out videos/rec_check_preview.mp4
```

## Default fast offline capture

Routine `$record` work uses faster-than-realtime FFV1/FLAC without an extra mode flag:

```sh
OUTDIR="$PWD/videos" tools/record_movie.sh \
  --config configs/PROFILE.toml --seconds 180 \
  --tag STEM_offline --record-size 256x224 --display :269 \
  --out videos/STEM_offline_preview.mp4
```

With no `--input-replay`, the high-level harness first records an input Replay under
`tmp/PROFILE/record/`, makes it 120 emulator frames longer than the main fixed-frame run,
and prints its path as `REPLAY=...`. Playback of that saved Replay fixes the captured input
frames. The recording retains the Mega-CD startup, CD player, START transition, full movie,
DEBUG HUD, and tail. Replay EOF before the frame limit is a hard failure.

Qualify an offline result against a realtime FFV1/FLAC run of the same Replay. Do not use
the Replay-generation run as the baseline: Replay initial-state handling can change its
audio boundary by one stereo PCM sample.

```sh
REPLAY=tmp/PROFILE/record/STEM_offline_input.replay

OUTDIR="$PWD/videos" tools/record_movie.sh \
  --disc out/PROFILE.cue --no-build --seconds 180 --realtime-lossless \
  --preset ffv1-flac --input-replay "$REPLAY" \
  --tag STEM_realtime --record-size 256x224 --display :270 \
  --out videos/STEM_realtime_preview.mp4

OUTDIR="$PWD/videos" tools/record_movie.sh \
  --disc out/PROFILE.cue --no-build --seconds 180 \
  --input-replay "$REPLAY" --tag STEM_offline_ab \
  --record-size 256x224 --display :271 \
  --out videos/STEM_offline_ab_preview.mp4

tools/python.sh tools/compare_recordings.py \
  videos/STEM_realtime_lossless.mkv videos/STEM_offline_ab_lossless.mkv \
  --json videos/STEM_offline_ab_compare.json
```

When requalifying the harness, run the offline command a second time with another tag and
require another passing exact comparison. The comparator checks every decoded video frame,
every decoded PCM sample, packet PTS/DTS/durations, stream metadata, and total counts without
trimming or alignment. Routine captures do not repeat the three-run qualification unless
RetroArch, the core, harness timing/recording code, or recorder settings changed, or a result
is suspect.

## What the harness guarantees

`tools/run_headless.sh` records with RetroArch's FFmpeg recorder; Xvfb only supplies the
headless display. Both modes initialize RetroArch's audio path through the SDL dummy sink,
so the core's PCM reaches the recorder without a physical output device.

- Default offline recording disables audio sync, rate control, and video vsync. It exits
  naturally after `--max-frames`, rejects Replay EOF, and requires packet and decoded-frame
  counts to equal the limit exactly.
- Explicit `--realtime-lossless` keeps audio sync and rate control enabled while preserving
  FFV1/FLAC. It is the paced baseline for qualification and diagnosis.

- `realtime` / `flac-fast`: x264 CRF 0 plus FLAC, but with 4:2:0 chroma, so the result is
  native-size and synchronized but not pixel-lossless.
- `ffv1-flac`: FFV1 video plus FLAC audio; use this raw MKV for pixel analysis and upload
  preparation because it preserves the recorder's pixels.

Stop RetroArch through the harness. Do not kill it first; doing so can leave an incomplete
Matroska trailer.

## Required verification

Check the raw MKV and reports before trusting a capture:

1. Use `ffprobe` to confirm video, audio, expected native raster, about 60000/1001 fps, and a
   valid duration.
2. For the default offline run, confirm the exact requested packet/frame count and report
   media-to-wall speed; faster-than-realtime is expected. For `--realtime-lossless`, confirm
   the harness timing is near the requested emulated duration.
3. Confirm exit zero plus `Content ran for a total of` and `Unloading core`. Some RetroArch
   builds also print `Average monitor Hz`, but 1.22.2 does not do so consistently. Reject a
   log ending at `SET_GEOMETRY`, a nonzero exit, Replay EOF, or an unreadable trailer.
4. Confirm that `ffprobe` sees a non-empty audio stream with the expected codec,
   sample rate, channels, and packet count. The recorder deliberately has no
   sample-jump, clipping, or RMS threshold gate: legitimate source transients
   and lossy preview encoding made those tests content-dependent.
5. Inspect frames from the MKV and confirm that the Mega-CD startup appears first, playback
   begins later, the DEBUG Window HUD is visible, and the movie advances. Do not use the HUD
   to seek the movie start. Extract these stills with
   `tools/extract_verification_frames.sh`; give it a `videos/<stem>/record_check` base and
   named `LABEL=SECONDS` samples. It creates a never-reused source-specific directory,
   records source/still hashes in `manifest.tsv`, and builds its montage only from the
   explicit files extracted by that invocation. Never montage a shared check directory with
   `*.png` or reuse loose stills from an earlier recording.
6. After changing RetroArch, the core, the offline harness, or recorder settings, requalify
   with a same-Replay `--realtime-lossless` comparison and a second offline run through
   `tools/compare_recordings.py`. Also requalify any suspect result.

Structural audio presence is not a substitute for listening. Claim that the
sound is clean or free of audible clicks only after listening to the final file.

## Optional HUD diagnostics

The standard capture already builds DEBUG. The HUD omits category glyphs and
shows values in the fixed internal order `F/P/S/D/R/L/C/W/M/A`, followed by the
H40-only `U/N` values. Parse them only when the task asks for those diagnostics;
otherwise leave the visible HUD unparsed. Keep OCR work separate from
ordinary recording and publication head cueing:

```sh
ps -eo pid,etimes,args | grep -v grep \
  | grep -iE "sim\.py|render_analysis\.py|retroarch|Xvfb|record_movie|run_headless"
make disc CONFIG=configs/PROFILE.toml DEBUG=1
OUTDIR="$PWD/videos" tools/run_headless.sh out/PROFILE.cue \
  --tag STEM_debug --record --record-preset ffv1-flac \
  --record-size 256x224 \
  --shots 68 --interval 2 --display :NNN
```

Confirm the contiguous Window-plane HUD is visible before a long OCR scan. H32
uses `xxxx xx xx xx xx xx xx xx xx xx`; H40 appends `xxxx xx`. The first value
(`F`) and the H40 penultimate value (`U`) contain four hexadecimal digits; every
other value contains two. Read the requested counters over the complete loop.
In H40, `U` is the Main pattern-transfer time in 30.72 us Mega-CD stopwatch
ticks and `N` is the packed cold-run count's low byte (wrapping at 256). Never
reuse this OCR as a publication trim point.

## Existing recordings and smoke tests

Inspect an existing file without recording again:

```sh
ffprobe -v error -count_packets \
  -show_entries stream=index,codec_name,codec_type,width,height,sample_rate,channels,nb_read_packets \
  -show_entries format=duration \
  -of json videos/rec_check_preview.mp4
```

Run a headless smoke test without video recording:

```sh
tools/run_headless.sh out/PROFILE.cue \
  --tag smoke --shots 8 --interval 1 --display :250
```

## Audio and boot triage

There is no waveform-threshold audio gate. If a startup-inclusive capture is
quiet at the beginning, inspect the complete movie section instead of silently
trimming the startup. Use `tools/compare_recordings.py` for exact same-Replay
PCM comparison, and listen to the final file before making an audible-quality
claim.

For boot failures, inspect frames extracted from the MKV rather than live Xvfb screenshots.
Keep the foreground defaults for boot wait and repeated START presses. If output is missing,
black, silent, or durationless, inspect the RetroArch/Xvfb logs, try another display number,
and rerun one capture in the foreground.

Raw FFV1 captures can be several GB. Keep the bounded upload input until publication is
complete, then remove only artifacts created by this session when space is needed.

## Report

Report the raw MKV path, preview MP4 path, duration, raster/fps, audio codec,
sample rate, channels and packet presence, whether startup was retained, and
whether human listening was performed. For offline runs also report the Replay
path, requested/max frame count, wall time, and speed. When the run requalifies
the fast path, additionally report the exact-comparison JSON/pass state and
repeat-run result.
