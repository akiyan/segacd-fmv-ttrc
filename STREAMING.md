# Streaming Memory and CPU Headroom

This document answers one planning question: **what memory and CPU time can a
new live-playback feature use without consuming an existing safety margin?** It
describes the current TTRC v8 player in `boot/movieplay_sp.s` and
`boot/movieplay_ip.s`, audited at commit `669e426` with player version p54 on
2026-07-18.

The short answer is:

| Domain | Safe fixed space, all supported playback | H40 fixed-N2 steady-stream space | Conditional space |
|---|---:|---:|---:|
| Sub PRG-RAM | 6.00 KiB | 6.00 KiB | 288 bytes of SP binary growth, not data RAM |
| Word RAM bank A | 56.43 KiB | 86.40 KiB | +5.75 KiB if `ISO_HOLD_DUMP` compatibility is dropped |
| Word RAM bank B | 56.43 KiB | 86.40 KiB | +5.75 KiB if `ISO_HOLD_DUMP` compatibility is dropped |
| Main RAM | 27.29 KiB | 28.66 KiB | None counted from palette or stack reservations |
| Main CPU | no hard positive guarantee | about 50,769 local cycles (6.62 ms) | qualified H40 reference only; Sub must already be ready |
| Sub CPU | no hard positive guarantee | about 247,000 instruction cycles before waits | not safe to spend: BIOS/CD/Word-RAM waits are excluded |

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
| Timed cold cap | 175 patterns/frame |
| Maximum updates | 1,120 entries/frame |
| PCM | 444 bytes/frame |
| Maximum BODY routing slot | 5 sectors |
| Build | specialized DEBUG player, Main code generation and short-run fast path enabled |

Memory that must work for every supported rate uses the larger H40/15 fps
limits where necessary: 350 cold patterns, 888 PCM bytes, and a 4,700-byte
control block. Frame 0 is outside timed streaming and may contain all 1,120
patterns. Its initial ascending slot allocation makes one 35,844-byte load run.

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
| `0x06000..0x06EDF` | 3,808 B | current specialized DEBUG Sub player | No |
| `0x06EE0..0x06FFF` | 288 B | remainder of the 4,096-byte SP boot slot | Code growth only |
| `0x07000..0x07FFF` | 4.00 KiB | boot ISO scratch and BIOS-unsafe streaming range | No |
| `0x08000..0x097FF` | **6.00 KiB** | unused, previously marker-verified safe | **Yes, fixed PRG feature area** |
| `0x09800..0x0BFFF` | 10.00 KiB | touched by BIOS during continuous reads | No |
| `0x0C000..0x6CFFF` | 388.00 KiB | scheduled payload RING / virtual VBV capacity ceiling | No free headroom |
| `0x6D000..0x76FFF` | 40.00 KiB | delivery-jitter reserve; frame-0 pattern staging during boot | No; this is timing safety, not free RAM |
| `0x77000..0x7F7FF` | 34.00 KiB | APPLY circular queue | No; its 4 KiB back-pressure gap is queue safety |
| `0x7F800..0x7FEFF` | 1.75 KiB | Sub stack reserve, growing downward from `0x7FF00` | No |
| `0x7FF00..0x7FFFF` | 256 B | above the configured stack top / reserved | No |

Do not count the difference between the 428 KiB physical payload RING and its
388 KiB scheduling cap. That 40 KiB is what keeps normal CD-delivery variation
away from the 424 KiB pump back-pressure threshold. Likewise, APPLY's 34 KiB
allocation is intentionally kept below about 30 KiB occupancy.

## Word RAM 1M/1M map

There are two independent 128 KiB physical banks. While the Sub CPU owns a
bank, it sees offsets `+0x00000..+0x1FFFF` at
`0xC0000..0xDFFFF`. The Main CPU simultaneously sees the other bank at
`0x200000 + offset`. A bank swap exchanges those roles; it does not make one
copy visible to both CPUs.

The table applies identically to both physical banks:

| Bank offset | Sub address | Size | Current owner / worst use | Fixed headroom |
|---|---|---:|---|---:|
| `+0x00000..+0x00083` | `0xC0000..0xC0083` | 132 B | palette reference, reserved CRAM area, `n_load` | 0 |
| `+0x00084..+0x097FF` | `0xC0084..0xC97FF` | 37.87 KiB | cold load runs | 2.87 KiB after the 35,844-byte frame-0 maximum; 31.72 KiB during H40/N2 streaming |
| `+0x09800..+0x09801` | `0xC9800..0xC9801` | 2 B | `n_upd` | 0 |
| `+0x09802..+0x0AEFF` | `0xC9802..0xCAEFF` | 5.75 KiB | obsolete normal-path `O_UPDS`; still used by dump diagnostics | Conditional 5.75 KiB |
| `+0x0AF00..+0x0AFFF` | `0xCAF00..0xCAFFF` | 256 B | DEBUG counters and copied header | 156 B in three fixed holes |
| `+0x0B000..+0x0CFFF` | `0xCB000..0xCCFFF` | 8.00 KiB | maximum 64-entry PALTAB staging | 0 |
| `+0x0D000..+0x0FFFF` | `0xCD000..0xCFFFF` | **12.00 KiB** | unused after maximum PALTAB | **12.00 KiB** |
| `+0x10000..+0x11FFF` | `0xD0000..0xD1FFF` | 8.00 KiB | linear control scratch | 3.41 KiB after the all-rate 4,700-byte maximum; 4.53 KiB for H40/N2 |
| `+0x12000..+0x127FF` | `0xD2000..0xD27FF` | 2.00 KiB | one CD-sector stage / pad discard | 0 |
| `+0x12800..+0x1BFFF` | `0xD2800..0xDBFFF` | **38.00 KiB** | unused | **38.00 KiB** |
| `+0x1C000..+0x1FFFF` | `0xDC000..0xDFFFF` | 16.00 KiB | resident routing table | 0 |

The fixed all-playback total in one bank is:

```text
2.867 KiB  load tail that survives frame 0
0.152 KiB  fixed status/header holes
12.000 KiB post-PALTAB hole
3.410 KiB  control-scratch tail at the all-rate maximum
38.000 KiB large unused hole
-----------
56.430 KiB safe fixed space per physical bank
```

The H40/N2 steady-stream total substitutes a 6,300-byte worst load block
(`175 * 32 + 175 * 4`) and a 3,556-byte worst control block, producing
**86.398 KiB per bank**. It is not replay-safe because frame 0 overwrites most
of the load-tail gain.

`O_UPDS` is not read or written by normal playback anymore; Main re-walks the
bitmap and entries in the linear control block. The old area remains used by
`ISO_HOLD_DUMP` and `dump_pats`. Reclaiming it is low risk for production, but
must be an explicit decision to retire or relocate those diagnostics.

Routing costs 16 KiB **in each bank**, not one 32 KiB contiguous allocation.
New persistent state that must follow the frame handoff usually also needs two
copies. Ping-pong frame-local state can instead use different contents in the
two banks.

## Main RAM map

Main RAM is the 64 KiB range `0xFF0000..0xFFFFFF`. The linked addresses below
come from the current specialized H40 DEBUG object. The H40 code generator has
a separate proof that its maximum output ends at `0xFF6580`.

| Address | Size | Current owner / worst use | Safe headroom |
|---|---:|---|---:|
| `0xFF0000..0xFF147F` | 5.125 KiB | loaded Main player image | 0 |
| `0xFF1480..0xFF1D65` | 2,278 B | BSS, including the 2,240-byte name-table shadow | 0 |
| `0xFF1D66..0xFF1FFF` | **666 B** | link gap before generated code | **666 B** of static code/BSS growth |
| `0xFF2000..0xFF657F` | 17.375 KiB | maximum generated bitmap handlers and two H40 blitters | 0 |
| `0xFF6580..0xFF7FFF` | **6.625 KiB** | proved code-generation tail | **6.625 KiB** |
| `0xFF8000..0xFF8AEF` | 2.734 KiB | 350 worst-case run records at 8 B each | 0 |
| `0xFF8AF0..0xFFAFFF` | **9.266 KiB** | all-rate RUN_TABLE tail | **9.266 KiB**; H40/N2 has 10.633 KiB |
| `0xFFB000..0xFFCFFF` | 8.00 KiB | 64-entry resident PALTAB | 0 |
| `0xFFD000..0xFFFAFF` | **10.750 KiB** | unused below the stack guard | **10.750 KiB** |
| `0xFFFB00..0xFFFCFF` | 512 B | conservative stack and interrupt reserve | 0 |
| `0xFFFD00..0xFFFFFF` | 768 B | above configured stack top / BIOS reserve | 0 |

This yields **27.291 KiB** safe across all supported rates. Restricting the run
table to H40/N2's 175-cold cap raises it to **28.658 KiB**. The 512-byte stack
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
        S->>W: Drain to 2 KiB stage, then APPLY / payload RING
        Note right of S: S1 55k-cycle planning envelope for routing and five stage copies<br/>BIOS wait/retry cycles excluded; remaining before waits about 362k
        S->>S: Fetch control, write 444 PCM bytes
        Note right of S: S2 30k-cycle planning envelope<br/>cumulative remaining before waits about 332k
        S->>W: Walk up to 1,120 entries and copy up to 175 cold patterns
        Note right of S: S3 75k-cycle planning envelope<br/>cumulative remaining before waits about 257k
        S->>S: Bookkeeping, polls, next READY preparation
        Note right of S: S4 10k reserve; rounded visible planning subtotal 170k<br/>raw remainder about 247k, safe spendable remainder = 0
    and Main consumes frame N
        M->>W: Parse load runs into RUN_TABLE
        Note right of M: M1 10k-cycle planning envelope<br/>remaining 245,937
        M->>M: Apply bitmap entries to shadow and generate back name table
        M->>V: Write complete 40 x 28 name table
        Note right of M: M2 at most 55,280 modeled cycles<br/>remaining 190,657
        M->>V: Wait for VBlank; transfer cold runs; repair DMA first words
        Note right of M: M3 124,888 elapsed Main cycles in the qualified full H40 capture<br/>remaining 65,769
        M->>V: DEBUG HUD, optional CRAM load, atomic name-table flip
        Note right of M: M4 15k-cycle planning reserve<br/>qualified local remainder about 50,769 cycles (6.62 ms)
    end

    M->>S: Next CMD_SWAP
    Note over M,S: If Sub is not ready, Main busy-waits here and consumes the apparent local remainder
```

### Main cycle basis

The generated bitmap and name-table model in
`harness/main_codegen/measure_cycles.py` follows the assembled MC68000 paths.
For H40 full screen:

| Main phase | Worst value used here | Basis |
|---|---:|---|
| Load-run parsing and fixed setup | 10,000 cycles | conservative planning envelope; 175 records is the format maximum at N2 |
| Bitmap handler | 38,690 cycles | theoretical worst of all 256 bitmap-byte handlers across 140 bytes |
| Generated 40 x 28 name-table blit | 16,590 cycles | exact instruction model |
| Pattern-transfer interval | 124,888 cycles | 530 hardware stopwatch ticks at 30.72 us/tick in the full 6,576-frame H40 capture |
| DEBUG HUD, CRAM/flip, residual setup | 15,000 cycles | planning reserve around code not covered by the two exact models |
| **Total planning envelope** | **205,168 cycles** | values above summed conservatively |
| **Local remainder** | **50,769 cycles / 6.62 ms** | 255,937 - 205,168 |

The 530-tick capture predates p54's disc-specific immediate constants. Those
changes only remove instructions, so it is conservative for p54's unchanged DMA
path. The frame with the largest pattern interval is not necessarily the frame
with the theoretical worst bitmap, which is why summing both is conservative.

This is still not a formal player-wide bound. The format permits up to 175
isolated one-tile runs, but the current contiguous allocator normally produces
far fewer (46 maximum in the reference H40 pack). There is no pack-time limit
on run count or Main elapsed transfer time. A pathological but currently
accepted stream can therefore consume the complete two-VBlank deadline. Until
such a guard exists, **50,769 cycles is a qualification target, not permission
to spend 50,769 cycles unconditionally**.

### Sub cycle basis

The rounded 170,000-cycle Sub subtotal covers visible assembly instructions in
the H40/N2 path:

| Sub phase | Rounded instruction subtotal | Important exclusion |
|---|---:|---|
| Routing plus up to five 2 KiB stage copies | 55,000 cycles | time inside `CDC_STAT`, `CDC_READ`, `CDC_TRN`, and `CDC_ACK` |
| Maximum 3,556-byte APPLY-to-control copy plus PCM write | 30,000 cycles | Word-RAM and RF5C164 wait states |
| 1,120-entry legacy walk, 175 cold copies, run construction | 75,000 cycles | asynchronous CD work reached by polls |
| Fixed bookkeeping and reserve | 10,000 cycles | bank-settle polling |
| **Visible subtotal** | **170,000 cycles** | all exclusions above |
| **Raw remainder before waits** | **247,083 cycles / 19.77 ms** | not safe spendable time |

This explains why a static instruction count can look comfortable even when a
real stream is near its limit: the expensive uncertainty lives in BIOS calls,
CDC readiness, shared-memory access, and recovery. A successful full recording
proves only that the margin was non-negative for that disc and machine run; it
does not measure the unused Sub cycles. Sub-side ADPCM or another sustained task
must therefore start with cycle instrumentation, not with the 247,083 raw
number.

## Allocation guidance for the next feature

Use the low-risk spaces in this order:

1. Put bank-local or ping-pong state in Word RAM
   `+0x12800..+0x1BFFF` (38 KiB per bank). It is the largest clean hole and
   does not borrow a delivery buffer.
2. Use Main RAM `0xFFD000..0xFFFAFF` (10.75 KiB) for Main-only state, retaining
   the 512-byte stack guard.
3. Use the proved Main code-generation tail and RUN_TABLE tail only with
   explicit end symbols and assertions; their safe starts depend on generated
   code and the supported cold cap.
4. Use PRG `0x08000..0x097FF` only after adding it to
   `tools/check_player_ring.py`. Never take bytes from payload jitter or APPLY
   back-pressure reserves.
5. Reclaim `O_UPDS` only if its dump diagnostics are retired or relocated.

For CPU time, assume zero shared deadline margin until the new path has:

- a per-frame Sub stopwatch around control fetch, PCM, and cold expansion;
- a pack-time Main guard for cold-run count or predicted transfer time;
- full-length H40/30, H40/24, H32/30, and H40/15 recordings with `S=0`, `D=0`,
  `R=0`, stable audio lead, and no extra cadence slips;
- cycle and memory assertions updated in the same commit as the allocation.

## Reproducing the audit

Use the project-managed Python and the current packed H40 stream:

```sh
tools/python.sh tools/check_player_ring.py
make movieplay CONFIG=configs/bad-apple-h40.toml \
  DEBUG=1 MAIN_CODEGEN=1 DMA_RUN_FASTPATH=1 PLAYER_SPECIALIZE=1

~/toolchains/mars/m68k-elf/bin/m68k-elf-size -A \
  tmp/bad-apple-h40/build/movieplay_ip.o \
  tmp/bad-apple-h40/build/movieplay_sp.o

tools/python.sh harness/main_codegen/measure_cycles.py \
  --header out/bad-apple-h40/HEADER.DAT \
  --body out/bad-apple-h40/BODY.DAT
```

Re-run the full DEBUG recording and HUD extraction before revising the 530-tick
qualified maximum. Do not replace that elapsed measurement with the instruction
model: VBlank alignment and DMA completion are part of the real Main deadline.
