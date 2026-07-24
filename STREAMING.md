# Streaming Memory and CPU Headroom

This document answers one planning question: **what memory and CPU time can a
new live-playback feature use without consuming an existing safety margin?** It
describes the current TTRC v12 player in `boot/movieplay_sp.s` and
`boot/movieplay_ip.s`, audited on 2026-07-22 with checkpointed ADPCM22, the
four-source pattern-supply path, and the e84/p76 H40 cold-cap path enabled.

The short answer is:

| Domain | Safe fixed space, all supported playback | H40 fixed-N2 steady-stream space | Conditional space |
|---|---:|---:|---:|
| Sub PRG-RAM | 6.00 KiB | 6.00 KiB | 2 bytes of SP boot-slot growth, not data RAM |
| Word RAM bank 0 | 10.64 KiB | 48.42 KiB | +5.75 KiB if `ISO_HOLD_DUMP` compatibility is dropped |
| Word RAM bank 1 | 10.64 KiB | 48.42 KiB | +5.75 KiB if `ISO_HOLD_DUMP` compatibility is dropped |
| Main RAM | 3.72 KiB | 10.23 KiB | Boot UI shares the future generated-code area; none counted from palette or stack reservations |
| Main CPU | no hard positive guarantee | measured `U` at most 409 ticks (12.56 ms) | repeated cap185 recording has zero cadence holds; this is evidence, not spendable slack |
| Sub CPU | no hard positive guarantee | ADPCM decoder measured 7.62-8.11 ms in H40/N2 | full Sonic capture passes; BIOS/CD/Word-RAM waits remain outside that stopwatch |

“Safe fixed” means that the address can be assigned a fixed purpose and still
survive frame 0, movie replay, the largest control block, the largest supported
cold cap, and both Word-RAM bank parities. “Steady-stream” is a larger scratch
budget that is valid only after frame 0 has been expanded. It must be cleared or
re-created on every replay.

The CPU rows deliberately distinguish a useful measurement from a hard
guarantee. The player polls the CD BIOS, Word-RAM bank ownership, VBlank, and
DMA completion. A sector retry can also stop and re-seek the disc. Those waits
have no finite cycle bound in the current code, so the mathematical worst case
is unbounded and the only guaranteed spendable CPU margin is zero. The measured
and instruction-model values below are still useful as engineering targets, but
every new live feature needs a fresh full-length DEBUG recording.

## Worst-case assumptions

The live throughput reference is the largest current fixed-cadence raster:

| Item | Assumption |
|---|---:|
| Display | H40, 40 x 28 tiles, 1,120 cells |
| Cadence | 60,000/1,001 / 2 = 29.970 fps (fixed N2) |
| Frame time | 33.367 ms |
| Main clock and frame budget | 7,670,454 Hz; 255,937 cycles |
| Sub clock and frame budget | 12,500,000 Hz; 417,083 cycles |
| Timed cold cap | 185 patterns/frame |
| Maximum updates | 1,120 entries/frame |
| Audio | 372 ADPCM control bytes -> 736 decoded RF5C164 samples |
| Maximum BODY routing slot | 5 sectors |
| Build | specialized DEBUG player, Main code generation and short-run fast path enabled |

Memory that must work for every supported rate uses the qualified H40/15 fps
720-active-tile limit where necessary: 500 cold patterns. The 1,040-active-tile
tuple remains 400. H40/15 with all 1,120 tiles active is
unmeasured and rejected. Frame 0 is outside timed streaming and may contain all 1,120 patterns.
Its initial ascending slot allocation makes one 35,844-byte load run.

Instruction-cycle models use the standard MC68000 four-clock memory cycle.
They do not include platform wait states or time spent inside Sega CD BIOS
routines. “Remaining” in the sequence diagram is cumulative within the stated
CPU's frame budget; it is not a cross-CPU deadline guarantee.

## Sub PRG-RAM map

PRG-RAM is 512 KiB at `0x00000..0x7FFFF`. The 6 KiB
`0x08000..0x097FF` hole is the only currently unused range that the streaming
evidence marks safe for ordinary feature data. It should receive a named
allocation and a build-time overlap check before use.

| Address | Size | Current owner | Available to a new feature? |
|---|---:|---|---|
| `0x00000..0x05FFF` | 24.00 KiB | BIOS / low PRG work area | No |
| `0x06000..0x06FFD` | 4,094 B | largest current specialized DEBUG Sub boot image | No |
| `0x06FFE..0x06FFF` | 2 B | remainder of the 4,096-byte SP boot slot | Code growth only |
| `0x07000..0x07FFF` | 4.00 KiB | boot ISO scratch and BIOS-unsafe streaming range | No |
| `0x08000..0x097FF` | **6.00 KiB** | unused, previously marker-verified safe | **Yes, fixed PRG feature area** |
| `0x09800..0x0BFFF` | 10.00 KiB | touched by BIOS during continuous reads | No |
| `0x0C000..0x70FFF` | 404.00 KiB | usable streamed `PrgBuf` / quality-budget capacity ceiling | No free headroom |
| `0x71000..0x75FFF` | 20.00 KiB | delivery-jitter headroom; frame-0 pattern staging during boot | No; this is timing safety, not free RAM |
| `0x76000..0x76FFF` | 4.00 KiB | physical PrgBuf overflow guard; frame-0 pattern staging during boot | No; pump back-pressure starts here |
| `0x77000..0x7F7FF` | 34.00 KiB | APPLY circular queue; its first 12 KiB is reused only by frame-0 boot staging | No; its 4 KiB back-pressure gap is queue safety |
| `0x7F800..0x7FEFF` | 1.75 KiB | Sub stack reserve, growing downward from `0x7FF00` | No |
| `0x7FF00..0x7FFFF` | 256 B | above the configured stack top / reserved | No |

Do not count the difference between the 428 KiB physical PrgBuf ring and its
404 KiB scheduling cap. The next 20 KiB absorbs timed-delivery variation before
the 424 KiB pump back-pressure threshold; the final 4 KiB is a separate physical
overflow guard. Likewise, APPLY's 34 KiB allocation is intentionally kept below
about 30 KiB occupancy.

## Word RAM 1M/1M map

There are two independent 128 KiB physical banks. While the Sub CPU owns a
bank, it sees offsets `+0x00000..+0x1FFFF` at
`0xC0000..0xDFFFF`. The Main CPU simultaneously sees the other bank at
`0x200000 + offset`. A bank swap exchanges those roles; it does not make one
copy visible to both CPUs.

The address map applies to both physical banks. The `+0x15200` contents differ:
WordBuf0 and WordBuf1 are independent chronological preload streams, not
duplicated caches. The frame-0 bank also carries the temporary DicBuf stage at
boot, so fixed-headroom accounting conservatively reserves that range in either
physical bank.

| Bank offset | Sub address | Size | Current owner / worst use | Fixed headroom |
|---|---|---:|---|---:|
| `+0x00000..+0x00083` | `0xC0000..0xC0083` | 132 B | palette reference, reserved CRAM area, `n_load` | 0 |
| `+0x00084..+0x097FF` | `0xC0084..0xC97FF` | 37.87 KiB | cold load runs | 2.87 KiB after the 35,844-byte frame-0 maximum; 31.37 KiB during H40/N2 streaming |
| `+0x09800..+0x09801` | `0xC9800..0xC9801` | 2 B | `n_upd` | 0 |
| `+0x09802..+0x0AEFF` | `0xC9802..0xCAEFF` | 5.75 KiB | obsolete normal-path `O_UPDS`; still used by dump diagnostics | Conditional 5.75 KiB |
| `+0x0AF00..+0x0AFFF` | `0xCAF00..0xCAFFF` | 256 B | DEBUG counters and copied header | 156 B in three fixed holes |
| `+0x0B000..+0x0CFFF` | `0xCB000..0xCCFFF` | 8.00 KiB | maximum 64-entry PALTAB staging | 0 |
| `+0x0D000..+0x0EFFF` | `0xCD000..0xCEFFF` | 8.00 KiB | DicBuf boot staging in the physical frame-0 bank | 0 for fixed all-playback allocation |
| `+0x0F000..+0x0FFFF` | `0xCF000..0xCFFFF` | **4.00 KiB** | tail after maximum DicBuf stage | **4.00 KiB** |
| `+0x10000..+0x11FFF` | `0xD0000..0xD1FFF` | 8.00 KiB | linear control scratch | 3.21 KiB after the all-rate 4,900-byte maximum; 4.49 KiB for H40/N2 |
| `+0x12000..+0x127FF` | `0xD2000..0xD27FF` | 2.00 KiB | one CD-sector stage / pad discard | 0 |
| `+0x12800..+0x14A5F` | `0xD2800..0xD4A5F` | 8,800 B | full ADPCM next-index, signed-delta, and output tables | 0 |
| `+0x14A60..+0x14BFF` | `0xD4A60..0xD4BFF` | **416 B** | alignment gap | **416 B** |
| `+0x14C00..+0x151FF` | `0xD4C00..0xD51FF` | 1.50 KiB | ADPCM reconstructed-PCM buffer, sized for the supported maximum chunk | 0 |
| `+0x15200..+0x1BFFF` | `0xD5200..0xDBFFF` | 27.50 KiB | immutable WordBuf0 or WordBuf1, 880 patterns | 0 |
| `+0x1C000..+0x1FFFF` | `0xDC000..0xDFFFF` | 16.00 KiB | resident routing table | 0 |

The fixed all-playback total in one bank is:

```text
2.867 KiB  load tail that survives frame 0
0.152 KiB  fixed status/header holes
4.000 KiB  tail after DicBuf boot staging
3.215 KiB  control-scratch tail at the all-rate maximum
0.406 KiB  ADPCM alignment gap
-----------
10.640 KiB safe fixed space per physical bank
```

The H40/N2 steady-stream total substitutes a 6,660-byte worst load block
(`185 * 32 + 185 * 4`) and a 3,524-byte worst ADPCM control block. After boot,
the temporary 8 KiB DicBuf stage is also reusable.
Together these produce **48.418 KiB per bank** after subtracting the persistent
27.5 KiB WordBuf. It is not replay-safe because frame 0 overwrites most of the
load-tail gain and reuses the DicBuf stage.

`O_UPDS` is not read or written by normal playback anymore; Main re-walks the
bitmap and entries in the linear control block. The old area remains used by
`ISO_HOLD_DUMP` and `dump_pats`. Reclaiming it is low risk for production, but
must be an explicit decision to retire or relocate those diagnostics.

Routing costs 16 KiB **in each bank**, not one 32 KiB contiguous allocation.
The ADPCM full table likewise costs 8,800 bytes in each bank. WordBuf is the
opposite: each bank intentionally holds different data selected for its frame
parity. New persistent state that must follow the frame handoff usually needs
two copies; ping-pong or parity-local state may use different contents.

## Main RAM map

Main RAM is the 64 KiB range `0xFF0000..0xFFFFFF`. The linked addresses below
come from the current specialized H40 DEBUG object. The H40 code generator has
a separate proof that its maximum output ends at `0xFF6580`.

| Address | Size | Current owner / worst use | Safe headroom |
|---|---:|---|---:|
| `0xFF0000..0xFF1C1F` | 7.031 KiB | permanent Main player text/data, including alignment and the preload UI routines but excluding its transient font and strings | 0 |
| `0xFF1C20..0xFF1FFF` | **0.969 KiB** | link gap before generated code | **0.969 KiB** of permanent code/data growth |
| `0xFF2000..0xFF657F` | 17.375 KiB | maximum generated bitmap handlers and two H40 blitters | 0 |
| `0xFF2000..0xFF267F` at boot only | 1.625 KiB | transient SGDK font, preload-screen text, and lookup data; deliberately overwritten by generated code before playback | 0 additional runtime use |
| `0xFF6580..0xFF65FF` | **128 B** | asserted guard after maximum generated code | **128 B** |
| `0xFF6600..0xFF85FF` | 8.00 KiB | persistent DicBuf, 256 patterns | 0 |
| `0xFF8600..0xFFAFFF` | 10.50 KiB | fixed RUN_TABLE capacity: 488 pre-swizzled records at 22 B each | 0 |
| `0xFFB000..0xFFCFFF` | 8.00 KiB | 64-entry resident PALTAB | 0 |
| `0xFFD000..0xFFF07D` | 8,318 B | BSS: 4,096-byte shadow, 72-byte DEBUG HUD row, 4,096-byte 64-pitch name-table DMA stage, and fixed state | 0 |
| `0xFFF07E..0xFFFAFF` | **2.627 KiB** | unused below the stack guard | **2.627 KiB** |
| `0xFFFB00..0xFFFCFF` | 512 B | conservative stack and interrupt reserve | 0 |
| `0xFFFD00..0xFFFFFF` | 768 B | above configured stack top / BIOS reserve | 0 |

This yields **3.721 KiB** safe while reserving the complete 488-record
RUN_TABLE. H40/N2 cap180 leaves another 6.617 KiB in RUN_TABLE, for
**10.338 KiB** of steady-stream Main RAM, but that profile-specific tail is not
counted as general-purpose fixed memory. The 512-byte stack
reserve is deliberately larger than the approximately 80-byte deepest visible
player call chain; it leaves room for interrupt/BIOS use that the assembly call
graph alone cannot prove.

## Per-frame CPU sequence

The CPUs operate as a pipeline. After a bank swap, Main displays frame `N`
while Sub prepares frame `N+1` in the other physical Word-RAM bank.

```mermaid
sequenceDiagram
    autonumber
    participant CD as CD / CDC
    participant S as Sub CPU (12.5 MHz)
    participant W as Word RAM 1M/1M
    participant M as Main CPU (7.67 MHz)
    participant V as VDP

    Note over S,M: H40 fixed-N2 budget: Sub 417,083 cycles; Main 255,937 cycles per 33.367 ms
    M->>S: CMD_SWAP for prepared frame N
    S->>W: Toggle bank ownership and wait for settle
    S-->>M: STAT_READY
    Note over S,M: Bank/CD/DMA polling has no finite hard bound; guaranteed shared slack = 0

    par Sub prepares frame N+1
        CD-->>S: BODY sectors become ready
        S->>W: Drain to 2 KiB stage, then APPLY / PrgBuf
        Note right of S: S1 55k-cycle planning envelope for routing and five stage copies<br/>BIOS wait/retry cycles excluded; remaining before waits about 362k
        S->>S: Fetch control, decode ADPCM, write 736 reconstructed samples
        Note right of S: S2 measured ADPCM decode is about 95k-101k cycles<br/>RF5C164 writes and bus wait states are not included in that stopwatch
        S->>W: Walk up to 1,120 entries and copy up to 185 cold patterns
        Note right of S: S3 75k-cycle planning envelope<br/>remaining before waits is less than 156k
        S->>S: Bookkeeping, polls, next READY preparation
        Note right of S: S4 10k reserve; ADPCM visible subtotal exceeds 271k cycles<br/>raw remainder before waits is less than 146k, safe spendable remainder = 0
    and Main consumes frame N
        M->>W: Parse load runs into RUN_TABLE
        Note right of M: M1 10k-cycle planning envelope<br/>remaining 245,937
        M->>M: Apply selected bitmap/list and stage the 40-pitch shadow at 64-entry pitch
        Note right of M: M2 is included in HUD E; polling and VBlank phase prevent a fixed spendable remainder
        M->>V: Wait for VBlank; transfer cold runs; repair DMA first words
        Note right of M: M3 at most 409 ticks / about 96,375 Main cycles in the qualified cap185 capture
        M->>V: DMA staged name table; republish HUD; optional CRAM; atomic flip
        Note right of M: M4 has no guaranteed positive slack; all 2,713 cap185 intervals still meet two fields
    end

    M->>S: Next CMD_SWAP
    Note over M,S: If Sub is not ready, Main busy-waits here and consumes the apparent local remainder
```

The Sub wait loops check an arrived or cleared `CMD_SWAP` before doing another
opportunistic CD pump. Pumping still continues whenever Main has not reached
the handshake, but future-sector work cannot delay a bank handoff that is
already on the current frame's fixed-N2 deadline. A control-first Bad Apple
p61 recording reproduced three one-VBlank misses with the old pump-first
ordering; p62 kept every one of the 6,575 timed transitions at exactly two
VBlanks with `S`, `D`, `R`, and `C` all zero.

### Main cycle basis

The generated bitmap and name-table model in
`harness/main_codegen/measure_cycles.py` follows the assembled MC68000 paths.
For H40 full screen:

| Main phase | Worst value used here | Basis |
|---|---:|---|
| Load-run parsing and fixed setup | 10,000 cycles | conservative planning envelope; 185 records is the cap maximum at N2 |
| Bitmap handler | 38,690 cycles | theoretical worst of all 256 bitmap-byte handlers across 140 bytes |
| 64-pitch staging plus name-table DMA | not independently bounded | staging is part of HUD `E`; DMA completion and VBlank alignment include hardware waits |
| Pattern-transfer interval | 96,375 cycles | 409 hardware stopwatch ticks at 30.72 us/tick in the repeated full cap185 Sonic capture |
| DEBUG HUD, CRAM/flip, residual setup | 15,000 cycles | planning reserve around code not covered by the two exact models |
| **Safe spendable remainder** | **0 cycles** | BIOS, bank, VBlank, and DMA waits have no finite hard bound; the measured phases are not independently additive |

The 409-tick measurement is from both byte-exact p76 full-length Sonic
recordings at cold185. `E` reaches 249 quarter-ticks and `N` reaches 65, while
every frame at 85% or more of the cap is constrained to 30 or fewer
source-aware runs. All 2,713 timed intervals remain exactly two fields, but the
largest measured phases need not occur on the same frame and cannot be summed
as independent work. The qualification is evidence for this stream, not a
general cycle allowance.

The format permits up to 185 isolated one-tile runs in this profile; the pack
observed at most 65. Whole-movie run total is not constrained because light
frames have ample deadline room and may trade extra fragmentation for fewer
runs on heavy frames. A pathological but format-valid stream can still consume
the complete two-VBlank deadline, so the safe unconditional Main-CPU allowance
remains zero.

### Sub cycle basis

The following planning table covers the visible non-decoder assembly in the
H40/N2 path. ADPCM decode and RF5C164 output are separate mandatory work:

| Sub phase | Rounded instruction subtotal | Important exclusion |
|---|---:|---|
| Routing plus up to five 2 KiB stage copies | 55,000 cycles | time inside `CDC_STAT`, `CDC_READ`, `CDC_TRN`, and `CDC_ACK` |
| Maximum 3,524-byte APPLY-to-control copy | within the shared frame envelope | Word-RAM wait states |
| 1,120-entry legacy walk, 185 cold copies, run construction | 75,000 cycles | asynchronous CD work reached by polls |
| Fixed bookkeeping and reserve | 10,000 cycles | bank-settle polling |
| ADPCM table decode | about 95,000-101,000 cycles | RF5C164 output writes and bus waits |

For specialized H40/N2, the ADPCM stopwatch surrounds only the table decode,
not the following 736-byte RF5C164 write. It has measured about 7.62-8.11 ms,
or roughly 95,000-101,000 Sub cycles. Because the writer, BIOS calls, CDC
readiness, Word-RAM access, and bank timing remain outside that measurement,
the arithmetic remainder is not a safe spendable allowance.

At H40/15, the 1,472-sample decode is about 16 ms and crosses a 13.3 ms
CD-sector interval. Player p56 therefore performs non-blocking CDC polls during
the decode; `Axx` includes any sector draining done by those polls. The 2,293-
frame Machi OP replacement recording completed with `S=0`, `D=0`, and `R=0`,
while the pre-fix recording ended at `S=3` and held `F0107`, `F0166`, and
`F0391` during recovery. This qualifies that low-rate path without turning its
variable BIOS time into spendable Sub-CPU margin.

The same geometry later qualified the H40/15 fps/720-active-tile cold cap at
500. Its confirmed active rows fit a 320x139 picture occupying 40 columns by
18 tile rows. The pack reached cold500, reported `under=0`, retained a 6 KiB
evaluation minimum, and reconstructed all 2,293 frames exactly. The complete
HUD gate passed with `S/D/R=0`, `C/M=4`, and `J=8 KiB`; cold-run count was at
most 134 and the longest pattern-update interval was 1,669 ticks (51.29 ms).
Higher probes were phase-sensitive; cap680 failed at `M=5` and 67.37 ms despite
nearby probes passing. This evidence applies only to that
mode/fps/active-tile tuple; it does not raise full-raster H40/15, H40/24,
H40/30, H32, or mode4 limits.

Machi ED separately qualified H40/15 fps with 1,040 active tiles at the same
400 cold cap. Its 320x204 picture touches 40 columns by 26 tile rows in the
320x224 raster. The pack completed all 3,998 frames with `under=0`, exact
reconstruction, and a one-pattern minimum ready payload. Across 3,997 timed
DEBUG HUD groups, `S`, `D`, and `R` stayed zero; Main-CPU VBlank waits were at
most two, cold-run count was at most 221, and the longest pattern-update
interval was 1,648 ticks (50.63 ms). Audio and extracted-frame gates passed.
The unmeasured 1,120-active-tile H40/15 case is rejected until it receives its
own full-length qualification; cold-cap selection requires an exact active-tile
match and no longer falls back to 350 or another measured area.

This explains why a static instruction count can look comfortable even when a
real stream is near its limit: the expensive uncertainty lives in BIOS calls,
CDC readiness, shared-memory access, and recovery. A successful full recording
proves only that the margin was non-negative for that disc and machine run; it
does not measure the unused Sub cycles. The H40 Sonic result is the acceptance
profile for the completed ADPCM implementation, but it qualifies only that one
profile and does not turn either arithmetic remainder into spendable time.

## Allocation guidance for the next feature

Use the low-risk spaces in this order:

1. Use Main RAM `0xFFF07E..0xFFFAFF` (2.627 KiB) for Main-only state, retaining
   the 512-byte stack guard.
2. Put small bank-local state in the 4 KiB tail after DicBuf staging or the
   416-byte ADPCM alignment gap, with explicit overlap assertions. Do not use
   the WordBuf region; its two banks intentionally hold different preloads.
3. Use the 128-byte Main code-generation guard and RUN_TABLE tail only with
   explicit end symbols and assertions; their safe starts depend on generated
   code and the supported cold cap.
4. Use PRG `0x08000..0x097FF` only after adding it to
   `tools/check_player_ring.py`. Never take bytes from payload jitter or APPLY
   back-pressure reserves.
5. Reclaim `O_UPDS` only if its dump diagnostics are retired or relocated.

For CPU time, assume zero shared deadline margin until the new path has:

- a per-frame Sub stopwatch around the new sustained work (ADPCM exports
  decode-phase `Axx`, including low-rate CDC service);
- a pack-time Main guard for cold-run count or predicted transfer time;
- full-length H40/30, H40/24, H32/30, and H40/15 recordings with `S=0`, `D=0`,
  `R=0`, stable audio lead, and no extra cadence slips;
- cycle and memory assertions updated in the same commit as the allocation.

## Reproducing the audit

Use the project-managed Python and the current packed H40 stream:

```sh
tools/python.sh tools/check_player_ring.py
make movieplay CONFIG=configs/sonic-jam-op-h40.toml \
  DEBUG=1 MAIN_CODEGEN=1 DMA_RUN_FASTPATH=1 PLAYER_SPECIALIZE=1

~/toolchains/mars/m68k-elf/bin/m68k-elf-size -A \
  tmp/sonic-jam-op-h40/build/movieplay_ip.o \
  tmp/sonic-jam-op-h40/build/movieplay_sp.o

tools/python.sh harness/cold_cap_model/extract_frames.py \
  out/sonic-jam-op-h40 --tsv /tmp/sonic-h40-frames.tsv
```

Re-run the full DEBUG recording and HUD extraction before revising the 409-tick
qualified maximum. Do not replace that elapsed measurement with an instruction
model: VBlank alignment and DMA completion are part of the real Main deadline.
