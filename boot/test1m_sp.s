/*
 * 1M/1M Word RAM swap self-test - Sub (SP) side, step 2: CD read into bank.
 *
 * On CMD_STEP2 the Sub:
 *   1. inits the CD drive and ISO9660, finds PROBE.BIN (2M/clean mode),
 *   2. switches to 1M mode,
 *   3. fills its 1M bank with 0xDEADDEAD (guard pattern),
 *   4. read_cd's exactly ONE sector of PROBE.BIN into the bank at 0x0C0000,
 *   5. swaps banks so the Main CPU can verify it.
 *
 * Main then checks the 'MPRB' magic at 0x200000 and that the guard pattern in
 * the following sector is intact (the read touched only 2048 bytes).
 */

.equ CDBIOS,      0x00005F22
.equ CDB_STAT,    0x00005E80

.equ BIOS_DRV_INIT, 0x0010
.equ BIOS_CDB_STAT, 0x0081
.equ BIOS_CDC_STOP, 0x0089
.equ BIOS_CDC_STAT, 0x008A
.equ BIOS_CDC_READ, 0x008B
.equ BIOS_CDC_TRN,  0x008C
.equ BIOS_CDC_ACK,  0x008D
.equ BIOS_ROM_READN,0x0020

.equ SUB_GA_BASE, 0x00FF8000
.equ MEMMODE,     SUB_GA_BASE+0x0002
.equ COMCMD0,     SUB_GA_BASE+0x0010
.equ COMSTAT0,    SUB_GA_BASE+0x0020
.equ COMSTAT1,    SUB_GA_BASE+0x0022

.equ SECTOR_BUFFER, 0x00008000
.equ SUB_BANK_1M,   0x000C0000
.equ GUARD,         0xDEADDEAD
.equ GUARD_LONGS,   0x1000/4		/* fill 2 sectors with the guard pattern */

.equ CMD_STEP2, 0x20

.macro BIOSCALL code
	move.w	#\code, d0
	jsr	CDBIOS
.endm

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
	cmp.w	#CMD_STEP2, (COMCMD0).l
	bne	command_done
	bsr	do_step2
command_done:
	move.w	#1, (COMSTAT0).l
2:
	tst.w	(COMCMD0).l
	bne	2b
	move.w	#0, (COMSTAT0).l
	bra	sp_main

do_step2:
	/* CD drive + ISO9660 init while Word RAM is in 2M/clean mode. */
	andi.b	#0xFA, (MEMMODE+1).l
	lea	drv_init_tracklist, a0
	BIOSCALL BIOS_DRV_INIT
1:
	BIOSCALL BIOS_CDB_STAT
	andi.b	#0xF0, (CDB_STAT).w
	bne	1b
	bsr	init_iso9660
	lea	file_probe, a0
	bsr	find_file		/* d0 = LBA, d1 = size sectors */

	/* Switch to 1M, lay down the guard pattern, then read one sector. */
	bset	#2, (MEMMODE+1).l
	movem.l	d0, -(sp)		/* preserve LBA across the fill */
	lea	SUB_BANK_1M, a0
	move.w	#GUARD_LONGS-1, d2
	move.l	#GUARD, d3
3:
	move.l	d3, (a0)+
	dbra	d2, 3b
	movem.l	(sp)+, d0
	moveq	#1, d1
	lea	SUB_BANK_1M, a0
	bsr	read_cd

	/* Swap so the freshly read bank becomes visible to Main. */
	bchg	#0, (MEMMODE+1).l
	move.w	#0x1000, d1
4:
	dbra	d1, 4b
	rts

read_cd:
	movem.l	d0-d7/a0-a6, -(sp)
	lea	bios_packet, a5
	move.l	d0, (a5)
	move.l	d1, 4(a5)
	move.l	a0, 8(a5)
	movea.l	a5, a0
	BIOSCALL BIOS_CDC_STOP
	BIOSCALL BIOS_ROM_READN
wait_stat:
	BIOSCALL BIOS_CDC_STAT
	bcs	wait_stat
wait_read:
	BIOSCALL BIOS_CDC_READ
	bcc	wait_read
wait_transfer:
	movea.l	8(a5), a0
	lea	12(a5), a1
	BIOSCALL BIOS_CDC_TRN
	bcc	wait_transfer
	BIOSCALL BIOS_CDC_ACK
	addq.l	#1, (a5)
	addi.l	#0x0800, 8(a5)
	subq.l	#1, 4(a5)
	bne	wait_stat
	movem.l	(sp)+, d0-d7/a0-a6
	rts

init_iso9660:
	movem.l	d0-d7/a0-a6, -(sp)
	move.l	#0x10, d0
	move.l	#2, d1
	lea	SECTOR_BUFFER, a0
	bsr	read_cd

	lea	SECTOR_BUFFER, a0
	lea	156(a0), a1
	moveq	#0, d0
	move.b	6(a1), d0
	lsl.l	#8, d0
	move.b	7(a1), d0
	lsl.l	#8, d0
	move.b	8(a1), d0
	lsl.l	#8, d0
	move.b	9(a1), d0
	move.l	#0x20, d1
	lea	SECTOR_BUFFER, a0
	bsr	read_cd
	movem.l	(sp)+, d0-d7/a0-a6
	rts

find_file:
	movem.l	a1-a2/a6, -(sp)
	lea	SECTOR_BUFFER, a1
read_filename_start:
	movea.l	a0, a6
	move.b	(a6)+, d0
find_first_char:
	movea.l	a1, a2
	cmp.b	(a1)+, d0
	bne	find_first_char
check_chars:
	move.b	(a6)+, d0
	beq	get_info
	cmp.b	(a1)+, d0
	bne	read_filename_start
	bra	check_chars
get_info:
	sub.l	#33, a2
	moveq	#0, d0
	move.b	6(a2), d0
	lsl.l	#8, d0
	move.b	7(a2), d0
	lsl.l	#8, d0
	move.b	8(a2), d0
	lsl.l	#8, d0
	move.b	9(a2), d0

	moveq	#0, d1
	move.b	14(a2), d1
	lsl.l	#8, d1
	move.b	15(a2), d1
	lsl.l	#8, d1
	move.b	16(a2), d1
	lsl.l	#8, d1
	move.b	17(a2), d1
	addi.l	#0x07FF, d1
	lsr.l	#8, d1
	lsr.l	#3, d1
	movem.l	(sp)+, a1-a2/a6
	rts

.global sp_int2
sp_int2:
	rts

.global sp_user
sp_user:
	rts

drv_init_tracklist:
	.byte	1, 0xFF

file_probe:
	.asciz	"MOVIE.DAT"
	.align	2

bios_packet:
	.long	0, 0, 0, 0, 0

sp_end:
