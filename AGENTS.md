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

## Language Policy

- Use English by default for repository files, documentation, code comments,
  and general project text.
- Use Japanese for commit messages, GitHub issues, and pull request comments.

## YouTube Upload Style (codec analysis videos)

Titles and descriptions for the codec analysis videos follow this fixed style.

- **Language**: English. In descriptions, write English first, then the same
  content in Japanese after it.
- **Title**: English, fixed format `SEGA-CD FMV of <work> - <specs>`.
  - `<work>`: the work name. For a native/kanji title, give the
    transliteration followed by the native title in parentheses, e.g.
    `Romaji (native)`. A romaji-only work needs no parentheses.
  - `<specs>`: the descriptive spec suffix (mode, resolution/grid, "max
    resolution", etc.). No version numbers.
  - Example: `SEGA-CD FMV of <Work> - mode4 max resolution 256x176/32x22`.
- **Description structure** (in both languages, in this order):
  1. Overview — one or two lines on what the video is.
  2. Output and source specs — the SEGA-CD output (mode, grid WxH, tile count,
     fps, audio, CBR rate, tank) and the Source (resolution, fps, audio).
  3. How to read the analysis layout — what each panel, meter, and timeline
     shows and how to interpret it (left = SEGA-CD sim output; right = Source /
     category map / per-metric flow graph; bottom status = Req / Band / Tank /
     Tank-delta / DMA plus the stacked timelines).
  4. What the encoder does — first a short list of the techniques applied, then
     the details for each.
  5. Project link — always include the source repository URL:
     `https://github.com/akiyan/segacd-fmv-ttrc` . Put it in every description
     (both the English and the Japanese section).
- Do not show bitrate in the Source spec line.
- Uploads are unlisted, category 20 (Gaming). Descriptive titles, not vNNN.
- **"Upload" always means the latest version.** Before uploading, rebuild the
  artifact from the current code and data (re-encode / re-render if anything
  changed since it was last made); never upload a stale file. Re-uploads use
  `--force` (the previous video stays unlisted).
- Never put `<` or `>` in the description — YouTube rejects it with
  `invalidDescription` (HTTP 400). Write "0.3s or more", "within 4s", etc.

## Documentation Policy

- Keep public documentation in `README.md`.
- Keep agent and maintenance instructions in `AGENTS.md`.
- Do not add new scattered Markdown documents for project notes.
- These dedicated reference docs are sanctioned and must be kept current:
  - `ANALYSIS.md` - the analysis-overlay reference (every meter/category/metric).
    Updated via the `/analysis` skill together with the layout code.
  - `MOVIE.md` - the `MOVIE.DAT` (TTRC) on-disc stream format. Keep in sync with
    `tools/pack_stream.py` and the `boot/movieplay_*.s` player.
  - `COMPARISON.md` - the comparison overlay (Real vs Encoder-ideal) layout,
    frame-sync, and pipeline. Keep in sync with `tools/comparison_preview.py`
    and `tools/render_comparison.py`.
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

The stream output is `MOVIE.DAT` using the TTRC layout.

Resolution, aspect, frame rate, and audio are **per-source encoder settings**
within Sega CD limits, not fixed presets:

- Display mode / resolution / aspect (H32 / H40 / mode4), tile grid sized to the
  per-frame DMA budget.
- Frame rate = the source's native rate.
- Audio = **PCM or ADPCM**, within the RF5C164 PCM chip's limits. The offline
  simulator currently uses ADPCM; on-hardware ADPCM support is planned, and PCM
  is the already-verified on-hardware path.

The old `OP.STR` / RLE and `PROBE.BIN` bring-up paths have been removed.
`make disc` builds the `MOVIE.DAT` disc played by `boot/movieplay_*.s`.

## Output Paths (videos/)

All generated video artifacts go under `videos/` (git-ignored, never committed —
they embed source frames). Do not accumulate video output in `tmp/`. Use one
stem per encode:

```
stem = <input-basename>_<display-mode>_<resolution>_<audio-format>
       e.g. OP1_ps2_H32_256x144_adpcm22
```

| Artifact | Path |
|---|---|
| Analysis-frame video (from `sim`) | `videos/<stem>_analysis.mp4` |
| Straight sim output, video+audio, no overlay (`export_sim_video.py`) | `videos/<stem>_sim.mp4` |
| PNGs, logs, stats for that encode  | `videos/<stem>/` (the sim working dir) |
| Emulator recording (`record-mcd`)  | `videos/<stem>_emu.mp4` |
| Real (emu) + Encoder ideal (sim) side-by-side compare (`render_comparison.py`) | `videos/<stem>_comparison.mp4` |

- `<input-basename>`: the source file name without extension.
- `<display-mode>`: `H32` / `H40` / `mode4`.
- `<resolution>`: the Sega CD output resolution in pixels, `WxH` (e.g. `256x144`).
- `<audio-format>`: e.g. `adpcm`, `adpcm22`, `pcm`.

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
  (vertical striping). With this recipe the Main CPU never copies pattern
  bytes (build a run table, DMA straight from Word RAM, one repair word per
  chunk).
- DMA from Main RAM needs no correction. Trigger writes: first control word,
  then the second word containing CD5 (`0x80`); keep the pre-DMA register
  writes (`0x93-0x97`) before the control words.

## Debugging Method (hardware/emulator investigations)

When playback breaks, do not guess from one symptom. Prove each layer
innocent in order, with byte-level or frame-level evidence:

1. **Data layer**: replay the exact writer/reader logic in a small Python
   replica against the real `MOVIE.DAT` (all frames, not samples). If the
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

- Headless RetroArch with audio disabled runs 4-5x realtime. Never map
  screenshot wall-clock time to frame numbers; a shot at "12 seconds" can be
  far past the section you meant to check. Use `ISO_HOLD_N` for exact frames.
- A crashed RetroArch leaves the log ending at `SET_GEOMETRY`; a healthy run
  ends with `Average monitor Hz` shutdown lines. Check this before trusting
  black/garbage screenshots (exit code 139 = the game code crashed the
  emulator, usually via runaway reads past mapped regions).
- Every consumer of a shared data format must be updated together: a
  diagnostic path that still writes the old layout (e.g. `dump_ring_head`)
  will corrupt a new-format reader and can crash the whole emulator.
- Guard stream readers against corrupt counts (clamp to remaining, treat 0
  as end). On compact formats, corruption otherwise turns into unbounded
  runaway reads instead of one glitched frame.

## HQ Deliverable Encode (final mp4)

The raw capture (`tmp/<tag>.mkv`, lossless) is 256x192 with non-square pixels.
For the final mp4, bake the pixel aspect and upscale with integer factors so
dots stay crisp:

```sh
ffmpeg -i tmp/<tag>.mkv \
  -vf "scale=1792:1152:flags=neighbor,setsar=1" \
  -c:v libx264 -crf 16 -preset slow -pix_fmt yuv420p \
  -c:a aac -b:a 192k out.mp4
```

- `1792x1152` = 7x horizontal, 6x vertical — this bakes the MD H32 PAR of 7:6
  exactly with integer scaling (nearest neighbor, no resampling blur).
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
- **The recording is not pixel-exact**: the capture is 192 lines (of a 224-line
  mode) and window screenshots are non-integer scaled, so pixel-perfect
  comparisons against decoded ground truth fail on dithered content (a 1px
  sampling shift flips half the dither pixels). Verify pixel-level issues via
  integer paths only (lossless mkv at native res, or emulator-side dumps), and
  treat cell-mean correlation as alignment-tolerant but detail-blind.

## Recording Rules

- Use emulator-synchronized A/V output for verification.
- Do not verify playback by replacing audio with an offline source.
- Prefer:

```sh
tools/record_movie.sh --disc out/MOVIEPLAY.cue --no-build \
  --seconds 180 --trim 0 --tag rec_delta --out tmp/op_delta.mp4
```

- Run one RetroArch/Xvfb recording at a time.
- If a run is black, silent, or has no duration, treat it as failed and rerun
  after checking `tmp/retroarch_<tag>.log` and `tmp/xvfb_<tag>.log`.

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
