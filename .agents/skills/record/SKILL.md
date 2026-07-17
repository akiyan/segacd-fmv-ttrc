---
name: record
description: Build a DEBUG Sega CD disc by default, then record it with RetroArch and Genesis Plus GX from emulator launch through the Mega-CD startup screens and playback, preserving synchronized A/V and producing a native-resolution lossless MKV plus a verification preview. Use for "record it", "capture playback as video", "record the OP", "verify the recording", or "/record". Build release only when the user explicitly requests it. Use DEBUG HUD OCR only for requested diagnostics, never for default head cueing. This skill records and verifies; compilation produces the final upload MP4 and publishes it.
---

# record: Sega CD Playback Recording

Record the emulator's own synchronized video and build-generated audio from launch through
the Mega-CD BIOS/CD player, START transition, movie, and tail. Keep the startup sequence by
default. Do not replace audio with an offline source.

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
`ffmpeg`, `ffprobe`, and `python3`. Keep the Sega CD BIOS in RetroArch's system directory.
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
  [--release-build]
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
  Explicit `realtime` uses H.264 with 4:2:0 chroma for a faster synchronized check, writes
  `_native.mkv` rather than `_lossless.mkv`, and must not feed an upload compilation.

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

## What the harness guarantees

`tools/run_headless.sh` records with RetroArch's FFmpeg recorder; Xvfb only supplies the
headless display. Every recording uses the SDL audio clock and rejects a duration outside
0.60x--1.50x of its requested wall-clock run.

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
2. Confirm the harness timing report is near the requested wall-clock duration. Reject a
   multi-times-faster capture.
3. Confirm the RetroArch log ends with normal core unload and `Average monitor Hz` near 60;
   reject a log ending at `SET_GEOMETRY`.
4. Confirm the audio JSON exists, has nonzero RMS when required, and reports zero clip/jump
   candidates at the selected thresholds.
5. Inspect frames from the MKV and confirm that the Mega-CD startup appears first, playback
   begins later, the DEBUG Window HUD is visible, and the movie advances. Do not use the HUD
   to seek the movie start.

The JSON report is a mechanical gate, not a substitute for listening. Claim that no audible
clicks exist only after listening to the final file.

## Optional HUD diagnostics

The standard capture already builds DEBUG. Parse `F/P/S/D/R/L/C/W/M/A` and the H40-only
`U/N` fields only when the task asks for those diagnostics; otherwise leave the visible HUD unparsed. Keep OCR work separate from
ordinary recording and publication head cueing:

```sh
ps -eo pid,etimes,args | grep -v grep \
  | grep -iE "sim\.py|render_analysis\.py|retroarch|Xvfb|record_movie|run_headless"
make disc CONFIG=configs/PROFILE.toml DEBUG=1
OUTDIR="$PWD/videos" tools/run_headless.sh out/PROFILE.cue \
  --tag STEM_debug --record --record-preset ffv1-flac \
  --record-size 256x224 --audio-min-rms 1 \
  --shots 68 --interval 2 --display :NNN
```

Confirm the contiguous Window-plane HUD is visible before a long OCR scan. H32 uses
`FxxxxPxxSxxDxxRxxLxxCxxWxxMxxAxx`; H40 appends `UxxxxNxx`. `F/U` contain four
hexadecimal digits; every other field contains two. Read the requested counters over the
complete loop. In H40, `U` is the Main pattern-transfer time in 30.72 us Mega-CD
stopwatch ticks and `N` is the packed cold-run count's low byte (wrapping at
256). Never reuse this OCR as a publication trim point.

## Existing recordings and smoke tests

Verify an existing file without recording again:

```sh
tools/verify_recording.sh videos/rec_check_preview.mp4 \
  --out-prefix tmp/PROFILE/verify/rec_check_postcheck
```

Run a headless smoke test without video recording:

```sh
tools/run_headless.sh out/PROFILE.cue \
  --tag smoke --shots 8 --interval 1 --display :250
```

## Audio and boot triage

Useful audio gates:

```sh
--audio-jump-threshold 12000
--audio-jump-threshold 6000
--audio-min-rms 1
--no-audio-check
```

Use `--no-audio-check` only while isolating harness failures. If a full startup-inclusive
capture is quiet at the beginning, verify the complete audio stream and movie section instead
of silently trimming the startup.

For boot failures, inspect frames extracted from the MKV rather than live Xvfb screenshots.
Keep the foreground defaults for boot wait and repeated START presses. If output is missing,
black, silent, or durationless, inspect the RetroArch/Xvfb logs, try another display number,
and rerun one capture in the foreground.

Raw FFV1 captures can be several GB. Keep the bounded upload input until publication is
complete, then remove only artifacts created by this session when space is needed.

## Report

Report the raw MKV path, preview MP4 path, duration, raster/fps, audio presence, audio JSON
path and key RMS/peak/clip/jump values, whether startup was retained, and whether human
listening was performed.
