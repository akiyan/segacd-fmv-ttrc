/*
 * Mega-CD System Program - continuous-stream movie player (Sub side).
 *
 * Boot:   COMCMD0 = 1 (CMD_LOAD_M_INIT) loads M_INIT.PRG to 2M Word RAM and the
 *         1-sector PROBE.BIN header to PROBE_DATA_2M, records probe_base_lba.
 * Stream: COMCMD0 = 8 (CMD_STREAM_START, frame count in COMCMD1) switches Word
 *         RAM to 1M, issues ONE ROM_READN over the whole movie and drains it one
 *         FRAME unit (FRAME_SECTORS sectors) at a time into the 1M Sub bank,
 *         swapping the 1M/1M double buffer every frame (COMCMD0 = 7 CMD_SWAP).
 *         No per-frame CDC restart; ROM_READN is reissued only to loop the movie.
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
.equ BIOS_MSCPLAYR, 0x0013		/* play one CD-DA track, repeat */
.equ BIOS_MSCSTOP,  0x0002		/* stop CD-DA */
.equ BIOS_FDRSET,   0x0085		/* set CD-DA fader (volume) */

.equ SUB_GA_BASE, 0x00FF8000
.equ MEMMODE,     SUB_GA_BASE+0x0002
.equ COMCMD0,     SUB_GA_BASE+0x0010
.equ COMCMD1,     SUB_GA_BASE+0x0012
.equ COMSTAT0,    SUB_GA_BASE+0x0020
.equ COMSTAT1,    SUB_GA_BASE+0x0022
.equ WORD_RAM_2M, 0x00080000
.equ PROBE_DATA_2M,0x00090000
.equ SECTOR_BUFFER, 0x00008000
.equ SUB_BANK_1M, 0x000C0000

.equ HEADER_SECTORS, 1
.equ FRAME_SECTORS,  5

/* Per-frame audio: 13.3kHz mono 8-bit, interleaved after the tile+pmap payload.
   AUDIO_OFF must match package_global4's frame layout. */
.equ AUDIO_OFF,   8864			/* tile 7680 + pmap 240 + pal 128 + overlay(24*34=816) */
.equ AUDIO_BYTES, 887			/* 13300/15 samples per frame, rounded up */
.equ PCM_ENV,   0x00FF0001
.equ PCM_PAN,   0x00FF0003
.equ PCM_FDL,   0x00FF0005
.equ PCM_FDH,   0x00FF0007
.equ PCM_LSL,   0x00FF0009
.equ PCM_LSH,   0x00FF000B
.equ PCM_ST,    0x00FF000D
.equ PCM_CTRL,  0x00FF000F
.equ PCM_ONOFF, 0x00FF0011
.equ PCM_WAVE,  0x00FF2001		/* wave RAM: 8-bit chip, odd bytes, step 2 */
.equ PCM_PLAY_H, 0x00FF0023		/* channel 1 current play position, high byte */
.equ WAVE_RING_END, 0x8000		/* loop marker here; ring = [0 .. 0x8000) samples */
.equ RING_MASK, WAVE_RING_END-1		/* 0x7FFF */
/* closed-loop sync: keep write_ptr a safe lead ahead of the RF5C164 play head so
   write/play drift never accumulates into a (growing) echo. Re-anchor only when
   the lead leaves [SYNC_MIN, SYNC_MAX] (rare); units = samples. */
.equ SYNC_LEAD, 0x1800			/* target lead after a re-anchor (~0.51s) */
.equ SYNC_MIN,  0x0C00			/* chip catching up -> re-anchor (~0.26s) */
.equ SYNC_MAX,  0x5000			/* write lapping ahead -> re-anchor (~1.7s) */

.equ CMD_LOAD_M_INIT, 1
.equ CMD_RETURN_WORD_RAM, 2
.equ CMD_SWAP_1M, 7
.equ CMD_STREAM_START_1M, 8
.equ CMD_PLAY_CDDA, 9
.equ STAT_DONE, 0x8003

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
	moveq	#0, d0
	move.w	(COMCMD0).l, d0
	cmp.w	#CMD_LOAD_M_INIT, d0
	beq	cmd_load_m_init
	cmp.w	#CMD_RETURN_WORD_RAM, d0
	beq	cmd_return_word_ram
	cmp.w	#CMD_STREAM_START_1M, d0
	beq	cmd_stream_start_1m
	cmp.w	#CMD_PLAY_CDDA, d0
	beq	cmd_play_cdda
	bra	command_done

cmd_load_m_init:
	andi.b	#0xFA, (MEMMODE+1).l
	lea	drv_init_tracklist, a0
	BIOSCALL BIOS_DRV_INIT
1:
	BIOSCALL BIOS_CDB_STAT
	andi.b	#0xF0, (CDB_STAT).w
	bne	1b
	bsr	init_iso9660
	lea	file_m_init, a0
	bsr	find_file
	lea	WORD_RAM_2M, a0
	bsr	read_cd
	lea	file_probe, a0
	bsr	find_file
	move.l	d0, probe_base_lba
	move.l	#HEADER_SECTORS, d1
	lea	PROBE_DATA_2M, a0
	bsr	read_cd
	bra	command_done

cmd_return_word_ram:
	bset	#0, (MEMMODE+1).l
	bra	command_done

/* Play the CD-DA bgm track (track 2), repeating. Drive is already DRV_INIT'd
   from boot; MSCPLAYR switches the drive to audio playback. */
cmd_play_cdda:
	ori.b	#0x04, (SUB_GA_BASE+0x37).l	/* HOCK: enable CDD communication */
	ori.b	#0x3C, (SUB_GA_BASE+0x33).l	/* IEN: enable INT2-5 (timer/CDD/CDC) */
	move.w	#0x2000, sr			/* enable ints BEFORE the BIOS calls */
	BIOSCALL BIOS_MSCSTOP			/* reset drive out of data-read state */
1:
	BIOSCALL BIOS_CDB_STAT			/* wait until the drive is ready/idle */
	andi.b	#0xF0, (CDB_STAT).w
	bne	1b
	move.w	#0x0400, d1			/* fader = max volume */
	BIOSCALL BIOS_FDRSET
	lea	cdda_track, a0
	BIOSCALL BIOS_MSCPLAYR
	bra	command_done

command_done:
	move.w	(COMCMD0).l, (COMSTAT0).l
1:
	tst.w	(COMCMD0).l
	bne	1b
	move.w	#0, (COMSTAT0).l
	bra	sp_main

/* --- Continuous-stream playback (Main sends frame count in COMCMD1) --- */
cmd_stream_start_1m:
	moveq	#0, d0
	move.w	(COMCMD1).l, d0
	move.w	#CMD_STREAM_START_1M, (COMSTAT0).l
	mulu.w	#FRAME_SECTORS, d0
	move.l	d0, stream_total
	move.l	d0, stream_remaining
	bset	#2, (MEMMODE+1).l		/* 1M mode */
	bsr	init_pcm
	bsr	issue_rom_readn
	bsr	fill_one_frame			/* frame 0 (also writes its audio chunk) */
	bsr	pcm_on
	bchg	#0, (MEMMODE+1).l		/* swap -> Main sees frame 0 */
	bsr	swap_settle
	move.w	#STAT_DONE, (COMSTAT0).l
1:
	tst.w	(COMCMD0).l
	bne	1b
	move.w	#0, (COMSTAT0).l
stream_loop:
	bsr	fill_one_frame
2:
	cmp.w	#CMD_SWAP_1M, (COMCMD0).l
	bne	2b
	bchg	#0, (MEMMODE+1).l
	bsr	swap_settle
	move.w	#STAT_DONE, (COMSTAT0).l
3:
	tst.w	(COMCMD0).l
	bne	3b
	move.w	#0, (COMSTAT0).l
	bra	stream_loop

/* One continuous ROM_READN over the whole movie (after the 1-sector header). */
issue_rom_readn:
	movem.l	d0-d7/a0-a6, -(sp)
	lea	bios_packet, a5
	move.l	probe_base_lba, d0
	add.l	#HEADER_SECTORS, d0
	move.l	d0, (a5)
	move.l	stream_total, 4(a5)
	move.l	#SUB_BANK_1M, 8(a5)
	movea.l	a5, a0
	BIOSCALL BIOS_CDC_STOP
	BIOSCALL BIOS_ROM_READN
	move.l	stream_total, stream_remaining
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* Drain one FRAME unit (FRAME_SECTORS sectors) into the Sub bank. When the whole
   stream has been consumed, stop the CDC and idle (no loop). */
fill_one_frame:
	movem.l	d0-d7/a0-a6, -(sp)
	tst.l	stream_remaining
	bne	1f
	lea	bios_packet, a5
	BIOSCALL BIOS_CDC_STOP			/* movie ended: stop reading, do not loop */
	movem.l	(sp)+, d0-d7/a0-a6
	rts
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
	lea	(SUB_BANK_1M+AUDIO_OFF).l, a0	/* this frame's audio chunk */
	bsr	write_wave_chunk
	movem.l	(sp)+, d0-d7/a0-a6
	rts

swap_settle:
	movem.l	d0, -(sp)
	move.w	#0x0400, d0
1:
	dbra	d0, 1b
	movem.l	(sp)+, d0
	rts

/* RF5C164 setup: enable chip, fill the wave ring [0..WAVE_RING_END) with silence
   plus a 0xFF loop marker, program channel 0 (13.3kHz, full vol/pan, loop to 0). */
init_pcm:
	movem.l	d0-d2/a0, -(sp)
	/* fill the wave ring [0..WAVE_RING_END) samples with silence.
	   wave RAM = 8-bit chip on ODD bytes: bank N at CTRL=0x80|N, samples at
	   0xFF2001 + (off&0xFFF)*2. */
	moveq	#0, d2
ip_loop:
	move.w	d2, d1
	andi.w	#0x0FFF, d1			/* within-bank sample offset */
	bne	1f
	move.w	d2, d0				/* bank boundary: CTRL = 0x80 | (off>>12) */
	lsr.w	#8, d0
	lsr.w	#4, d0
	ori.b	#0x80, d0
	move.b	d0, (PCM_CTRL).l
1:
	lea	(PCM_WAVE).l, a0
	add.w	d1, d1				/* *2 -> odd-byte address */
	adda.w	d1, a0
	move.b	#0x00, (a0)
	addq.w	#1, d2
	cmp.w	#WAVE_RING_END, d2
	blo	ip_loop
	/* loop / end marker at sample WAVE_RING_END (bank 8, offset 0) */
	move.b	#0x88, (PCM_CTRL).l		/* 0x80 | 8 */
	move.b	#0xFF, (PCM_WAVE).l
	/* configure channel 1: CTRL = CHANNEL(1) = 0xC0, regs at odd bytes + NOP wait */
	move.b	#0xC0, (PCM_CTRL).l
	move.b	#0xFF, (PCM_ENV).l
	nop
	nop
	move.b	#0xFF, (PCM_PAN).l
	nop
	nop
	move.b	#0x45, (PCM_FDL).l		/* FD = 0x0345 -> ~13.3kHz */
	nop
	nop
	move.b	#0x03, (PCM_FDH).l
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
	move.w	#0, write_ptr
	movem.l	(sp)+, d0-d2/a0
	rts

pcm_on:
	move.b	#0xFE, (PCM_ONOFF).l		/* channel 1 on (bit0=0) */
	rts

/* Copy AUDIO_BYTES samples from (a0) into the wave ring at write_ptr (advancing/
   wrapping, switching the 4KB wave bank). Wave RAM = odd bytes, step 2;
   bank select CTRL=0x80|bank. Leaves CTRL in channel-reg mode (0xC0). */
write_wave_chunk:
	movem.l	d0-d5/a0-a1, -(sp)
	/* --- closed-loop sync: keep write_ptr a safe lead ahead of the play head --- */
	moveq	#0, d5
	move.b	(PCM_PLAY_H).l, d5		/* play position high byte (0..0x7F) */
	lsl.w	#8, d5				/* play_pos (sample, 256-granular) */
	move.w	write_ptr, d2
	move.w	d2, d0
	sub.w	d5, d0
	andi.w	#RING_MASK, d0			/* lead = (write_ptr - play_pos) mod ring */
	cmp.w	#SYNC_MIN, d0
	blo	1f				/* chip caught up -> re-anchor */
	cmp.w	#SYNC_MAX, d0
	bls	2f				/* in band -> free-run (smooth) */
1:
	move.w	d5, d2				/* re-anchor: write_ptr = play_pos + LEAD */
	add.w	#SYNC_LEAD, d2
	andi.w	#RING_MASK, d2
2:
	move.w	#AUDIO_BYTES-1, d3
	move.w	d2, d0
	lsr.w	#8, d0
	lsr.w	#4, d0
	ori.b	#0x80, d0
	move.b	d0, (PCM_CTRL).l
wwc_loop:
	move.w	d2, d4
	andi.w	#0x0FFF, d4
	bne	1f
	move.w	d2, d0
	lsr.w	#8, d0
	lsr.w	#4, d0
	ori.b	#0x80, d0
	move.b	d0, (PCM_CTRL).l
1:
	lea	(PCM_WAVE).l, a1
	add.w	d4, d4				/* *2 -> odd-byte address */
	adda.w	d4, a1
	move.b	(a0)+, (a1)
	addq.w	#1, d2
	cmp.w	#WAVE_RING_END, d2
	blo	2f
	moveq	#0, d2
	move.b	#0x80, (PCM_CTRL).l		/* bank 0 after wrap */
2:
	dbra	d3, wwc_loop
	move.w	d2, write_ptr
	move.b	#0xC0, (PCM_CTRL).l		/* back to channel-reg mode */
	movem.l	(sp)+, d0-d5/a0-a1
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
	.align	2
cdda_track:
	.word	2			/* TOC track 2 = first CD-DA audio track */

file_m_init:
	.asciz	"M_INIT.PRG"
	.align	2
file_probe:
	.asciz	"PROBE.BIN"
	.align	2

bios_packet:
	.long	0, 0, 0, 0, 0
probe_base_lba:
	.long	0
stream_total:
	.long	0
stream_remaining:
	.long	0
bank_dest:
	.long	0
bank_count:
	.word	0
write_ptr:
	.word	0

sp_end:
