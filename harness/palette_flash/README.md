# Palette-flash detection harness

Catches the bright-garbage flash at palette-segment boundaries by comparing the
**real recording** against **pixel-exact GT decodes of `MOVIE.DAT`**, synced by
the debug HUD frame counter (not by wall-clock — linear time-sync drifts and
produces false positives on moving content).

## Files

- `decode.py` — TTRC decoder. Streams `MOVIE.DAT`, snapshots per-frame
  `(cells=name-table (palrow,slot), pool=slot→index-tile, cram)`, and renders any
  `(cells, pool)` with **any** segment's CRAM. The arbitrary-CRAM render is what
  lets us model "old tiles under the new palette".
- `detect.py` — extracts real capture frames around each boundary, F-syncs each
  via `tools/read_frameno.py`, and classifies against candidates
  `prev / new / early(old content×new pal) / late(new content×old pal)`.
  NOTE: the automated verdict is only a screen; **always eyeball the dumps** — a
  wide window + moving content can still fool a pure score.

## How to use

```sh
python3 harness/palette_flash/detect.py tmp/<rec>.mkv out/movieplay/MOVIE.DAT \
    --dump harness/palette_flash/out
```

Then montage `out/f<boundary>_{real,prev,new,early,late}.png` and look.

## Root-cause finding (2026-07-10, machi_op H40)

The flash is **NOT a player CRAM-timing bug** — it is an **encoder correctness
bug**: cross-segment tile reuse.

Evidence (boundary f829, P1→P2):
- Real hardware and the GT decode of `MOVIE.DAT` are **identical bright-purple
  garbage** for one game frame. The player faithfully shows the stream.
- The sim's **ideal preview is clean/dark**. Divergence is sim-vs-stream.
- Decision log: only 588/720 cells updated at f829; of the 417 garbage cells,
  80 were "not-updated" (kept via `near_keep`) and **337 were "updated" but
  repointed to a resident tile loaded in the previous segment** (dedup / Coa /
  Near reuse).

Mechanism: a tile is stored in the sim as RGB (`pat_rgb[key]`), but on hardware
it is **palette indices** looked up through the current CRAM. When the whole
CRAM changes at a segment boundary, every tile carried over from the previous
segment keeps its indices but renders under the new palette → garbage. The sim
preview hides this because it renders stored RGB, never re-looking-up indices.

**Invariant that must hold:** a displayed tile must have been quantized under
the currently-active CRAM. Therefore **each palette segment is a fresh tile
epoch** — no tile from a previous segment may be reused (`near_keep`, dedup,
Coa, Near, Flbk, or Miss carry-over all violate this at a boundary).

### Fix direction (pending)

At each segment boundary the encoder must ensure every cell points to a tile
quantized under the new palette. Options under consideration:
1. Invalidate all resident/loaded/coa/l3 pools at the boundary and reload — but
   720 cold tiles/boundary exceeds the per-frame cold cap → still 1 garbage
   frame unless the tiles are **pre-resident in VRAM**.
2. Pre-load the next segment's tiles into spare VRAM slots during the quiet
   frames before the boundary, so the boundary itself is only a name-table
   repoint + CRAM swap (cheap, instant, clean). This is the tank/prebuffer idea
   extended to whole-segment keyframes.

Also: the **sim preview should render through palette indices** (not stored RGB)
so this class of bug shows up in the analysis video, not just on hardware.
