/*
 * RF5C164 PCM self-test - Main (IP) side.
 *
 * Tells the Sub to start a looping 250Hz tone on the PCM chip, then shows a
 * counter ticking up so a headless capture confirms the program is alive
 * (the tone itself must be checked by ear / recorded audio). Backdrop:
 *   blue  = before audio start
 *   green = audio start handshake done, counter running
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

.equ CMD_START_AUDIO, 0x60
.equ STAT_DONE, 0x00D0

.equ NAME_A, 0xC000
.equ CNT_CELL, NAME_A+(10*64+4)*2

.equ COL_PRE, 0x0E00		/* blue  */
.equ COL_RUN, 0x00E0		/* green */

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

	move.w	#0x8230, (VDP_CTRL).l
	move.w	#0x9001, (VDP_CTRL).l
	bsr	load_font
	move.w	#COL_PRE, d0
	bsr	set_palette
	jsr	BIOS_VDP_DISP_ENABLE

	move.w	#0, d0
	move.l	#CNT_CELL, d1
	bsr	print_hex16

	/* start the PCM tone */
	move.w	#CMD_START_AUDIO, (GA_COMCMD0).l
1:
	cmp.w	#STAT_DONE, (GA_COMSTAT0).l
	bne	1b
	move.w	#0, (GA_COMCMD0).l
2:
	tst.w	(GA_COMSTAT0).l
	bne	2b

	move.w	#COL_RUN, d0
	bsr	set_palette

	clr.w	counter
play_loop:
	bsr	wait_vblank
	addq.w	#1, counter
	move.w	counter, d0
	move.l	#CNT_CELL, d1
	bsr	print_hex16
	bra	play_loop

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
counter:
	.space 2
