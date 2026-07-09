/*
 * Isolated CDC throughput test - Sub (SP) side.
 *
 * Protocol (COMCMD0 from Main):
 *   CMD_INIT       (0x40): DRV_INIT + ISO9660 + locate BENCH.DAT, store its LBA.
 *   CMD_BENCH_CONT (0x41): read BENCH_TOTAL sectors in ONE continuous ROM_READN.
 *   CMD_BENCH_CHUNK(0x42): read BENCH_TOTAL sectors as ceil(BENCH_TOTAL/CHUNK)
 *                          separate CDC_STOP+ROM_READN calls of CHUNK sectors
 *                          each (the per-chunk restart the real player pays).
 *
 * Both timed modes transfer to the SAME 2 KB buffer (no dest increment) and bump
 * COMSTAT1 per drained sector so Main can sample the live sector count. Each
 * command acks via COMSTAT0 and returns to the command loop for the next one.
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

.equ CMD_INIT,        0x40
.equ CMD_BENCH_CONT,  0x41
.equ CMD_BENCH_CHUNK, 0x42
.equ STAT_INIT_DONE, 0x0001
.equ STAT_DONE,      0x00D0

.equ BENCH_TOTAL,   1472	/* sectors per timed run (46 * 32) */
.equ CHUNK_SECTORS, 32		/* chunk size for the chunked mode  */

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
	beq	command_loop
	moveq	#0, d0
	move.w	(COMCMD0).l, d0
	cmp.w	#CMD_INIT, d0
	beq	c_init
	cmp.w	#CMD_BENCH_CONT, d0
	beq	c_cont
	cmp.w	#CMD_BENCH_CHUNK, d0
	beq	c_chunk
	/* unknown: just ack-clear */
	move.w	#STAT_DONE, (COMSTAT0).l
	bra	ack_wait

c_init:
	bsr	do_init
	move.w	#STAT_INIT_DONE, (COMSTAT0).l
	bra	ack_wait
c_cont:
	bsr	do_cont
	move.w	#STAT_DONE, (COMSTAT0).l
	bra	ack_wait
c_chunk:
	bsr	do_chunk
	move.w	#STAT_DONE, (COMSTAT0).l
	bra	ack_wait

ack_wait:
	tst.w	(COMCMD0).l
	bne	ack_wait
	move.w	#0, (COMSTAT0).l
	bra	sp_main

do_init:
	andi.b	#0xFA, (MEMMODE+1).l
	lea	drv_init_tracklist, a0
	BIOSCALL BIOS_DRV_INIT
1:
	BIOSCALL BIOS_CDB_STAT
	andi.b	#0xF0, (CDB_STAT).w
	bne	1b
	bsr	init_iso9660
	lea	file_bench, a0
	bsr	find_file		/* d0 = LBA, d1 = size sectors */
	move.l	d0, bench_lba
	rts

/* One continuous ROM_READN of BENCH_TOTAL sectors. COMSTAT1 = live count. */
do_cont:
	movem.l	d0-d7/a0-a6, -(sp)
	move.w	#0, (COMSTAT1).l
	lea	bios_packet, a5
	move.l	bench_lba, (a5)
	move.l	#BENCH_TOTAL, 4(a5)
	move.l	#SECTOR_BUFFER, 8(a5)
	movea.l	a5, a0
	BIOSCALL BIOS_CDC_STOP
	BIOSCALL BIOS_ROM_READN
1:
	BIOSCALL BIOS_CDC_STAT
	bcs	1b
2:
	BIOSCALL BIOS_CDC_READ
	bcc	2b
3:
	movea.l	#SECTOR_BUFFER, a0
	lea	12(a5), a1
	BIOSCALL BIOS_CDC_TRN
	bcc	3b
	BIOSCALL BIOS_CDC_ACK
	addq.w	#1, (COMSTAT1).l
	subq.l	#1, 4(a5)
	bne	1b
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* BENCH_TOTAL sectors as CHUNK_SECTORS-sized separate reads, each restarting
   the CDC pipeline (CDC_STOP + ROM_READN). COMSTAT1 = live count. */
do_chunk:
	movem.l	d0-d7/a0-a6, -(sp)
	move.w	#0, (COMSTAT1).l
	move.l	bench_lba, chunk_lba
	move.l	#BENCH_TOTAL, chunk_remaining
next_chunk:
	move.l	chunk_remaining, d0
	cmp.l	#CHUNK_SECTORS, d0
	bls	1f
	move.l	#CHUNK_SECTORS, d0
1:
	move.l	d0, chunk_size
	lea	bios_packet, a5
	move.l	chunk_lba, (a5)
	move.l	chunk_size, 4(a5)
	move.l	#SECTOR_BUFFER, 8(a5)
	movea.l	a5, a0
	BIOSCALL BIOS_CDC_STOP
	BIOSCALL BIOS_ROM_READN
2:
	BIOSCALL BIOS_CDC_STAT
	bcs	2b
3:
	BIOSCALL BIOS_CDC_READ
	bcc	3b
4:
	movea.l	#SECTOR_BUFFER, a0
	lea	12(a5), a1
	BIOSCALL BIOS_CDC_TRN
	bcc	4b
	BIOSCALL BIOS_CDC_ACK
	addq.w	#1, (COMSTAT1).l
	subq.l	#1, 4(a5)
	bne	2b
	move.l	chunk_size, d0
	add.l	d0, chunk_lba
	move.l	chunk_remaining, d1
	sub.l	d0, d1
	move.l	d1, chunk_remaining
	bne	next_chunk
	movem.l	(sp)+, d0-d7/a0-a6
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

file_bench:
	.asciz	"BENCH.DAT"
	.align	2

bios_packet:
	.long	0, 0, 0, 0, 0
bench_lba:
	.long	0
chunk_lba:
	.long	0
chunk_size:
	.long	0
chunk_remaining:
	.long	0

sp_end:
