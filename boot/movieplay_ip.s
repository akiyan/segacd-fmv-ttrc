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
.equ GA_COMSTAT1, 0x00A12022
.equ GA_STOPWATCH, 0x00A1200C		/* 12-bit, 30.72 us/tick, Main read-only */

.equ PROBE_BANK, 0x00200000

.equ CMD_STREAM, 0x50
.equ CMD_SWAP,   0x51
.equ STAT_READY, 0x8003
.equ STAT_END,   0x8004			/* SPからの映画終端通知(15秒待って再ループ) */

.equ NT0, 0xC000
.equ NT1, 0xE000

/* 0xFF2000..0xFF65FF is no longer a tile staging buffer: streamed pattern DMA
   reads Word RAM directly and repairs the first destination word on the CPU.
   Keep this range for boot-time Main-CPU code generation, then use the gap up
   to RUN_TABLE as the immutable MainBuf pattern preload. */
.equ MAIN_CODEGEN_BASE,  0x00FF2000
.equ RUN_TABLE,          0x00FF8000	/* (dst.w,len.w,src.l) cold-run records; 0x3000B capacity */
.equ MAIN_BUF,           0x00FF6600	/* boot-preloaded patterns; direct Main-RAM VDP DMA */
.equ MAIN_BUF_END,       RUN_TABLE
.equ MAIN_BUF_PATTERNS,  208
.equ MAIN_CODEGEN_LIMIT, MAIN_BUF
.equ MAIN_CODEGEN_TABLE_BYTES, 0x0200	/* 256 signed word offsets */
.equ MAIN_CODEGEN_HANDLER_MAX, 70	/* mask FF: guarded before writing */
.equ MAIN_CODEGEN_EXPECTED_END, 0x00FF4900
.equ MAIN_CODEGEN_BLITTER_MAX, 7296	/* H40 40x28, NT0+NT1 */
.equ WORD_BUF_OFF,       0x15200		/* same offset in physical Wr0/Wr1 banks */
.equ WORD_BUF_END,       0x1C000
.equ WORD_BUF_PATTERNS,  880
.equ MAIN_STAGE_OFF,     0xD000		/* frame0 Word-RAM handoff staging for MainBuf */

/* Exact 68000 words emitted by init_main_codegen.  Keep synchronized with
   harness/main_codegen/verify_handlers.py. */
.equ CG_OP_MOVE_ENTRY_D3,      0x3618	/* move.w (a0)+,d3 */
.equ CG_OP_STRIP_COLD_D6_D3,   0xC646	/* and.w d6,d3 */
.equ CG_ENTRY_MASK_LONG,       0x67FF67FF
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
/* DEBUG HUD: only hexadecimal glyphs, directly above the resident pool. */
.equ DBGFONT_N, 16
/* フォントVRAM位置はヘッダの base+pool 直上を実行時に計算(md_font_vtile/md_font_addr) */
/* リリースビルドが既定。make movieplay DEBUG=1 でオーバーレイ一式を有効化
   (ストリーム側は CBRSIM_PACK_DEBUG=1 でデバッグ欄ありを生成) */
/* CRAM pre-load: 全区間パレット表。boot時にWord-RAM(PALTAB_OFF, frame0バンク)から一度だけ
   コピーし、以降の区間切替はO_PALWの区間番号+1でこの表を引く(ストリーム到着に依存しない)。
   容量はav_config.PALTAB_MAX_SEGと一致必須(check_player_ring.pyがビルド時検証)。 */
.equ PALTAB_OFF, 0xB000			/* Word-RAM内ステージ位置(sp.sと一致必須) */
.equ PALTAB_MAX_SEG, 64			/* Main-RAM表の容量(区間数)。64*128B=8KB */
.equ PALTAB_RAM, 0x00FFB000		/* 表本体 0xFFB000..0xFFD000; high BSS follows */
/* 1VBLANKで安全に転送できる語数はモード別(md_vbudget)。実測(dmabench)に基づき保守的に。
   これを超える転送はランをまたいで次VBLANKへ分割=active表示中へのはみ出し防止(ares対策)。 */
.equ VB_WORDS_H32, 2800		/* H32 V28 NTSC */
.equ VB_WORDS_H40, 3400		/* H40 V28 NTSC(理論~3895語より保守的) */
.equ CPU_DIRECT_MAX_WORDS, 32	/* 1-2 tiles: CPU writes beat per-run DMA setup */
.equ FEATURE_FIXED_N2_BIT, 1	/* header features bit 1 */
.equ FEATURE_PATTERN_SUPPLY_BIT, 3
.equ PACE_N2_ARM_TICKS, 800	/* 24.576ms: safely between VBlank 1 and 2 */

.ifdef DEBUG
.ifdef PLAYER_SPECIALIZED
.equ HUD_HEX_TABLE, 1
.endif
.endif

.ifdef PLAYER_SPECIALIZED
	.include "player_constants.inc"
.endif

.macro PC_MOVE_W runtime, constant, dest
.ifdef PLAYER_SPECIALIZED
	move.w	#\constant, \dest
.else
	move.w	\runtime, \dest
.endif
.endm

.macro PC_MOVE_L runtime, constant, dest
.ifdef PLAYER_SPECIALIZED
	move.l	#\constant, \dest
.else
	move.l	\runtime, \dest
.endif
.endm

.macro PC_CMP_W runtime, constant, dest
.ifdef PLAYER_SPECIALIZED
	cmpi.w	#\constant, \dest
.else
	cmp.w	\runtime, \dest
.endif
.endm

.macro PC_ADD_W runtime, constant, dest
.ifdef PLAYER_SPECIALIZED
	addi.w	#\constant, \dest
.else
	add.w	\runtime, \dest
.endif
.endm

.macro PC_ADDA_W runtime, constant, dest
.ifdef PLAYER_SPECIALIZED
	adda.w	#\constant, \dest
.else
	adda.w	\runtime, \dest
.endif
.endm

/* The specialized DEBUG player knows the HUD font tile base at assembly time.
   Map one byte directly to its two name-table words, avoiding two nibble
   conversions and all formatter calls in the per-frame deadline. */
.macro DBG_PUT2
.ifdef HUD_HEX_TABLE
	andi.w	#0x00FF, d4
	add.w	d4, d4			/* *4: two ADDs beat LSL.W #2 by 2 clocks */
	add.w	d4, d4
	move.l	(a1,d4.w), (a0)+
.else
	bsr	dbg_put2
.endif
.endm

.macro DBG_PUT4
.ifdef HUD_HEX_TABLE
	move.w	d4, d3
	lsr.w	#8, d4
	DBG_PUT2
	move.w	d3, d4
	DBG_PUT2
.else
	bsr	dbg_put4
.endif
.endm

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

.ifdef PLAYER_SPECIALIZED
.if PC_MODE == 1
	move.w	#0x8C81, (VDP_CTRL).l		/* show the preload screen in H40 too */
.endif
	bsr	draw_startup
.else
	bsr	load_movie_palette
.endif

	jsr	BIOS_VDP_DISP_ENABLE
	move.w	#0x8174, (VDP_CTRL).l		/* reg1: 表示on+vint+DMA許可(M1)+mode5 */

	clr.w	dbg_seg
	clr.w	display_blank			/* .bss is not cleared by the BIOS */

	clr.w	back_idx			/* 裏=NT0(0) から構築, 表示=NT1 */

	move.w	#CMD_STREAM, d0
.ifdef PLAYER_SPECIALIZED
	bsr	cmd_wait_startup
	/* The SGDK startup font and the movie HUD share the transient font range.
	   Hide all initialization that replaces it, then reveal frame 0 only after
	   its complete Plane A table has been selected in do_flip. */
	move.w	#0x8134, (VDP_CTRL).l		/* display off; keep VInt, DMA and mode 5 */
	move.w	#1, display_blank
	bsr	load_movie_palette		/* replace temporary UI colours before frame 0 */
.else
	bsr	cmd_wait_ready
.endif

	/* frame0準備完了=バンクにヘッダ写し(O_HDR)がある。mode/tcols/trows/pool/base を読み
	   モード依存のVDP設定と実行時変数を確定する(汎用化: H32/H40, mode4は将来) */
	lea	(PROBE_BANK+0xAF80), a0
.ifndef PLAYER_SPECIALIZED
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
	/* v8 feature bit 1 is the authoritative fixed-N2 contract. Force N=2
	   from that bit so a stale hint cannot desynchronise display and BODY. */
	clr.w	md_fixed_n2
	btst	#FEATURE_FIXED_N2_BIT, 63(a0)
	beq	1f
	moveq	#2, d0
	move.w	#1, md_fixed_n2
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
.else
.if PC_MODE == 0
	move.w	#0x8C00, (VDP_CTRL).l		/* generated H32 profile */
.elseif PC_MODE == 1
	move.w	#0x8C81, (VDP_CTRL).l		/* generated H40 profile */
.else
	.error "unsupported generated player mode"
.endif
.endif
	/* DEBUG HUD is embedded into the inactive Plane A table after its full movie
	   blit. Disable the Window region explicitly: a Window's transparent pixels
	   expose Plane B, not Plane A, and previously showed stale/wrong-parity data. */
.ifdef DEBUG
	move.w	#0x9100, (VDP_CTRL).l		/* reg17: left of column-pair 0 = no side strip */
	move.w	#0x9200, (VDP_CTRL).l		/* reg18: rows above 0 = no top strip */
.endif
.ifndef PLAYER_SPECIALIZED
	move.w	d3, md_vbudget
	sub.w	md_tcols, d2			/* col0 = (screen_cols-tcols)/2 */
	lsr.w	#1, d2
	move.w	d2, md_col0
	move.w	#28, d2				/* screen_rows(H32/H40) */
	sub.w	md_trows, d2			/* row0 = (screen_rows-trows)/2 */
	lsr.w	#1, d2
	move.w	d2, md_row0
.endif
.ifdef MAIN_CODEGEN
	/* Generate once, before playback.  A failed range/size proof leaves
	   md_codegen=0 and the per-bit reference path remains active. */
	bsr	init_main_codegen
.endif
	/* CRAM pre-load: PALTAB(全区間パレット)をWord-RAM(frame0バンク)からMain-RAM表へ
	   一度だけコピー。n_seg=O_HDR+20。以降の区間切替はこの表を引くだけ(bf_flip)。 */
	PC_MOVE_W 20(a0), PC_NSEG, d1		/* n_seg */
.ifndef PLAYER_SPECIALIZED
	cmp.w	#PALTAB_MAX_SEG, d1		/* 壊れたヘッダ対策: 表容量にクランプ */
	bls	1f
	move.w	#PALTAB_MAX_SEG, d1
1:
	move.w	d1, md_nseg
.endif
	lsl.w	#6, d1				/* n_seg*64語(=128B) */
	beq	2f
	subq.w	#1, d1
	lea	(PROBE_BANK+PALTAB_OFF).l, a1
	lea	PALTAB_RAM, a2
1:
	move.w	(a1)+, (a2)+
	dbra	d1, 1b
2:
	/* v10 MainBuf is staged beside PALTAB in the frame0 Word-RAM bank.  Copy it
	   once into the fixed Main-RAM gap after codegen; Wr0/Wr1 remain in their
	   physical banks and are read directly after each handoff. */
.ifdef PLAYER_SPECIALIZED
.if (PC_FEATURES & 0x0008)
.if PC_MAIN_PATTERNS > 0
	lea	(PROBE_BANK+MAIN_STAGE_OFF).l, a1
	lea	MAIN_BUF, a2
	move.w	#PC_MAIN_PATTERNS*8-1, d1
1:
	move.l	(a1)+, (a2)+
	dbra	d1, 1b
.endif
	bsr	reset_pattern_supply
.endif
.endif
	/* Upload the 16-glyph DEBUG font once. Expand source index 1 to P0/index15
	   (brightest) and source index 0 to P0/index1 (darkest). */
.ifdef DEBUG
	PC_MOVE_L md_font_addr, PC_FONT_ADDR, d0
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
.endif
	/* With display disabled, some VDP implementations keep the VBlank status
	   asserted and the first frame's VBlank waits cannot advance. After every
	   startup-font replacement is complete, erase the old startup table while
	   still hidden, then re-enable a clean black front plane. Frame 0 is built
	   into NT0 and replaces this black transition at its normal atomic flip. */
	tst.w	display_blank
	beq.s	2f
	move.l	#NT1, d0
	bsr	set_vram_write
	moveq	#0, d0
	move.w	#64*32-1, d1
1:
	move.w	d0, (VDP_DATA).l
	dbra	d1, 1b
	move.w	#0x8174, (VDP_CTRL).l		/* display on + VInt + DMA + mode 5 */
	clr.w	display_blank
2:

	clr.w	frame_no
	clr.w	started
	clr.w	vsync_acc			/* v4: ペーシングカウンタ初期化(.bssはMD上でクリアされない) */
	bsr	prime_fixed_cadence		/* frame0 has no preceding movie flip */
.ifdef DEBUG
	clr.w	sub_wait_lines
	clr.w	dma_elapsed_ticks
	clr.w	dma_start_tick
.endif
play_loop:
	/* v8: feature bit 1ならSubの1001/400 sector rateと対になるflip直前N2
	   deadlineで1/3 VBlankの表示揺れを除く。bit clearの24/15fpsはCD配送律速。 */
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
.ifdef PLAYER_SPECIALIZED
.if (PC_FEATURES & 0x0008)
	bsr	reset_pattern_supply
.endif
.endif
	bsr	prime_fixed_cadence		/* 15s tail already satisfies frame0 cadence */
	bra	play_loop

.ifdef PLAYER_SPECIALIZED
.if (PC_FEATURES & 0x0008)
reset_pattern_supply:
	move.l	#PROBE_BANK+WORD_BUF_OFF, wr_ptr0
	move.l	#PROBE_BANK+WORD_BUF_OFF, wr_ptr1
	move.l	#MAIN_BUF, main_ptr
	rts
.endif
.endif

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
	PC_MOVE_W md_mode, PC_MODE, d0
	cmpi.w	#1, d0
	bhi	10f
	move.w	#32, d1
	tst.w	d0
	beq	11f
	move.w	#40, d1
11:
	PC_MOVE_W md_tcols, PC_TCOLS, d0
	beq	10f
	cmp.w	d1, d0
	bhi	10f
	PC_MOVE_W md_col0, PC_COL0, d2
	add.w	d0, d2
	cmp.w	d1, d2
	bhi	10f
	PC_MOVE_W md_trows, PC_TROWS, d0
	beq	10f
	cmpi.w	#28, d0
	bhi	10f
	PC_MOVE_W md_row0, PC_ROW0, d2
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
	PC_MOVE_W md_row0, PC_ROW0, d4
	PC_MOVE_W md_trows, PC_TROWS, d5
	subq.w	#1, d5
1:
	/* Precompute the exact command produced by set_vram_write for this row. */
	moveq	#0, d0
	move.w	d4, d0
	lsl.w	#7, d0				/* plane row * 128 bytes */
	PC_MOVE_W md_col0, PC_COL0, d1
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

	PC_MOVE_W md_tcols, PC_TCOLS, d2
	lsr.w	#1, d2				/* two name-table words per MOVE.L */
	beq	3f
	subq.w	#1, d2
2:
	move.w	#CG_OP_MOVE_L_A1_ABS, (a0)+
	move.l	#VDP_DATA, (a0)+
	dbra	d2, 2b
3:
	PC_MOVE_W md_tcols, PC_TCOLS, d2
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
	clr.w	frame_vblank_waits
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
	move.w	(a0)+, d6			/* source bits15..14 + count bits13..0 */
	move.w	d6, d3
	andi.w	#0xC000, d3			/* 0=Prg inline, 1=Wr current bank, 2=Main */
	andi.w	#0x3FFF, d6
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
	moveq	#0, d2				/* source bytes = count*32 */
	move.w	d6, d2
	lsl.l	#5, d2
	tst.w	d3
	bne	bf_stage_preload
	move.l	a0, (a2)+			/* Prg: Sub copied inline bytes into O_LOADS */
	adda.l	d2, a0
	bra	bf_stage_recorded
bf_stage_preload:
	cmpi.w	#0x4000, d3
	bne	bf_stage_main
	move.w	frame_no, d1			/* Wr0 on even frames, Wr1 on odd frames */
	andi.w	#1, d1
	lsl.w	#2, d1
	lea	wr_ptr0, a1
	movea.l	(a1,d1.w), a3
	move.l	a3, d0
	add.l	d2, d0
	cmpi.l	#PROBE_BANK+WORD_BUF_END, d0
	bhi	bf_stage_done			/* corrupt cache count: do not walk into routing */
	move.l	a3, (a2)+
	move.l	d0, (a1,d1.w)
	bra	bf_stage_recorded
bf_stage_main:
	cmpi.w	#0x8000, d3
	bne	bf_stage_done			/* source 3 is reserved */
	movea.l	main_ptr, a3
	move.l	a3, d0
	add.l	d2, d0
	cmpi.l	#MAIN_BUF_END, d0
	bhi	bf_stage_done
	move.l	a3, (a2)+
	move.l	d0, main_ptr
bf_stage_recorded:
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
	PC_ADDA_W md_bmbytes, PC_BMBYTES, a0	/* entries */
	lea	shadow, a1
	PC_MOVE_W md_bmbytes, PC_BMBYTES, d5
	subq.w	#1, d5
.ifdef MAIN_CODEGEN
	/* The fixed flag check is the only generated success-path overhead.  The
	   fallback branches around the generated loop; the successful loop falls
	   directly into bf_blit. */
	move.w	(md_codegen).l, d0
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
	andi.w	#0x67FF, d3			/* strip cold and Prg/Wr/Main source bits */
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
	andi.w	#0x67FF, d3
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
	move.w	(md_codegen_blit).l, d0
	beq	bf_blit_reference
	move.w	(back_idx).l, d0
	lsl.w	#2, d0
	lea	(md_codegen_blit_addr).l, a3
	movea.l	(a3,d0.w), a3
	jsr	(a3)
	bra	bf_dma
bf_blit_reference:
.endif
	lea	shadow, a1
	PC_MOVE_W md_row0, PC_ROW0, d4	/* plane_row = (screen_rows-trows)/2 */
	PC_MOVE_W md_trows, PC_TROWS, d6
	subq.w	#1, d6
bf_row:
	move.w	d4, d1
	lsl.w	#7, d1				/* plane_row*128 */
.ifdef PLAYER_SPECIALIZED
.if PC_COL0 != 0
	addi.w	#PC_COL0*2, d1			/* generated horizontal centering */
.endif
.else
	add.w	md_col0, d1
	add.w	md_col0, d1			/* +col0*2 (横センタリング) */
.endif
	move.l	d5, d0
	andi.l	#0xFFFF, d1
	add.l	d1, d0				/* NT addr */
	bsr	set_vram_write
	PC_MOVE_W md_tcols, PC_TCOLS, d2
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
	PC_MOVE_W md_vbudget, PC_VBUDGET, d7	/* d7 = 残VBLANK予算(語) */
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
	PC_MOVE_W md_vbudget, PC_VBUDGET, d7
1:
	move.w	d1, d6				/* chunk = min(ラン残, 予算) */
	cmp.w	d7, d6
	bls	2f
	move.w	d7, d6
2:
	cmpa.l	#MAIN_BUF, a3			/* MainBuf has normal DMA; Prg/Wr sources are Word RAM */
	bcs.s	3f
	bsr	dma_chunk
	bra.s	4f
3:
	bsr	dma_chunk_wr			/* Word-RAM DMA + first-word repair */
4:
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
	PC_MOVE_W md_vbudget, PC_VBUDGET, d7
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
	move.w	vsync_acc, frame_vblank_waits	/* exclude display pacing from workload HUD M */
.endif
	/* Precompute the display-register write before the cadence wait.
	   do_flip performs only a final VBlank check followed by this command, so
	   the check-to-reg2 race is a few bus cycles instead of an address/branch
	   calculation at the end of VBlank. */
	move.l	d5, d0
	lsr.l	#8, d0
	lsr.l	#2, d0				/* back_base>>10 */
	andi.w	#0xFF, d0
	ori.w	#0x8200, d0
	move.w	d0, d5				/* prebuilt reg2 word */
	/* パレット区間切替: CRAM総入替(64語≈0.1ms)→flip を新しいvblank頭で連続実行=
	   同一VBLANK内で原子的。DEBUGフォントはP0/index15固定なので切替時作業はない。
	   v3: pal = 区間番号+1。CRAM本体はboot時に積んだMain-RAMのPALTAB表から引く
	   (ストリーム到着タイミング非依存=スリップ回復でも色が壊れない)。 */
	move.w	(PROBE_BANK).l, d0		/* pal(=区間番号+1) @ +0 */
	beq	bf_doflip
	PC_CMP_W md_nseg, PC_NSEG, d0	/* 壊れた参照対策: 表の範囲外は切替しない */
	bhi	bf_doflip
	subq.w	#1, d0				/* 区間番号 */
	move.w	d0, dbg_seg			/* 絶対値で更新(増分でなく自己修復) */
	lsl.w	#7, d0				/* *128B */
	lea	PALTAB_RAM, a0
	adda.w	d0, a0				/* src = 表[区間] (最大63*128=8064<32767でadda.w可) */
.ifdef DEBUG
	bsr	prepare_dbg			/* build the inactive HUD row before the deadline */
	bsr	publish_dbg
.endif
.ifdef PLAYER_SPECIALIZED
.if (PC_FEATURES & 0x0002) != 0
	bsr	wait_fixed_palette_flip		/* cadence target plus a fresh CRAM VBlank */
.else
	bsr	wait_vb_start			/* 頭から使える新しいvblank(CRAM+flipが確実に収まる) */
.endif
.else
	tst.w	md_fixed_n2
	beq.s	1f
	bsr	wait_fixed_palette_flip		/* cadence target plus a fresh CRAM VBlank */
	bra.s	2f
1:
	bsr	wait_vb_start			/* 頭から使える新しいvblank(CRAM+flipが確実に収まる) */
2:
.endif
	move.l	#0xC0000000, (VDP_CTRL).l	/* CRAM addr 0 */
	move.w	#64-1, d1
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d1, 1b
	bsr	do_flip				/* CRAM直後・同vblank内にflip */
	bra	bf_after_flip
bf_doflip:
	/* Pattern DMA normally leaves us inside VBlank, but reuse-only frames and
	   the DEBUG Plane A HUD write can reach here during active display.  A reg2
	   switch there horizontally splices the old and new name tables at the
	   current scanline.  Build the HUD row in Main RAM and copy it into the
	   inactive video name table before the cadence wait.  The target VBlank then
	   switches reg2, so the fixed 11/14-MOVE.L HUD copy is
	   off the display deadline and cannot lead or defer the picture.  Re-check
	   immediately before the atomic flip; count a newly waited VBlank through
	   wait_vb_start just like a split DMA. */
.ifdef DEBUG
	bsr	prepare_dbg
	bsr	publish_dbg
.endif
.ifdef PLAYER_SPECIALIZED
.if (PC_FEATURES & 0x0002) != 0
	bsr	wait_fixed_flip			/* normal frame: exactly N flip-to-flip VBlanks */
.endif
.else
	tst.w	md_fixed_n2
	beq.s	1f
	bsr	wait_fixed_flip			/* normal frame: exactly N flip-to-flip VBlanks */
1:
.endif
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

/* Make frame 0 immediately eligible: its synthetic preceding flip is one
   midpoint threshold in the past. Trashes d0. */
prime_fixed_cadence:
.ifdef PLAYER_SPECIALIZED
.if (PC_FEATURES & 0x0002) != 0
	move.w	(GA_STOPWATCH).l, d0
	sub.w	#PACE_N2_ARM_TICKS, d0
	andi.w	#0x0FFF, d0
	move.w	d0, pace_flip_tick
.endif
.else
	tst.w	md_fixed_n2
	beq.s	1f
	move.w	(GA_STOPWATCH).l, d0
	sub.w	#PACE_N2_ARM_TICKS, d0
	andi.w	#0x0FFF, d0
	move.w	d0, pace_flip_tick
1:
.endif
	rts

/* The stopwatch midpoint is safely after VBlank 1 ends and before VBlank 2
   begins for any legal flip phase.  do_flip performs the authoritative VBlank
   and end-of-blank guard immediately beside the precomputed register write. */
wait_fixed_flip:
1:
	move.w	(GA_STOPWATCH).l, d0
	sub.w	pace_flip_tick, d0
	andi.w	#0x0FFF, d0
	cmpi.w	#PACE_N2_ARM_TICKS, d0
	bcc.s	2f
	bra.s	1b
2:
	rts

/* CRAM replacement needs a fresh VBlank. At the midpoint we are between the
   first and second VBlanks, so the next fresh start is exactly VBlank 2. */
wait_fixed_palette_flip:
1:
	move.w	(GA_STOPWATCH).l, d0
	sub.w	pace_flip_tick, d0
	andi.w	#0x0FFF, d0
	cmpi.w	#PACE_N2_ARM_TICKS, d0
	bcc.s	2f
	bra.s	1b
2:
	bsr	wait_vb_start
	rts

/* Final display flip. d5 is the precomputed reg2 word. Re-check VBlank here,
   immediately next to the control-port write, so an end-of-blank race cannot
   defer an otherwise on-time frame.  trashes d0. */
do_flip:
	/* Accept the target VBlank even when frame work reached it after the midpoint,
	   but never accept its final four V-counter lines.  The NTSC counter is not
	   monotonic across all VBlank lines, yet FC..FF is always the terminal tail.
	   Re-read status after HV so a boundary between the first two reads is also
	   caught.  A guarded/fresh return from wait_vb_start has the full blank. */
	move.w	(VDP_CTRL).l, d0
	btst	#3, d0
	beq.s	2f
	move.w	(VDP_HV).l, d0
	cmpi.w	#0xFC00, d0
	bhs.s	2f
	move.w	(VDP_CTRL).l, d0
	btst	#3, d0
	bne.s	1f
2:
	bsr	wait_vb_start
1:
	move.w	d5, (VDP_CTRL).l
	eori.w	#1, back_idx			/* 裏を反転 */
.ifdef PLAYER_SPECIALIZED
.if (PC_FEATURES & 0x0002) != 0
	move.w	(GA_STOPWATCH).l, pace_flip_tick	/* exact flip-to-flip deadline */
.endif
.else
	tst.w	md_fixed_n2
	beq.s	1f
	move.w	(GA_STOPWATCH).l, pace_flip_tick	/* exact flip-to-flip deadline */
1:
.endif
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

load_movie_palette:
	move.l	#0xC0000000, (VDP_CTRL).l
	lea	palettes, a0
	move.w	#64-1, d0
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d0, 1b
	rts

.ifdef PLAYER_SPECIALIZED
/* The preload UI uses only temporary CRAM, name-table and font VRAM. It does
   not reserve or reduce PrgBuf, APPLY, WordBuf, MainBuf, or the movie pool. */
draw_startup:
	movem.l	d0-d5/a0-a1, -(sp)
	move.l	#0xC0000000, (VDP_CTRL).l
	lea	startup_palette, a0
	move.w	#64-1, d0
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d0, 1b

	move.l	#PC_FONT_ADDR, d0
	bsr	set_vram_write
	lea	startup_font_bits, a0
	lea	startup_nibble_words, a1
	move.w	#STARTUP_FONT_N*8-1, d4
1:
	moveq	#0, d0
	move.b	(a0)+, d0
	move.w	d0, d1
	lsr.w	#4, d1
	add.w	d1, d1
	move.w	(a1,d1.w), (VDP_DATA).l
	andi.w	#0x000F, d0
	add.w	d0, d0
	move.w	(a1,d0.w), (VDP_DATA).l
	dbra	d4, 1b

	lea	startup_lines, a0
2:
	moveq	#0, d0
	move.b	(a0)+, d0			/* row or 0xFF terminator */
	cmpi.b	#0xFF, d0
	beq.s	5f
	lsl.w	#7, d0			/* 64-cell plane row = 128 bytes */
	moveq	#0, d1
	move.b	(a0)+, d1			/* column */
	add.w	d1, d1
	add.w	d1, d0
	addi.l	#NT1, d0
	moveq	#0, d3
	move.b	(a0)+, d3			/* palette number */
	lsl.w	#8, d3
	lsl.w	#5, d3			/* name-table palette bits 13..14 */
	moveq	#0, d4
	move.b	(a0)+, d4			/* glyph count */
	bsr	set_vram_write
	subq.w	#1, d4
3:
	moveq	#0, d0
	move.b	(a0)+, d0
	addi.w	#PC_FONT_VTILE, d0
	or.w	d3, d0
	move.w	d0, (VDP_DATA).l
	dbra	d4, 3b
	bra.s	2b
5:
	movem.l	(sp)+, d0-d5/a0-a1
	rts

/* d0.w = remaining 2-KiB PrgBuf preload sectors. */
startup_update_prg:
	movem.l	d0-d5, -(sp)
	move.w	#PC_PREBUF_SEC, d4
	sub.w	d0, d4
	add.w	d4, d4			/* sectors -> KiB */
	move.w	d4, d3			/* preserve loaded KiB across set_vram_write */
	move.l	#STARTUP_PRG_VALUE_ADDR, d0
	bsr	set_vram_write
	moveq	#0, d5
	move.w	d4, d5
	divu.w	#100, d5
	move.w	d5, d0			/* hundreds */
	addi.w	#PC_FONT_VTILE+STARTUP_GLYPH_0, d0
	ori.w	#0x6000, d0			/* amber streaming preload line */
	move.w	d0, (VDP_DATA).l
	swap	d5				/* remainder */
	moveq	#0, d4
	move.w	d5, d4
	divu.w	#10, d4
	move.w	d4, d0			/* tens */
	addi.w	#PC_FONT_VTILE+STARTUP_GLYPH_0, d0
	ori.w	#0x6000, d0
	move.w	d0, (VDP_DATA).l
	swap	d4				/* ones */
	move.w	d4, d0
	addi.w	#PC_FONT_VTILE+STARTUP_GLYPH_0, d0
	ori.w	#0x6000, d0
	move.w	d0, (VDP_DATA).l

	/* Redraw the 30-cell object-style progress bar from the same live count. */
	moveq	#0, d5
	move.w	d3, d5
	mulu.w	#30, d5
	divu.w	#STARTUP_PRG_CAP_KB, d5
	move.l	#STARTUP_PRG_BAR_ADDR, d0
	bsr	set_vram_write
	move.w	#30-1, d2
1:
	move.w	#STARTUP_GLYPH_DASH, d0
	tst.w	d5
	beq.s	2f
	move.w	#STARTUP_GLYPH_HASH, d0
	subq.w	#1, d5
2:
	addi.w	#PC_FONT_VTILE, d0
	ori.w	#0x6000, d0
	move.w	d0, (VDP_DATA).l
	dbra	d2, 1b
	movem.l	(sp)+, d0-d5
	rts

/* Earlier HEADER.DAT regions are complete when the first PrgBuf sector is
   reported. Turn their staged dotted rows into OK together at that boundary. */
startup_mark_prefix_ok:
	movem.l	d0-d2/a0, -(sp)
	lea	startup_prefix_ok_addrs, a0
	move.w	#STARTUP_PREFIX_OK_N-1, d2
1:
	moveq	#0, d0
	move.w	(a0)+, d0
	bsr	set_vram_write
	move.w	#PC_FONT_VTILE+STARTUP_GLYPH_O, d0
	ori.w	#0x4000, d0
	move.w	d0, (VDP_DATA).l
	move.w	#PC_FONT_VTILE+STARTUP_GLYPH_K, d0
	ori.w	#0x4000, d0
	move.w	d0, (VDP_DATA).l
	dbra	d2, 1b
	movem.l	(sp)+, d0-d2/a0
	rts

startup_mark_ok:
	movem.l	d0-d2/a0, -(sp)
	move.l	#STARTUP_PRG_STATUS_ADDR, d0
	bsr	set_vram_write
	lea	startup_ok_glyphs, a0
	moveq	#7-1, d2
1:
	moveq	#0, d0
	move.b	(a0)+, d0
	addi.w	#PC_FONT_VTILE, d0
	ori.w	#0x4000, d0
	move.w	d0, (VDP_DATA).l
	dbra	d2, 1b
	movem.l	(sp)+, d0-d2/a0
	rts

/* d0.w = negative 0xBADx startup diagnostic from the Sub CPU. Keep the
   preload screen visible and replace LOADING with the exact failing marker. */
startup_mark_error:
	movem.l	d0-d3/a0, -(sp)
	move.w	d0, d3
	move.l	#STARTUP_PRG_STATUS_ADDR, d0
	bsr	set_vram_write
	lea	startup_bad_glyphs, a0
	moveq	#3-1, d2
1:
	moveq	#0, d0
	move.b	(a0)+, d0
	addi.w	#PC_FONT_VTILE, d0
	ori.w	#0x6000, d0
	move.w	d0, (VDP_DATA).l
	dbra	d2, 1b
	andi.w	#0x000F, d3
	addi.w	#PC_FONT_VTILE+STARTUP_GLYPH_0, d3
	ori.w	#0x6000, d3
	move.w	d3, (VDP_DATA).l
	moveq	#0, d0
	move.w	#3-1, d2
2:
	move.w	#PC_FONT_VTILE, d0
	ori.w	#0x6000, d0
	move.w	d0, (VDP_DATA).l
	dbra	d2, 2b
	movem.l	(sp)+, d0-d3/a0
	rts

startup_mark_sub_ok:
	movem.l	d0-d2/a0, -(sp)
	move.l	#STARTUP_SUB_STATUS_ADDR, d0
	bsr	set_vram_write
	lea	startup_sub_ok_glyphs, a0
	moveq	#4-1, d2
1:
	moveq	#0, d0
	move.b	(a0)+, d0
	addi.w	#PC_FONT_VTILE, d0
	ori.w	#0x4000, d0
	move.w	d0, (VDP_DATA).l
	dbra	d2, 1b
	movem.l	(sp)+, d0-d2/a0
	rts

/* Initial-stream wait with live PrgBuf preload progress. COMSTAT1 is otherwise
   still free for boot errors and later desync diagnostics. */
cmd_wait_startup:
	move.w	d0, (GA_COMCMD0).l
	move.w	#0xFFFF, d5			/* last displayed remaining count */
	moveq	#0, d4				/* prefix rows not yet marked OK */
	moveq	#0, d6				/* Sub CPU acknowledgement not yet drawn */
1:
	move.w	(GA_COMSTAT0).l, d0
	tst.w	d0
	beq.s	6f
	tst.w	d6
	bne.s	6f
	bsr	startup_mark_sub_ok
	moveq	#1, d6
6:
	cmp.w	#STAT_READY, d0
	beq.s	3f
	move.w	(GA_COMSTAT1).l, d0
	tst.w	d0				/* zero is also the prebuffer-complete value */
	beq.s	7f
	tst.w	d0				/* 0xBADx boot errors stay negative */
	bpl.s	5f
	bsr	startup_mark_error
	bra.s	7f
5:
	cmp.w	d5, d0
	beq.s	7f
	move.w	d0, d5
	tst.w	d4
	bne.s	2f
	bsr	startup_mark_prefix_ok
	moveq	#1, d4
2:
	bsr	startup_update_prg
7:
	/* The UI is frame-paced: sample the Sub state once per VBlank instead of
	   hammering the gate-array registers in an unbounded Main-CPU loop. */
	bsr	wait_vblank
	bra	1b
3:
	moveq	#0, d0
	bsr	startup_update_prg
	bsr	startup_mark_ok
	move.w	#0, (GA_COMCMD0).l
4:
	tst.w	(GA_COMSTAT0).l
	bne.s	4b
	rts
.endif

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
	move.w	d0, d3				/* preserve READY/END across cadence polling */
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
	move.w	d3, d0				/* swap_or_end return contract */
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

/* Build the values-only HUD row in Main RAM before the display deadline.
   Publishing the finished row into the inactive Plane A table is a short fixed
   copy; reg2 selects the completed picture and HUD atomically.
   Category glyphs are omitted to reserve cells for future supply metrics.
   H32: xxxx xx xx xx xx xx xx xx xx xx = 22 words.
   H40: the same 22 words followed by xxxx xx = 28 words.
	frame/Main-timeは16-bit、leadはhigh byte、他はlow byteの2桁。leadは256B単位。 */
prepare_dbg:
.ifdef HUD_HEX_TABLE
	movem.l	d0-d4/a0-a1, -(sp)
	lea	dbg_hex_pairs, a1
.else
	movem.l	d0-d4/a0, -(sp)
.endif
	lea	dbg_row, a0
	/* frame number, 4 digits */
	move.w	frame_no, d4
	DBG_PUT4
	/* palette segment, low byte */
	move.w	dbg_seg, d4
	DBG_PUT2
	/* slip/reseek count, low byte */
	move.w	(PROBE_BANK+0xAF00).l, d4
	DBG_PUT2
	/* desync count, low byte */
	move.w	(PROBE_BANK+0xAF7E).l, d4
	DBG_PUT2
	/* audio re-sync count, low byte */
	move.w	(PROBE_BANK+0xAF20).l, d4
	DBG_PUT2
	/* current audio lead high byte (256-byte units) */
	move.w	(PROBE_BANK+0xAF22).l, d4
	lsr.w	#8, d4
	DBG_PUT2
	/* total blocking CD pumps (current control + older BODY slot) */
	move.w	(PROBE_BANK+0xAF18).l, d4
	add.w	(PROBE_BANK+0xAF1A).l, d4
	DBG_PUT2
	/* Main's CMD_SWAP wait for Sub completion, in approximate scanlines */
	move.w	sub_wait_lines, d4
	DBG_PUT2
	/* VBlank starts waited by this frame's Main-side pattern path */
	move.w	frame_vblank_waits, d4
	DBG_PUT2
	/* Sub ADPCM decode time in 4*30.72us units (zero for PCM builds). */
	move.w	(PROBE_BANK+0xAF1C).l, d4
	lsr.w	#2, d4
	DBG_PUT2
	/* H40 has exactly eight additional visible HUD cells. Keep the shared
	   H32 prefix stable and use the tail for direct Main/DMA correlation. */
.ifdef PLAYER_SPECIALIZED
.if PC_MODE == 1
	move.w	dma_elapsed_ticks, d4
	DBG_PUT4
	move.w	n_runs, d4
	DBG_PUT2
.endif
.else
	cmpi.w	#1, md_mode
	bne.s	1f
	move.w	dma_elapsed_ticks, d4
	DBG_PUT4
	move.w	n_runs, d4
	DBG_PUT2
1:
.endif
.ifdef HUD_HEX_TABLE
	movem.l	(sp)+, d0-d4/a0-a1
.else
	movem.l	(sp)+, d0-d4/a0
.endif
	rts

/* Publish a prebuilt row over the first cells of the inactive Plane A movie
   table. It is not displayed yet, so the copy is safe during active display.
   Cells to the right remain the exact same movie table; no Window/Plane B
   transparency or stale alternate frame is involved. */
publish_dbg:
	movem.l	d0-d1/a0, -(sp)
	moveq	#0, d0
	move.w	back_idx, d0
	lsl.l	#8, d0
	lsl.l	#5, d0				/* back_idx*0x2000 */
	add.l	#NT0, d0
	bsr	set_vram_write
	lea	dbg_row, a0
.ifdef PLAYER_SPECIALIZED
.if PC_MODE == 1
	.rept 14
	move.l	(a0)+, (VDP_DATA).l
	.endr
.else
	.rept 11
	move.l	(a0)+, (VDP_DATA).l
	.endr
.endif
.else
	moveq	#11-1, d1			/* H32: 22 words */
	cmpi.w	#1, md_mode
	bne.s	1f
	moveq	#14-1, d1			/* H40: 28 words */
1:
	move.l	(a0)+, (VDP_DATA).l
	dbra	d1, 1b
.endif
	movem.l	(sp)+, d0-d1/a0
	rts

/* Append four value digits to the prebuilt row.  Reuse the straight byte-pair
   formatter instead of walking four nibbles through a DBRA loop. */
dbg_put4:
	move.w	d4, d3
	lsr.w	#8, d4
	bsr	dbg_put2
	move.w	d3, d4
	bra	dbg_put2

/* Append the low byte as two digits.  Calculate both name-table words directly;
   this is the hot DEBUG formatter and avoids a per-nibble loop and DBRA. */
dbg_put2:
	move.w	d4, d0
	andi.w	#0xF, d0
	PC_ADD_W md_font_vtile, PC_FONT_VTILE, d0
	move.w	d0, 2(a0)			/* low nibble */
	lsr.w	#4, d4
	andi.w	#0xF, d4
	PC_ADD_W md_font_vtile, PC_FONT_VTILE, d4
	move.w	d4, (a0)			/* high nibble */
	addq.l	#4, a0
	rts

	.data
	.align 2
palettes:
	.incbin "palettes.bin"
dbgfont:
	.incbin "dbgfont.bin"
.ifdef HUD_HEX_TABLE
/* Longword order matches two consecutive VDP name-table writes.  This table
   remains in the permanent IP image; it does not consume MainBuf capacity. */
	.align 2
dbg_hex_pairs:
	.set dbg_hex_byte, 0
	.rept 256
	.word PC_FONT_VTILE + ((dbg_hex_byte >> 4) & 0x0F)
	.word PC_FONT_VTILE + (dbg_hex_byte & 0x0F)
	.set dbg_hex_byte, dbg_hex_byte + 1
	.endr
.endif
.ifdef PLAYER_SPECIALIZED
	.section .startup,"a"
	.include "startup_screen.inc"
.if PC_FONT_VTILE + DBGFONT_N > NT0/32
	.error "DEBUG font overlaps the movie name table"
.endif
.if PC_FONT_VTILE + STARTUP_FONT_N > NT0/32
	.error "startup font overlaps the movie name table"
.endif
.endif

	.bss
	.align 2
shadow:
	.space 1120*2				/* 最大グリッド(H40 40x28)ぶん */
dbg_row:
	.space 28*2				/* prebuilt values-only H40 row; H32 uses first 22 words */
.ifndef PLAYER_SPECIALIZED
md_mode:
	.space 2
md_vsync_n:
	.space 2				/* v4: 1コマの表示VBLANK数(15fps=4, 30fps=2) */
md_fixed_n2:
	.space 2				/* v8 header feature bit 1; 24fps N2 hint alone stays unpaced */
.endif
vsync_acc:
	.space 2				/* v4: 現コマで消費したVBLANK数(ペーシング用) */
pace_flip_tick:
	.space 2				/* v8: GA stopwatch tick at preceding fixed-N2 flip */
.ifndef PLAYER_SPECIALIZED
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
.endif
back_idx:
	.space 2
display_blank:
	.space 2				/* startup-to-frame0 VDP blanking latch */
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
frame_vblank_waits:
	.space 2				/* DEBUG HUD M snapshot before display pacing */
dma_elapsed_ticks:
	.space 2				/* DEBUG H40 Uxxxx: 30.72 us stopwatch ticks */
dma_start_tick:
	.space 2				/* DEBUG stopwatch sample at first pattern transfer */
wr_ptr0:
	.space 4				/* next Wr0 preload address in the currently mapped bank */
wr_ptr1:
	.space 4				/* next Wr1 preload address in the currently mapped bank */
main_ptr:
	.space 4				/* next MainBuf preload address */
.ifndef PLAYER_SPECIALIZED
md_nseg:
	.space 2				/* PALTAB区間数(表コピー時にクランプ済み) */
.endif
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
