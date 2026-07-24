# Tile Texture Reuse Codec — a SEGA-CD / Genesis FMV codec

A full-motion-video codec built **specifically for the Sega CD**,
not a general video codec ported onto it. It targets the exact hardware the
Sega CD gives you — the Genesis VDP with its CRAM palettes and VRAM tile pool,
a constant-rate CD data stream, PRG-RAM as a buffer, and the RF5C164 PCM chip —
and squeezes moving pictures through those constraints on real hardware (and on
Genesis Plus GX).

> The codec name may change. The on-disc stream carries a `version` field, and
> file names in this repo are kept generic so a rename never breaks paths.

## Why this is a SEGA-CD-specific codec

The whole design falls out of one Genesis fact: **the screen is built from
8x8 tile patterns in VRAM, addressed by a name table.** A tile pattern already
resident in VRAM can be shown at any cell for the cost of a **2-byte name-table
entry**, versus **32 bytes** to send a fresh pattern. So the codec's core move —
the one it is named for — is **reusing tile textures already resident in VRAM,
across frames**, paying for a fresh pattern only when nothing resident is good
enough. General codecs think in pixels and macroblocks; this one thinks in
*"which resident tile is closest, and can I just re-point a name-table entry?"*

Everything else is shaped by Sega CD hardware, not by video theory:

- **CRAM palette budget.** The VDP shows at most 4 lines x 15 colours = 60
  colours at once. The codec trains those 60 colours, splits the movie into
  segments at safe (dark) cut points, and re-trains per segment via a single
  CRAM reload during the cut. A lossless row/index permutation keeps the
  globally brightest existing colour at P0/index15 without sacrificing any of
  the 60 colours.
- **VRAM tile pool + name table.** A persistent pool of resident tile patterns
  is kept in VRAM. Each frame the codec searches that pool for the best match to
  every changed cell and re-points name-table entries (2 bytes) instead of
  re-sending patterns (32 bytes). This *tile texture reuse* is the codec.
- **Best-match tiers (Near / Flbk).** With no exact resident match, a
  near-perfect resident may be accepted as `Near`. For everything else, one
  automatic three-phase pass commits cheap reuse, selects exact cold loads
  while reserving the two-byte fallback names, then lets `Flbk` reuse the best
  resident that improves the current display. Flbk may inspect adjacent
  mean-colour buckets only when the target bucket cannot improve the result.
  Both avoid a pattern transfer, but Flbk remains emergency fallback rather
  than normal quality.
- **Whole-movie quality budget + four pattern supplies.** The encoder first
  dry-runs the quantized movie through the same VRAM allocator used for the
  final encode. It selects the boot-only 256-entry DicBuf from whole-movie reuse,
  assigns WordBuf0 and WordBuf1 to the remaining risky bursts, then a backwards pass reserves only the offline quality
  allowance needed by future updates. Remaining patterns arrive through the
  streamed PRG-RAM PrgBuf. Quality funding and physical source are frozen
  independently for every update.
- **DMA-limited refresh.** How many tiles can be written to VRAM per frame is
  bounded by the VBLANK DMA window for the screen mode and fps, so the tile grid
  size is chosen to fit that budget.
- **RF5C164 audio, interleaved.** Checkpointed 22.05 kHz mono IMA ADPCM controls
  share the same CD stream. The Sub CPU reconstructs them into the
  wave-RAM writer, with a persistent startup lead keeping audio aligned. The
  analysis and straight sim videos audition the same reconstructed IMA and
  RF5C164-quantized samples, not the clean extraction used as packer input.
- **PRG-RAM discipline.** Buffers, queues, and PrgBuf live in PRG-RAM regions
  that stay safe during continuous CD reads (see [AGENTS.md](AGENTS.md) hardware notes).

## Configurable within Sega CD limits

Resolution, aspect, and frame rate are **encoder settings**, chosen per
source within what the hardware allows — not fixed project constants:

- **Display mode / resolution / aspect:** H32, H40, or mode4, with the tile grid
  sized to the per-frame DMA budget and the source's display aspect.
- **Frame rate:** the source's native rate is kept (15 / 24 / 30 fps, etc.).
- **Audio:** checkpointed 22.05 kHz mono IMA ADPCM, decoded directly by the Sub
  CPU and written to the RF5C164. It is the sole TTRC v16 audio format. Routine
  recording verifies audio stream structure but does not apply
  content-dependent waveform thresholds. Real hardware and additional
  cadence/display combinations are broader compatibility checks. See
  [ADPCM.md](ADPCM.md).
  The separate Z80-offload experiment remains shelved because BUSREQ feeding
  contends with Main CPU video work.

## Pipeline

Generic, source-side video handling (things any codec might do) lives here; the
Sega CD-specific compression is the "Encode" step.

1. **Preprocess** the source: crop black bars, scale, optionally remove the
   source's own dithering. Ordinary video preprocessing.
2. **Detect** fades / flashes as safe points for a palette change.
3. **Build palettes** per segment, weighting the k-means so thin high-contrast
   edges (e.g. anime line art) keep palette slots despite tiny area — a general
   image-quality trick, not a hardware one. The encoder then canonicalizes the
   palette rows and indices, remapping tile attributes and pixel indices so the
   rendered RGB333 image stays exactly the same.
4. **Quantize** each 8x8 tile to the chosen Genesis palettes (position-fixed
   Bayer dithering).
5. **Plan and encode (the codec):** dry-run the exact target to predict
   name-table and cold-pattern demand, spread any capacity-unavoidable
   Miss-risk shortage across that burst, and build backwards reserve curves
   that end at zero; then maintain the resident VRAM tile pool and reuse exact /
   near / fallback residents where possible and spend only the
   whole-movie quality allowance not reserved for a harder future burst. Exact
   cold loads are then assigned to Prg, Wr0, Wr1, or Dic.
6. **Pack** video control, tile payload, palettes, and ADPCM audio into the
   two-file CD stream: an armed startup `HEADER.DAT` and
   a continuously read `BODY.DAT`.

## Analysis

Every encode can be rendered as a 1920x1080 analysis overlay (left = decoded
Sega CD output, right = source / per-tile category map / Miss and MissCarry
state, bottom = bandwidth, four physical pattern supplies, DMA, waveform, and
stacked timelines).
[`ANALYSIS.md`](ANALYSIS.md) is the exact reference for every meter and tile
category.

## Documentation

- [README.md](README.md): this overview of the codec concept, pipeline, build
  targets, and repository layout.
- [ANALYSIS.md](ANALYSIS.md): the analysis-overlay reference, covering every
  panel, meter, timeline, and tile category drawn by `tools/render_analysis.py`.
- [HUD.md](HUD.md): the on-hardware values-only DEBUG HUD, including field
  meanings, timing units, layout, cross-field diagnosis, and OCR workflow.
- [BUEFFERING.md](BUEFFERING.md): the four physical pattern supplies, the
  separate whole-movie quality planner, diagnostics, and validation.
- [MOVIE.md](MOVIE.md): the exact `HEADER.DAT` / `BODY.DAT` on-disc stream
  format written by `tools/pack_stream.py` and read by the Sega CD player.
- [STREAMING.md](STREAMING.md): the live player memory maps and conservative
  Main/Sub CPU headroom for planning additional streaming features.
- [BUDGETS.md](BUDGETS.md): working notes for tile, DMA, CD bandwidth, and
  playback pipeline budgets used when choosing encoder targets.
- [ADPCM.md](ADPCM.md): the current v10 checkpointed 22.05 kHz Sub-CPU ADPCM
  design, full-table Word-RAM allocation, completed profile evidence, and the
  remaining physical-hardware compatibility scope.
- [AGENTS.md](AGENTS.md): agent and maintenance guidance, including hardware
  facts, recording rules, output paths, and documentation policy.
- [CLAUDE.md](CLAUDE.md): compatibility entry point for Claude-based agents; it
  points to the shared project guidance in [`AGENTS.md`](AGENTS.md).

## Implementation

- `tools/sim.py`: the offline encoder simulator — makes every per-tile
  decision and emits the decision log plus analysis data. Its seed and
  accounting passes share one invocation-local, identity-checked cache for
  palette/quantization/future-planning results; the cache is deleted when that
  invocation exits. A completed sim artifact remains reusable across
  invocations when the source bytes, effective settings, and encoder code
  fingerprint all match.
- `tools/pack_stream.py`: packs the decisions into `HEADER.DAT` and `BODY.DAT`,
  and writes the matching canonical segment-0 `palettes.bin` used to build the
  Main CPU player. It also writes their concatenation as an off-disc
  `MOVIE.DAT` compatibility file for analysis and regression tools.
- `tools/render_analysis.py` + `tools/layout_preview.py`: the analysis overlay.
  Disposable sim PNG/MP4 data lives in managed tmpfs behind the familiar
  `videos/` paths. Per-frame TSVs remain persistent below `logs/`, uniquely
  named by time, profile, short profile checksum, and encoder version.
- `boot/`: the Sub/Main CPU playback runtime for real hardware. DEBUG builds
  keep a values-only hexadecimal HUD in the top row of the inactive VDP Plane A
  movie table. H32 and H40 share the same 30-cell internal order
  `F/P/S/D/R/L/C/W/M/A/U/N/J`. `U/N` show Main pattern-transfer time and the
  source-aware cold-run count; `J` is the sticky maximum streamed PrgBuf
  occupancy above the stream's fps-derived normal ceiling. Only those 30 cells are
  replaced; the unused right-hand cells retain the current movie frame. Before
  playback, specialized DEBUG and
  release builds use the same 16-glyph font to show loaded PrgBuf KiB as four
  hexadecimal digits at the physical top-left. The font remains reserved above
  the 1,518-tile resident pool; its fixed P0/index15 colours do not blink across
  video-plane flips or palette switches.

## Build Targets

| Target | Purpose |
|---|---|
| `movieplay` | Current stream player (`disc`, the default target, is an alias). |
| `cdcbench` | Measures continuous versus restarted CD reads. |
| `dmabench` | Measures the largest VRAM DMA that fits in one VBlank, per screen mode. |
| `still256` | Static one-frame H32 still renderer (display bring-up test). |
| `streamtest` | Minimal continuous stream test. |
| `pcmtest` | RF5C164 PCM register and wave RAM test. |
| `test1m` | 1M/1M Word RAM swap test. |
| `prgtest` | PRG-RAM write and streaming interaction test. |
| `asictest` / `upscaletest` | Graphics ASIC and CPU upscale experiments. |

## Workstation Setup (Ubuntu)

Install the host-side encoder, disc, video, and headless-recording tools:

```sh
sudo apt update
sudo apt install \
  ffmpeg fonts-ipafont-gothic genisoimage imagemagick \
  libretro-genesisplusgx \
  pipx \
  retroarch rsync xdotool xvfb
```

`fonts-ipafont-gothic` supplies the exact font used by
`tools/layout_preview.py` and `tools/render_analysis.py`.

Install the pinned `uv` bootstrap, then let it install the project's managed
CPython. The CPU environment is fully isolated under `.venv`: it does not use
`/usr/bin/python` or distribution NumPy/Pillow packages.

```sh
pipx install 'uv==0.11.29'
tools/bootstrap_python.sh --cpu
tools/python.sh -c \
  'import sys, numpy, PIL; print(sys.base_prefix, numpy.__version__, PIL.__version__)'
```

For NVIDIA GPU acceleration, create a second isolated environment from the same
lock. The `ctk` extra supplies CUDA user-space libraries without depending on a
system CUDA Toolkit; the host NVIDIA driver is still required. Run the CUDA
probe outside a sandbox that hides `/dev/nvidia*`.

```sh
tools/bootstrap_python.sh --gpu
tools/python.sh --gpu -c \
  'import cupy as cp; assert int(cp.arange(16).sum()) == 120'
```

`tools/python.sh` selects `.venv`; `tools/python.sh --gpu` selects
`.venv-gpu`. It never falls back to a system Python or system site-packages.
The CPU environment uses managed CPython 3.14.4; the long-sim-qualified GPU
environment uses managed CPython 3.13.14. Both pin NumPy 2.3.5 and Pillow
12.1.1, while the GPU extra pins CuPy 14.1.1. The version files and lock make
both environments reproducible.

Install a Marsdev `m68k-elf` toolchain at `~/toolchains/mars`. The Makefile
expects these executables by default:

```text
~/toolchains/mars/m68k-elf/bin/m68k-elf-as
~/toolchains/mars/m68k-elf/bin/m68k-elf-gcc
~/toolchains/mars/m68k-elf/bin/m68k-elf-ld
~/toolchains/mars/m68k-elf/bin/m68k-elf-objcopy
```

Set `MARSDEV=/another/path` or `M68K_PREFIX=/another/prefix/m68k-elf-` when
using a different location. Run `make check-tools
CONFIG=configs/PROFILE.toml` to verify the toolchain and ISO writer before a
full build.

The Japanese Mega-CD BIOS used for local testing is a user-supplied,
git-ignored file at `original/jp_mcd2_9212.bin`; it is not distributed by this
repository. Install that project-local copy for Genesis Plus GX as follows:

```sh
install -d -m 700 ~/.config/retroarch/system
install -m 600 original/jp_mcd2_9212.bin \
  ~/.config/retroarch/system/bios_CD_J.bin
```

The recording harness uses the distro Genesis Plus GX core at
`/usr/lib/x86_64-linux-gnu/libretro/genesis_plus_gx_libretro.so`. Override
`CORE`, `BIOS_IMAGE`, or `SYSTEM_DIR` only when the distro or BIOS layout
differs. Each run stages `BIOS_IMAGE` as `bios_CD_J.bin` and prints its SHA-256
so Replay and capture provenance stay explicit. Replay generation presses
START once per second across the BIOS/CD-player transition instead of relying
on one BIOS revision's fixed startup time.

## Build

Required tools: `uv`, Marsdev / `m68k-elf` toolchain, `mkisofs` or
`genisoimage`, `ffmpeg` / `ffprobe`, and a Sega CD BIOS for emulator testing.
Bootstrap `.venv` (and `.venv-gpu` for an NVIDIA encode) as described above.

For a new encode, run the pipeline in this order:

```sh
tools/python.sh --gpu tools/sim.py configs/PROFILE.toml
make disc CONFIG=configs/PROFILE.toml DEBUG=1
```

`make disc` first removes any packed stream left by an older format, profile,
or decision log. It then runs the packer's complete verification against the
current profile-authenticated `decisions.pkl` before building the player disc
as `out/PROFILE.iso` + `out/PROFILE.cue`. This prevents a stale
`HEADER.DAT`/`BODY.DAT` pair from entering a new disc. The packer writes `HEADER.DAT`,
`BODY.DAT`, `MOVIE.DAT`, and `palettes.bin` under `out/PROFILE/`, using the
TOML filename as the artifact identity. Transient assembler files, disc staging,
and the default direct-emulator scratch area are separated under `tmp/PROFILE/`.
`HEADER.DAT` contains all startup state, including an exact Raw/Same-only frame
0, any future patterns placed into otherwise-free VRAM during that boot load,
and the PrgBuf prebuffer;
`BODY.DAT` starts at frame 1 and is read continuously. Their on-disc names
remain fixed because the player opens those TTRC format names.

## Recording

Use the headless RetroArch harness (emulator-synchronized A/V output; do not
remux offline audio when verifying playback):

```sh
tools/record_movie.sh --config configs/PROFILE.toml \
  --seconds 180 --tag STEM_emu --out videos/STEM_emu_preview.mp4
```

The high-level recorder defaults to a fixed-Replay, faster-than-realtime
native-size FFV1/FLAC lossless MKV under `videos/`; the MP4 is only its quick
verification preview. Use `--realtime-lossless` for a wall-clock-paced
FFV1/FLAC diagnostic baseline. An explicit `--preset realtime` capture uses
4:2:0 chroma and is not an upload master. The default keeps the Mega-CD startup
screens. Trimming is an explicit movie-only option, not part of the normal
recording or upload path.

## YouTube Upload Setup

Upload automation intentionally keeps OAuth credentials and its Python
environment outside the public repository. The project automation currently
expects this user-local layout:

```text
~/.claude/skills/youtube/youtube.py
~/.claude/skills/youtube/client_secret.json
~/.config/youtube/youtube_token.json
~/.config/youtube/venv/
```

Create the Python environment on each workstation instead of copying a venv
from another Python version:

```sh
uv venv --managed-python --python 3.14.4 ~/.config/youtube/venv
uv pip install --python ~/.config/youtube/venv/bin/python \
  google-api-python-client google-auth-oauthlib google-auth-httplib2
chmod 600 ~/.claude/skills/youtube/client_secret.json \
  ~/.config/youtube/youtube_token.json
```

If no reusable token is available, run the helper's `auth` command once on the
new workstation. A copied token is usable only when it carries the full
`youtube` scope and its refresh token remains valid. Never commit the client
secret, token, BIOS, source media, generated videos, or their upload sidecars.

## Repository Layout

```text
boot/        68000 IP/SP/Main player and test programs
cfg/         linker scripts
tools/       encoders, packers, analysis tools, and recording harnesses
vendor/      third-party reference code
```

Generated output and copyrighted sample media are not part of the public repo.
