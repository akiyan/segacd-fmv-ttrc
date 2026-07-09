/*
 * Continuous-stream self-test - Main (IP) side.
 *
 * The Sub streams STREAM.DAT continuously into a 1M/1M double buffer, swapping
 * one frame at a time. Each frame is filled with its own index byte, so after
 * every swap we read one byte from the Main bank and it should equal the frame
 * counter (mod 256).
 *
 *   row 8  (MARK) = byte read back from the streamed frame
 *   row 11 (EXP ) = expected frame index (mod 256)
 *   backdrop blue  = streaming, values match
 *   backdrop red   = MARK != EXP at some point (stream stalled/desynced) - latched
 *
 * Glance test: blue backdrop + MARK ticking up 00->FF smoothly == the
 * continuous read keeps up and never stops. A frozen MARK or a red backdrop is
 * an obvious failure.
 */

.equ STACK, 0x00FFFD00

.equ BIOS_CLEAR_VRAM, 0x000002A0
.equ BIOS_LOAD_DEFAULT_VDP_REGS, 0x000002AC
.equ BIOS_VDP_DISP_ENABLE, 0x000002D8
.equ BIOS_CLEAR_COMM, 0x00000340

.equ VDP_DATA, 0x00C00000
.equ VDP_CTRL, 0x00C00004

.equ GA_COMCMD0, 0x00A12010
.equ GA_COMCMD1, 0x00A12012
.equ GA_COMSTAT0, 0x00A12020

.equ PROBE_BANK, 0x00200000

.equ NUM_FRAMES, 256

.equ CMD_STREAM, 0x50
.equ CMD_SWAP,   0x51
.equ STAT_READY, 0x8003

.equ NAME_A, 0xC000
.equ MARK_CELL, NAME_A+(8*64+4)*2
.equ EXP_CELL,  NAME_A+(11*64+4)*2

.equ COL_RUN, 0x0E00		/* blue  */
.equ COL_BAD, 0x000E		/* red   */

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
	move.w	#COL_RUN, d0
	bsr	set_palette
	jsr	BIOS_VDP_DISP_ENABLE

	move.w	#0, d0
	move.l	#MARK_CELL, d1
	bsr	print_hex16
	move.w	#0, d0
	move.l	#EXP_CELL, d1
	bsr	print_hex16

	/* Start the continuous stream; Sub inits CD, switches to 1M, shows frame 0. */
	move.w	#NUM_FRAMES, (GA_COMCMD1).l
	move.w	#CMD_STREAM, d0
	bsr	cmd_wait_ready

	clr.w	frame_no
	clr.w	started
play_loop:
	tst.w	started
	beq	1f
	move.w	#CMD_SWAP, d0
	bsr	cmd_wait_ready
1:
	move.w	#1, started
	bsr	wait_vblank

	moveq	#0, d0
	move.b	(PROBE_BANK).l, d0		/* marker byte from the streamed frame */
	move.w	d0, d3				/* d3 = marker */
	move.w	frame_no, d2
	andi.w	#0x00FF, d2			/* d2 = expected */

	move.w	d3, d0
	move.l	#MARK_CELL, d1
	bsr	print_hex16
	move.w	d2, d0
	move.l	#EXP_CELL, d1
	bsr	print_hex16

	cmp.w	d2, d3
	beq	2f
	move.w	#COL_BAD, d0			/* mismatch -> latch red */
	bsr	set_palette
2:
	addq.w	#1, frame_no
	cmp.w	#NUM_FRAMES, frame_no
	bne	play_loop
	clr.w	frame_no
	bra	play_loop

/* Send command d0, wait for STAT_READY, finish the handshake. */
cmd_wait_ready:
	move.w	d0, (GA_COMCMD0).l
1:
	cmp.w	#STAT_READY, (GA_COMSTAT0).l
	bne	1b
	move.w	#0, (GA_COMCMD0).l
2:
	tst.w	(GA_COMSTAT0).l
	bne	2b
	rts

load_font:
	move.l	#0x40200000, (VDP_CTRL).l
	lea	hexfont, a0
	move.w	#(512/2)-1, d0
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d0, 1b
	rts

set_palette:
	move.l	#0xC0000000, (VDP_CTRL).l
	move.w	d0, (VDP_DATA).l
	move.w	#0x0EEE, (VDP_DATA).l
	rts

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
	addq.w	#1, d0
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
frame_no:
	.space 2
started:
	.space 2
