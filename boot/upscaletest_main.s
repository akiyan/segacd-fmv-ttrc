/*
 * upscaletest - Main (M_INIT.PRG) verification build.
 *
 * Goal: while streaming the movie's PROBE.BIN (continuous 1M/1M stream, same as
 * the player), take each 160x80 frame (20x10 tiles), CPU-upscale it 2x in both
 * axes into a 320x160 tile image (40x20 tiles = 25600 bytes) and DMA that into
 * VRAM in 4 slices (6400 bytes each) across 4 VBlanks. Verify that each slice's
 * VRAM DMA actually finishes inside its VBlank, i.e. whether 320x160 can be
 * pushed in 4 VBlanks.
 *
 * Verdict signal (backdrop / palette-0 colour 0, CRAM[0]):
 *   BLUE   = reached just before stream_start (boot stage)
 *   YELLOW = stream_start returned (boot stage)
 *   GREEN  = in playback loop, no DMA slice has ever overrun its VBlank
 *            (4-VBlank transfer of 320x160 IS feasible)
 *   RED    = at least one slice finished after VBlank ended (latched)
 *
 * Result (GPGX): GREEN - each 6400-byte slice fits inside one VBlank, so 4
 * slices push 320x160 in 4 VBlanks. NOTE: the heavy CPU 2x-upscale plus the
 * serial 4-VBlank DMA cannot sustain the 15fps continuous CD stream (the DMA
 * alone uses the whole 4-VBlank/frame budget); the stream stalls after a few
 * frames. Sustaining 15fps would need the upscale pipelined into the DMA's
 * active-display gaps. DMA_REPEAT runs the DMA check many times per frame so
 * the verdict is robust even when only a few frames stream.
 *
 * 2x tile expansion: each source 8x8 tile becomes a 16x16 block = 4 dest tiles
 * (TL/TR/BL/BR). Each source pixel is doubled H and V. H-doubling uses a
 * 256-entry byte->word LUT (0xAB -> 0xAABB); V-doubling writes each expanded
 * row twice.
 */

.equ STACK, 0x00FFFD00
.equ VDP_DATA, 0x00C00000
.equ VDP_CTRL, 0x00C00004

.equ EXVEC_LEVEL6, 0x00FFFD08
.equ BIOS_VBLANK_HANDLER_FLAGS, 0x00FFFE26
.equ BIOS_VBLANK_HANDLER, 0x00000290
.equ BIOS_CLEAR_VRAM, 0x000002A0
.equ BIOS_LOAD_DEFAULT_VDP_REGS, 0x000002AC
.equ BIOS_VDP_DISP_ENABLE, 0x000002D8
.equ BIOS_CLEAR_COMM, 0x00000340

.equ GA_COMCMD0, 0x00A12010
.equ GA_COMCMD1, 0x00A12012
.equ GA_COMSTAT0, 0x00A12020

.equ CMD_SWAP_1M, 7
.equ CMD_STREAM_START_1M, 8
.equ STAT_DONE, 0x8003

.equ HDR_SRC, 0x00210000
.equ HDR, 0x00FF7000
.equ HDR_MAGIC, 0x4D504734		/* 'MPG4' */
.equ HDR_FRAMES_OFF, 6
.equ HDR_PAL_OFF, 32

.equ PROBE_BANK, 0x00200000		/* current streamed frame: tile + pmap */
.equ SRC_TILE_BYTES, 6400		/* 200 src tiles * 32 */

.equ STAGE, 0x00FF0000			/* dest 320x160 tile image (25600 bytes) */
.equ LUT, 0x00FF6800			/* 256-entry byte->word doubling LUT */

.equ SW_TILES, 20			/* source 160x80 = 20x10 tiles */
.equ SH_TILES, 10
.equ SRC_TILES, 200

.equ DW_TILES, 40			/* dest 320x160 = 40x20 tiles */
.equ DH_TILES, 20
.equ DST_TILES, 800
.equ DST_TILE_BYTES, DST_TILES*32	/* 25600 */

.equ DMA_SLICES, 4
.equ DMA_SLICE_WORDS, (DST_TILE_BYTES/2)/DMA_SLICES	/* 3200 */
.equ DMA_SLICE_BYTES, DMA_SLICE_WORDS*2			/* 6400 */
.equ DMA_REPEAT, 8			/* DMA the 320x160 image this many times/frame */

.equ PLANE_W, 64
.equ PLANE_X, 0
.equ PLANE_Y, 4				/* centre 20 rows inside 28 */
.equ MAP_A, 0xC000
.equ TILE_VRAM, 0x0020			/* dest tile data starts at tile 1 */
.equ TILE_BASE, 1
.equ PLANEA_REG, 0x8230			/* name table A = 0xC000 */

.equ COL_OK,  0x00E0			/* green */
.equ COL_BAD, 0x000E			/* red   */

.text

.global handoff_entry
handoff_entry:
	move.w	#0x2700, sr
	lea	STACK, sp

	jsr	BIOS_LOAD_DEFAULT_VDP_REGS
	jsr	BIOS_CLEAR_VRAM
	jsr	BIOS_CLEAR_COMM
	move.b	#0x00, (BIOS_VBLANK_HANDLER_FLAGS).l
	move.l	#BIOS_VBLANK_HANDLER, (EXVEC_LEVEL6).l
	jsr	BIOS_VDP_DISP_ENABLE
	bsr	enable_vdp_dma

	bsr	build_lut
	bsr	copy_header
	bsr	validate_header
	bsr	load_palettes
	bsr	clear_name_table
	move.w	#PLANEA_REG, (VDP_CTRL).l
	move.w	#0x9001, (VDP_CTRL).l		/* plane size 64x32 */
	move.w	#0x0E00, d0			/* stage: BLUE = before stream_start */
	bsr	put_cram0

	bsr	stream_start

	move.w	#0x00EE, d0			/* stage: YELLOW = stream started */
	bsr	put_cram0

	move.w	#0x2000, sr
	moveq	#0, d7				/* frame counter */
playback_loop:
	bsr	upscale_frame			/* bank 160x80 -> STAGE 320x160 */
	moveq	#DMA_REPEAT-1, d6		/* exercise the 4-VBlank DMA repeatedly */
1:
	bsr	dma_tiles			/* 4 slices over 4 VBlanks (overrun check) */
	dbra	d6, 1b
	bsr	build_name_table		/* per-tile palette from pmap */
	bsr	set_ok				/* verdict: GREEN unless an overrun latched */
	bsr	request_swap
	addq.w	#1, d7
	cmp.w	(HDR+HDR_FRAMES_OFF).l, d7
	bne	playback_loop
	moveq	#0, d7
	bra	playback_loop

/* --- setup --- */
build_lut:
	lea	(LUT).l, a0
	moveq	#0, d0				/* i = 0..255 */
1:
	move.w	d0, d1
	andi.w	#0x00F0, d1
	lsl.w	#4, d1				/* hi<<8 */
	move.w	d0, d2
	andi.w	#0x00F0, d2			/* hi<<4 */
	or.w	d2, d1
	lsl.w	#4, d1				/* (hi<<12)|(hi<<8) */
	move.w	d0, d2
	andi.w	#0x000F, d2
	move.w	d2, d3
	lsl.w	#4, d3
	or.w	d3, d2				/* (lo<<4)|lo */
	or.w	d2, d1
	move.w	d1, (a0)+
	addq.w	#1, d0
	cmp.w	#256, d0
	bne	1b
	rts

copy_header:
	lea	(HDR_SRC).l, a0
	lea	(HDR).l, a1
	move.w	#(256/4)-1, d0
1:
	move.l	(a0)+, (a1)+
	dbra	d0, 1b
	rts

validate_header:
	move.l	(HDR).l, d0
	cmp.l	#HDR_MAGIC, d0
	beq	1f
2:
	bra	2b
1:
	rts

load_palettes:
	move.l	#0xC0000000, (VDP_CTRL).l
	lea	(HDR+HDR_PAL_OFF).l, a0
	move.w	#(4*16)-1, d0
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d0, 1b
	rts

clear_name_table:
	move.l	#0x40000000|((MAP_A&0x3FFF)<<16)|((MAP_A>>14)&3), (VDP_CTRL).l
	move.w	#(0x1000/2)-1, d0		/* clear 64x32 name table */
1:
	move.w	#0, (VDP_DATA).l
	dbra	d0, 1b
	rts

stream_start:
	move.w	(HDR+HDR_FRAMES_OFF).l, d0
	move.w	d0, (GA_COMCMD1).l
	move.w	#CMD_STREAM_START_1M, (GA_COMCMD0).l
1:
	cmp.w	#STAT_DONE, (GA_COMSTAT0).l
	bne	1b
	move.w	#0, (GA_COMCMD0).l
2:
	tst.w	(GA_COMSTAT0).l
	bne	2b
	rts

/* request the next frame; tolerate a stalled stream (timeout) so the DMA
   overrun verification keeps running on whatever frame is current. */
request_swap:
	move.l	d1, -(sp)
	move.w	#CMD_SWAP_1M, (GA_COMCMD0).l
	move.l	#0x00200000, d1
1:
	cmp.w	#STAT_DONE, (GA_COMSTAT0).l
	beq	3f
	subq.l	#1, d1
	bne	1b
3:
	move.w	#0, (GA_COMCMD0).l
	move.l	#0x00200000, d1
2:
	tst.w	(GA_COMSTAT0).l
	beq	4f
	subq.l	#1, d1
	bne	2b
4:
	move.l	(sp)+, d1
	rts

/* --- CPU 2x upscale: PROBE_BANK 20x10 tiles -> STAGE 40x20 tiles --- */
upscale_frame:
	movem.l	d0-d7/a0-a6, -(sp)
	lea	(PROBE_BANK).l, a2		/* source tile data */
	lea	(LUT).l, a6
	moveq	#0, d7				/* k = source tile 0..199 */
uf_tile:
	/* sr = k / SW_TILES, sc = k % SW_TILES */
	move.w	d7, d0
	andi.l	#0xFFFF, d0
	divu.w	#SW_TILES, d0
	move.w	d0, d1				/* d1 = sr (quotient) */
	swap	d0
	move.w	d0, d2				/* d2 = sc (remainder) */
	/* dest TL tile index = (2*sr)*DW_TILES + (2*sc) */
	add.w	d1, d1				/* 2*sr */
	mulu.w	#DW_TILES, d1			/* (2*sr)*40 */
	add.w	d2, d2				/* 2*sc */
	add.w	d2, d1				/* TL tile index */
	moveq	#0, d3
	move.w	d1, d3
	lsl.l	#5, d3				/* *32 -> byte offset */
	lea	(STAGE).l, a3
	adda.l	d3, a3				/* a3 = dTL */
	lea	32(a3), a4			/* a4 = dTR */
	lea	(DW_TILES*32)(a3), a5		/* a5 = dBL */
	/* dBR = a5 + 32 computed inline */

	/* top half: source rows 0..3 -> dTL/dTR rows (0,1)(2,3)(4,5)(6,7) */
	movea.l	a3, a0				/* dest left  (dTL) */
	movea.l	a4, a1				/* dest right (dTR) */
	moveq	#4-1, d4			/* 4 source rows */
uf_top:
	bsr	uf_row
	dbra	d4, uf_top

	/* bottom half: source rows 4..7 -> dBL/dBR */
	movea.l	a5, a0				/* dBL */
	lea	32(a5), a1			/* dBR */
	moveq	#4-1, d4
uf_bot:
	bsr	uf_row
	dbra	d4, uf_bot

	adda.l	#32, a2				/* next source tile */
	addq.w	#1, d7
	cmp.w	#SRC_TILES, d7
	bne	uf_tile
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* Expand one source row (a2 advances 4 bytes) into left tile (a0) and right
   tile (a1), writing the doubled row TWICE (a0/a1 advance 8 bytes each).
   Uses LUT base a6. Clobbers d0-d3,d5,d6. */
uf_row:
	moveq	#0, d5
	move.b	(a2)+, d5			/* b0 (cols 0,1) */
	add.w	d5, d5
	move.w	(a6,d5.w), d0			/* left word0 */
	moveq	#0, d5
	move.b	(a2)+, d5			/* b1 (cols 2,3) */
	add.w	d5, d5
	move.w	(a6,d5.w), d1			/* left word1 */
	moveq	#0, d5
	move.b	(a2)+, d5			/* b2 (cols 4,5) */
	add.w	d5, d5
	move.w	(a6,d5.w), d2			/* right word0 */
	moveq	#0, d5
	move.b	(a2)+, d5			/* b3 (cols 6,7) */
	add.w	d5, d5
	move.w	(a6,d5.w), d3			/* right word1 */
	/* write left row twice (V-double) */
	move.w	d0, (a0)+
	move.w	d1, (a0)+
	move.w	d0, (a0)+
	move.w	d1, (a0)+
	/* write right row twice */
	move.w	d2, (a1)+
	move.w	d3, (a1)+
	move.w	d2, (a1)+
	move.w	d3, (a1)+
	rts

/* --- DMA the 25600-byte dest image into VRAM in DMA_SLICES slices, one per
   VBlank, checking each slice finishes before its VBlank ends. --- */
dma_tiles:
	movem.l	d0-d5/a0, -(sp)
	lea	(STAGE).l, a0
	move.w	#TILE_VRAM, d4			/* VRAM dest */
	moveq	#DMA_SLICES-1, d5
1:
	bsr	wait_vblank_start
	move.w	d4, d0
	move.w	#DMA_SLICE_WORDS, d1
	bsr	vram_dma_copy_now		/* issues DMA + waits done */
	/* overrun check: still in VBlank? */
	move.w	(VDP_CTRL).l, d2
	btst	#3, d2				/* VBlank flag */
	bne	2f
	bsr	set_bad				/* DMA spilled past VBlank */
2:
	adda.l	#DMA_SLICE_BYTES, a0
	addi.w	#DMA_SLICE_BYTES, d4
	bsr	wait_vblank_end
	dbra	d5, 1b
	movem.l	(sp)+, d0-d5/a0
	rts

/* Build 40x20 name table; each dest cell takes its source tile's pmap line. */
build_name_table:
	movem.l	d0-d7/a0-a1, -(sp)
	lea	(PROBE_BANK+SRC_TILE_BYTES).l, a1	/* pmap (200 bytes) */
	moveq	#0, d5				/* dest row dr = 0..19 */
bnt_row:
	/* VRAM write addr for (PLANE_Y+dr, PLANE_X) */
	move.w	d5, d0
	add.w	#PLANE_Y, d0
	mulu.w	#PLANE_W, d0
	add.w	#PLANE_X, d0
	lsl.l	#1, d0
	add.l	#MAP_A, d0
	bsr	vram_write_cmd
	/* source row = dr/2, pmap base = (dr/2)*SW_TILES */
	move.w	d5, d6
	lsr.w	#1, d6
	mulu.w	#SW_TILES, d6			/* pmap row base */
	/* tile index base for this dest row = TILE_BASE + dr*DW_TILES */
	move.w	d5, d4
	mulu.w	#DW_TILES, d4
	add.w	#TILE_BASE, d4
	moveq	#0, d3				/* dest col dc = 0..39 */
bnt_col:
	move.w	d3, d2
	lsr.w	#1, d2				/* sc = dc/2 */
	add.w	d6, d2				/* pmap index */
	moveq	#0, d0
	move.b	(a1,d2.w), d0			/* pmap value 0-3 */
	lsl.w	#8, d0
	lsl.w	#5, d0				/* <<13 palette bits */
	or.w	d4, d0				/* | tile index */
	move.w	d0, (VDP_DATA).l
	addq.w	#1, d4
	addq.w	#1, d3
	cmp.w	#DW_TILES, d3
	bne	bnt_col
	addq.w	#1, d5
	cmp.w	#DH_TILES, d5
	bne	bnt_row
	movem.l	(sp)+, d0-d7/a0-a1
	rts

/* --- verdict (backdrop = CRAM[0]) --- */
set_ok:
	tst.w	overran
	bne	set_bad_now			/* once bad, stay bad */
	move.w	#COL_OK, d0
	bra	put_cram0
set_bad:
	move.w	#1, overran
set_bad_now:
	move.w	#COL_BAD, d0
put_cram0:
	move.l	#0xC0000000, (VDP_CTRL).l
	move.w	d0, (VDP_DATA).l
	rts

/* --- VDP helpers --- */
enable_vdp_dma:
	move.w	#0x8174, (VDP_CTRL).l
	move.w	#0x8F02, (VDP_CTRL).l
	rts

vram_dma_copy_now:
	move.w	#0x8F02, (VDP_CTRL).l
	move.w	d1, d2
	move.w	#0x9300, d3
	or.b	d2, d3
	move.w	d3, (VDP_CTRL).l
	lsr.w	#8, d2
	move.w	#0x9400, d3
	or.b	d2, d3
	move.w	d3, (VDP_CTRL).l
	move.l	a0, d2
	lsr.l	#1, d2
	move.w	#0x9500, d3
	or.b	d2, d3
	move.w	d3, (VDP_CTRL).l
	lsr.l	#8, d2
	move.w	#0x9600, d3
	or.b	d2, d3
	move.w	d3, (VDP_CTRL).l
	lsr.l	#8, d2
	move.w	#0x9700, d3
	or.b	d2, d3
	move.w	d3, (VDP_CTRL).l
	bsr	vram_dma_write_cmd
	bsr	wait_dma_done
	rts

vram_dma_write_cmd:
	and.l	#0x0000FFFF, d0
	move.l	d0, d2
	andi.w	#0x3FFF, d2
	ori.w	#0x4000, d2
	move.w	d2, (VDP_CTRL).l
	move.l	d0, d2
	lsr.l	#8, d2
	lsr.l	#6, d2
	andi.w	#0x0003, d2
	ori.w	#0x0080, d2
	move.w	d2, (VDP_CTRL).l
	rts

wait_dma_done:
1:
	move.w	(VDP_CTRL).l, d3
	btst	#1, d3
	bne	1b
	rts

vram_write_cmd:
	and.l	#0x0000FFFF, d0
	lsl.l	#2, d0
	lsr.w	#2, d0
	swap	d0
	or.l	#0x40000000, d0
	move.l	d0, (VDP_CTRL).l
	rts

wait_vblank_start:
1:
	move.w	(VDP_CTRL).l, d3
	btst	#3, d3
	beq	1b
	rts

wait_vblank_end:
1:
	move.w	(VDP_CTRL).l, d3
	btst	#3, d3
	bne	1b
	rts

	.data
	.align 2
overran:
	.word 0
