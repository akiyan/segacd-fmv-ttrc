/*
 * RF5C164 PCM self-test - Sub (SP) side.
 *
 * On CMD_START_AUDIO: enable the PCM chip, write a short looping square wave
 * into wave RAM, and play it on channel 0 at ~12kHz. The sample loops in
 * hardware so the Sub just sets it up; a clean steady tone on a real machine /
 * audio-enabled emulator confirms the RF5C164 register path works.
 *
 * RF5C164 registers (Sub side, byte-wide at odd addresses):
 *   FF0001 ENV  FF0003 PAN  FF0005 FDL  FF0007 FDH  FF0009 LSL  FF000B LSH
 *   FF000D ST   FF000F CTRL FF0011 ONOFF      wave RAM window: FF2000-FF3FFF
 *   CTRL: bit7 sound enable, bit6 0=channel-regs / 1=wave-bank, bits0-3 ch/bank
 *   ONOFF: bit n = 1 disables channel n.  Sample: 8-bit, 0xFF = loop marker.
 */

.equ SUB_GA_BASE, 0x00FF8000
.equ MEMMODE,     SUB_GA_BASE+0x0002
.equ COMCMD0,     SUB_GA_BASE+0x0010
.equ COMSTAT0,    SUB_GA_BASE+0x0020

.equ PCM_ENV,   0x00FF0001
.equ PCM_PAN,   0x00FF0003
.equ PCM_FDL,   0x00FF0005
.equ PCM_FDH,   0x00FF0007
.equ PCM_LSL,   0x00FF0009
.equ PCM_LSH,   0x00FF000B
.equ PCM_ST,    0x00FF000D
.equ PCM_CTRL,  0x00FF000F
.equ PCM_ONOFF, 0x00FF0011
.equ PCM_WAVE,  0x00FF2001		/* wave RAM: 8-bit chip on odd bytes, step 2 */

.equ TONE_LEN, 48			/* square-wave period in samples (12000/48 = 250Hz) */

.equ CMD_START_AUDIO, 0x60
.equ STAT_DONE, 0x00D0

.text

sp_header:
	.ascii	"MAIN       "
	.byte	0
	.word	0x0100
	.word	0
	.long	0
	.long	sp_end-sp_header
	.long	sp_jmptbl-sp_header
	.long	0

sp_jmptbl:
	.word	sp_init-sp_jmptbl
	.word	sp_main-sp_jmptbl
	.word	sp_int2-sp_jmptbl
	.word	sp_user-sp_jmptbl
	.word	0

.global sp_init
sp_init:
	move.w	#0x2700, sr
	andi.w	#0xFFFA, (MEMMODE).l
	move.w	#0, (COMSTAT0).l
	rts

.global sp_main
sp_main:
command_loop:
	tst.w	(COMCMD0).l
	bne	1f
	bra	command_loop
1:
	cmp.w	#CMD_START_AUDIO, (COMCMD0).l
	bne	done
	bsr	start_audio
done:
	move.w	#STAT_DONE, (COMSTAT0).l
2:
	tst.w	(COMCMD0).l
	bne	2b
	move.w	#0, (COMSTAT0).l
	bra	sp_main

start_audio:
	/* select wave-RAM bank 0: CTRL = WAVEBANK(0) = 0x80 (bit7 enable, bit6=0) */
	move.b	#0x80, (PCM_CTRL).l
	/* square wave into wave RAM at ODD bytes (0xFF2001), stepping by 2 */
	lea	(PCM_WAVE).l, a0
	move.w	#(TONE_LEN/2)-1, d0
1:
	move.b	#0x7F, (a0)			/* +127 */
	addq.l	#2, a0				/* odd bytes only: step 2 */
	dbra	d0, 1b
	move.w	#(TONE_LEN/2)-1, d0
2:
	move.b	#0xFE, (a0)			/* sign-magnitude -126 */
	addq.l	#2, a0
	dbra	d0, 2b
	move.b	#0xFF, (a0)			/* loop / end marker */

	/* configure channel 1: CTRL = CHANNEL(1) = 0xC0 (bit7 enable, bit6=1, ch 0) */
	move.b	#0xC0, (PCM_CTRL).l
	move.b	#0xFF, (PCM_ENV).l		/* full volume */
	nop
	nop
	move.b	#0xFF, (PCM_PAN).l		/* left+right */
	nop
	nop
	/* FD = round(2048 * 12000 / 32552) = 755 = 0x02F3 */
	move.b	#0xF3, (PCM_FDL).l
	nop
	nop
	move.b	#0x02, (PCM_FDH).l
	nop
	nop
	move.b	#0x00, (PCM_LSL).l		/* loop start 0x0000 */
	nop
	nop
	move.b	#0x00, (PCM_LSH).l
	nop
	nop
	move.b	#0x00, (PCM_ST).l		/* start address 0x0000 (ST<<8) */
	nop
	nop
	/* channel on: bit0=0 enables ch1, others off */
	move.b	#0xFE, (PCM_ONOFF).l
	rts

.global sp_int2
sp_int2:
	rts

.global sp_user
sp_user:
	rts

sp_end:
