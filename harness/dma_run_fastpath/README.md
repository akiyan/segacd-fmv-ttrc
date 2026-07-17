# DMA run fast-path proof

This harness independently verifies the two representation changes used by the
Main-CPU pattern-transfer fast paths. It does not import the encoder, packer, or
player implementation.

Run it from the repository root:

```sh
python3 harness/dma_run_fastpath/verify.py
```

The proof covers:

- every 16-bit VRAM destination address, requiring the reusable longword
  command to emit the same two control-port words as the former construction;
- the DMA command's CD5 bit in the low word, so a big-endian 68000 `MOVE.L`
  writes the ordinary high control word first and the DMA-trigger word second;
- the accepted Word-RAM behavior where `src+2` with the full programmed length
  writes source words 1 onward at destination word 1 onward, followed by the
  Main CPU repairing destination word 0;
- one- and two-tile CPU-direct paths, explicitly splitting every source
  longword into its high and low 16-bit VDP data-port writes; and
- fixed edge patterns plus deterministic randomized source, destination, and
  surrounding-VRAM values.

This is an equivalence proof, not a hardware timing measurement. It assumes the
already-established Word-RAM DMA first-word behavior and does not validate
VBlank budgets, VDP wait states, or the performance break-even point between
DMA and CPU-direct writes. It also does not change the logical cold-run count:
both CPU-direct runs and DMA-backed runs remain one Main run-table record, and a
long run split across VBlanks still remains one record. That record count is the
analysis `Run` value and H40 DEBUG HUD `N`; it is not a VDP DMA command count.
