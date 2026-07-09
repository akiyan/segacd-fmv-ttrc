/*
 * Continuous-stream self-test - Sub (SP) side.
 *
 * Streams STREAM.DAT with ONE ROM_READN over the whole file and drains it one
 * FRAME unit (FRAME_SECTORS sectors) at a time into the 1M Sub bank, swapping
 * every single frame. There is NO per-frame CDC restart - the read runs
 * continuously; only at end-of-file is ROM_READN reissued (movie loop).
 *
 * This validates the real goal: a continuous read that never stops, feeding a
 * 1M/1M double buffer that swaps every frame.
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

.equ NUM_FRAMES,    256
.equ FRAME_SECTORS, 5
.equ STREAM_TOTAL,  NUM_FRAMES*FRAME_SECTORS

.equ CMD_STREAM, 0x50
.equ CMD_SWAP,   0x51
.equ STAT_READY, 0x8003

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
	move.w	#0, (COMSTAT1).l
	rts

.global sp_main
sp_main:
command_loop:
	tst.w	(COMCMD0).l
	bne	1f
	bra	command_loop
1:
	cmp.w	#CMD_STREAM, (COMCMD0).l
	beq	do_stream
	move.w	(COMCMD0).l, (COMSTAT0).l
2:
	tst.w	(COMCMD0).l
	bne	2b
	move.w	#0, (COMSTAT0).l
	bra	sp_main

do_stream:
	move.w	#CMD_STREAM, (COMSTAT0).l
	/* CD drive + ISO9660 init while still in 2M/clean mode. */
	andi.b	#0xFA, (MEMMODE+1).l
	lea	drv_init_tracklist, a0
	BIOSCALL BIOS_DRV_INIT
1:
	BIOSCALL BIOS_CDB_STAT
	andi.b	#0xF0, (CDB_STAT).w
	bne	1b
	bsr	init_iso9660
	lea	file_stream, a0
	bsr	find_file		/* d0 = LBA */
	move.l	d0, stream_lba
	/* Switch to 1M, start the continuous read, fill + show frame 0. */
	bset	#2, (MEMMODE+1).l
	bsr	issue_rom_readn
	bsr	fill_one_frame
	bchg	#0, (MEMMODE+1).l
	bsr	swap_settle
	move.w	#STAT_READY, (COMSTAT0).l
2:
	tst.w	(COMCMD0).l
	bne	2b
	move.w	#0, (COMSTAT0).l
stream_loop:
	bsr	fill_one_frame
3:
	cmp.w	#CMD_SWAP, (COMCMD0).l
	bne	3b
	bchg	#0, (MEMMODE+1).l
	bsr	swap_settle
	move.w	#STAT_READY, (COMSTAT0).l
4:
	tst.w	(COMCMD0).l
	bne	4b
	move.w	#0, (COMSTAT0).l
	bra	stream_loop

issue_rom_readn:
	movem.l	d0-d7/a0-a6, -(sp)
	lea	bios_packet, a5
	move.l	stream_lba, (a5)
	move.l	#STREAM_TOTAL, 4(a5)
	move.l	#SUB_BANK_1M, 8(a5)
	movea.l	a5, a0
	BIOSCALL BIOS_CDC_STOP
	BIOSCALL BIOS_ROM_READN
	move.l	#STREAM_TOTAL, stream_remaining
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* Drain one FRAME unit (FRAME_SECTORS sectors) into the Sub bank, reissuing the
   read once the whole file has been consumed (loop). */
fill_one_frame:
	movem.l	d0-d7/a0-a6, -(sp)
	tst.l	stream_remaining
	bne	1f
	bsr	issue_rom_readn
1:
	move.l	#SUB_BANK_1M, bank_dest
	move.w	#FRAME_SECTORS, bank_count
fof_sector:
	lea	bios_packet, a5
2:
	BIOSCALL BIOS_CDC_STAT
	bcs	2b
3:
	BIOSCALL BIOS_CDC_READ
	bcc	3b
4:
	movea.l	bank_dest, a0
	lea	12(a5), a1
	BIOSCALL BIOS_CDC_TRN
	bcc	4b
	BIOSCALL BIOS_CDC_ACK
	add.l	#0x0800, bank_dest
	subq.l	#1, stream_remaining
	subq.w	#1, bank_count
	bne	fof_sector
	movem.l	(sp)+, d0-d7/a0-a6
	rts

swap_settle:
	movem.l	d0, -(sp)
	move.w	#0x0400, d0
1:
	dbra	d0, 1b
	movem.l	(sp)+, d0
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

file_stream:
	.asciz	"STREAM.DAT"
	.align	2

bios_packet:
	.long	0, 0, 0, 0, 0
stream_lba:
	.long	0
stream_remaining:
	.long	0
bank_dest:
	.long	0
bank_count:
	.word	0

sp_end:
