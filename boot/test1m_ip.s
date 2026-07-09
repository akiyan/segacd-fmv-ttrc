/*
 * 1M/1M Word RAM swap self-test - Main (IP) side, step 2: CD read into bank.
 *
 * Asks the Sub to read one sector of PROBE.BIN straight into its 1M Word RAM
 * bank (after filling the bank with a guard pattern) and swap. The Main CPU
 * then verifies the result via the backdrop colour:
 *
 *   green  : 'MPRB' magic visible at 0x200000 AND the guard pattern in the
 *            next sector is intact -> CD read landed exactly one sector into
 *            the Sub bank and the swap exposed it to Main (PASS)
 *   yellow : 'MPRB' magic present but the guard sector was clobbered
 *            (the read overran one sector / wrote outside the bank window)
 *   blue   : no 'MPRB' magic -> CD read or swap did not deliver the data
 */

.equ STACK, 0x00FFFD00

.equ EXVEC_LEVEL6, 0x00FFFD08
.equ BIOS_VBLANK_HANDLER_FLAGS, 0x00FFFE26
.equ BIOS_VBLANK_HANDLER, 0x00000290
.equ BIOS_CLEAR_VRAM, 0x000002A0
.equ BIOS_LOAD_DEFAULT_VDP_REGS, 0x000002AC
.equ BIOS_CLEAR_COMM, 0x00000340

.equ GA_COMCMD0, 0x00A12010
.equ GA_COMSTAT0, 0x00A12020

.equ VDP_CTRL, 0x00C00004
.equ VDP_DATA, 0x00C00000

.equ WORD_RAM, 0x00200000
.equ GUARD_OFF, 0x0800
.equ PROBE_MAGIC, 0x4D505242		/* 'MPRB' */
.equ GUARD, 0xDEADDEAD

.equ CMD_STEP2, 0x20

.text

	.incbin "security.bin"

	bra.w	ip_entry
	.org	0x584

.global ip_entry
ip_entry:
	move.w	#0x2700, sr
	lea	STACK, sp

	bsr	init_bios_runtime

	move.w	#CMD_STEP2, d0
	bsr	sub_command

	/* Magic check: did the CD read + swap deliver PROBE.BIN's header? */
	move.l	(WORD_RAM).l, d0
	cmp.l	#PROBE_MAGIC, d0
	bne	result_no_magic

	/* Non-destruction check: the next sector must still hold the guard. */
	move.l	(WORD_RAM+GUARD_OFF).l, d0
	cmp.l	#GUARD, d0
	bne	result_corrupt

	move.w	#0x00E0, d0		/* green */
	bra	show
result_corrupt:
	move.w	#0x00EE, d0		/* yellow */
	bra	show
result_no_magic:
	move.w	#0x0E00, d0		/* blue */
show:
	bsr	set_backdrop
hang:
	bra	hang

init_bios_runtime:
	jsr	BIOS_LOAD_DEFAULT_VDP_REGS
	jsr	BIOS_CLEAR_VRAM
	jsr	BIOS_CLEAR_COMM
	move.b	#0x00, (BIOS_VBLANK_HANDLER_FLAGS).l
	move.l	#BIOS_VBLANK_HANDLER, (EXVEC_LEVEL6).l
	move.w	#0x2000, sr
	rts

sub_command:
	move.w	d0, (GA_COMCMD0).l
1:
	tst.w	(GA_COMSTAT0).l
	beq	1b
	move.w	#0, (GA_COMCMD0).l
2:
	tst.w	(GA_COMSTAT0).l
	bne	2b
	rts

set_backdrop:
	move.l	#0xC0000000, (VDP_CTRL).l
	move.w	d0, (VDP_DATA).l
	rts
