/*
 * Phase A: H32 256x144 静止画レンダラ (Main/IP)。
 *
 * 実機描画土台の切り分け用。CD読み込み/Sub/DMA/差分を一切使わず、IPに埋め込んだ
 * 1フレーム分(576タイル+ネームテーブル+パレット)を CPU書き込みだけで VRAM へ載せ、
 * H32 256x144(32x18) を画面中央(縦offset 5セル)に表示して停止する。
 *
 * still256.bin レイアウト:
 *   [0]      tiles     576*32 = 18432B  (VDPタイル順, 4bpp)
 *   [18432]  nametable 576*2  = 1152B   (big-endian, (pal<<13)|(1+cell))
 *   [19584]  palettes  4*16*2 = 128B    (CRAMワード)
 */

.equ STACK, 0x00FFFD00

.equ BIOS_CLEAR_VRAM,            0x000002A0
.equ BIOS_LOAD_DEFAULT_VDP_REGS, 0x000002AC
.equ BIOS_VDP_DISP_ENABLE,       0x000002D8
.equ BIOS_CLEAR_COMM,            0x00000340

.equ VDP_DATA, 0x00C00000
.equ VDP_CTRL, 0x00C00004

.equ NAME_A,  0xC000
.equ TILE_VRAM, 0x0020          /* タイル1をここに置く(tile index = addr/0x20) */

.equ TILES_OFF, 0
.equ NT_OFF,    18432
.equ PAL_OFF,   19584

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

	/* --- VDP: H32, plane A=0xC000, 64x32, autoinc=2 --- */
	move.w	#0x8C00, (VDP_CTRL).l		/* reg12 = 0x00 : H32(256)   */
	move.w	#0x8230, (VDP_CTRL).l		/* reg2  = 0x30 : plane A 0xC000 */
	move.w	#0x9001, (VDP_CTRL).l		/* reg16 : plane size 64x32  */
	move.w	#0x8F02, (VDP_CTRL).l		/* reg15 : autoincrement 2   */

	/* VSRAM=0 (垂直スクロールを消す; CLEAR_VRAMはVSRAMを消さない) */
	move.l	#0x40000010, (VDP_CTRL).l	/* VSRAM write addr 0 */
	move.w	#0, (VDP_DATA).l		/* plane A vscroll = 0 */
	move.w	#0, (VDP_DATA).l		/* plane B vscroll = 0 */

	lea	still_data, a1			/* a1 = base of embedded frame */

	/* --- palette -> CRAM 0 (64 words) --- */
	move.l	#0xC0000000, (VDP_CTRL).l	/* CRAM write addr 0 */
	lea	PAL_OFF(a1), a0
	move.w	#64-1, d1
pal_loop:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d1, pal_loop

	/* --- tiles -> VRAM 0x0020 (9216 words) --- */
	move.l	#0x40200000, (VDP_CTRL).l	/* VRAM write addr 0x0020 */
	lea	TILES_OFF(a1), a0
	move.w	#9216-1, d1
tile_loop:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d1, tile_loop

	/* --- nametable -> plane rows 5..22, cols 0..31 --- */
	lea	NT_OFF(a1), a0			/* a0 = nametable entries (cell順) */
	moveq	#5, d3				/* plane_row = 5 (縦中央: 28行に18行) */
	move.w	#18-1, d2			/* 18 rows */
row_loop:
	/* VRAM addr = NAME_A + plane_row*64*2 = 0xC000 + plane_row*128 */
	move.l	d3, d0
	lsl.l	#7, d0				/* plane_row*128 */
	add.l	#NAME_A, d0
	bsr	vram_cmd			/* d0 = write command */
	move.l	d0, (VDP_CTRL).l
	move.w	#32-1, d1
cell_loop:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d1, cell_loop
	addq.l	#1, d3
	dbra	d2, row_loop

	jsr	BIOS_VDP_DISP_ENABLE

halt:
	bra	halt

/* d0 = VRAM address(<=0xFFFF) -> d0 = VDP VRAM write command. trashes d1 */
vram_cmd:
	move.l	d0, d1
	andi.l	#0x3FFF, d0
	swap	d0				/* (addr&0x3FFF)<<16 */
	ori.l	#0x40000000, d0
	lsr.w	#7, d1
	lsr.w	#7, d1				/* (addr>>14) */
	andi.w	#3, d1
	or.w	d1, d0
	rts

	.align	2
still_data:
	.incbin "tmp/sim/still256.bin"
