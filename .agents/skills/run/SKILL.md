---
name: run
description: >-
  Orchestrate the complete SEGA-CD FMV delivery pipeline for one or more source
  videos: inspect geometry, create a strict H32/H40 profile, simulate and verify
  the packed stream, make a DEBUG lossless emulator recording, require its
  complete HUD gate to pass, then render/upload the analysis and create/upload
  the boot-preserving square-pixel compilation. Use when the user invokes
  "$run", says "same as usual", or asks for /sim, /record, and /compilation as
  one end-to-end job, including sequential batches.
---

# run: Complete FMV Pipeline

Take each source from inspection through both YouTube uploads. Finish and verify
one source completely before starting the next source.

Expected invocation:

```text
$run SOURCE MODE [work title, platform, year, and other label details]
```

`MODE` must be `H32` or `H40`. Do not infer a pixel aspect for `mode4`; its
upload path remains unsupported until the geometry harness verifies it.

## Scope

Include all of the following unless the user explicitly excludes a stage:

1. Source inspection and a checked-in-style strict TOML profile
2. Full simulation, persistent TSV, and detailed timeline PNG/Gist
3. Packed-stream verification
4. DEBUG disc build, synchronized native lossless emulator capture, and passing HUD gate
5. Analysis render, metadata, CRAM chapters, verification, and upload
6. Square-pixel playback compilation, boot-aware CRAM chapters, verification, and upload

Treat both uploads as part of `$run`, not as optional follow-up work. Upload
analysis and playback videos as unlisted, category 20.

Do not sync repositories, create or switch branches, commit, merge, or push.
Git operations are outside this skill.

## Load the Governing Instructions

Before acting, read these files completely:

1. `AGENTS.md`
2. `.agents/skills/sim/SKILL.md`
3. `.agents/skills/record/SKILL.md`
4. `.agents/skills/compilation/SKILL.md`

Use those files as the detailed source of truth. This skill defines their
ordering, handoffs, gates, and completion criteria; it does not replace their
stage-specific rules.

## Establish the Run Identity

Resolve and retain one run record containing:

- absolute source path;
- display mode and full native raster;
- work title and source label;
- source platform and year when known;
- source raster, SAR/DAR, frame rate, duration, and audio presence;
- confirmed black-bar crop, if any;
- TOML profile path;
- artifact stem and sim output directory;
- encoder/player version from `tools/av_version.txt`.

Discover missing values from the source, nearby profiles, and user context when
the answer is unambiguous. Ask only when a missing title or source identity
would make public metadata materially wrong.

Use one profile and one stem throughout sim, pack, record, and compilation.
Never hand-copy different geometry or timing into a later stage.

## Enforce Shared-Machine and CUDA Safety

Before every sim, analysis render, pack/build, or emulator capture, run the
shared-machine process check from `AGENTS.md`. Wait while the other kind of
heavy work is active. Never overlap sim/render with an emulator capture, never
run two captures together, and never kill another session's process.

Use the locked GPU Python environment without a system or legacy fallback:

```sh
tools/python.sh --gpu -c 'import sys; print(sys.executable)'
```

Verify `nvidia-smi` and a small CuPy allocation outside a restricted sandbox
before diagnosing CUDA. A sandbox-only `cudaErrorNoDevice` or missing
`/dev/nvidia*` is not a driver failure. Do not reinstall drivers or reboot on
that evidence. Use CPU only as a deliberate fallback after confirming the host
GPU is genuinely unavailable or when the user requests it.

## Stage 1: Inspect and Profile the Source

Follow `sim` source inspection exactly:

- inspect at least one ordinary content frame;
- use `ffprobe` for exact raster, SAR/DAR, frame rate, duration, and audio;
- run crop detection in at least three separated content sections;
- crop only fixed edge-to-edge black bars confirmed by both samples and visual inspection;
- keep the source frame rate, including first-time 24 fps material;
- use the full H32 256x224 or H40 320x224 raster;
- preserve the displayed aspect with the mode's HAR-aware fit/pad conversion;
- allow starvation instead of shrinking the raster.

Create or update one strict `schema_version = 1` profile under `configs/`. Put
the exact full duration, source timing and aspect, mode raster, audio format,
output path, encoder settings, palette settings, and DEBUG pack settings in the
profile. Use ADPCM22 unless the user explicitly requests PCM13 or a
physical-console-qualified fallback. Use the filename-derived profile identity
and canonical `videos/` artifact paths from `AGENTS.md`.

Do not bump `tools/av_version.txt` merely for a new source profile. Apply the
version policy in `AGENTS.md` if output-affecting encoder or player code changes.

## Stage 2: Simulate and Publish Numeric Evidence

Run `tools/sim.py` with the profile and preferred GPU Python. Require a normal
completion and record:

- frame count and effective source fps;
- average useful BODY delivery rate (`body_useful_bps`), kept separate from
  the encoder's `codec_work_bps` diagnostic;
- starved-frame count and percentage;
- resolved output raster/grid and audio settings.

Starvation is reportable, not automatically a failure. Reject an incomplete
run or missing decision data. Band divides useful bytes by each slot's actual
physical CD read time, so it must stay at or below CD 1x (150 KiB/s); pad is
shown as unused bandwidth.

Write the persistent TSV immediately with the zero-frame analysis-data mode,
run the `timeline` skill, inspect the PNG, publish it to a public Gist, and show
it to the user. Do not render, mux, verify, or upload the full 1920x1080
analysis MP4 yet. The emulator recording and complete HUD gate must pass first.

## Stage 3: Pack, Prove, and Build the DEBUG Disc

Build the disc against the same profile:

```sh
make disc CONFIG=configs/PROFILE.toml DEBUG=1
```

The Make target removes every previous packed stream file first, runs the
packer's full verification against the profile-authenticated current decisions,
and only then builds the specialized player and ISO. Require it to walk the
complete stream successfully and confirm simulation/packed preview agreement,
delivery/ring result, frame ordering, and audio ordering. Retry a transient
host-process failure with diagnostic output; do not waive a failed proof or
reuse files left by an older format.

## Stage 4: Record and Verify Playback

Use `record` with the same profile and the exact DEBUG disc just proved in
Stage 3. Pass `--no-build` only for that exact current disc so the recorder does
not repeat the already-completed verified pack. Keep the Plane A HUD,
and retain the full Mega-CD startup. Choose a launch-to-tail duration at least
30 seconds longer than the source when using the default
`original/jp_mcd2_9212.bin`, so its roughly 21-second verified startup plus a
short ending margin are both retained. `record`
uses the qualified fixed-Replay offline FFV1/FLAC path by default. Use:

- `ffv1-flac`;
- `--record-size 256x224` for H32 or `320x224` for H40;
- an unused X display;
- the canonical `videos/<stem>_emu_lossless.mkv` and preview paths.

Record emulator-synchronized A/V. "Offline" means unpaced emulation, not an
offline audio replacement. Never replace the recorded audio with the source
and never trim the normal compilation input.

Before accepting the recording, verify:

- native raster, about 60000/1001 fps, audio, and bounded duration with `ffprobe`;
- exact raw packet/decoded-frame counts, media-to-wall speed, and normal
  RetroArch/core shutdown logs;
- a non-empty recorded audio stream with valid codec, sample-rate, channel,
  and packet metadata;
- startup screens, later movie playback, visible DEBUG HUD, progression, and tail;
- representative lossless frames against the sim when timing or fps behavior is new or suspect.
- one complete HUD loop with `harness/startup_resync/analyze.py --gate-json`;
  pass the encode profile as the required second positional argument and
  require every expected movie frame. Fixed-N2 requires `S/D/R/C=00`, `M<=01`;
  delivery-paced 15 fps permits `C<=04`, `M<=04`; delivery-paced 24 fps permits
  `C<=03`, `M<=03`. Every cadence requires `S/D/R=00` and `J<=17`.
  Explicitly report whether `J` exceeded the 20 KiB jitter headroom (`J>14`).
  Preserve the CSV and passing gate JSON next to the recording.

Use `tools/extract_verification_frames.sh` for representative recording stills. Pass named
timestamps and a `videos/<stem>/record_check` base; inspect only the new directory and its
manifest/montage. Never build a montage from a shared `*.png` glob or loose stills left by a
previous capture.

Do not apply waveform-threshold gates to routine recordings; legitimate source
transients and lossy-preview ringing make them content-dependent. State that
human listening occurred only if it actually occurred. Call this an emulator
recording, not a physical hardware recording.

Use full HUD OCR only for requested diagnostics or to investigate a failure.
Never use HUD OCR to choose a publication head cue or chapter offset.

Do not enter Stage 5 when the HUD gate is missing or fails. Never waive or edit
the sidecar.

## Stage 5: Render and Upload the Analysis

Only after Stage 4 produced a matching `pass: true` gate JSON, render the full
canonical 1920x1080 analysis with `tools/render_analysis.py`. Verify its video,
audio, duration, and selected frames. Confirm the source aspect, content,
category/miss panels, and layout are visually credible.

The full render writes another persistent TSV. Immediately run the `timeline`
skill for that TSV, publish the PNG to a public Gist, show it to the user, and
put the Gist URL in the YouTube description.

Generate CRAM chapters with `tools/youtube_chapters.py`. Build the title and
English-then-Japanese description from the current `AGENTS.md` convention,
including the repository URL in both language sections and never adding source
bitrate or angle brackets. Upload the newly rebuilt analysis as unlisted,
category 20. Use `--force` only for a re-upload and retain the returned URL.

After the analysis upload succeeds, report the exact `S/D/R/C/M/J` maxima and
continue to the already-authorized playback compilation/upload. Do not request
another approval merely because the gate ran.

## Stage 6: Compile and Upload Playback

Pass only the latest verified native lossless MKV with its matching passing
HUD gate JSON to `compilation`. Bake the
validated H32/H40 pixel aspect into 2048x1568 square pixels using nearest-neighbor
scaling, H.264 CRF 10 slow, yuv420p, AAC 192 kbps, and faststart. Do not add
`-ss`, `-t`, an fps filter, or `-r`.

Watch the completed recording normally and determine when movie frame 0 begins.
Use that time only as the CRAM chapter offset; do not trim the video and do not
derive the time from the DEBUG HUD.

Verify the final MP4 has:

- the complete Mega-CD startup and tail;
- 2048x1568 raster, SAR 1:1, and DAR 64:49;
- the recording's frame rate and nearly identical duration;
- video, audio, and undistorted movie content.

Extract startup/movie/tail stills with `tools/extract_verification_frames.sh`, using
`videos/<stem>/compilation_check` as the base. Inspect only that invocation's printed
`CHECK_DIR`; do not mix files from an older compilation.

Generate boot-aware CRAM chapters and current bilingual metadata according to
`AGENTS.md`, then upload as unlisted, category 20. Use `--force` only for a
re-upload and retain the returned URL.

## Failure and Resume Policy

Stop before the next source whenever a stage fails. Preserve logs and evidence,
identify the failing layer, fix it when the requested scope permits, and rerun
the failed stage plus every downstream stage whose inputs changed.

An absent or failed `S/D/R/C/M/J` gate is a Stage 4 failure. Do not create or
upload either public MP4 until a newly recorded, complete loop passes.

For new frame rates such as 24 fps, do not hide a player, recorder, or encoder
defect by changing fps, shrinking the raster, loosening checks blindly, or
substituting offline audio. Prove whether an anomaly is in the source, sim,
pack, playback, or harness, and resume only after the exact case passes.

On an interrupted run, inspect timestamps, profile hashes, reports, and logs.
Reuse an artifact only when it was produced by the current code, profile, and
source and has already passed the relevant gate. Rebuild every public upload
artifact from current inputs, as required by `AGENTS.md`.

## Completion Report

Report one compact result block per source with:

- profile and artifact stem;
- analysis URL, output path, average rate, and starvation result;
- pack verification result;
- lossless recording and preview paths, duration, raster/fps, and audio metrics;
- whether startup was retained and whether human listening was performed;
- playback compilation URL and path, duration, raster/SAR/DAR, and audio presence;
- any diagnosed anomaly, workaround, or remaining limitation.

Do not call a source complete until both uploads succeeded and all preceding
verification gates passed.
