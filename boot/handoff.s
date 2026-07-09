/*
 * OP movie player - Main CPU (M_INIT.PRG).
 *
 * Plays an uncompressed 160x96 / 15fps movie (H32 256-wide mode, 20x12 tiles)
 * quantised to 4 fixed global palettes with per-tile palette selection, fed by
 * the continuous-stream Sub (one ROM_READN, 1M/1M Word RAM double buffer).
 *
 * The source is letterboxed; the quantiser crops the widescreen content
 * (320x152 ~= 2:1) and scales it to 160x96. On a 4:3 TV an H32 dot is ~1.167x
 * wider than tall, so 20x12 cells display at ~1.95 (~2:1) = the content's true
 * aspect (160x112 squashed the 2:1 content to 10:7 and looked vertically tall).
 * tile+pmap+audio = 7680+240+800 = 8720 B <= 10240 (5 sectors/frame @15fps).
 *
 * Per frame: the streamed frame is in the Main 1M bank at PROBE_BANK as
 *   [ TILE_BYTES VDP tile data ][ TILES_PER_FRAME pmap bytes (palette line 0-3) ].
 * Main copies it to Main RAM, DMAs the tiles into the back VRAM buffer, builds
 * the back name table, switches the displayed buffer, asks the Sub to swap in
 * the next frame, and halts (no loop) once the last frame is shown.
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
.equ CMD_PLAY_CDDA, 9
.equ STAT_DONE, 0x8003

/* Header (copied to Main RAM): >4sHHHHHHHH + 4 palettes(128B) at off 32 */
.equ HDR_SRC, 0x00210000		/* 2M Word RAM view of the loaded header */
.equ HDR, 0x00FF4000			/* Main RAM copy */
.equ HDR_MAGIC, 0x4D504734		/* 'MPG4' */
.equ HDR_FRAMES_OFF, 6
.equ HDR_PAL_OFF, 32

.equ PROBE_BANK, 0x00200000		/* Main 1M bank: current streamed frame */
.equ DMA_TILE_STAGE, 0x00FF0000		/* Main RAM tile staging for VDP DMA */
.equ PMAP_BUF, 0x00FF3000		/* Main RAM copy of the per-tile palette map */
.equ PAL_BUF, 0x00FF3400		/* Main RAM copy of the per-frame palette (128B) */

.equ W_TILES, 20			/* 160px / 8 */
.equ H_TILES, 12			/* 96px / 8. cropped content 320x152(~2:1) -> 160x96 */
.equ TILES_PER_FRAME, 240
.equ TILE_BYTES, TILES_PER_FRAME*32	/* 7680 */
.equ TILE_WORDS, TILE_BYTES/2		/* 3840 */
.equ PMAP_LONGS, (TILES_PER_FRAME+3)/4	/* 60 (240 bytes) */
.equ FRAME_PMAP_OFF, TILE_BYTES		/* pmap starts after tile data in the bank */
.equ FRAME_PAL_OFF, TILE_BYTES+TILES_PER_FRAME	/* per-frame palette after pmap */
.equ PAL_WORDS, 4*16			/* 64 CRAM words = 128 bytes */

.equ DMA_SLICES, 2
.equ DMA_SLICE_WORDS, TILE_WORDS/DMA_SLICES	/* 1920 */
.equ DMA_SLICE_BYTES, DMA_SLICE_WORDS*2

.equ PLANE_W, 64
.equ PLANE_X, 6				/* centre 20x12 in H32 32x28 */
.equ PLANE_Y, 8
.equ MAP_A, 0xC000
.equ MAP_B, 0x8000
.equ TILE_A_VRAM, 0x0020			/* tile slot 1   (tiles 1..240)   */
.equ TILE_B_VRAM, 0x1E20			/* tile slot 241 (tiles 241..480) */
.equ TILE_BASE_A, 1
.equ TILE_BASE_B, 241
.equ PLANEA_REG_A, 0x8230		/* name table A = 0xC000 */
.equ PLANEA_REG_B, 0x8220		/* name table B = 0x8000 */

/* Plane B overlay: per frame 24 cells (CBR). High-priority tiles sit over the
 * low-priority movie on Plane A, each using one of the 4 CRAM palettes with
 * colour 0 transparent -> pseudo 2-palettes on the hardest cells.
 * Frame bank layout: [tile][pmap][pal][24*32 patterns][24*2 descriptors][audio]. */
.equ N_OVL, 24
.equ OVL_PAT_BYTES, N_OVL*32		/* 768 */
.equ OVL_DESC_BYTES, N_OVL*2		/* 48: per cell (cell index, palette); 0xFF=skip */
.equ FRAME_OVL_OFF, FRAME_PAL_OFF+PAL_WORDS*2	/* patterns after palette (8048) */
.equ FRAME_OVLDESC_OFF, FRAME_OVL_OFF+OVL_PAT_BYTES	/* descriptors after patterns (8816) */
.equ OVL_PAT_BUF, 0x00FF3480		/* Main RAM: 24 overlay patterns (768B) */
.equ OVL_DESC_BUF, 0x00FF3780		/* Main RAM: 24 descriptors (48B) */
.equ PBNAME_BUF, 0x00FF3800		/* Main RAM: Plane B 20x12 name-table image (480B) */
.equ PB_TILE_A_VRAM, 0x7000		/* overlay pattern bank A (24 tiles) */
.equ PB_TILE_B_VRAM, 0x7300		/* overlay pattern bank B (24 tiles) */
.equ PB_TILE_BASE_A, PB_TILE_A_VRAM/32	/* 896 */
.equ PB_TILE_BASE_B, PB_TILE_B_VRAM/32	/* 920 */
.equ PBMAP_A, 0x4000			/* Plane B name table A (paired with MAP_A) */
.equ PBMAP_B, 0x6000			/* Plane B name table B (paired with MAP_B) */
.equ PLANEB_REG_A, 0x8402		/* reg4: Plane B name table = 0x4000 */
.equ PLANEB_REG_B, 0x8403		/* reg4: Plane B name table = 0x6000 */

/* CD-DA screen (shown after the OP, or immediately with SKIP_OP) */
.equ OPFONT_VRAM, 0x0020		/* font tiles at VRAM tile index 1 */
.equ OPFONT_TILES, 11
.equ OPFONT_BYTES, OPFONT_TILES*32
.equ CDDA_ROW, 13			/* message position in the H32 plane */
.equ CDDA_COL, 9

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
	move.w	#0x8C00, (VDP_CTRL).l		/* reg12 = H32 (256px, no shadow/ilace) */

.ifdef SKIP_OP
	/* dev build: skip the OP movie, go straight to the CD-DA screen */
	bsr	cdda_screen
.else
	bsr	copy_header
	bsr	validate_header
	bsr	load_palettes
	bsr	clear_name_tables
	bsr	clear_planeb			/* clear Plane B name tables (0x4000..0x7000) */
	move.w	#PLANEA_REG_A, (VDP_CTRL).l
	move.w	#PLANEB_REG_A, (VDP_CTRL).l	/* reg4: show (empty) Plane B name table A */

	bsr	stream_start

	move.w	#0x2000, sr
	moveq	#0, d6				/* d6 = back buffer index (0/1) */
	moveq	#0, d7				/* d7 = frame counter           */
playback_loop:
	bsr	copy_frame_from_bank
	bsr	load_tiles_dma
	bsr	build_name_table
	bsr	build_planeb_name_table		/* Plane B overlay back name table */
	bsr	switch_buffer			/* show this frame */
	addq.w	#1, d7
	cmp.w	(HDR+HDR_FRAMES_OFF).l, d7
	beq	playback_done			/* displayed last frame -> transition */
	bsr	request_swap			/* fetch next frame */
	bra	playback_loop
playback_done:
	move.w	#0x2700, sr			/* movie done: mask interrupts */
.ifndef OP_ONLY
	bsr	cdda_screen			/* OP -> CD-DA screen (OP_ONLY halts instead) */
.endif
.endif
halt:
	stop	#0x2000				/* halt with the CD-DA screen shown */
	bra	halt

/* --- setup --- */
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

/* 4 palette lines (black + 15) -> CRAM lines 0-3 (64 words from HDR+32). */
load_palettes:
	move.l	#0xC0000000, (VDP_CTRL).l
	lea	(HDR+HDR_PAL_OFF).l, a0
	move.w	#(4*16)-1, d0
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d0, 1b
	rts

clear_name_tables:
	move.l	#0x40000000|((MAP_B&0x3FFF)<<16)|((MAP_B>>14)&3), (VDP_CTRL).l
	move.w	#(0x2000/2)-1, d0	/* clear 0x8000..0xFFFF (both name tables) */
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

/* --- per frame --- */
copy_frame_from_bank:
	lea	(PROBE_BANK).l, a0
	lea	(DMA_TILE_STAGE).l, a1
	move.w	#(TILE_BYTES/4)-1, d0
1:
	move.l	(a0)+, (a1)+
	dbra	d0, 1b
	lea	(PROBE_BANK+FRAME_PMAP_OFF).l, a0
	lea	(PMAP_BUF).l, a1
	move.w	#PMAP_LONGS-1, d0		/* 64 longs covers 255 pmap bytes */
2:
	move.l	(a0)+, (a1)+
	dbra	d0, 2b
	lea	(PROBE_BANK+FRAME_PAL_OFF).l, a0	/* per-frame palette -> Main RAM */
	lea	(PAL_BUF).l, a1
	move.w	#(PAL_WORDS*2/4)-1, d0		/* 128 bytes = 32 longs */
3:
	move.l	(a0)+, (a1)+
	dbra	d0, 3b
	lea	(PROBE_BANK+FRAME_OVL_OFF).l, a0	/* 24 overlay patterns -> Main RAM */
	lea	(OVL_PAT_BUF).l, a1
	move.w	#(OVL_PAT_BYTES/4)-1, d0		/* 768 bytes = 192 longs */
4:
	move.l	(a0)+, (a1)+
	dbra	d0, 4b
	lea	(PROBE_BANK+FRAME_OVLDESC_OFF).l, a0	/* 24 descriptors -> Main RAM */
	lea	(OVL_DESC_BUF).l, a1
	move.w	#(OVL_DESC_BYTES/4)-1, d0		/* 48 bytes = 12 longs */
5:
	move.l	(a0)+, (a1)+
	dbra	d0, 5b
	rts

request_swap:
	move.w	#CMD_SWAP_1M, (GA_COMCMD0).l
1:
	cmp.w	#STAT_DONE, (GA_COMSTAT0).l
	bne	1b
	move.w	#0, (GA_COMCMD0).l
2:
	tst.w	(GA_COMSTAT0).l
	bne	2b
	rts

/* DMA the staged tile data into the back VRAM tile buffer (split over VBlanks). */
load_tiles_dma:
	tst.w	d6
	beq	1f
	move.w	#TILE_B_VRAM, d0
	bra	2f
1:
	move.w	#TILE_A_VRAM, d0
2:
	lea	(DMA_TILE_STAGE).l, a0
	movem.l	d0-d5/a0, -(sp)
	moveq	#DMA_SLICES-1, d5
3:
	bsr	wait_vblank_start
	move.w	#DMA_SLICE_WORDS, d1
	bsr	vram_dma_copy_now
	tst.w	d5
	beq	4f
	adda.l	#DMA_SLICE_BYTES, a0
	addi.w	#DMA_SLICE_BYTES, d0
	bsr	wait_vblank_end
	dbra	d5, 3b
4:
	movem.l	(sp)+, d0-d5/a0
	rts

/* Build the back name table: cell k -> (TILE_BASE+k) | (pmap[k]<<13). */
build_name_table:
	movem.l	d0-d5/a0-a1, -(sp)
	tst.w	d6
	beq	1f
	move.w	#TILE_BASE_B, d4
	move.l	#MAP_B, d3
	bra	2f
1:
	move.w	#TILE_BASE_A, d4
	move.l	#MAP_A, d3
2:
	lea	(PMAP_BUF).l, a1
	moveq	#0, d5				/* d5 = row y */
bnt_row:
	move.w	d5, d0
	add.w	#PLANE_Y, d0
	mulu.w	#PLANE_W, d0
	add.w	#PLANE_X, d0
	lsl.l	#1, d0
	add.l	d3, d0
	bsr	vram_write_cmd
	move.w	#W_TILES-1, d2
bnt_col:
	moveq	#0, d0
	move.b	(a1)+, d0			/* pmap value 0-3 */
	lsl.w	#8, d0
	lsl.w	#5, d0				/* <<13 -> palette bits */
	or.w	d4, d0				/* | tile index */
	move.w	d0, (VDP_DATA).l
	addq.w	#1, d4
	dbra	d2, bnt_col
	addq.w	#1, d5
	cmp.w	#H_TILES, d5
	bne	bnt_row
	movem.l	(sp)+, d0-d5/a0-a1
	rts

switch_buffer:
	bsr	wait_vblank_start
	bsr	load_overlay_dma_now		/* overlay patterns -> back Plane B bank */
	bsr	load_frame_palette		/* per-frame CRAM update during VBlank */
	tst.w	d6
	beq	1f
	move.w	#PLANEA_REG_B, (VDP_CTRL).l
	move.w	#PLANEB_REG_B, (VDP_CTRL).l
	bra	2f
1:
	move.w	#PLANEA_REG_A, (VDP_CTRL).l
	move.w	#PLANEB_REG_A, (VDP_CTRL).l
2:
	bsr	wait_vblank_end
	eori.w	#1, d6
	rts

/* DMA the 24 overlay patterns into the back Plane B tile bank. Caller is already
 * inside VBlank (called from switch_buffer); no wait_vblank here. */
load_overlay_dma_now:
	movem.l	d0-d3/a0, -(sp)
	tst.w	d6
	beq	1f
	move.w	#PB_TILE_B_VRAM, d0
	bra	2f
1:
	move.w	#PB_TILE_A_VRAM, d0
2:
	lea	(OVL_PAT_BUF).l, a0
	move.w	#OVL_PAT_BYTES/2, d1		/* 384 words */
	bsr	vram_dma_copy_now
	movem.l	(sp)+, d0-d3/a0
	rts

/* Build the back Plane B name table: a 20x12 window of mostly-0 (transparent)
 * cells, with the 24 overlay descriptors placed as priority|palette|tile. */
build_planeb_name_table:
	movem.l	d0-d5/a0-a1, -(sp)
	/* zero the 240-word Main RAM image */
	lea	(PBNAME_BUF).l, a1
	move.w	#(TILES_PER_FRAME/2)-1, d0	/* 120 longs = 240 words */
	moveq	#0, d2
1:
	move.l	d2, (a1)+
	dbra	d0, 1b
	/* place overlay cells: tile slot k uses bank base + k (k = 0..23) */
	tst.w	d6
	beq	2f
	move.w	#PB_TILE_BASE_B, d4
	bra	3f
2:
	move.w	#PB_TILE_BASE_A, d4
3:
	lea	(OVL_DESC_BUF).l, a0
	lea	(PBNAME_BUF).l, a1
	move.w	#N_OVL-1, d5
pb_desc:
	moveq	#0, d0
	move.b	(a0)+, d0			/* cell index 0..239 (0xFF=skip) */
	moveq	#0, d1
	move.b	(a0)+, d1			/* palette 0..3 */
	cmp.b	#0xFF, d0
	beq	pb_skip
	lsl.w	#8, d1
	lsl.w	#5, d1				/* palette << 13 */
	ori.w	#0x8000, d1			/* priority bit -> over low-pri Plane A */
	or.w	d4, d1				/* | tile index (base + k) */
	add.w	d0, d0				/* cell*2 = word offset */
	move.w	d1, (a1,d0.w)
pb_skip:
	addq.w	#1, d4				/* advance slot index even on skip */
	dbra	d5, pb_desc
	/* write the 20x12 window to the back Plane B name table */
	tst.w	d6
	beq	4f
	move.l	#PBMAP_B, d3
	bra	5f
4:
	move.l	#PBMAP_A, d3
5:
	lea	(PBNAME_BUF).l, a1
	moveq	#0, d5				/* d5 = row y */
pb_row:
	move.w	d5, d0
	add.w	#PLANE_Y, d0
	mulu.w	#PLANE_W, d0
	add.w	#PLANE_X, d0
	lsl.l	#1, d0
	add.l	d3, d0
	bsr	vram_write_cmd
	move.w	#W_TILES-1, d2
pb_col:
	move.w	(a1)+, (VDP_DATA).l
	dbra	d2, pb_col
	addq.w	#1, d5
	cmp.w	#H_TILES, d5
	bne	pb_row
	movem.l	(sp)+, d0-d5/a0-a1
	rts

/* Clear both Plane B name tables (0x4000..0x7000) at startup. */
clear_planeb:
	move.l	#0x40000000|((PBMAP_A&0x3FFF)<<16)|((PBMAP_A>>14)&3), (VDP_CTRL).l
	move.w	#(0x3000/2)-1, d0		/* 0x4000..0x7000 */
1:
	move.w	#0, (VDP_DATA).l
	dbra	d0, 1b
	rts

/* Load this frame's 4 palettes (PAL_BUF, 64 words) into CRAM lines 0-3. */
load_frame_palette:
	move.l	#0xC0000000, (VDP_CTRL).l
	lea	(PAL_BUF).l, a0
	move.w	#PAL_WORDS-1, d0
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d0, 1b
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

/* --- CD-DA screen ---
 * Clear the planes, set a simple 2-colour palette, upload the ASCII font,
 * print the "CD-DA PLAYING" message, then tell the Sub to start the CD-DA bgm.
 */
cdda_screen:
	bsr	clear_name_tables		/* clears name tables 0x8000..0xFFFF */
	move.w	#PLANEA_REG_A, (VDP_CTRL).l	/* show plane A (name table 0xC000) */
	move.l	#0xC0000000, (VDP_CTRL).l	/* CRAM line 0 */
	move.w	#0x0600, (VDP_DATA).l		/* col0: dark blue backdrop */
	move.w	#0x0EEE, (VDP_DATA).l		/* col1: white text */
	bsr	load_font_op
	bsr	print_cdda_msg
	bsr	play_cdda_cmd
	rts

load_font_op:
	move.l	#0x40000000|((OPFONT_VRAM&0x3FFF)<<16)|((OPFONT_VRAM>>14)&3), (VDP_CTRL).l
	lea	(opfont).l, a0
	move.w	#(OPFONT_BYTES/2)-1, d0
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d0, 1b
	rts

/* Write the pre-resolved tile indices in cdda_msg into plane A at CDDA_ROW/COL. */
print_cdda_msg:
	movem.l	d0/a0, -(sp)
	move.l	#MAP_A+(CDDA_ROW*PLANE_W+CDDA_COL)*2, d0
	bsr	vram_write_cmd
	lea	(cdda_msg).l, a0
1:
	moveq	#0, d0
	move.b	(a0)+, d0
	cmp.b	#0xFF, d0
	beq	2f
	move.w	d0, (VDP_DATA).l		/* name entry = tile index, palette 0 */
	bra	1b
2:
	movem.l	(sp)+, d0/a0
	rts

/* Tell the Sub to play the CD-DA bgm (echo handshake, matches Sub command_done). */
play_cdda_cmd:
	move.w	#CMD_PLAY_CDDA, (GA_COMCMD0).l
1:
	tst.w	(GA_COMSTAT0).l
	beq	1b
	move.w	#0, (GA_COMCMD0).l
2:
	tst.w	(GA_COMSTAT0).l
	bne	2b
	rts

	.align	2
cdda_msg:				/* "CD-DA PLAYING" as font tile indices (1=C..11=G) */
	.byte	1,2,3,2,4,5,6,7,4,8,9,10,11,0xFF
	.align	2
opfont:
	.incbin	"opfont.bin"
