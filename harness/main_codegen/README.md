# Main-CPU boot-time code generation

This harness fixes the executable-byte contract for issue #27 before the
player emits any code at runtime. Phase 1 replaces the mixed bitmap-byte loop
with 256 straight-line handlers generated once after stream-header setup.

The reserved Main RAM layout is:

```text
0xFF2000  256-entry table of signed word offsets (512 bytes)
0xFF2200  generated bitmap handlers
0xFF5100  expected Phase 1 end
0xFF8000  hard limit and existing RUN_TABLE
```

Each set bit emits exactly one entry read, cold-flag strip, and shadow write.
Every handler then advances the shadow cursor by 16 bytes and branches to the
assembly loop continuation. No handler modifies its generated instructions
during playback.

Run the full proof:

```sh
python3 harness/main_codegen/verify_handlers.py
```

The proof covers all 256 masks with deterministic entry/shadow data, parses
every emitted opcode, verifies every table offset and branch target, checks the
24 KiB boundary, and asks the project 68000 objdump to decode representative
handlers. To retain the complete generated image for manual disassembly:

```sh
python3 harness/main_codegen/verify_handlers.py \
  --output tmp/main_codegen/bitmap_handlers.bin

~/toolchains/mars/m68k-elf/bin/m68k-elf-objdump \
  -b binary -m 68000 --adjust-vma=0xFF2000 -D \
  tmp/main_codegen/bitmap_handlers.bin
```

The runtime assembly generator must emit byte-for-byte the same table and
handlers. Keep this harness synchronized whenever that instruction template
changes.
