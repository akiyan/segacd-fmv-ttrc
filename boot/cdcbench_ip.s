/*
 * Isolated CDC throughput test - Main (IP) side.
 *
 * Runs two timed reads of the same total (BENCH_TOTAL sectors) back to back and
 * shows the frame cost of each, so continuous vs per-chunk-restart can be
 * compared from one headless screenshot:
 *
 *   row  8 (CONT)  = frames for the continuous single ROM_READN
 *   row 11 (CHUNK) = frames for the chunked (CHUNK_SECTORS per ROM_READN) read
 *   row 14 (SECT)  = live sectors drained in the current phase
 *   backdrop blue   = phase 1 (continuous)
 *   backdrop yellow = phase 2 (chunked)
 *   backdrop green  = done (both counters frozen)
 *
 * Sustained rate = BENCH_TOTAL * 2048 / (frames/59.92) bytes/sec.
 * Interrupts stay off; frames are counted by polling the VDP vblank flag.
 */

.equ STACK, 0x00FFFD00

.equ BIOS_CLEAR_VRAM, 0x000002A0
.equ BIOS_LOAD_DEFAULT_VDP_REGS, 0x000002AC
.equ BIOS_VDP_DISP_ENABLE, 0x000002D8
.equ BIOS_CLEAR_COMM, 0x00000340

.equ VDP_DATA, 0x00C00000
.equ VDP_CTRL, 0x00C00004

.equ GA_COMCMD0, 0x00A12010
.equ GA_COMSTAT0, 0x00A12020
.equ GA_COMSTAT1, 0x00A12022

.equ CMD_INIT,        0x40
.equ CMD_BENCH_CONT,  0x41
.equ CMD_BENCH_CHUNK, 0x42
.equ STAT_DONE, 0x00D0

.equ NAME_A, 0xC000
.equ CONT_CELL,  NAME_A+(8*64+4)*2
.equ CHUNK_CELL, NAME_A+(11*64+4)*2
.equ SECT_CELL,  NAME_A+(14*64+4)*2

.equ COL_CONT,  0x0E00		/* blue   */
.equ COL_CHUNK, 0x0EE0		/* yellow */
.equ COL_DONE,  0x00E0		/* green  */

.text

	.incbin "security.bin"

	bra.w	ip_entry
	.org	0x584

.global ip_entry
ip_entry:
	move.w	#0x2700, sr
	lea	STACK, sp

	jsr	BIOS_LOAD_DEFAULT_VDP_REGS
	jsr	BIOS_CLEAR_VRAM
	jsr	BIOS_CLEAR_COMM

	move.w	#0x8230, (VDP_CTRL).l		/* plane A name table = 0xC000 */
	move.w	#0x9001, (VDP_CTRL).l		/* plane size 64x32          */
	bsr	load_font
	move.w	#COL_CONT, d0
	bsr	set_palette
	jsr	BIOS_VDP_DISP_ENABLE

	move.w	#0, d0
	move.l	#CONT_CELL, d1
	bsr	print_hex16
	move.w	#0, d0
	move.l	#CHUNK_CELL, d1
	bsr	print_hex16
	move.w	#0, d0
	move.l	#SECT_CELL, d1
	bsr	print_hex16

	/* Untimed drive/ISO init. */
	move.w	#CMD_INIT, d0
	bsr	sub_command

	/* --- Phase 1: continuous --- */
	clr.l	frame_count
	move.w	#CMD_BENCH_CONT, (GA_COMCMD0).l
p1:
	bsr	wait_vblank
	addq.l	#1, frame_count
	move.w	(GA_COMSTAT1).l, d0
	move.l	#SECT_CELL, d1
	bsr	print_hex16
	move.w	(frame_count+2), d0
	move.l	#CONT_CELL, d1
	bsr	print_hex16
	cmp.w	#STAT_DONE, (GA_COMSTAT0).l
	bne	p1
	move.l	frame_count, cont_frames
	bsr	finish_handshake

	/* --- Phase 2: chunked --- */
	move.w	#COL_CHUNK, d0
	bsr	set_palette
	clr.l	frame_count
	move.w	#CMD_BENCH_CHUNK, (GA_COMCMD0).l
p2:
	bsr	wait_vblank
	addq.l	#1, frame_count
	move.w	(GA_COMSTAT1).l, d0
	move.l	#SECT_CELL, d1
	bsr	print_hex16
	move.w	(frame_count+2), d0
	move.l	#CHUNK_CELL, d1
	bsr	print_hex16
	cmp.w	#STAT_DONE, (GA_COMSTAT0).l
	bne	p2
	move.l	frame_count, chunk_frames
	bsr	finish_handshake

	/* Done: freeze both counters, go green. */
	move.w	#COL_DONE, d0
	bsr	set_palette
	move.w	(cont_frames+2), d0
	move.l	#CONT_CELL, d1
	bsr	print_hex16
	move.w	(chunk_frames+2), d0
	move.l	#CHUNK_CELL, d1
	bsr	print_hex16
	move.w	(GA_COMSTAT1).l, d0
	move.l	#SECT_CELL, d1
	bsr	print_hex16
hang:
	bra	hang

sub_command:
	move.w	d0, (GA_COMCMD0).l
1:
	tst.w	(GA_COMSTAT0).l
	beq	1b
finish_handshake:
	move.w	#0, (GA_COMCMD0).l
1:
	tst.w	(GA_COMSTAT0).l
	bne	1b
	rts

load_font:
	move.l	#0x40200000, (VDP_CTRL).l	/* VRAM write at tile 1 (byte 0x20) */
	lea	hexfont, a0
	move.w	#(512/2)-1, d0
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d0, 1b
	rts

/* d0 = colour 0 (state/backdrop); colour 1 fixed white. */
set_palette:
	move.l	#0xC0000000, (VDP_CTRL).l
	move.w	d0, (VDP_DATA).l
	move.w	#0x0EEE, (VDP_DATA).l
	rts

/* d0 = value (word), d1 = name-table byte address. */
print_hex16:
	movem.l	d0-d3, -(sp)
	move.w	d0, d3
	move.l	d1, d0
	bsr	vram_write_cmd
	moveq	#4-1, d2
1:
	rol.w	#4, d3
	move.w	d3, d0
	andi.w	#0x000F, d0
	addq.w	#1, d0			/* glyph tile = 1 + nibble */
	move.w	d0, (VDP_DATA).l
	dbra	d2, 1b
	movem.l	(sp)+, d0-d3
	rts

vram_write_cmd:
	and.l	#0x0000FFFF, d0
	lsl.l	#2, d0
	lsr.w	#2, d0
	swap	d0
	or.l	#0x40000000, d0
	move.l	d0, (VDP_CTRL).l
	rts

wait_vblank:
	move.w	d1, -(sp)
1:
	move.w	(VDP_CTRL).l, d1
	btst	#3, d1
	beq	1b
2:
	move.w	(VDP_CTRL).l, d1
	btst	#3, d1
	bne	2b
	move.w	(sp)+, d1
	rts

	.data
	.align 2
hexfont:
	.incbin "hexfont.bin"

	.bss
	.align 2
frame_count:
	.space 4
cont_frames:
	.space 4
chunk_frames:
	.space 4
