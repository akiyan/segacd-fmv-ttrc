/*
 * Phase B3: delta stream player - Main (IP) side (ダブルバッファ, tearing除去)。
 *
 * タイルプールは単一の永続VRAM領域(両ネームテーブルが共有, B1のLRUで表示中slotは
 * 上書きされないことが保証済み)。ネームテーブルは2枚(NT0=0xC000, NT1=0xE000)を
 * 交互に使う。Main RAM に shadow[576](cell->entry) を持ち:
 *   1. n_load 個のタイルを slot へ書込(共有プール)
 *   2. n_upd をシャドウに反映 shadow[cell]=entry
 *   3. シャドウ全体(576)を「裏」ネームテーブルへ blit (裏は非表示なので安全)
 *   4. VBlank で reg2 を裏へ flip(原子的) → tearing無し
 * これで「前フレーム差分の追いつき」不要(裏は常に完全な現フレーム)。
 */

.equ STACK, 0x00FFFD00

.equ BIOS_CLEAR_VRAM,            0x000002A0
.equ BIOS_LOAD_DEFAULT_VDP_REGS, 0x000002AC
.equ BIOS_VDP_DISP_ENABLE,       0x000002D8
.equ BIOS_CLEAR_COMM,            0x00000340

.equ VDP_DATA, 0x00C00000
.equ VDP_CTRL, 0x00C00004
.equ VDP_HV,   0x00C00008

.equ GA_COMCMD0, 0x00A12010
.equ GA_COMCMD1, 0x00A12012
.equ GA_COMSTAT0, 0x00A12020
.equ GA_STOPWATCH, 0x00A1200C		/* 12-bit, 30.72 us/tick, Main read-only */

.equ PROBE_BANK, 0x00200000

.equ CMD_STREAM, 0x50
.equ CMD_SWAP,   0x51
.equ STAT_READY, 0x8003
.equ STAT_END,   0x8004			/* SPからの映画終端通知(15秒待って再ループ) */

.equ NT0, 0xC000
.equ WIN_NT, 0xD000			/* DEBUG Window NT: H40 4KB / H32 2KB alignment both satisfied */
.equ NT1, 0xE000

/* 0xFF2000..0xFF7FFF is no longer a tile staging buffer: pattern DMA reads
   Word RAM directly and repairs the first destination word on the CPU.  Keep
   the whole 24 KiB range reserved for boot-time Main-CPU code generation. */
.equ MAIN_CODEGEN_BASE,  0x00FF2000
.equ RUN_TABLE,          0x00FF8000	/* (dst.w,len.w,src.l) cold-run records; 0x3000B capacity */
.equ MAIN_CODEGEN_LIMIT, RUN_TABLE
.equ MAIN_CODEGEN_TABLE_BYTES, 0x0200	/* 256 signed word offsets */
.equ MAIN_CODEGEN_HANDLER_MAX, 70	/* mask FF: guarded before writing */
.equ MAIN_CODEGEN_EXPECTED_END, 0x00FF4900
.equ MAIN_CODEGEN_BLITTER_MAX, 7296	/* H40 40x28, NT0+NT1 */

/* Exact 68000 words emitted by init_main_codegen.  Keep synchronized with
   harness/main_codegen/verify_handlers.py. */
.equ CG_OP_MOVE_ENTRY_D3,      0x3618	/* move.w (a0)+,d3 */
.equ CG_OP_STRIP_COLD_D6_D3,   0xC646	/* and.w d6,d3 */
.equ CG_ENTRY_MASK_LONG,       0x7FFF7FFF
.equ CG_OP_STORE_D3_A1,        0x3283	/* move.w d3,(a1) */
.equ CG_OP_STORE_D3_D16_A1,    0x3343	/* move.w d3,disp(a1) */
.equ CG_OP_ADVANCE_SHADOW,     0x43E9	/* lea 16(a1),a1 */
.equ CG_SHADOW_BYTE_ADVANCE,   16
.equ CG_OP_BRA_W,              0x6000
.equ CG_OP_LEA_SHADOW_A1,      0x43F9	/* lea shadow.l,a1 */
.equ CG_OP_MOVE_L_IMM_ABS,     0x23FC	/* move.l #cmd,(VDP_CTRL).l */
.equ CG_OP_MOVE_L_A1_ABS,      0x23D9	/* move.l (a1)+,(VDP_DATA).l */
.equ CG_OP_MOVE_W_A1_ABS,      0x33D9	/* move.w (a1)+,(VDP_DATA).l */
.equ CG_OP_RTS,                0x4E75
/* デバッグオーバーレイ: フォントは予約VRAM(プール1360の直上 tile1361)。 */
.equ DBGFONT_N, 28			/* dbgfont.bin のタイル数 */
/* フォントVRAM位置はヘッダの base+pool 直上を実行時に計算(md_font_vtile/md_font_addr) */
/* リリースビルドが既定。make movieplay DEBUG=1 でオーバーレイ一式を有効化
   (ストリーム側は CBRSIM_PACK_DEBUG=1 でデバッグ欄ありを生成) */
/* CRAM pre-load: 全区間パレット表。boot時にWord-RAM(PALTAB_OFF, frame0バンク)から一度だけ
   コピーし、以降の区間切替はO_PALWの区間番号+1でこの表を引く(ストリーム到着に依存しない)。
   容量はav_config.PALTAB_MAX_SEGと一致必須(check_player_ring.pyがビルド時検証)。 */
.equ PALTAB_OFF, 0xB000			/* Word-RAM内ステージ位置(sp.sと一致必須) */
.equ PALTAB_MAX_SEG, 64			/* Main-RAM表の容量(区間数)。64*128B=8KB */
.equ PALTAB_RAM, 0x00FFB000		/* 表本体 0xFFB000..0xFFD000(スタックまで11KB余裕) */
/* 1VBLANKで安全に転送できる語数はモード別(md_vbudget)。実測(dmabench)に基づき保守的に。
   これを超える転送はランをまたいで次VBLANKへ分割=active表示中へのはみ出し防止(ares対策)。 */
.equ VB_WORDS_H32, 2800		/* H32 V28 NTSC */
.equ VB_WORDS_H40, 3400		/* H40 V28 NTSC(理論~3895語より保守的) */
.equ CPU_DIRECT_MAX_WORDS, 32	/* 1-2 tiles: CPU writes beat per-run DMA setup */

.text

	.incbin "security.bin"

	bra.w	ip_entry
	.org	0x584

.global ip_entry
ip_entry:
	move.w	#0x2700, sr
	lea	STACK, sp

	jsr	BIOS_LOAD_DEFAULT_VDP_REGS
	jsr	BIOS_CLEAR_VRAM			/* WIN_NT D000-DFFFもここで一度だけzero初期化 */
	jsr	BIOS_CLEAR_COMM

	/* VDP: H32, autoinc=2, plane 64x32, VSRAM=0, HScroll/Sprite を安全域へ */
	move.w	#0x8C00, (VDP_CTRL).l		/* reg12 H32 */
	move.w	#0x9001, (VDP_CTRL).l		/* reg16 plane 64x32 */
	move.w	#0x8F02, (VDP_CTRL).l		/* reg15 autoinc 2 */
	move.w	#0x8B00, (VDP_CTRL).l		/* reg11 scroll full-screen */
	move.w	#0x8407, (VDP_CTRL).l		/* reg4  Plane B NT = NT1(0xE000) */
	move.w	#0x8578, (VDP_CTRL).l		/* reg5  sprite table 0xF000 */
	move.w	#0x8D3F, (VDP_CTRL).l		/* reg13 hscroll 0xFC00 */
	move.w	#0x8238, (VDP_CTRL).l		/* reg2  表示=NT1(front)。裏はNT0から構築 */
	move.l	#0x40000010, (VDP_CTRL).l	/* VSRAM=0 */
	move.w	#0, (VDP_DATA).l
	move.w	#0, (VDP_DATA).l

	/* palette -> CRAM 0 */
	move.l	#0xC0000000, (VDP_CTRL).l
	lea	palettes, a0
	move.w	#64-1, d0
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d0, 1b

	jsr	BIOS_VDP_DISP_ENABLE
	move.w	#0x8174, (VDP_CTRL).l		/* reg1: 表示on+vint+DMA許可(M1)+mode5 */

	clr.w	dbg_seg

	clr.w	back_idx			/* 裏=NT0(0) から構築, 表示=NT1 */

	move.w	#CMD_STREAM, d0
	bsr	cmd_wait_ready

	/* frame0準備完了=バンクにヘッダ写し(O_HDR)がある。mode/tcols/trows/pool/base を読み
	   モード依存のVDP設定と実行時変数を確定する(汎用化: H32/H40, mode4は将来) */
	lea	(PROBE_BANK+0xAF80), a0
	move.w	8(a0), md_tcols
	move.w	10(a0), md_trows
	move.w	12(a0), d0			/* cells; supported grids are multiples of 8 */
	lsr.w	#3, d0
	move.w	d0, md_bmbytes
	move.w	14(a0), d1			/* pool */
	add.w	16(a0), d1			/* +base */
	move.w	d1, md_font_vtile
	moveq	#0, d0
	move.w	d1, d0
	lsl.l	#5, d0
	move.l	d0, md_font_addr		/* フォントVRAM = (base+pool)*32 */
	moveq	#0, d0
	move.b	38(a0), d0			/* mode: 0=H32 1=H40 (2=mode4将来) */
	move.w	d0, md_mode
	/* v4: N(1コマの表示VBLANK数)@52。0(v2/v3ディスク)なら4(=15fps)。表示をN vblank間隔に */
	move.w	52(a0), d0
	bne	1f
	moveq	#4, d0
1:
	move.w	d0, md_vsync_n
	/* Select the VDP width from the stream's mode byte, not from N.
	   N is the frame pacing interval (2 at 30fps, 4 at 15fps), so testing
	   it here made every v4 stream fall through to H40. */
	move.w	#0x8C00, (VDP_CTRL).l		/* reg12 H32 */
	move.w	#32, d2				/* screen_cols */
	move.w	#VB_WORDS_H32, d3
	cmpi.w	#1, md_mode
	bne	1f					/* mode 0=H32; mode 2 is reserved */
	move.w	#0x8C81, (VDP_CTRL).l		/* reg12 H40 */
	move.w	#40, d2
	move.w	#VB_WORDS_H40, d3
1:
	/* DEBUG HUDはPlane Aと独立したWindow name tableを使う。reg3=0x34は
	   D000/0x400。D000はH40の4KB境界とH32の2KB境界の両方を満す。
	   reg17=left,pos0で横Windowを空にし、reg18=top,pos1で上1タイル行だけ
	   Windowにする。NT0/NT1のreg2 flipはWIN_NTに影響しない。 */
.ifdef DEBUG
	move.w	#0x8334, (VDP_CTRL).l		/* reg3: Window NT = 0xD000 */
	move.w	#0x9100, (VDP_CTRL).l		/* reg17: left of column-pair 0 = no side strip */
	move.w	#0x9201, (VDP_CTRL).l		/* reg18: rows above 1 = top row only */
.endif
	move.w	d3, md_vbudget
	sub.w	md_tcols, d2			/* col0 = (screen_cols-tcols)/2 */
	lsr.w	#1, d2
	move.w	d2, md_col0
	move.w	#28, d2				/* screen_rows(H32/H40) */
	sub.w	md_trows, d2			/* row0 = (screen_rows-trows)/2 */
	lsr.w	#1, d2
	move.w	d2, md_row0
.ifdef MAIN_CODEGEN
	/* Generate once, before playback.  A failed range/size proof leaves
	   md_codegen=0 and the per-bit reference path remains active. */
	bsr	init_main_codegen
.endif
	/* CRAM pre-load: PALTAB(全区間パレット)をWord-RAM(frame0バンク)からMain-RAM表へ
	   一度だけコピー。n_seg=O_HDR+20。以降の区間切替はこの表を引くだけ(bf_flip)。 */
	move.w	20(a0), d1			/* n_seg (a0=O_HDR) */
	cmp.w	#PALTAB_MAX_SEG, d1		/* 壊れたヘッダ対策: 表容量にクランプ */
	bls	1f
	move.w	#PALTAB_MAX_SEG, d1
1:
	move.w	d1, md_nseg
	lsl.w	#6, d1				/* n_seg*64語(=128B) */
	beq	2f
	subq.w	#1, d1
	lea	(PROBE_BANK+PALTAB_OFF).l, a1
	lea	PALTAB_RAM, a2
1:
	move.w	(a1)+, (a2)+
	dbra	d1, 1b
2:
	/* デバッグフォントをフォントVRAM位置へ一度だけCPUロード。
	   dbgfont.binの画素index 1をP0/index15(最明色)、背景index 0を
	   P0/index1(最暗色)へ展開する。Windowは動画の上に不透明に重なるが、
	   背後の動画NTはDEBUGでも全行を通常どおり更新する。 */
.ifdef DEBUG
	move.l	md_font_addr, d0
	bsr	set_vram_write
	lea	dbgfont, a0
	move.w	#DBGFONT_N*16-1, d1
1:
	move.w	(a0)+, d0			/* each nibble is 0 or 1 */
	move.w	d0, d2
	lsl.w	#1, d2
	or.w	d2, d0
	lsl.w	#1, d2
	or.w	d2, d0
	lsl.w	#1, d2
	or.w	d2, d0			/* 1 -> 0xF independently in every nibble */
	ori.w	#0x1111, d0			/* 0 -> 0x1; 0xF remains 0xF */
	move.w	d0, (VDP_DATA).l
	dbra	d1, 1b
	/* Initialize the maximum-width Window row once with the reserved blank glyph.
	   H40 uses all 64 entries; H32 displays the first 32.  Per-frame code then
	   overwrites 32 H32 or 40 H40 HUD entries and never clears the row again. */
	move.l	#WIN_NT, d0
	bsr	set_vram_write
	move.w	md_font_vtile, d0
	add.w	#24, d0				/* dbgfont glyph 24 = blank */
	move.w	#64-1, d1
1:
	move.w	d0, (VDP_DATA).l
	dbra	d1, 1b
.endif

	clr.w	frame_no
	clr.w	started
	clr.w	vsync_acc			/* v4: ペーシングカウンタ初期化(.bssはMD上でクリアされない) */
.ifdef DEBUG
	clr.w	sub_wait_lines
	clr.w	dma_elapsed_ticks
	clr.w	dma_start_tick
.endif
play_loop:
	/* v4: ディスクが CD 1x レートマッチpadding済み(pack)=1コマぶんのデータ配送が表示レートに
	   一致。よって旧来のデータ律速(Subシグナル=CMD_SWAP handshake)で正しい fps になる(15fps=
	   5セクタ配送=~15fps, 30fps=2.5セクタ=~30fps)。vsync明示ペーシングは不要(むしろMainを
	   ディスクレートから僅かにずらしSub過剰pump→スリップを招くため撤去)。 */
	tst.w	started
	beq	1f
	bsr	swap_or_end			/* CMD_SWAP → READY(継続) or END(映画終端) */
	cmp.w	#STAT_END, d0
	beq	movie_end_md
1:
	move.w	#1, started
	bsr	build_frame

	addq.w	#1, frame_no
	bra	play_loop

/* 映画終端: 最終フレームを表示したまま15秒(900vblank)待ち、先頭からループ再生 */
movie_end_md:
	move.w	#900-1, d2
1:
	bsr	wait_vblank
	dbra	d2, 1b
	move.w	#CMD_STREAM, d0			/* SPを再ストリーム開始させる */
	bsr	cmd_wait_ready			/* SPのframe0準備完了(STAT_READY)まで待つ */
	clr.w	frame_no
	clr.w	started
	clr.w	dbg_seg
	bra	play_loop

.ifdef MAIN_CODEGEN
/* Emit the 256 straight-line bitmap handlers once into Main RAM.
   The table stores signed word offsets from MAIN_CODEGEN_BASE.  Every handler
   reads only its set-bit entries, strips the cold flag, writes fixed shadow
   displacements, advances a1 by one bitmap byte, and BRA.Ws to bf_cg_unext.
   Nothing patches or rewrites this region after this routine returns. */
init_main_codegen:
	movem.l	d0-d7/a0-a2, -(sp)
	clr.w	md_codegen
	clr.w	md_codegen_blit
	clr.l	md_codegen_end
	clr.l	md_codegen_blit_addr
	clr.l	md_codegen_blit_addr+4
	lea	MAIN_CODEGEN_BASE, a1		/* jump table cursor */
	lea	(MAIN_CODEGEN_BASE+MAIN_CODEGEN_TABLE_BYTES), a0 /* emitted code cursor */
	moveq	#0, d7				/* mask 0..255 */
1:
	/* Refuse before writing this handler if even the largest template could
	   cross into RUN_TABLE.  Partial generated data is harmless while the
	   success flag remains clear. */
	move.l	a0, d0
	addi.l	#MAIN_CODEGEN_HANDLER_MAX, d0
	cmpi.l	#MAIN_CODEGEN_LIMIT, d0
	bhi	9f
	move.l	a0, d0
	subi.l	#MAIN_CODEGEN_BASE, d0
	cmpi.l	#0x7FFF, d0			/* dispatch sign-extends d4.w */
	bhi	9f
	move.w	d0, (a1)+

	moveq	#0, d5				/* source/shadow bit 0..7 */
2:
	btst	d5, d7
	beq	4f
	move.w	#CG_OP_MOVE_ENTRY_D3, (a0)+
	move.w	#CG_OP_STRIP_COLD_D6_D3, (a0)+
	tst.w	d5
	bne	3f
	move.w	#CG_OP_STORE_D3_A1, (a0)+
	bra	4f
3:
	move.w	#CG_OP_STORE_D3_D16_A1, (a0)+
	move.w	d5, d0
	add.w	d0, d0
	move.w	d0, (a0)+
4:
	addq.w	#1, d5
	cmpi.w	#8, d5
	blo	2b

	move.w	#CG_OP_ADVANCE_SHADOW, (a0)+
	move.w	#CG_SHADOW_BYTE_ADVANCE, (a0)+
	move.w	#CG_OP_BRA_W, (a0)+
	/* BRA.W displacement is relative to the PC after its extension word.
	   a0 currently points at that extension word. */
	move.l	#bf_cg_unext, d0
	sub.l	a0, d0
	subq.l	#2, d0
	cmpi.l	#-32768, d0
	blt	9f
	cmpi.l	#32767, d0
	bgt	9f
	move.w	d0, (a0)+

	addq.w	#1, d7
	cmpi.w	#256, d7
	blo	1b

	move.l	a0, d0
	cmpi.l	#MAIN_CODEGEN_EXPECTED_END, d0
	bne	9f
	cmpi.l	#MAIN_CODEGEN_LIMIT, d0
	bhi	9f
	move.l	d0, md_codegen_end
	move.w	#1, md_codegen

	/* Phase 2 needs a valid H32/H40 aperture.  Reject before emitting so the
	   existing generic blitter remains an untouched fallback. */
	move.w	md_mode, d0
	cmpi.w	#1, d0
	bhi	10f
	move.w	#32, d1
	tst.w	d0
	beq	11f
	move.w	#40, d1
11:
	move.w	md_tcols, d0
	beq	10f
	cmp.w	d1, d0
	bhi	10f
	move.w	md_col0, d2
	add.w	d0, d2
	cmp.w	d1, d2
	bhi	10f
	move.w	md_trows, d0
	beq	10f
	cmpi.w	#28, d0
	bhi	10f
	move.w	md_row0, d2
	add.w	d0, d2
	cmpi.w	#28, d2
	bhi	10f
	move.l	a0, d0
	addi.l	#MAIN_CODEGEN_BLITTER_MAX, d0
	cmpi.l	#MAIN_CODEGEN_LIMIT, d0
	bhi	10f

	move.l	a0, md_codegen_blit_addr
	move.l	#NT0, d6
	bsr	emit_main_blitter
	move.l	a0, md_codegen_blit_addr+4
	move.l	#NT1, d6
	bsr	emit_main_blitter
	move.l	a0, d0
	cmpi.l	#MAIN_CODEGEN_LIMIT, d0
	bhi	10f				/* preflight above makes this defensive only */
	move.l	d0, md_codegen_end
	move.w	#1, md_codegen_blit
	bra	10f
9:
	move.l	a0, md_codegen_end		/* diagnostic only; fallback stays selected */
10:
	movem.l	(sp)+, d0-d7/a0-a2
	rts

/* Emit one fixed-geometry name-table blitter at a0.  d6 is NT0 or NT1.
   The caller has already proved the H40 maximum pair fits below RUN_TABLE. */
emit_main_blitter:
	move.w	#CG_OP_LEA_SHADOW_A1, (a0)+
	move.l	#shadow, (a0)+
	move.w	md_row0, d4
	move.w	md_trows, d5
	subq.w	#1, d5
1:
	/* Precompute the exact command produced by set_vram_write for this row. */
	moveq	#0, d0
	move.w	d4, d0
	lsl.w	#7, d0				/* plane row * 128 bytes */
	move.w	md_col0, d1
	add.w	d1, d1				/* centered column * 2 bytes */
	add.w	d1, d0
	add.l	d6, d0				/* NT0/NT1 base */
	move.l	d0, d1
	andi.l	#0x3FFF, d0
	swap	d0
	ori.l	#0x40000000, d0
	lsr.w	#7, d1
	lsr.w	#7, d1
	andi.w	#3, d1
	or.w	d1, d0
	move.w	#CG_OP_MOVE_L_IMM_ABS, (a0)+
	move.l	d0, (a0)+
	move.l	#VDP_CTRL, (a0)+

	move.w	md_tcols, d2
	lsr.w	#1, d2				/* two name-table words per MOVE.L */
	beq	3f
	subq.w	#1, d2
2:
	move.w	#CG_OP_MOVE_L_A1_ABS, (a0)+
	move.l	#VDP_DATA, (a0)+
	dbra	d2, 2b
3:
	move.w	md_tcols, d2
	andi.w	#1, d2
	beq	4f
	move.w	#CG_OP_MOVE_W_A1_ABS, (a0)+
	move.l	#VDP_DATA, (a0)+
4:
	addq.w	#1, d4
	dbra	d5, 1b
	move.w	#CG_OP_RTS, (a0)+
	rts
.endif

/* ---- 1フレーム分をデコードし裏へ描画してflip ----
   タイル転送はWord-RAM直DMA(VDPが自走=CPUを空ける)。手順を2パスに分離:
     Pass1(active可): 全ランの(dst,len,src)表だけを作る
     Pass2(vblank内): 表を順にDMAし、Word-RAM DMAの欠落先頭wordをCPUで修復する */
build_frame:
	movem.l	d0-d7/a0-a3, -(sp)
.ifdef DEBUG
	clr.w	vsync_acc			/* per-frame VBlank-start waits shown as Mxx */
	clr.w	dma_elapsed_ticks		/* H40 Uxxxx: Main pattern-transfer stopwatch ticks */
.endif
	/* Pass1: パターンコピー無し。(dst.w, len.w, src.l)のラン表だけ作る。
	   src は Word-RAM 内のパターン先頭。Pass2は長runをDMA+先頭補修、短runをCPU直書きする。 */
	lea	(PROBE_BANK+0x82), a0		/* n_load @ +0x82, loads @ +0x84 */
	move.w	(a0)+, d7			/* n_load 合計タイル数 */
	lea	RUN_TABLE, a2
	moveq	#0, d4				/* run count */
	tst.w	d7
	beq	bf_none
bf_stage:
	move.w	(a0)+, d0			/* slot_start */
	move.w	(a0)+, d6			/* count */
	beq	bf_stage_done			/* count=0 打切り */
	cmp.w	d7, d6				/* count>残り 切詰め */
	bls	1f
	move.w	d7, d6
1:
	addq.w	#1, d0				/* tile index=1+slot */
	lsl.w	#5, d0				/* dst=(1+slot)*0x20 */
	move.w	d0, (a2)+			/* 表: dst */
	move.w	d6, d1
	lsl.w	#4, d1				/* len words = count*16 */
	move.w	d1, (a2)+			/* 表: len */
	move.l	a0, (a2)+			/* 表: src(Word-RAM内パターン先頭) */
	moveq	#0, d2				/* a0 をパターン分スキップ(count*32B)。
					   count>=1024でword演算は符号あふれ(adda.wは
					   符号拡張)するため必ずlongで行う */
	move.w	d6, d2
	lsl.l	#5, d2
	adda.l	d2, a0
	addq.w	#1, d4
	sub.w	d6, d7
	bne	bf_stage
bf_stage_done:
bf_none:
	move.w	d4, n_runs			/* cold-run record数(0可、物理DMA発行数ではない) */
bf_upd:
	/* Read bitmap+entries directly from the linear control block in the swapped
	   Word-RAM bank.  The Sub already walks them to build cold runs; rewriting
	   every (cell,entry) pair was duplicate work on the bottleneck CPU. */
	lea	(PROBE_BANK+0x10000+4), a0	/* skip total_len + frame_seq */
	move.w	(a0)+, d7			/* n_upd */
	beq	bf_blit
	move.w	(a0)+, d0			/* pal(hi), dbg flag(lo) */
	tst.b	d0
	beq	1f
	adda.w	#22, a0				/* optional debug block */
1:
	movea.l	a0, a2				/* bitmap */
	adda.w	md_bmbytes, a0			/* entries */
	lea	shadow, a1
	move.w	md_bmbytes, d5
	subq.w	#1, d5
.ifdef MAIN_CODEGEN
	/* PC-relative flag check is the only fixed success-path overhead.  The
	   fallback branches around the generated loop; the successful loop falls
	   directly into bf_blit. */
	move.w	md_codegen(pc), d0
	bne	bf_cg_start
.endif
bf_ubyte:
	move.b	(a2)+, d0
	beq	bf_uzero			/* no entries: advance eight shadow words at once */
	cmpi.b	#0xFF, d0
	beq	bf_ufull			/* all entries: straight pointer writes, no bit branches */
	moveq	#7, d4
bf_ubit:
	lsr.b	#1, d0
	bcc	1f
	move.w	(a0)+, d3
	andi.w	#0x7FFF, d3			/* strip the on-disc cold flag */
	move.w	d3, (a1)
1:
	addq.l	#2, a1
	dbra	d4, bf_ubit
	bra	bf_unext
bf_uzero:
	lea	16(a1), a1
	bra	bf_unext
bf_ufull:
	.rept 8
	move.w	(a0)+, d3
	andi.w	#0x7FFF, d3
	move.w	d3, (a1)+
	.endr
bf_unext:
	dbra	d5, bf_ubyte
.ifdef MAIN_CODEGEN
	bra	bf_blit				/* failed generator: safe reference fallback */
bf_cg_uzero:
	lea	16(a1), a1
	bra	bf_cg_unext
bf_cg_ufull:
	.rept 4
	move.l	(a0)+, d3
	and.l	d6, d3				/* strip two packed cold flags */
	move.l	d3, (a1)+
	.endr
	bra	bf_cg_unext
bf_cg_start:
	lea	MAIN_CODEGEN_BASE, a3
	move.l	#CG_ENTRY_MASK_LONG, d6		/* shared word/long cold-flag mask */
bf_cg_ubyte:
	move.b	(a2)+, d0
	beq	bf_cg_uzero			/* exactly the reference zero-mask path */
	cmpi.b	#0xFF, d0
	beq	bf_cg_ufull
	andi.w	#0x00FF, d0			/* MOVE.B leaves the upper byte unchanged */
	add.w	d0, d0				/* signed-word table index */
	move.w	(a3,d0.w), d4
	jmp	(a3,d4.w)				/* prefetch starts generated handler */
bf_cg_unext:
	dbra	d5, bf_cg_ubyte
.endif
bf_blit:
	/* シャドウ全体を裏NTへ blit (裏は非表示=active可) */
	moveq	#0, d5
	move.w	back_idx, d5
	lsl.l	#8, d5
	lsl.l	#5, d5				/* back_idx*0x2000 */
	add.l	#NT0, d5			/* back_base = 0xC000 or 0xE000 (flipまで保持) */
.ifdef MAIN_CODEGEN
	move.w	md_codegen_blit(pc), d0
	beq	bf_blit_reference
	move.w	back_idx(pc), d0
	lsl.w	#2, d0
	lea	md_codegen_blit_addr(pc), a3
	movea.l	(a3,d0.w), a3
	jsr	(a3)
	bra	bf_dma
bf_blit_reference:
.endif
	lea	shadow, a1
	move.w	md_row0, d4			/* plane_row = (screen_rows-trows)/2 */
	move.w	md_trows, d6
	subq.w	#1, d6
bf_row:
	move.w	d4, d1
	lsl.w	#7, d1				/* plane_row*128 */
	add.w	md_col0, d1
	add.w	md_col0, d1			/* +col0*2 (横センタリング) */
	move.l	d5, d0
	andi.l	#0xFFFF, d1
	add.l	d1, d0				/* NT addr */
	bsr	set_vram_write
	move.w	md_tcols, d2
	move.w	d2, d1
	lsr.w	#3, d1
	beq.s	bf_btail
	subq.w	#1, d1
bf_bw:
	move.l	(a1)+, (VDP_DATA).l		/* high word then low word at the VDP data port */
	move.l	(a1)+, (VDP_DATA).l
	move.l	(a1)+, (VDP_DATA).l
	move.l	(a1)+, (VDP_DATA).l
	dbra	d1, bf_bw
bf_btail:
	andi.w	#7, d2				/* preserve arbitrary per-source widths, not just 32/40 */
	beq.s	bf_bdone
	subq.w	#1, d2
bf_bword:
	move.w	(a1)+, (VDP_DATA).l
	dbra	d2, bf_bword
bf_bdone:
	addq.w	#1, d4
	dbra	d6, bf_row

	/* CRAM総入替は flip と同一VBLANKで行う(bf_flip側)。ここで先に書くと、
	   タイルDMAが複数vblankに渡る間「旧フレーム表示×新パレット」が見える
	   (パレット区間切替の瞬間に実機側だけ明るいゴミタイルが出る実バグ)。 */
bf_dma:
	/* Pass2: 表を順に Word-RAM からVRAMへ転送。VBLANK予算(d7)でランをまたいで分割。
	   長runのWord-RAM DMAは先頭1ワードが化ける(実測/Sega文書)ため、src+2/full lengthを
	   dstへDMAした後、チャンク先頭の1ワードをCPUで上書き修復する。短runはCPU直書き。 */
	move.w	n_runs, d4
	beq	bf_flip
	lea	RUN_TABLE, a2
	move.w	(VDP_CTRL).l, d0		/* 現vblank内でなければ次vblankへ */
	btst	#3, d0
	bne	1f
	bsr	wait_vb_start
1:
.ifdef DEBUG
	move.w	(GA_STOPWATCH).l, d0
	move.w	d0, dma_start_tick		/* begin inside the first transfer VBlank */
.endif
	move.w	md_vbudget, d7			/* d7 = 残VBLANK予算(語, モード別) */
bf_run_lp:
	move.w	(a2)+, d3			/* dst(VRAMバイト) */
	move.w	(a2)+, d1			/* len(語, このランの残) */
	movea.l	(a2)+, a3			/* src(Word-RAM) */
.ifdef DMA_RUN_FASTPATH
	/* A one-time run branch is much cheaper than programming a DMA for one or
	   two tiles.  Test the original run length here, never a budget-split tail. */
	cmpi.w	#CPU_DIRECT_MAX_WORDS, d1
	bls.s	bf_short_run
.endif
bf_chunk:
	tst.w	d7				/* 予算切れなら次vblank開始まで待って補充 */
	bgt	1f
	bsr	wait_vb_start
	move.w	md_vbudget, d7
1:
	move.w	d1, d6				/* chunk = min(ラン残, 予算) */
	cmp.w	d7, d6
	bls	2f
	move.w	d7, d6
2:
	bsr	dma_chunk_wr			/* d6語を a3(Word-RAM)→d3 へ(DMA+先頭CPU修復, 完了待ち) */
	sub.w	d6, d7				/* 予算 -= chunk */
	sub.w	d6, d1				/* ラン残 -= chunk */
	add.w	d6, d6				/* chunk*2 = バイト */
	adda.w	d6, a3				/* src += バイト */
	add.w	d6, d3				/* dst += バイト */
.ifdef DMA_RUN_FASTPATH
	tst.w	d1
	beq	bf_run_done			/* usual one-chunk run avoids an extra BRA */
	bra	bf_chunk
.else
	tst.w	d1
	bne	bf_chunk
.endif

.ifdef DMA_RUN_FASTPATH
bf_short_run:
	/* Keep the whole short run in one VBlank.  H40's 3400-word budget leaves
	   an 8-word tail, so a 16/32-word run may need to start in the next blank. */
	cmp.w	d7, d1
	bls.s	1f
	bsr	wait_vb_start
	move.w	md_vbudget, d7
1:
	move.w	d3, d0
	bsr	set_vram_write
	cmpi.w	#16, d1				/* two tiles write one extra 32-byte block */
	beq.s	2f
	.rept 8
	move.l	(a3)+, (VDP_DATA).l
	.endr
2:
	.rept 8
	move.l	(a3)+, (VDP_DATA).l
	.endr
	sub.w	d1, d7
.endif
bf_run_done:
	subq.w	#1, d4
	bne	bf_run_lp
bf_flip:
.ifdef DEBUG
	tst.w	n_runs
	beq.s	1f
	move.w	(GA_STOPWATCH).l, d0
	sub.w	dma_start_tick, d0
	andi.w	#0x0FFF, d0			/* stopwatch wraps naturally after 4096 ticks */
	move.w	d0, dma_elapsed_ticks
1:
	bsr	render_dbg			/* fixed Window最上段を更新(reg2 flipと独立) */
.endif
	/* パレット区間切替: CRAM総入替(64語≈0.1ms)→flip を新しいvblank頭で連続実行=
	   同一VBLANK内で原子的。DEBUGフォントはP0/index15固定なので切替時作業はない。
	   v3: pal = 区間番号+1。CRAM本体はboot時に積んだMain-RAMのPALTAB表から引く
	   (ストリーム到着タイミング非依存=スリップ回復でも色が壊れない)。 */
	move.w	(PROBE_BANK).l, d0		/* pal(=区間番号+1) @ +0 */
	beq	bf_doflip
	cmp.w	md_nseg, d0			/* 壊れた参照対策: 表の範囲外は切替しない */
	bhi	bf_doflip
	subq.w	#1, d0				/* 区間番号 */
	move.w	d0, dbg_seg			/* 絶対値で更新(増分でなく自己修復) */
	lsl.w	#7, d0				/* *128B */
	lea	PALTAB_RAM, a0
	adda.w	d0, a0				/* src = 表[区間] (最大63*128=8064<32767でadda.w可) */
	bsr	wait_vb_start			/* 頭から使える新しいvblank(CRAM+flipが確実に収まる) */
	move.l	#0xC0000000, (VDP_CTRL).l	/* CRAM addr 0 */
	move.w	#64-1, d1
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d1, 1b
	bsr	do_flip				/* CRAM直後・同vblank内にflip */
	bra	bf_after_flip
bf_doflip:
	/* Pattern DMA normally leaves us inside VBlank, but reuse-only frames and
	   the DEBUG Window write can reach here during active display.  A reg2
	   switch there horizontally splices the old and new name tables at the
	   current scanline.  Re-check immediately before the atomic flip; count a
	   newly waited VBlank through wait_vb_start just like a split DMA. */
	move.w	(VDP_CTRL).l, d0
	btst	#3, d0
	bne.s	1f
	bsr	wait_vb_start
1:
	bsr	do_flip
bf_after_flip:
.ifndef DEBUG
	/* Release build has no Sxx HUD, so retain the existing red slip indicator. */
	move.w	(PROBE_BANK+0xAF00).l, d0
	beq	1f
	move.l	#0xC0000000, (VDP_CTRL).l
	move.w	#0x000E, (VDP_DATA).l
1:
.endif
	movem.l	(sp)+, d0-d7/a0-a3
	rts

/* vblankに入るまで待つ(既に中なら即戻る)。trashes d0 */
wait_vb_in:
1:
	move.w	(VDP_CTRL).l, d0
	btst	#3, d0
	beq	1b
	rts

/* 次のvblank開始まで待つ(vblank中なら一度activeを抜けてから)。予算補充用。trashes d0 */
wait_vb_start:
1:
	move.w	(VDP_CTRL).l, d0
	btst	#3, d0
	bne	1b				/* active(非vblank)になるまで */
2:
	move.w	(VDP_CTRL).l, d0
	btst	#3, d0
	beq	2b				/* vblankに入るまで */
	addq.w	#1, vsync_acc			/* v4: 1コマのVBLANK数を計上(表示ペーシング用) */
	rts

/* NT flip: reg2をback_baseへ(1ワード書き=原子的)。d5=back_base。trashes d0 */
do_flip:
	move.l	d5, d0
	lsr.l	#8, d0
	lsr.l	#2, d0				/* back_base>>10 */
	andi.w	#0xFF, d0
	ori.w	#0x8200, d0			/* reg2 = 0x82xx */
	move.w	d0, (VDP_CTRL).l
	eori.w	#1, back_idx			/* 裏を反転 */
	rts

/* d6語を Word-RAM(a3) → VRAM(d3) へDMA。完了待ち。trashes d0,d2
   Word-RAM源はフェッチが1ワード遅延するため、src+2/full lengthを通常dstへDMAし、
   DMAが書かないdst先頭をCPUでa3の先頭ワードから修復する。 */
dma_chunk_wr:
	move.w	#0x8F02, (VDP_CTRL).l		/* autoinc=2 */
	move.w	d6, d2				/* 長さ = chunk 語 */
	move.w	#0x9300, d0
	or.b	d2, d0
	move.w	d0, (VDP_CTRL).l
	lsr.w	#8, d2
	move.w	#0x9400, d0
	or.b	d2, d0
	move.w	d0, (VDP_CTRL).l
	move.l	a3, d2				/* 源 = (src+2)/2 : 1ワード遅延の補正 */
	addq.l	#2, d2
	lsr.l	#1, d2
	move.w	#0x9500, d0
	or.b	d2, d0
	move.w	d0, (VDP_CTRL).l
	lsr.l	#8, d2
	move.w	#0x9600, d0
	or.b	d2, d0
	move.w	d0, (VDP_CTRL).l
	lsr.l	#8, d2
	move.w	#0x9700, d0
	or.b	d2, d0
	move.w	d0, (VDP_CTRL).l
.ifdef DMA_RUN_FASTPATH
	/* Build the normal VRAM-write command once.  CD5 in its low word starts
	   DMA; the preserved command then restores the same destination for the
	   one-word CPU repair without recomputing set_vram_write. */
	move.l	d3, d0
	andi.l	#0x0000FFFF, d0
	move.l	d0, d2
	andi.l	#0x00003FFF, d0
	swap	d0
	ori.l	#0x40000000, d0
	lsr.w	#7, d2
	lsr.w	#7, d2
	andi.w	#0x0003, d2
	or.w	d2, d0				/* d0 = ordinary VRAM-write command */
	move.l	d0, d2				/* preserved across wait_dma_done */
	ori.w	#0x0080, d0			/* CD5: memory-to-VRAM DMA */
	move.l	d0, (VDP_CTRL).l		/* high control word, then CD5 trigger word */
	bsr	wait_dma_done
	/* 先頭1ワードはDMA開始ラッチの古い値(ゴミ)が書かれるため、CPUで上書き修復。
	   (src+2補正で2ワード目以降は正しい。ゴミはチャンク先頭の1ワードのみ) */
	move.l	d2, (VDP_CTRL).l
.else
	move.l	d3, d0				/* dst コマンド(VRAM書込+CD5起動) */
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
	bsr	wait_dma_done
	/* Restore the ordinary destination command before repairing dst[0]. */
	move.w	d3, d0
	bsr	set_vram_write
.endif
	move.w	(a3), (VDP_DATA).l
	rts

/* d6語を Main-RAM(a3) → VRAM(d3=バイトアドレス) へDMA。完了待ち。trashes d0,d2 */
dma_chunk:
	move.w	#0x8F02, (VDP_CTRL).l		/* autoinc=2 */
	move.w	d6, d2				/* 長さ 0x93/94 */
	move.w	#0x9300, d0
	or.b	d2, d0
	move.w	d0, (VDP_CTRL).l
	lsr.w	#8, d2
	move.w	#0x9400, d0
	or.b	d2, d0
	move.w	d0, (VDP_CTRL).l
	move.l	a3, d2				/* 源 = a3/2 (Main-RAM) */
	lsr.l	#1, d2
	move.w	#0x9500, d0
	or.b	d2, d0
	move.w	d0, (VDP_CTRL).l
	lsr.l	#8, d2
	move.w	#0x9600, d0
	or.b	d2, d0
	move.w	d0, (VDP_CTRL).l
	lsr.l	#8, d2
	move.w	#0x9700, d0
	or.b	d2, d0
	move.w	d0, (VDP_CTRL).l
	move.l	d3, d0				/* dst=d3 コマンド(VRAM書込+CD5起動) */
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
	bsr	wait_dma_done
	rts

/* DMA完了待ち(status bit1)。trashes d0 */
wait_dma_done:
1:
	move.w	(VDP_CTRL).l, d0
	btst	#1, d0
	bne	1b
	rts

/* d0 = VRAM addr(<=0xFFFF) -> VDP_CTRL に write コマンド。trashes d0,d2 */
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

cmd_wait_ready:
	move.w	d0, (GA_COMCMD0).l
1:
	cmp.w	#STAT_READY, (GA_COMSTAT0).l
	bne	1b
	move.w	#0, (GA_COMCMD0).l
2:
	tst.w	(GA_COMSTAT0).l
	bne	2b
	rts

/* CMD_SWAP送信 → STAT_READY(通常) か STAT_END(映画終端) を待つ。d0=受けたSTAT */
swap_or_end:
.ifdef DEBUG
	move.w	(VDP_HV).l, d1
	lsr.w	#8, d1				/* V-counter at CMD_SWAP request */
.endif
	move.w	#CMD_SWAP, (GA_COMCMD0).l
1:
	move.w	(GA_COMSTAT0).l, d0
	cmp.w	#STAT_READY, d0
	beq	2f
	cmp.w	#STAT_END, d0
	bne	1b
2:
.ifdef DEBUG
	move.w	(VDP_HV).l, d2
	lsr.w	#8, d2
	sub.w	d1, d2
	andi.w	#0x00FF, d2			/* approximate elapsed scanlines */
	move.w	d2, sub_wait_lines
.endif
	move.w	#0, (GA_COMCMD0).l
3:
	tst.w	(GA_COMSTAT0).l
	bne	3b
	rts

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

/* 独立Window planeの最上段1行にデバッグHUDを連続書きする。
   H32: FxxxxPxxSxxDxxRxxLxxCxxWxxMxxAxx = 32 words.
   H40: the same 32 words followed by UxxxxNxx = 40 words.
	F/Uは16-bit、Lはleadのhigh byte、他はlow byteの2桁。Lは256B単位。
   WIN_NTは固定なのでNT0/NT1の裏表flipに影響されない。 */
render_dbg:
	movem.l	d0-d4, -(sp)
	/* WIN_NT has no back buffer.  VBlank is safe immediately; during active
	   display, only scanlines 0..7 can race this top-row update.  If currently
	   there, wait just until V-counter reaches 8 instead of waiting a full frame. */
	move.w	(VDP_CTRL).l, d0
	btst	#3, d0
	bne	1f
0:
	move.w	(VDP_HV).l, d0			/* V-counter is the high byte */
	cmpi.w	#0x0800, d0
	blo	0b
1:
	move.l	#WIN_NT, d0
	bsr	set_vram_write			/* one address setup, then 32/40 sequential writes */
	/* F: frame number, 4 digits */
	move.w	#15, d3				/* glyph 'F'(=hex F) */
	move.w	frame_no, d4
	bsr	dbg_put4
	/* P: palette segment, low byte */
	move.w	#19, d3				/* glyph 'P' */
	move.w	dbg_seg, d4
	bsr	dbg_put2
	/* S: slip/reseek count, low byte */
	move.w	#23, d3				/* glyph 'S' */
	move.w	(PROBE_BANK+0xAF00).l, d4
	bsr	dbg_put2
	/* D: desync count, low byte */
	move.w	#13, d3				/* glyph 'D'(=hex D) */
	move.w	(PROBE_BANK+0xAF7E).l, d4
	bsr	dbg_put2
	/* R: audio re-sync count, low byte */
	move.w	#16, d3				/* glyph 'R' */
	move.w	(PROBE_BANK+0xAF20).l, d4
	bsr	dbg_put2
	/* L: current audio lead high byte (256-byte units) */
	move.w	#21, d3				/* glyph 'L' */
	move.w	(PROBE_BANK+0xAF22).l, d4
	lsr.w	#8, d4
	bsr	dbg_put2
	/* C: total blocking CD pumps (current control + older BODY slot) */
	move.w	#12, d3				/* glyph 'C'(=hex C) */
	move.w	(PROBE_BANK+0xAF18).l, d4
	add.w	(PROBE_BANK+0xAF1A).l, d4
	bsr	dbg_put2
	/* W: Main's CMD_SWAP wait for Sub completion, in approximate scanlines */
	move.w	#17, d3				/* glyph 'W' */
	move.w	sub_wait_lines, d4
	bsr	dbg_put2
	/* M: VBlank starts waited by this frame's Main-side pattern path */
	move.w	#18, d3				/* glyph 'M' */
	move.w	vsync_acc, d4
	bsr	dbg_put2
	/* A: startup-audio duplicate chunks still skipped after this frame */
	move.w	#10, d3				/* glyph 'A'(=hex A) */
	move.w	(PROBE_BANK+0xAF1C).l, d4
	bsr	dbg_put2
	/* H40 has exactly eight additional visible Window cells.  Keep the shared
	   H32 prefix stable and use the tail for direct Main/DMA correlation. */
	cmpi.w	#1, md_mode
	bne.s	1f
	move.w	#20, d3				/* glyph 'U': Main pattern-transfer stopwatch ticks */
	move.w	dma_elapsed_ticks, d4
	bsr	dbg_put4
	move.w	#27, d3				/* glyph 'N': cold-run descriptor count */
	move.w	n_runs, d4
	bsr	dbg_put2
1:
	movem.l	(sp)+, d0-d4
	rts

/* d3=label glyph, d4=value. Append label+4 digits to the current Window write. */
dbg_put4:
	moveq	#3, d2
	bra	dbg_put_digits

/* Append label+low-byte 2 digits. Rotate the low byte into the same high-to-low
   nibble walk used by dbg_put4; no clamp or counter mutation is needed. */
dbg_put2:
	rol.w	#8, d4
	moveq	#1, d2
dbg_put_digits:
	move.w	d3, d1				/* label */
	add.w	md_font_vtile, d1
	move.w	d1, (VDP_DATA).l
1:
	rol.w	#4, d4
	move.w	d4, d1
	andi.w	#0xF, d1
	add.w	md_font_vtile, d1
	move.w	d1, (VDP_DATA).l
	dbra	d2, 1b
	rts

	.data
	.align 2
palettes:
	.incbin "palettes.bin"
dbgfont:
	.incbin "dbgfont.bin"

	.bss
	.align 2
shadow:
	.space 1120*2				/* 最大グリッド(H40 40x28)ぶん */
md_mode:
	.space 2
md_vsync_n:
	.space 2				/* v4: 1コマの表示VBLANK数(15fps=4, 30fps=2) */
vsync_acc:
	.space 2				/* v4: 現コマで消費したVBLANK数(ペーシング用) */
md_tcols:
	.space 2
md_trows:
	.space 2
md_bmbytes:
	.space 2				/* ceil(cells/8); supported grids divide exactly */
md_row0:
	.space 2
md_col0:
	.space 2
md_vbudget:
	.space 2
md_font_vtile:
	.space 2
md_font_addr:
	.space 4
back_idx:
	.space 2
frame_no:
	.space 2
started:
	.space 2
n_runs:
	.space 2
dbg_seg:
	.space 2
sub_wait_lines:
	.space 2				/* DEBUG HUD W: Main wait for Sub at last bank swap */
dma_elapsed_ticks:
	.space 2				/* DEBUG H40 Uxxxx: 30.72 us stopwatch ticks */
dma_start_tick:
	.space 2				/* DEBUG stopwatch sample at first pattern transfer */
md_nseg:
	.space 2				/* PALTAB区間数(表コピー時にクランプ済み) */
.ifdef MAIN_CODEGEN
md_codegen:
	.space 2				/* 1 only after the complete runtime proof succeeds */
md_codegen_blit:
	.space 2				/* Phase 2 geometry/range proof succeeded */
md_codegen_blit_addr:
	.space 8				/* NT0 and NT1 generated entry addresses */
md_codegen_end:
	.space 4				/* generated end address, including failed attempts */
.endif
