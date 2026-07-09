/*
 * asictest - Sub (SP) side: 2x upscale 160x80 -> 320x160 using the Sega CD
 * Graphics ASIC (stamp / trace-table scaler), in 2M Word RAM mode.
 *
 * Embedded ASIC.DAT (incbin) layout:
 *   0x0000 pmap   (200 B, source 20x10 tile palette lines)
 *   0x0100 pal    (128 B, 4 CRAM lines)
 *   0x0180 stamp_data (6528 B: stamp 0 blank + 50 16x16 stamps)
 *   0x1B00 stamp_map  (512 B: 16x16 entries, 10x5 region = stamps 1..50)
 *
 * Word RAM (2M) layout (Sub view base 0x080000):
 *   0x00000 stamp data
 *   0x02000 stamp map     (STAMPMAPBASE = 0x2000/4 = 0x800)
 *   0x02400 trace table   (TRACEVECTBASE = 0x2400/4 = 0x900, 160 entries)
 *   0x03000 meta: pal(128)@0x3000, pmap(200)@0x3080  (for Main)
 *   0x04000 image buffer  (IMGBUFSTART = 0x4000/4 = 0x1000, 320x160 = 25600 B)
 *
 * Per frame: set ASIC regs, write TRACEVECTBASE (starts), time the GRON-busy
 * loop, hand Word RAM to Main; Main DMAs the image buffer and hands it back.
 */

.equ SUB_GA_BASE, 0x00FF8000
.equ MEMMODE,   SUB_GA_BASE+0x0002	/* low byte 0xFF8003: RET=0 DMNA=1 MODE=2 */
.equ COMSTAT0,  SUB_GA_BASE+0x0020

.equ GA_STAMPSIZE,   0x00FF8058
.equ GA_STAMPMAPBASE,0x00FF805A
.equ GA_IMGBUFVSIZE, 0x00FF805C
.equ GA_IMGBUFSTART, 0x00FF805E
.equ GA_IMGBUFOFFSET,0x00FF8060
.equ GA_IMGBUFHDOT,  0x00FF8062
.equ GA_IMGBUFVDOT,  0x00FF8064
.equ GA_TRACEVECT,   0x00FF8066

.equ WR, 0x00080000			/* Sub 2M Word RAM base */
.equ WR_STAMP,  WR+0x00000
.equ WR_MAP,    WR+0x02000
.equ WR_TRACE,  WR+0x02400
.equ WR_META,   WR+0x03000
.equ WR_IMG,    WR+0x04000

.equ OUT_LINES, 160

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
	andi.b	#0xFB, (MEMMODE+1).l		/* clear MODE (bit2) -> 2M mode */
	move.w	#0, (COMSTAT0).l
	rts

.global sp_main
sp_main:
	bsr	wait_2m				/* Sub owns Word RAM */
	bsr	copy_data			/* stamps/map/meta/trace into Word RAM */
asic_loop:
	bsr	setup_asic
	/* start + time the graphics operation */
	move.w	#(WR_TRACE-WR)/4, (GA_TRACEVECT).l	/* write starts ASIC */
	moveq	#0, d2
1:
	addq.l	#1, d2
	move.w	(GA_STAMPSIZE).l, d0
	btst	#15, d0				/* GRON: operation in progress */
	bne	1b
	move.w	d2, (COMSTAT0).l		/* GRON-busy loop count -> Main */
	bsr	grant_main			/* hand Word RAM to Main */
	bsr	wait_2m				/* wait for Main to hand it back */
	bra	asic_loop

setup_asic:
	move.w	#0x0000, (GA_STAMPSIZE).l	/* 16x16 stamps, 256x256 map, no repeat */
	move.w	#(WR_MAP-WR)/4, (GA_STAMPMAPBASE).l
	move.w	#(160/8)-1, (GA_IMGBUFVSIZE).l	/* 19 */
	move.w	#(WR_IMG-WR)/4, (GA_IMGBUFSTART).l
	move.w	#0, (GA_IMGBUFOFFSET).l
	move.w	#320, (GA_IMGBUFHDOT).l
	move.w	#160, (GA_IMGBUFVDOT).l
	rts

/* Copy embedded data into Word RAM and build the 2x trace table. */
copy_data:
	movem.l	d0-d2/a0-a1, -(sp)
	/* stamp data 6528 B */
	lea	(asic_data+0x180).l, a0
	lea	(WR_STAMP).l, a1
	move.w	#(6528/2)-1, d0
1:	move.w	(a0)+, (a1)+
	dbra	d0, 1b
	/* stamp map 512 B */
	lea	(asic_data+0x1B00).l, a0
	lea	(WR_MAP).l, a1
	move.w	#(512/2)-1, d0
2:	move.w	(a0)+, (a1)+
	dbra	d0, 2b
	/* meta: pal 128 @ WR_META, pmap 200 @ WR_META+0x80 */
	lea	(asic_data+0x100).l, a0
	lea	(WR_META).l, a1
	move.w	#(128/2)-1, d0
3:	move.w	(a0)+, (a1)+
	dbra	d0, 3b
	lea	(asic_data+0x0).l, a0
	lea	(WR_META+0x80).l, a1
	move.w	#(200/2)-1, d0
4:	move.w	(a0)+, (a1)+
	dbra	d0, 4b
	/* trace table: 160 entries {Xstart=0, Ystart=L*4, Xdelta=0x400, Ydelta=0} */
	lea	(WR_TRACE).l, a1
	moveq	#0, d1				/* L */
5:	move.w	#0, (a1)+			/* Xstart */
	move.w	d1, d0
	lsl.w	#2, d0				/* L*4 (13.3: 0.5 src px/line) */
	move.w	d0, (a1)+			/* Ystart */
	move.w	#0x0400, (a1)+			/* Xdelta = 0.5 (5.11) */
	move.w	#0, (a1)+			/* Ydelta */
	addq.w	#1, d1
	cmp.w	#OUT_LINES, d1
	bne	5b
	movem.l	(sp)+, d0-d2/a0-a1
	rts

/* 2M handoff (Sub side), bits at low byte MEMMODE+1 (0xFF8003) */
wait_2m:					/* wait until Sub owns (DMNA=1) */
1:	btst	#1, (MEMMODE+1).l
	beq	1b
	rts

grant_main:					/* hand Word RAM to Main (RET=1) */
1:	bset	#0, (MEMMODE+1).l
	btst	#0, (MEMMODE+1).l
	beq	1b
	rts

.global sp_int2
sp_int2:
	rts
.global sp_user
sp_user:
	rts

	.data
	.align 2
asic_data:
	.incbin "out/asic/ASIC.DAT"

sp_end:
