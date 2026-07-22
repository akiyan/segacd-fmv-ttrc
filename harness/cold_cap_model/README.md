# Cold-cap model: why H40/30fps sticks at 175, measured

Goal: identify, with per-frame evidence, which resource actually breaks the
fixed two-VBlank cadence when the H40/30fps/1,120-tile cold cap rises above
175, then rank the fixes. Phase 0 mines the packs and DEBUG HUD OCR series
that already exist on this machine; later phases add a calibrated two-CPU
timeline model and targeted re-measurements.

## Tools

- `extract_frames.py` — parses a packed `HEADER.DAT` + `BODY.DAT` pair
  (TTRC v10-v12) and emits one CSV row per frame: cell updates, physical
  pattern loads by source (Prg/Wr/Dic), cold-run descriptor structure
  (count, short runs, max run length), Pass2 word total, palette-switch
  flag, control bytes, and the CD slot schedule (control/payload sectors,
  rated allowance, delivery lead). With `--hud-csv` it cross-checks the
  parsed per-frame run count against the DEBUG HUD `N` column of a
  recording; a full-length match also proves that recording played exactly
  that stream.

```sh
tools/python.sh harness/cold_cap_model/extract_frames.py \
    out/sonic-jam-op-h40 --csv frames_175.csv \
    --hud-csv videos/SonicJamOp_H40_jitter_final_hud.csv
```

## Specimen inventory (all full-length Sonic Jam OP, H40/30fps/1,120 tiles)

| Stream (realized max loads/frame) | Pack | Recording + HUD series | N cross-check |
|---|---|---|---|
| 175 (v12, shadow lists, current) | `out/sonic-jam-op-h40` | `SonicJamOp_H40_jitter_final(_repeat)` | 2,714/2,714 |
| 178 (v10) | `videos/SonicJamOp_H40_cold195_test/packed` (byte-identical copy in `..._cold200_test/packed`) | `SonicJamOp_H40_cold195_emu`, `SonicJamOp_H40_cold200_emu` | 2,714/2,714 each |
| 190 (v10) | `out/sonic-jam-op-h40-cold190-hudinline` | `SonicJamOp_H40_cold190_hudinline(_repeat)` | 2,714/2,714 |

Every `*_repeat` recording is hash-identical to its first run (see
`videos/*_repeat_compare.json`), so each disc's behaviour is fully
deterministic in the emulator; when two recordings of the *same stream
content* break at different frames, the difference is the player build on
the disc, not chance.

## Phase 0 findings

**1. The "cap 195" and "cap 200" retry recordings replayed a realized-178
stream.** The packs behind them are byte-identical and never exceed 178
loads/frame; the full-length HUD `N` match pins both recordings to that
stream. The historical conclusion "195/200 break the cadence" therefore
actually observed *a realized-178 stream breaking on newer player builds*.
The realized-190 stream is a separate real specimen and broke differently.

**2. Cadence-break census** (a break = a movie frame held for 3 captures in
the 59.94 Hz HUD series; S/D/R stayed 0 everywhere):

| Recording | Breaks (frame held) | Held frame's next-frame workload |
|---|---|---|
| jitter_final, realized 175 | none | max U 544 ticks, max W 99 lines, J ≤ 14 KiB |
| cold195_emu, realized 178 | 1380, 2619 | culprits 1381 (75 loads), 2620 (178-load plateau), both 2-sector slots |
| cold200_emu, same 178 stream, different build | 2621 | culprit 2622 (178-load plateau), 2-sector slot |
| cold190_hudinline, realized 190 | 1191 | culprit 1192 = palette-switch frame (pal seg 7, 60 loads, 16 runs), 2-sector slot |
| cold187_qual (older build) | 31 | culprit in the opening heavy section (U 576-605 ticks, N 107-143) |
| cold178/179/180/181_qual (older build) | none | — |

**3. Three break signatures, none of them raw DMA:**

- *Main-window overrun* (older builds, opening section): U reaches 576-605
  ticks (17.7-18.6 ms), i.e. the pattern-transfer interval itself spills
  past a field. Run-count N 107-143 in the same frames — run overhead, not
  word volume (2.8k words fits one 3,400-word VBlank budget).
- *Palette collision* (realized-190): the only break sits exactly on a
  CRAM-switch frame, which must fit CRAM + flip atomically in one VBlank.
  The same stream survived its 190-load plateaus.
- *Plateau phase loss* (realized-178 on current-era builds): breaks land on
  2-sector slots inside long stretches pinned at exactly 178 loads, with
  modest U (73-375 ticks) and W ~60 lines. Neither CPU is visibly saturated
  on the culprit frame; the two-VBlank margin has been eroded to ~zero and
  build-level differences (code placement, HUD variant) decide which frame
  slips. The same plateaus pass on the hudinline build even at 190 loads.

**4. The W (Main-waits-for-Sub) series alternates ~60 lines / ~1 line with
frame parity in the heavy sections**, matching the 1001/400 CD delivery
pattern (3,2,3,2 sectors) — the Sub finishes visibly later on one parity.
All plateau culprits are 2-sector (rated 2) frames.

**5. Corrected qualification landscape (current player era):** realized 175
is clean with real margin; realized 178 is marginal (0-2 slips depending on
build); realized 190 survived its plateaus on one build and lost only a
palette frame. The gap between 175 and ~190 is not a hard cycle wall but a
near-zero *phase margin* around the flip deadline, plus one real Main-window
ceiling (high-run-count frames) and one palette-frame hazard.

## Next

- Reconstruct per-frame two-CPU timelines from U/W plus the extracted
  workload, model the flip deadline, and calibrate until the model
  reproduces every break in the census (gates G1-G4 of the plan).
- Then rank fixes: run-overhead reduction (run count, not words, drives U),
  palette-frame scheduling, Sub parity smoothing (2/3-sector pump), and
  flip-phase headroom.
