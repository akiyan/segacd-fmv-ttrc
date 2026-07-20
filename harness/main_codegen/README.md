# Main-CPU boot-time code generation

This harness fixes the executable-byte contract for Main-CPU code generation
before the player emits any code at runtime. Phase 1 replaces the mixed
bitmap-byte loop with 256 straight-line handlers generated once after
stream-header setup.

The reserved Main RAM layout is:

```text
0xFF2000  256-entry table of signed word offsets (512 bytes)
0xFF2200  generated bitmap handlers
0xFF4900  expected Phase 1 end
0xFF6580  maximum H40 NT blitter end
0xFF6600  hard code-generation limit and MainBuf start
0xFF8000  MainBuf end and existing RUN_TABLE
```

Each set bit emits exactly one entry read, register-mask cold/source-bit strip, and shadow write.
Every handler then advances the shadow cursor by 16 bytes and branches to the
assembly loop continuation. No handler modifies its generated instructions
during playback.

The dispatch keeps the existing `00` skip and uses four masked longword writes
for `FF`; only partial masks enter the generated jump table. The shared
`0x67FF67FF` register mask strips the cold bit and the two Prg/Wr/Main
source bits while keeping the palette and tile index intact.

Run the full proof:

```sh
tools/python.sh harness/main_codegen/verify_handlers.py
```

The proof covers all 256 masks with deterministic entry/shadow data, parses
every emitted opcode, verifies every table offset and branch target, checks the
MainBuf boundary, and asks the project 68000 objdump to decode representative
handlers. To retain the complete generated image for manual disassembly:

```sh
tools/python.sh harness/main_codegen/verify_handlers.py \
  --output tmp/main_codegen/bitmap_handlers.bin

~/toolchains/mars/m68k-elf/bin/m68k-elf-objdump \
  -b binary -m 68000 --adjust-vma=0xFF2000 -D \
  tmp/main_codegen/bitmap_handlers.bin
```

The runtime assembly generator must emit byte-for-byte the same table and
handlers. Keep this harness synchronized whenever that instruction template
changes.

Phase 2 emits one fixed-geometry name-table blitter for NT0 and another for
NT1 immediately after the Phase 1 handlers. Each row contains a precomputed
VRAM write command followed by straight `MOVE.L` writes from `shadow`; an odd
tile width ends the row with one `MOVE.W`. Verify the H32/H40 maximum layouts,
offset layouts, odd widths, generated size, semantics, and disassembly with:

```sh
tools/python.sh harness/main_codegen/verify_blitters.py
```

The maximum H40 pair occupies 7,296 bytes (`0xFF4900..0xFF657F`), leaving a
128-byte guard before `MainBuf` at `0xFF6600`. Invalid or oversized geometry is
rejected so the player can retain the existing generic blitter as fallback.

Measure the per-frame instruction cost against a real packed stream with:

```sh
tools/python.sh harness/main_codegen/measure_cycles.py \
  --header out/lunar-sss-op-h32/HEADER.DAT \
  --body out/lunar-sss-op-h32/BODY.DAT
```

The cycle model follows the actual word-displacement branches and indexed jump
shown by `m68k-elf-objdump`. Its timings come from the official
[MC68000 User's Manual](https://www.nxp.com/docs/en/reference-manual/MC68000UM.pdf),
Section 8, Tables 8-1, 8-2, 8-4, 8-5, 8-6, 8-7, 8-9, and 8-10. It measures the
packed mask distribution for every frame and excludes the one-time startup
generator. Platform wait states are also outside this instruction-cycle model.
