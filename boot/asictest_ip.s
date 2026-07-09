/*
 * asictest - Main (IP) side: display the ASIC-upscaled 320x160 image.
 *
 * Ping-pong with the Sub over 2M Word RAM: give Word RAM to the Sub (it runs
 * the ASIC), wait to get it back, then read palettes/pmap from the meta area,
 * build the (column-major) name table, and DMA the image buffer to VRAM.
 *
 * Word RAM (Main 2M view base 0x200000):
 *   0x203000 pal (128B)   0x203080 pmap (200B)   0x204000 image buffer (25600B)
 *
 * The image buffer is column-major 8x8 tiles, so screen cell (col,row) ->
 * tile (TILE_BASE + col*VH + row).
 */

.equ STACK, 0x00FFFD00
.equ VDP_DATA, 0x00C00000
.equ VDP_CTRL, 0x00C00004

.equ BIOS_CLEAR_VRAM, 0x000002A0
.equ BIOS_LOAD_DEFAULT_VDP_REGS, 0x000002AC
.equ BIOS_VDP_DISP_ENABLE, 0x000002D8
.equ BIOS_CLEAR_COMM, 0x00000340

.equ GA_MEMMODE, 0x00A12002		/* low byte 0xA12003: RET=0 DMNA=1 */

.equ WR_PAL,  0x00203000
.equ WR_PMAP, 0x00203080
.equ WR_IMG,  0x00204000

.equ VW, 40				/* output cells across (320/8) */
.equ VH, 20				/* output cells down  (160/8) */
.equ OUT_TILES, VW*VH			/* 800 */
.equ OUT_BYTES, OUT_TILES*32		/* 25600 */
.equ DMA_SLICES, 4
.equ DMA_SLICE_WORDS, (OUT_BYTES/2)/DMA_SLICES	/* 3200 */
.equ DMA_SLICE_BYTES, DMA_SLICE_WORDS*2		/* 6400 */

.equ PLANE_W, 64
.equ PLANE_X, 0
.equ PLANE_Y, 4
.equ MAP_A, 0xC000
.equ TILE_VRAM, 0x0020			/* tile 1 */
.equ TILE_BASE, 1
.equ PLANEA_REG, 0x8230

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
	jsr	BIOS_VDP_DISP_ENABLE
	bsr	enable_vdp_dma

	bsr	clear_name_table
	move.w	#PLANEA_REG, (VDP_CTRL).l
	move.w	#0x9001, (VDP_CTRL).l		/* plane size 64x32 */

	bsr	grant_sub			/* give Word RAM to Sub to render */
disp_loop:
	bsr	wait_main_count			/* d7 = VBlanks the Sub's ASIC took */
	bsr	load_pal
	bsr	build_nt
	bsr	dma_image			/* +DMA_SLICES VBlanks */
	addq.w	#DMA_SLICES, d7			/* total VBlanks this frame cycle */
	bsr	verdict
	bsr	grant_sub
	bra	disp_loop

/* backdrop verdict from total VBlanks/cycle: <=4 green(15fps ok), 5-6 yellow, >6 red */
verdict:
	move.w	#0x00E0, d0			/* green */
	cmp.w	#4, d7
	bls	1f
	move.w	#0x00EE, d0			/* yellow */
	cmp.w	#6, d7
	bls	1f
	move.w	#0x000E, d0			/* red */
1:
	move.l	#0xC0000000, (VDP_CTRL).l
	move.w	d0, (VDP_DATA).l
	rts

/* wait until Main owns Word RAM (RET=1), counting VBlank rising edges into d7 */
wait_main_count:
	moveq	#0, d7
1:	btst	#0, (GA_MEMMODE+1).l
	bne	3f
2:	move.w	(VDP_CTRL).l, d0		/* wait VBlank end */
	btst	#3, d0
	bne	2b
	btst	#0, (GA_MEMMODE+1).l
	bne	3f
4:	move.w	(VDP_CTRL).l, d0		/* wait VBlank start (edge) */
	btst	#3, d0
	beq	4b
	addq.w	#1, d7
	bra	1b
3:	rts

/* --- 2M handoff (Main side), low byte 0xA12003 --- */
wait_main:					/* wait RET=1 (Main owns) */
1:	btst	#0, (GA_MEMMODE+1).l
	beq	1b
	rts

grant_sub:					/* set DMNA=1 (hand to Sub) */
1:	bset	#1, (GA_MEMMODE+1).l
	btst	#1, (GA_MEMMODE+1).l
	beq	1b
	rts

/* --- setup / per frame --- */
load_pal:
	move.l	#0xC0000000, (VDP_CTRL).l
	lea	(WR_PAL).l, a0
	move.w	#(4*16)-1, d0
1:	move.w	(a0)+, (VDP_DATA).l
	dbra	d0, 1b
	rts

clear_name_table:
	move.l	#0x40000000|((MAP_A&0x3FFF)<<16)|((MAP_A>>14)&3), (VDP_CTRL).l
	move.w	#(0x1000/2)-1, d0
1:	move.w	#0, (VDP_DATA).l
	dbra	d0, 1b
	rts

/* Column-major name table: cell(col,row) -> tile (TILE_BASE+col*VH+row),
   palette = pmap[(row/2)*20 + (col/2)]. */
build_nt:
	movem.l	d0-d6/a1, -(sp)
	lea	(WR_PMAP).l, a1
	moveq	#0, d5				/* row 0..VH-1 */
bnt_row:
	move.w	d5, d0
	add.w	#PLANE_Y, d0
	mulu.w	#PLANE_W, d0
	add.w	#PLANE_X, d0
	lsl.l	#1, d0
	add.l	#MAP_A, d0
	bsr	vram_write_cmd
	move.w	d5, d6				/* pmap row base = (row/2)*20 */
	lsr.w	#1, d6
	mulu.w	#20, d6
	moveq	#0, d3				/* col 0..VW-1 */
bnt_col:
	/* palette = pmap[d6 + col/2] */
	move.w	d3, d2
	lsr.w	#1, d2
	add.w	d6, d2
	moveq	#0, d0
	move.b	(a1,d2.w), d0
	lsl.w	#8, d0
	lsl.w	#5, d0				/* <<13 */
	/* tile index = TILE_BASE + col*VH + row */
	move.w	d3, d1
	mulu.w	#VH, d1
	add.w	d5, d1
	add.w	#TILE_BASE, d1
	or.w	d1, d0
	move.w	d0, (VDP_DATA).l
	addq.w	#1, d3
	cmp.w	#VW, d3
	bne	bnt_col
	addq.w	#1, d5
	cmp.w	#VH, d5
	bne	bnt_row
	movem.l	(sp)+, d0-d6/a1
	rts

dma_image:
	movem.l	d0-d5/a0, -(sp)
	lea	(WR_IMG).l, a0
	move.w	#TILE_VRAM, d4
	moveq	#DMA_SLICES-1, d5
1:	bsr	wait_vblank_start
	move.w	d4, d0
	move.w	#DMA_SLICE_WORDS, d1
	bsr	vram_dma_copy_now
	adda.l	#DMA_SLICE_BYTES, a0
	addi.w	#DMA_SLICE_BYTES, d4
	bsr	wait_vblank_end
	dbra	d5, 1b
	movem.l	(sp)+, d0-d5/a0
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
1:	move.w	(VDP_CTRL).l, d3
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
1:	move.w	(VDP_CTRL).l, d3
	btst	#3, d3
	beq	1b
	rts

wait_vblank_end:
1:	move.w	(VDP_CTRL).l, d3
	btst	#3, d3
	bne	1b
	rts
