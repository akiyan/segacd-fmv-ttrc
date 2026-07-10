# ADPCM.md — 22.05 kHz ADPCM real-time decode: investigation & structural limit

**Status: shelved (structural limit).** The audio *decode* (~17–20 ms of 68000
compute per 15 fps frame) does not robustly fit in the per-frame budget on either
CPU during sustained motion. The verified **PCM** audio path remains the shipping
choice. The full ADPCM implementation is preserved on branch `adpcm-h40`; this
doc records why it doesn't fit so a future attempt starts informed.

Goal was: play the movie with **22.05 kHz IMA ADPCM** audio (fixed, no
PCM/ADPCM switching) on real Mega-CD hardware, under the TTRC streaming pipeline,
**without degrading video** and **without depending on the specific clip's motion
distribution** (must hold even for all-motion content).

## What works (verified, reusable)

- **Codec**: `tools/ima_adpcm.py` — continuous (no-block) IMA, low-nibble-first,
  735 B/frame → 1470 s16 samples. Bit-exact round-trip. State resets at loop head.
- **Pack**: `tools/pack_stream.py --audio-format ima22` — embeds 735 B/frame in
  the control block, `total_len` forced even. **Disc bandwidth is a non-issue**:
  ADPCM 22 kHz = 735 B/frame, *less* than the current 13.3 kHz PCM (887 B/frame).
- **Decoder (68000)**: offset representation (value = predictor+0x8000, kept in
  [0,0xFFFF] by a branchless bit-16 clamp), high-byte-via-stack, sign-magnitude
  output LUT, 2 nibbles inlined. Two Python-verified bit-exact table sets:
  - reduced 3816 B (fits the 0x8000–0x9800 safe PRG window): ~140 cyc/sample.
  - full 8.8 KB 2D (`ima_indices` = new_index*32 words, `ima_deltas` = signed
    longs): ~104 cyc/sample. Fits the now-dead `DMA_STAGE` Main-RAM.
- **A/V split proof**: `tools/verify_audio_split.py` proves the Main-offload ferry
  (decode on Main, play one swap later) is byte-identical (delay1 = 1-frame audio
  lag; preshift = exact) to the inline decode, across 2 loop iterations.

## Failure mode: the bistable ring ratchet

Always the same. On a run of frames where a CPU's per-frame work exceeds the
15 fps period (66.7 ms), the CD (fixed ~75 sectors/s) outruns consumption, the
pattern-ring prefetch depth (`drain_frame - frame_idx`) climbs monotonically,
`pump_poll`'s ring back-pressure eventually stops draining the CDC, the CDC FIFO
drops sectors, and the payload stream permanently desyncs → progressive tile
garbage. `slip_count` spikes at onset. It is **bistable**: once prefetch climbs,
the Sub races (data pre-buffered) and stops CD-blocking, so the idle that would
have absorbed the decode evaporates — positive feedback, non-recovering.

## What was tried, and how each failed (GPGX headless / real hardware)

| Placement | Collapse frame | Note |
|---|---|---|
| Sub inline (in `expand_frame`) | ~40–79 | decode is *sequential* → adds wall-clock |
| Sub decode-in-wait (label-3 swap-wait, sliced + pumped) | ~79 | still ratchets |
| Main offload, block decode before swap | ~120 | Main is the long pole on motion |
| Main offload, sliced into `wait_vb_start` idle | ~120 | swap-wait idle evaporates during motion |
| Main offload + full 8.8 KB tables | ~120 (unchanged) | 7 ms/frame saved didn't move it |

Diagnostic: the Sub exports prefetch depth to Word-RAM `O_PREFETCH` (0xAF7E); the
Main renders it as backdrop-green intensity. Healthy = dark/flat; over-budget =
green ramp before collapse.

## The decisive experiment (settles budget vs bug)

Keep the **exact** Sub decode-in-wait structure but set `dec_left = 0` in
`dec_begin` (decode work removed, everything else identical):

- **With decode**: clean to frame ~33, then prefetch ramps green, collapse ~79.
- **Without decode (bypass)**: clean to frame 229+, prefetch flat/dark
  (capture 4 MB → 39 MB).

Video-alone fits real-time with margin on **both** CPUs; adding the decode's
compute is what tips motion frames over budget. So it is the decode **time** —
not disc bandwidth, not decoder correctness, not scheduling, not a
danger-zone/CDC-frequency bug.

Corollaries refuted along the way: the "danger zone" 0x6800–0x8000 is not fatal
(the shipping PCM build runs per-frame code at 0x699C and works); `pump_poll`
frequency is not the lever (it already has ring back-pressure); the swap-wait is
CD-paced idle that only exists while the ring is *not* prefetched, so it can't be
relied on during sustained motion.

## Why the three constraints can't coexist on this pipeline

22 kHz audio needs *either* ~17 ms decode CPU (ADPCM — doesn't fit) *or* the disc
bandwidth of raw 22 kHz PCM (1470 B/frame, which eats the shared CBR video DMA
budget). Both trade against the same maxed resource. With "no video degradation"
+ "robust for any clip" + "22.05 kHz fixed" all hard, there is no free slot.

## Paths for a future retry (ranked, no-compromise first)

1. **Video codec efficiency** — reduce cold-tile count per motion frame at equal
   picture quality (better tile reuse/matching). Frees per-frame CPU with no
   visible loss and no rate change. Largest/most uncertain, but the only truly
   free path — the intended re-entry point.
2. **Surgical peak-shave** — cap cold tiles only on the handful of over-budget
   peak-motion frames (a brief, localized smear where the eye already tracks
   motion). Keeps 22.05 kHz and every calm scene untouched.
3. **Lower audio rate** (16/11 kHz) — halving the rate ~halves the decode and
   likely fits, but muffles the whole soundtrack always. Last resort.

Frame rate note: 30 fps neither helps nor hurts — per-frame decode, video DMA,
and the frame period all halve together, so this limit is rate-invariant.

## Where the code is

All committed on `adpcm-h40` (`git log main..adpcm-h40`). The latest commit is
the Sub decode-in-wait build with the prefetch diagnostic still wired in — a good
starting point once path 1 above creates headroom.
