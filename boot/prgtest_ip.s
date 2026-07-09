/*
 * PRG-RAM 書込テスト(Main側)。COMSTAT0(Sub進捗)を画面全体の色で表示する。
 *   1 dim-gray / 2 red(0x20000無読込OK) / 3 green(0x40000) / 4 yellow(全無読込OK)
 *   5 blue(読込開始) / 6 magenta(読込active) / 7 cyan(0x20000読込中OK) / 8 gray(0x40000)
 *   9 white(全部OK=書込は常に可能) / 0x80 orange(読み戻し不一致)
 * 観測: 白=バス競合でも容量でもない, マゼンタ=読込中に固まる(バス競合),
 *       赤/緑=読込前でも固まる(容量制限)。
 */
.equ STACK, 0x00FFFD00
.equ BIOS_CLEAR_VRAM,            0x000002A0
.equ BIOS_LOAD_DEFAULT_VDP_REGS, 0x000002AC
.equ BIOS_VDP_DISP_ENABLE,       0x000002D8
.equ BIOS_CLEAR_COMM,            0x00000340
.equ VDP_DATA, 0x00C00000
.equ VDP_CTRL, 0x00C00004
.equ GA_COMSTAT0, 0x00A12020

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
	move.w	#0x8C00, (VDP_CTRL).l		/* H32 */
	move.w	#0x9001, (VDP_CTRL).l		/* plane 64x32 */
	move.w	#0x8F02, (VDP_CTRL).l		/* autoinc 2 */
	move.w	#0x8B00, (VDP_CTRL).l
	move.w	#0x8578, (VDP_CTRL).l
	move.w	#0x8D3F, (VDP_CTRL).l
	move.w	#0x8230, (VDP_CTRL).l		/* reg2 plane A = 0xC000 */
	move.l	#0x40000010, (VDP_CTRL).l
	move.w	#0, (VDP_DATA).l
	move.w	#0, (VDP_DATA).l
	/* solid tile 1 (VRAM 0x20): 全画素 index 1 */
	move.l	#0x40200000, (VDP_CTRL).l
	move.w	#16-1, d1
1:
	move.w	#0x1111, (VDP_DATA).l
	dbra	d1, 1b
	/* plane A(0xC000) を entry 0x0001(tile1,pal0) で埋める */
	move.l	#0x40000003, (VDP_CTRL).l
	move.w	#(64*32)-1, d1
2:
	move.w	#0x0001, (VDP_DATA).l
	dbra	d1, 2b
	jsr	BIOS_VDP_DISP_ENABLE
loop:
	bsr	wait_vblank
	moveq	#0, d0
	move.w	(GA_COMSTAT0).l, d0
	andi.w	#0x00FF, d0
	cmp.w	#16, d0
	bcs	3f
	moveq	#15, d0				/* 0x80等は index15(fail色)へ */
3:
	add.w	d0, d0
	lea	coltab, a0
	move.w	(a0,d0.w), d0
	move.l	#0xC0020000, (VDP_CTRL).l	/* CRAM color 1 */
	move.w	d0, (VDP_DATA).l
	bra	loop

wait_vblank:
	move.w	d1, -(sp)
1:
	move.w	(VDP_CTRL).l, d1
	btst	#3, d1
	beq	1b
2:
	move.w	(VDP_CTRL).l, d1
	btst	#3, d1
	bne	2b
	move.w	(sp)+, d1
	rts

	.align	2
coltab:
	.word	0x0000		/* 0 black */
	.word	0x0222		/* 1 dim-gray  開始 */
	.word	0x000E		/* 2 red       0x20000 無読込OK */
	.word	0x00E0		/* 3 green     0x40000 無読込OK */
	.word	0x0EE0		/* 4 yellow    全無読込OK */
	.word	0x0E00		/* 5 blue      読込開始 */
	.word	0x0E0E		/* 6 magenta   読込active */
	.word	0x00EE		/* 7 cyan      0x20000 読込中OK */
	.word	0x0888		/* 8 gray      0x40000 読込中OK */
	.word	0x0EEE		/* 9 white     全部OK */
	.word	0x0000, 0x0000, 0x0000, 0x0000, 0x0000
	.word	0x008E		/* 15 orange   fail(不一致/その他) */
