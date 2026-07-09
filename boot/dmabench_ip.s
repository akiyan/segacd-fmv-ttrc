/*
 * dmabench: 表示モード別の VRAM DMA スループット実測(再利用可能)。
 *
 * 1VBLANK で「Main-RAM → VRAM」DMA に何ワード入るかを二分探索で測る。
 * 手順(fits): active→vblank の立ち上がりを待って即 X ワードDMA。DMA完了後に
 * まだ vblank 中なら「その vblank に収まった」。X を二分探索して最大語数を求める。
 * 結果を左上にフォント表示: W=語/vblank F=タイル/コマ(3vblank換算)。
 *
 * モード: --defsym MODE=0(H32,既定) / 1(H40) / 2(mode4=SMS)。Makefile の dmabench-<mode> から。
 */
.equ VDP_DATA, 0x00C00000
.equ VDP_CTRL, 0x00C00004
.equ BIOS_LOAD_DEFAULT_VDP_REGS, 0x000002AC
.equ BIOS_CLEAR_VRAM,            0x000002A0
.equ BIOS_VDP_DISP_ENABLE,       0x000002D8
.equ SRC, 0x00FF4000			/* Main-RAM テスト源(内容不問, タイミングのみ) */
.equ DMA_DST, 0x2000			/* フォント/NTを壊さない測定用VRAM先 */
.equ DBGFONT_VTILE, 1
.equ DBGFONT_VADDR, 1*32
.equ DBGFONT_N, 27
.equ NT, 0xC000				/* nametable */
.equ HI0, 9000				/* 二分探索上限(mode4理論値も超える値) */

.ifndef MODE
.equ MODE, 0
.endif

.text
	.incbin "security.bin"
	bra.w	ip_entry
	.org	0x584

.global ip_entry
ip_entry:
	move.w	#0x2700, sr
	lea	0x00FFFD00, sp
	jsr	BIOS_LOAD_DEFAULT_VDP_REGS
	jsr	BIOS_CLEAR_VRAM
	/* 表示モード。BIOS_VDP_DISP_ENABLE は reg1 を戻し得るので使わない。 */
.if MODE == 1
	move.w	#0x8C81, (VDP_CTRL).l		/* reg12 H40 */
.elseif MODE == 2
	move.w	#0x8006, (VDP_CTRL).l		/* SMS reg0: M4+M3, 192-line mode4 */
	move.w	#0x81E0, (VDP_CTRL).l		/* SMS reg1: display+vint, M1/M2=0 */
	move.w	#0x82FF, (VDP_CTRL).l		/* SMS NT base = 0x3800 */
	move.w	#0x83FF, (VDP_CTRL).l		/* SMS color mask normal */
	move.w	#0x84FF, (VDP_CTRL).l		/* SMS pattern mask normal */
	move.w	#0x85FF, (VDP_CTRL).l		/* SMS sprite attr mask normal */
	move.w	#0x86FF, (VDP_CTRL).l		/* SMS sprite pattern mask normal */
.else
	move.w	#0x8C00, (VDP_CTRL).l		/* reg12 H32 */
.endif
	move.w	#0x8F02, (VDP_CTRL).l		/* autoinc 2 */
	move.w	#0x9001, (VDP_CTRL).l		/* plane 64x32 */
	move.w	#0x8230, (VDP_CTRL).l		/* reg2 plane A = 0xC000 */
	/* CRAM: index0=黒, index1=白 */
	move.l	#0xC0000000, (VDP_CTRL).l
	move.w	#0x0000, (VDP_DATA).l
	move.w	#0x0EEE, (VDP_DATA).l
	/* フォントを VRAM tile1 へ */
	move.l	#(0x40000000|((DBGFONT_VADDR&0x3FFF)<<16)|(((DBGFONT_VADDR>>14)&3))), (VDP_CTRL).l
	lea	dbgfont, a0
	move.w	#DBGFONT_N*16-1, d1
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d1, 1b
	/* 表示ON + DMA許可 */
.if MODE != 2
	move.w	#0x8174, (VDP_CTRL).l		/* reg1: disp on+vint+DMA+M5 */
.endif

	/* 二分探索: lo=収まる最大, hi=収まらない最小 */
	moveq	#0, d4				/* lo */
	move.w	#HI0, d5			/* hi */
bs_loop:
	move.w	d5, d0
	sub.w	d4, d0
	cmp.w	#8, d0
	bls	bs_done
	move.w	d4, d0
	add.w	d5, d0
	lsr.w	#1, d0				/* mid */
	move.w	d0, d6				/* keep mid */
	bsr	fits				/* d0=1 fits / 0 not */
	tst.w	d0
	beq	1f
	move.w	d6, d4				/* fits -> lo=mid */
	bra	bs_loop
1:
	move.w	d6, d5				/* not -> hi=mid */
	bra	bs_loop
bs_done:
	/* true mode4 ではMDフォント/NTが読めないので、結果表示だけH32 mode5へ戻す。 */
	move.w	#0x8004, (VDP_CTRL).l
	move.w	#0x8174, (VDP_CTRL).l
	move.w	#0x8C00, (VDP_CTRL).l
	move.w	#0x9001, (VDP_CTRL).l
	move.w	#0x8230, (VDP_CTRL).l
	/* 結果表示: d4 = 最大語/vblank。行間はプレーン64幅=128バイト。 */
	move.w	d4, d7				/* W */
	/* 行2: W xxxx = 語/vblank */
	move.l	#NT+2*128+2*2, d0
	move.w	#17, d3				/* 'W' */
	move.w	d7, d4
	bsr	put_row
	/* 行4: F xxxx = タイル/コマ ≈ (W/16)*3 (3vblank/コマ換算) */
	move.l	#NT+4*128+2*2, d0
	move.w	d7, d4
	lsr.w	#4, d4				/* /16 = タイル/vblank */
	move.w	d4, d1
	add.w	d1, d1
	add.w	d1, d4				/* *3 */
	move.w	#15, d3				/* 'F' */
	bsr	put_row
hlt:
	bra	hlt

/* d0=語数 → 1vblankに収まるか(d0=1/0)。trashes d0-d2 */
fits:
	movem.l	d3/d6/a0, -(sp)
	move.w	d0, d6				/* words */
1:
	move.w	(VDP_CTRL).l, d0		/* active になるまで */
	btst	#3, d0
	bne	1b
2:
	move.w	(VDP_CTRL).l, d0		/* vblank 立ち上がり */
	btst	#3, d0
	beq	2b
	move.w	d6, d0
	bsr	dma_words			/* X語DMA(完了待ち) */
	move.w	(VDP_CTRL).l, d0
	btst	#3, d0				/* まだvblank? */
	bne	3f
	moveq	#0, d0				/* はみ出た */
	bra	4f
3:
	moveq	#1, d0
4:
	movem.l	(sp)+, d3/d6/a0
	rts

/* d0=語数を SRC→VRAM tile0 へDMA。完了待ち。trashes d0,d1,d2 */
dma_words:
	move.w	#0x8F02, (VDP_CTRL).l
	move.w	d0, d2
	move.w	#0x9300, d1
	or.b	d2, d1
	move.w	d1, (VDP_CTRL).l
	lsr.w	#8, d2
	move.w	#0x9400, d1
	or.b	d2, d1
	move.w	d1, (VDP_CTRL).l
	move.l	#SRC, d2
	lsr.l	#1, d2
	move.w	#0x9500, d1
	or.b	d2, d1
	move.w	d1, (VDP_CTRL).l
	lsr.l	#8, d2
	move.w	#0x9600, d1
	or.b	d2, d1
	move.w	d1, (VDP_CTRL).l
	lsr.l	#8, d2
	move.w	#0x9700, d1
	or.b	d2, d1
	move.w	d1, (VDP_CTRL).l
	move.l	#DMA_DST, d2			/* dst コマンド(VRAM書込+CD5起動) */
	move.l	d2, d1
	andi.w	#0x3FFF, d1
	ori.w	#0x4000, d1
	move.w	d1, (VDP_CTRL).l
	move.l	d2, d1
	lsr.l	#8, d1
	lsr.l	#6, d1
	andi.w	#0x0003, d1
	ori.w	#0x0080, d1
	move.w	d1, (VDP_CTRL).l
1:
	move.w	(VDP_CTRL).l, d1
	btst	#1, d1
	bne	1b
	rts

/* d0=NTアドレス, d3=ラベルglyph, d4=値(hex4). trashes d0,d1,d2 */
put_row:
	bsr	set_vram_write
	move.w	d3, d1
	add.w	#DBGFONT_VTILE, d1
	move.w	d1, (VDP_DATA).l
	moveq	#3, d2
1:
	rol.w	#4, d4
	move.w	d4, d1
	andi.w	#0xF, d1
	add.w	#DBGFONT_VTILE, d1
	move.w	d1, (VDP_DATA).l
	dbra	d2, 1b
	rts

/* d0=VRAMアドレス → 書込コマンド。trashes d0,d2 */
set_vram_write:
	move.l	d0, d2
	andi.l	#0x3FFF, d0
	swap	d0
	ori.l	#0x40000000, d0
	lsr.w	#7, d2
	lsr.w	#7, d2
	andi.w	#3, d2
	or.w	d2, d0
	move.l	d0, (VDP_CTRL).l
	rts

	.align 2
dbgfont:
	.incbin "dbgfont.bin"
