# Project Guidance

This repository is now a SEGA-CD FMV Encoder project. Do not steer new work
back toward the old game-specific port unless the user explicitly asks for it.

## Explanation Style

- Avoid math jargon and dense technical terms when a plain explanation is
  enough.
- Prefer everyday wording first.
- If a technical term is necessary, define it immediately in simple language.
- The priority is that the explanation is understandable, not that it uses the
  most formal vocabulary.

## Terminology and Intent Checks

This project has several similarly named objects whose substitution changes the
entire design. Be deliberately strict about checking the user's terminology
before beginning broad investigation, benchmarking, or implementation.

- If a statement combines terms in a way that looks inconsistent with the
  current format, hardware ownership, or the preceding discussion, treat it as
  a possible wording slip. Do not silently reinterpret it or investigate every
  possible meaning.
- First restate the intended **object, operation, and memory domain** in one
  short question. For example: "Do you mean keeping the routing table, rather
  than the pattern payload ring, resident in Word RAM?"
- This confirmation is required whenever the ambiguity would change the file
  format, memory map, bank ownership, buffering model, cycle analysis, or work
  branch. Ask even when one interpretation seems likely.
- After the user corrects a term, discard conclusions based on the mistaken
  interpretation and re-evaluate from the corrected object.

Keep these distinctions explicit:

- **routing table**: per-delivery-slot sector counts used to sort BODY sectors;
  it is not pattern data.
- **payload RING**: the PRG-RAM FIFO holding prefetched 32-byte cold patterns.
- **APPLY ring**: the PRG-RAM circular queue holding continuous control blocks.
- **resident pattern**: currently a pattern retained in the VRAM tile pool. A
  proposed Word-RAM pattern cache is a separate, second-level cache.
- **tank**: the simulator/packer model of usable payload buffering, not another
  physical player buffer.
- **Word RAM output bank**: the 1M/1M frame handoff area exchanged between Sub
  and Main CPUs; it is not automatically shared by both CPUs at once.

## Language Policy

- Use English by default for repository files, documentation, code comments,
  and general project text.
- Use Japanese for commit messages, GitHub issues, and pull request comments.

## Commit Attribution (all agents — Claude, Codex, etc.)

- Do NOT add `Co-Authored-By: Claude ...`, `Co-Authored-By:` any AI, or
  `Claude-Session:` / `Codex-Session:` trailers to commit messages. The public
  repo's Contributors must not list an AI assistant.
- Author and committer are the human owner only (`akiyan`); never an
  `@anthropic.com`/AI address.
- This repo is public and its history was rewritten once to strip such trailers.
  Every agent working here must follow this so it does not reappear.

## YouTube Upload Style (codec analysis videos)

Titles and descriptions for the codec analysis videos follow this fixed style.

- **Language**: English. In descriptions, write English first, then the same
  content in Japanese after it.
- **Title**: English, fixed format `SEGA-CD FMV of <work> - <specs> <ver>`.
  - `<work>`: the work name. For a native/kanji title, give the
    transliteration followed by the native title in parentheses, e.g.
    `Romaji (native)`. A romaji-only work needs no parentheses.
  - `<specs>`: the descriptive spec suffix (mode, resolution/grid, "max
    resolution", etc.). No **sequence** version numbers (no `vNNN`).
  - `<ver>`: the encoder/player build version `YYYYMMDD.eN.pM` (from
    `tools/av_version.txt`). `e` = encoder (sim.py / pack_stream.py params or
    code), `p` = player (`boot/movieplay_*.s`). Bump `e` and/or `p` whenever
    that side's output-affecting params or code change; never decrease either.
    When bumping, set the date to today if it differs. This is the title build
    version only — the TTRC `HEADER.DAT` format-version field is separate and must NOT
    be touched. Update `tools/av_version.txt` whenever you bump.
  - Example: `SEGA-CD FMV of <Work> - mode4 max resolution 256x176/32x22 20260710.e1.p1`.
- **Description structure** (in both languages, in this order):
  1. Overview — one or two lines on what the video is.
  2. Output and source specs — the SEGA-CD output (mode, grid WxH, tile count,
     fps, audio, CBR rate, tank) and the Source (resolution, fps, audio).
  3. How to read the analysis layout — what each panel, meter, and timeline
     shows and how to interpret it (left = SEGA-CD sim output; right = Source /
     category map / per-metric flow graph; bottom status = Req / Band / Tank /
     Tank-delta / DMA plus the stacked timelines). Define Band as useful
     `BODY.DAT` payload + control bytes in the physical delivery slot, excluding
     all pad, `HEADER.DAT`, and frame 0; note that bursts above CD 1x are shown
     without clamping and repay their lead in later slots.
  4. What the encoder does — first a short list of the techniques applied, then
     the details for each.
  5. Project link — always include the source repository URL:
     `https://github.com/akiyan/segacd-fmv-ttrc` . Put it in every description
     (both the English and the Japanese section).
- **CRAM chapters (permanent).** Every codec video (analysis and real-playback)
  MUST carry YouTube chapters at the CRAM (palette-segment) switch points, so the
  switches are navigable. Generate analysis chapters with
  `tools/youtube_chapters.py <sim_out>` and prepend the block to the description
  (a blank line after it), before the Overview. For a playback recording that
  retains the Mega-CD startup, pass
  `--content-offset <movie-start-seconds> --intro-label "Mega-CD startup"`.
  Determine the offset by ordinary visual playback, not DEBUG HUD OCR. It shifts
  chapter metadata only: do not trim or seek the recording. The tool reads the
  sim's `frame_seg` and enforces YouTube's rules (first at 0:00, 10 s minimum
  spacing, ascending). This is not optional or per-video — it is the standing
  convention for these uploads.
- Do not show bitrate in the Source spec line.
- Uploads are unlisted, category 20 (Gaming). Descriptive titles, not vNNN.
- **"Upload" always means the latest version.** Before uploading, rebuild the
  artifact from the current code and data (re-encode / re-render if anything
  changed since it was last made); never upload a stale file. Re-uploads use
  `--force` (the previous video stays unlisted).
- Never put `<` or `>` in the description — YouTube rejects it with
  `invalidDescription` (HTTP 400). Write "0.3s or more", "within 4s", etc.

## Documentation Policy

- Keep public documentation in [`README.md`](README.md).
- Keep agent and maintenance instructions in `AGENTS.md`.
- Keep Markdown self-contained. Do not link to or name GitHub issues from
  Markdown files; describe the current behavior or plan directly instead.
- Do not add new scattered Markdown documents for project notes at the repo root.
- **Harness / diagnostic docs are allowed under `harness/`.** When a debugging
  effort needs its own tooling and notes (detectors, repro scripts, findings),
  put both the scripts and their [`README.md`](README.md) under `harness/<topic>/`. This is
  the sanctioned place for build-your-own-detector work; keep each topic's doc
  next to its code so the harness stays reproducible.
- These dedicated reference docs are sanctioned and must be kept current:
  - [`ANALYSIS.md`](ANALYSIS.md) - the analysis-overlay reference (every meter/category/metric).
    Updated via the `/analysis` skill together with the layout code.
  - [`MOVIE.md`](MOVIE.md) - the `HEADER.DAT` + `BODY.DAT` (TTRC) on-disc stream format. Keep in sync with
    `tools/pack_stream.py` and the `boot/movieplay_*.s` player.
  - [`CONFIG.md`](CONFIG.md) - the tunable settings, throttles and buffers (ring/tank,
    cold cap, audio sync, CD pump, DMA budget, encoder knobs, per-source env). Keep in
    sync with `tools/av_config.py`, `tools/sim.py`, `tools/pack_stream.py` and the
    `boot/movieplay_*.s` player.
- Claude skill files under `.claude/skills/**/SKILL.md` are allowed and should
  remain in place.
- Do not reintroduce game-specific extraction notes or copyrighted sample
  metadata.

## Canonical Path

This project is the **Tile Texture Reuse Codec** (name may change; file names
are kept generic). The current encoder/player path is:

```text
tools/sim.py -> tools/pack_stream.py -> boot/movieplay_*.s
```

The on-disc stream is split into `HEADER.DAT` (all startup state, including
frame 0) and `BODY.DAT` (frame 1 onward) using the TTRC layout. The packer also
writes a concatenated `MOVIE.DAT` compatibility container for offline tools;
the player does not read it.

Resolution, aspect, frame rate, and audio are **per-source encoder settings**
within Sega CD limits, not fixed presets:

- Display mode / resolution / aspect (H32 / H40 / mode4), tile grid sized to the
  per-frame DMA budget.
- Frame rate = the source's native rate.
- Audio = per-profile `pcm13` or `adpcm22`; **ADPCM22 is the default**.
  **PCM13** (RF5C164, 13.3 kHz mono 8-bit) is the conservative
  physical-hardware-qualified fallback. **ADPCM22** is
  the completed checkpointed 22.05 kHz mono IMA path, decoded directly by the
  Sub CPU through full lookup tables duplicated in both physical 1M Word-RAM
  banks. H40 Sonic is full-length emulator-, automated-check-, and
  listening-qualified; H40/15 Machi OP with 720 active tiles and Machi ED with
  1,040 active tiles are full-length emulator- and automated-check-qualified.
  Physical hardware and the remaining modes are
  broader compatibility checks rather than implementation blockers (see
  [ADPCM.md](ADPCM.md)). Z80 offload remains shelved because BUSREQ-based
  feeding contends with Main CPU video work.

The old `OP.STR` / RLE and `PROBE.BIN` bring-up paths have been removed.
`make disc CONFIG=configs/PROFILE.toml` builds the `HEADER.DAT` + `BODY.DAT`
disc played by `boot/movieplay_*.s`. The TOML filename is the artifact identity:
packed stream files live under `out/PROFILE/`, transient build, disc-staging,
and direct-emulator scratch files live under `tmp/PROFILE/`, and the bootable
pair is `out/PROFILE.iso` + `out/PROFILE.cue`.

## Output Paths (videos/)

All generated video artifacts go under `videos/` (git-ignored, never committed —
they embed source frames). Do not accumulate video output in `tmp/`. Use one
stem per encode:

```
stem = <input-basename>_<display-mode>_<resolution>_<audio-format>
       e.g. OP1_ps2_H32_256x144_pcm
```

| Artifact | Path |
|---|---|
| Analysis-frame video (from `sim`) | `videos/<stem>_analysis.mp4` |
| Straight sim output, video+audio, no overlay (`export_sim_video.py`) | `videos/<stem>_sim.mp4` |
| PNGs, logs, stats for that encode  | `videos/<stem>/tmp/` (the sim working dir) |
| Lossless emulator capture (`record`) | `videos/<stem>_emu_lossless.mkv` |
| Verification preview (`record`) | `videos/<stem>_emu_preview.mp4` |
| Upload compilation (`compilation`) | `videos/<stem>_emu.mp4` |

- `<input-basename>`: the source file name without extension.
- `<display-mode>`: `H32` / `H40` / `mode4`.
- `<resolution>`: the Sega CD output resolution in pixels, `WxH` (e.g. `256x144`).
- `<audio-format>`: `pcm` for `pcm13`, or `adpcm22` for the completed Sub-CPU
  IMA path (see [ADPCM.md](ADPCM.md)).

## Hardware Facts

- Keep CD reads continuous where possible. Reissuing `CDC_STOP + ROM_READN`
  costs too much bandwidth.
- 1M/1M Word RAM bank swaps are cheap enough for frame-granular buffering.
- RF5C164 wave RAM writes use the odd byte window and correct PCM bank select.
- PRG-RAM `0x6800-0x8000` is unsafe during continuous reads; BIOS code touches
  it. Prefer safe high PRG areas for routing and queues.
- Long CDC drain gaps can silently drop sectors. Streaming code must keep
  pumping while Main CPU work is happening.
- `total_len` fields in apply/control blocks must stay even.

### VDP DMA rules (measured)

- Enable DMA first: VDP reg 1 bit 4 (M1, e.g. `0x8174`). The BIOS default
  leaves it off and DMA requests are then silently ignored (symptom: only
  CPU-written words appear, everything else stays blank).
- The DMA length registers (`0x93/0x94`) count down to zero during a transfer;
  rewrite them before **every** DMA. Reassert autoinc (`0x8F02`) too.
- Poll DMA completion (status bit 1) before touching VDP registers again.
- Issue DMAs inside VBlank only, and split large transfers across VBlanks with
  a word budget (`VBLANK_DMA_WORDS`); a transfer spilling into active display
  corrupts on strict emulators/hardware.
- **DMA from Word RAM: `src+2`, full length, normal destination, then CPU-write
  the destination's FIRST word after the DMA.** Verified against the GPGX
  source (`vdp_ctrl.c`): the first word the VDP receives is stale bus data and
  the last source word is discarded — the emulator models this as `dst += 2;
  length -= 1`, i.e. **the destination's first word is never written by the
  DMA**. So: program source = `src+2` (delivers `A[1..L-1]` to `dst[1..L-1]`),
  then repair `dst[0] = A[0]` with one CPU write per transfer. Without the
  repair, fresh tiles keep one stale VRAM word each (dark 4px dashes scattered
  on updated tiles; settled frames look clean). The variant "CPU first word +
  DMA `src+2 -> dst+2` with `len-1`" is WRONG here: every word lands one early
  (vertical striping). The default player uses this recipe for runs of three or
  more tiles and reuses the ordinary destination command for the repair. Runs
  of one or two tiles are faster as 8 or 16 direct `MOVE.L` writes from Word
  RAM; ordinary CPU reads do not have the DMA first-word defect. Set
  `DMA_RUN_FASTPATH=0` only for an all-DMA A/B build.
- DMA from Main RAM needs no correction. Trigger writes: first control word,
  then the second word containing CD5 (`0x80`); keep the pre-DMA register
  writes (`0x93-0x97`) before the control words.

## Debugging Method (hardware/emulator investigations)

When playback breaks, do not guess from one symptom. Prove each layer
innocent in order, with byte-level or frame-level evidence:

1. **Data layer**: replay the exact writer/reader logic in a small Python
   replica against the real `HEADER.DAT` + `BODY.DAT` pair (all frames, not samples). If the
   format walks cleanly, the data and format are innocent.
2. **Logic layer**: when changing stream layout or allocation, prove display
   equivalence in Python first (old vs new must produce identical
   cell->pattern states for every frame) before touching assembly.
3. **Assembly layer**: disassemble the built binary
   (`m68k-elf-objdump -m 68000 -b binary -D`) and read the changed region.
   Confirm the instructions match intent before blaming hardware.
4. **Regression check**: rebuild yesterday's artifact from a `git worktree`
   of the old commit and `cmp` it byte-for-byte with today's. `IDENTICAL`
   means the regression is not in the code you changed.
5. **Bisect in time**: use the `ISO_HOLD_N` freeze diagnostic to hold the
   player at frame N and screenshot. Binary-search N to find the first bad
   frame instead of staring at post-collapse garbage.
6. **Environment last, but verify it**: boot the BIOS with no START presses
   to confirm the emulator itself is healthy; check core/BIOS mtimes.

Emulator pitfalls learned the hard way:

- Uncapped headless RetroArch can run many times faster than realtime. Never
  map screenshot wall-clock time to frame numbers; a shot at "12 seconds" can
  be far past the section you meant to check. Use `ISO_HOLD_N` for exact
  diagnostic frames, or an input Replay plus `--max-frames` for an exact
  recording window.
- A crashed RetroArch commonly leaves the log ending at `SET_GEOMETRY`.
  A healthy run returns zero and reaches `Content ran for a total of` plus
  `Unloading core`; some RetroArch builds additionally print `Average monitor
  Hz`, but 1.22.2 does not do so consistently. A natural `--max-frames` exit
  also requires a readable Matroska trailer and packet plus decoded-frame counts
  equal to the requested limit. Check the appropriate ending before trusting
  black/garbage screenshots (exit code 139 usually means runaway reads past
  mapped regions).
- Every consumer of a shared data format must be updated together: a
  diagnostic path that still writes the old layout (e.g. `dump_ring_head`)
  will corrupt a new-format reader and can crash the whole emulator.
- Guard stream readers against corrupt counts (clamp to remaining, treat 0
  as end). On compact formats, corruption otherwise turns into unbounded
  runaway reads instead of one glitched frame.

## HQ Deliverable Encode (final mp4)

`record` owns the native lossless capture; `compilation` owns the upload
transcode. Keep the complete recording, including the Mega-CD startup. Do not
leave a non-square SAR for YouTube to rescale: bake the mode's pixel aspect into
a high-resolution square-pixel raster with nearest-neighbour scaling:

```sh
ffmpeg -i videos/<stem>_emu_lossless.mkv \
  -vf "scale=2048:1568:flags=neighbor,setsar=1" \
  -c:v libx264 -crf 10 -preset slow -pix_fmt yuv420p \
  -c:a aac -b:a 192k -movflags +faststart videos/<stem>_emu.mp4
```

- H32: 256x224 PAR 8:7 becomes 2048x1568 SAR 1:1. This is exact 8x horizontal
  and 7x vertical pixel replication.
- H40: 320x224 PAR 32:35 becomes the same 2048x1568 SAR 1:1 aperture. A
  practical-size exact integer replication is impossible, so nearest-neighbour
  assigns each source column to 6 or 7 output columns without colour blending.
- The nearest-neighbour enlargement preserves source colour samples, but the
  H.264 mezzanine and YouTube delivery are re-encoded and are not end-to-end
  lossless. Use CRF 10 to give YouTube a clean high-resolution input.
- Do not add `-ss`, `-t`, an fps filter, or `-r` to the standard upload path.
- Do not guess a mode4 PAR; verify it in the geometry harness before adding it.
- Telegram's bot limit is 50MB; send a `896x576` crf20 preview and keep the
  full-quality file on disk (or upload to YouTube).

## Debugging Method — additions

- **Transient vs persistent artifacts**: if an `ISO_HOLD_N` freeze of frame N
  is clean but live playback of the same frame shows artifacts, the corruption
  is stochastic per run (timing/phase dependent), not deterministic data
  corruption. Diff consecutive 60fps captures of one 15fps game frame: if the
  artifact is identical across them, the tile content was wrong for that whole
  game frame (upstream of scanout).
- **Quantify artifacts with a detector, then A/B builds**: eyeballing dithered
  frames misleads. Write a small detector (e.g. dark isolated horizontal
  dashes = pixels much darker than both vertical neighbors on a bright field)
  and run it over recordings of each build generation. 3.2/frame vs a 0.4
  false-positive floor identified the Word-RAM DMA first-word bug in one pass.
- **Legacy 192-line captures and window screenshots are not pixel-exact**:
  window screenshots are non-integer scaled, so pixel-perfect comparisons
  against decoded ground truth fail on dithered content (a 1px sampling shift
  flips half the dither pixels). Use the current native 256x224/320x224 FFV1
  recording or emulator-side dumps for pixel-level checks, and treat cell-mean
  correlation as alignment-tolerant but detail-blind.
- **The sim is a MODEL of the hardware — when the two disagree, suspect the sim,
  not just the encoder tuning.** The hardware's job is to reproduce the sim
  faithfully (the sim's Miss/residual is expected and fine; extra garbage or a
  freeze on top of it is a divergence). When the hardware cannot reproduce the
  sim *no matter how you tune*, STOP and question whether the sim mis-models the
  hardware's real limit — do not keep shaving the encoder to fit a symptom.
  Precedents: the CRAM emulation had a sim-side bug; an older 22.05 kHz ADPCM
  player exceeded the 68000 streaming margin, while the later optimized Sub
  path had to be re-qualified rather than inheriting that conclusion; Z80
  offload introduced Main-bus contention (see `ADPCM.md`); and the streaming
  ring: the sim's VBV tank was set equal to the player's ring
  (`RING_SIZE`=420 KB, TANK=440→400), i.e. it assumed the *entire* ring is usable
  for banking. Real CD-delivery jitter makes the usable ring smaller, so a
  schedule the pack calls feasible (`under`=0, `ring_min`≈1–2 KB) still underruns
  live. The fix is a sim-side correction — TANK a jitter margin *below* the ring
  (e.g. 350 KB) so the sim only decides loads the hardware can actually deliver —
  not a per-frame cold cap papering over it. Keep `pack_stream.py`'s
  `RING_CAP_KB` tied to the player's real `RING_SIZE` minus that margin. Shape
  useful payload to the CD-1x allowance (replace rate padding while space is
  available), and exceed that allowance only when the future-deadline proof
  requires a burst; the following light frames repay the temporary lead.

## Recording Rules

- Use emulator-synchronized A/V output for verification.
- Extract visual-check stills with `tools/extract_verification_frames.sh`. It
  creates a never-reused directory and a source-hashed manifest for each
  invocation, then builds the montage from that invocation's explicit frame
  list. Never montage a shared check directory with `*.png`; loose stills from
  an older recording or transcode can silently contaminate the result.
- Do not verify playback by replacing audio with an offline source.
- Real/emulator recordings use a `DEBUG=1` disc by default, including the Window HUD. Build
  release only when the user explicitly requests it. `tools/record_movie.sh` enforces this;
  its `--release-build` option is the explicit release override.
- Prefer:

```sh
tools/record_movie.sh --config configs/PROFILE.toml \
  --seconds 180 --tag STEM_emu --out videos/STEM_emu_preview.mp4
```

- The high-level recorder defaults to FFV1/FLAC and writes its bounded
  pixel-lossless MKV under `videos/`. It uses the qualified fixed-Replay offline
  path by default: the same DEBUG disc, Mega-CD startup, CD player, START
  transition, movie and tail, with audio sync, rate control and video vsync
  disabled so the fixed emulator-frame run can proceed uncapped.
- `--offline-record` is an explicit spelling of that default.
  `--realtime-lossless` selects the paced FFV1/FLAC fallback for qualification
  or diagnosis. Explicit `--preset realtime` instead selects paced H.264 4:2:0
  and must not be used as a `compilation` input.
- If the default has no `--input-replay`, `record_movie.sh` first records one
  under `tmp/PROFILE/record/`, 120 emulator frames longer than the main run.
  A supplied Replay must also extend beyond `--max-frames`; Replay EOF is a
  hard failure because RetroArch may otherwise repeat a cached end frame.
  Replays belong to the exact disc, core and configuration that created them;
  regenerate after any of those change.
- Requalify after changing RetroArch, the core, offline harness/recording code,
  or recorder settings, and whenever a result is suspect. Play the same Replay
  once with `--realtime-lossless --preset ffv1-flac` and once with the offline
  default. Run `tools/compare_recordings.py` on the two bounded MKVs and require
  exact decoded-frame hashes, PCM SHA-256/sample count, packet PTS/DTS/durations
  and stream metadata. Repeat the offline run and compare it too. The
  Replay-generation run is not the realtime baseline. Routine recordings use
  their built-in count/audio/log/visual gates without rerunning all
  three qualification captures.
- The default keeps the Mega-CD startup. Use trimming only when the user
  explicitly asks for a movie-only clip.
- Run one RetroArch/Xvfb recording at a time.
- If a run is black, silent, or has no duration, treat it as failed and rerun
  after checking `retroarch_<tag>.log` and `xvfb_<tag>.log` in the selected
  `OUTDIR` (`videos/` by default for `record_movie.sh`).

## Shared-Machine Exclusion (sim/render ↔ emulator)

This is a shared machine. The encoder passes and the emulator runs are both
CPU-heavy, and the headless emulator is timing-sensitive, so overlapping them
corrupts both (a slow sim starves the emulator's real-time capture; the
emulator steals cores from the sim). They MUST be mutually exclusive:

- **Before starting a sim/render** (`tools/sim.py`,
  `tools/render_analysis.py`), verify no emulator run is active.
- **Before starting an emulator run** (`tools/record_movie.sh`,
  `tools/run_headless.sh`, i.e. `retroarch` / `Xvfb`), verify no sim/render is
  active.
- If the other side is running, **wait for it to finish** before starting.
  Do not start concurrently.
- **Never kill another session's processes** — only stop what this session
  started. Other sessions' emulator/sim runs are theirs to finish.

One check covers both sides:

```sh
ps -eo pid,etimes,args | grep -v grep \
  | grep -iE "sim\.py|render_analysis\.py|retroarch|Xvfb|record_movie|run_headless"
```

Any match on the *other* kind of work means wait. This extends the sim-only
coordination rule (previously in the `/sim` skill) to cover the emulator too.

### Project Python environments

Do not run project tools with the system `python3` or inherit distribution
site-packages. The repository pins `uv`, CPython and every Python package:

- `tools/bootstrap_python.sh --cpu` creates the CPU environment in `.venv`.
  Use `tools/python.sh` for pack, tests, builds, recording checks and CPU tools.
- `tools/bootstrap_python.sh --gpu` creates the separate CUDA environment in
  `.venv-gpu`. Use
  `tools/python.sh --gpu` for GPU sim/render.
- The launcher must fail when its selected environment is absent. Never add a
  silent fallback to `/usr/bin/python`, a distro NumPy/Pillow, or an older venv.
- `.python-version`, `.python-version-gpu`, `pyproject.toml` and `uv.lock` are
  the reproducible source of truth. The environments themselves remain
  git-ignored.
- The YouTube credential environment is intentionally separate and is not part
  of the codec tool lock.

### NVIDIA/CUDA in the Codex sandbox

The normal Codex workspace sandbox can hide `/dev/nvidia*` even when the host
driver and GPU are healthy. Inside that sandbox, `nvidia-smi` then reports that
it cannot communicate with the driver and CuPy reports `cudaErrorNoDevice`.
Do not treat those sandbox-only symptoms as proof that the NVIDIA driver is
broken, and do not reinstall the driver or reboot the workstation on that
evidence alone.

- Check `nvidia-smi` and a small CuPy allocation outside the sandbox first.
- Run GPU `tools/sim.py` and `tools/render_analysis.py` outside the sandbox so
  they can access the NVIDIA device nodes.
- Use `tools/python.sh --gpu`, which selects the isolated `.venv-gpu` containing
  managed CPython 3.13.14 + NumPy 2.3.5 + Pillow 12.1.1 + CuPy 14.1.1. This
  exact environment completed a full 2,535-frame Lunar H32 sim. The CPU
  `.venv` remains on managed CPython 3.14.4. The former
  `cbrsim-gpu-stable` venv inherited system NumPy/Pillow and is rollback-only.
  The still older `cbrsim-gpu` venv's NumPy 2.5.1 corrupted long runs (segfaults
  and NumPy `SystemError`) even though short CUDA allocations passed. The sim
  rejects that exact unsafe combination before doing work.
- The GPU sim initializes CUDA before its CPU frame-loader pool. Those workers
  must use multiprocessing `spawn`, never `fork`: forking the live CUDA parent
  can segfault the parent interpreter part-way through precomputation.
  CPU-only sim runs may keep the cheaper `fork` path. GPU runs default to the
  verified four feeder processes; `CBRSIM_WORKERS` remains a diagnostic override.
- All supported Python versions default sim PNG output to synchronous writes.
  Six concurrent Pillow/NumPy writer threads corrupted NumPy array metadata on
  CPython 3.13 and crashed CPython 3.14 during long encodes.
  `CBRSIM_PNG_WORKERS` is a diagnostic override, not a normal tuning knob.
- If `/sbin/ub-device-create --verbose` says the `/dev/nvidia*` nodes already
  exist with correct permissions outside the sandbox, the host device setup is
  healthy; the missing nodes seen inside the sandbox are an isolation artifact.
- Reboot only when the outside-sandbox checks also fail and host kernel logs or
  device state support it.

### Analysis renderer multiprocessing in the Codex sandbox

`tools/render_analysis.py` creates a multiprocessing pool. The normal Codex
workspace sandbox can reject the pool's local IPC socket with
`PermissionError: Operation not permitted` even though rendering is healthy.
Run real-frame and full analysis renders outside the sandbox; do not replace a
failed pool render with a single-process result and call the multiprocessing
path verified. On Linux the renderer explicitly uses the proven `fork` context
because Python 3.14's new `forkserver` default can reset its worker connection
after the large read-only analysis tables have loaded.
