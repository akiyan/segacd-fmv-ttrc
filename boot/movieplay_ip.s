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

.equ GA_COMCMD0, 0x00A12010
.equ GA_COMCMD1, 0x00A12012
.equ GA_COMSTAT0, 0x00A12020

.equ PROBE_BANK, 0x00200000

.equ CMD_STREAM, 0x50
.equ CMD_SWAP,   0x51
.equ STAT_READY, 0x8003
.equ STAT_END,   0x8004			/* SPからの映画終端通知(15秒待って再ループ) */

.equ NT0, 0xC000
.equ NT1, 0xE000

/* VDP DMAの源は必ずMain-RAM。1フレームのタイルをここへステージしてからDMA。 */
.equ DMA_STAGE, 0x00FF2000		/* タイルステージ(24KB, ~768タイル) */
.equ RUN_TABLE, 0x00FF8000		/* (dst.w,len.w) ラン表(最大~128ラン) */
/* デバッグオーバーレイ: フォントは予約VRAM(プール1360の直上 tile1361)。 */
.equ DBGFONT_N, 28			/* dbgfont.bin のタイル数 */
/* フォントVRAM位置はヘッダの base+pool 直上を実行時に計算(md_font_vtile/md_font_addr) */
/* リリースビルドが既定。make movieplay DEBUG=1 でオーバーレイ一式を有効化
   (ストリーム側は CBRSIM_PACK_DEBUG=1 でデバッグ欄ありを生成) */
.equ DBG_COL, 20			/* 右下オーバーレイの左端セル列 */
.equ DBG_STAGE, 0x00FFA000		/* フォント色替えのステージ(Main-RAM) */
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
	clr.w	dbg_palattr

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
	move.w	#HUD_PITCH_H32, md_hud_pitch
	cmpi.w	#1, md_mode
	bne	1f					/* mode 0=H32; mode 2 is reserved */
	move.w	#0x8C81, (VDP_CTRL).l		/* reg12 H40 */
	move.w	#40, d2
	move.w	#VB_WORDS_H40, d3
	move.w	#HUD_PITCH_H40, md_hud_pitch
1:
	move.w	d3, md_vbudget
	sub.w	md_tcols, d2			/* col0 = (screen_cols-tcols)/2 */
	lsr.w	#1, d2
	move.w	d2, md_col0
	move.w	#28, d2				/* screen_rows(H32/H40) */
	sub.w	md_trows, d2			/* row0 = (screen_rows-trows)/2 */
	lsr.w	#1, d2
	move.w	d2, md_row0
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
	lea	palettes, a0			/* pal_write前のHUD色替え用の安全な初期値 */
	move.l	a0, cur_pal_src
	/* デバッグフォントをフォントVRAM位置へCPUロード(pal_write時にB案で色替え) */
.ifdef DEBUG
	move.l	md_font_addr, d0
	bsr	set_vram_write
	lea	dbgfont, a0
	move.w	#DBGFONT_N*16-1, d1
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d1, 1b
.endif

	clr.w	frame_no
	clr.w	started
	clr.w	vsync_acc			/* v4: ペーシングカウンタ初期化(.bssはMD上でクリアされない) */
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

/* ---- 1フレーム分をデコードし裏へ描画してflip ----
   タイル転送はDMA(VDPが自走=CPUを空ける)。**VDP DMAの源は必ずMain-RAM**
   (Word-RAM直DMAは化ける)。手順を2パスに分離:
     Pass1(active可): 全ランのタイルをWord-RAM→Main-RAMステージへコピー+(dst,len)表を作る
     Pass2(vblank内): 表を順にDMA。**stageのCPUコピーでvblankを食い潰してDMAがactiveに
     はみ出すと化ける**ので、コピー(遅い)とDMA発行(vblank厳守)を分ける。 */
build_frame:
	movem.l	d0-d7/a0-a3, -(sp)
	/* Pass1: コピー無し。(dst.w, len.w, src.l)のラン表だけ作る=Main CPUはパターンに触れない。
	   src は Word-RAM 内のパターン先頭(タイルDMAはWord-RAM直, 先頭1ワード化けはPass2で対処) */
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
	move.w	d4, n_runs			/* このフレームのDMAラン数(0可) */
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
	moveq	#0, d6				/* cell */
	move.w	md_bmbytes, d5
	subq.w	#1, d5
bf_ubyte:
	move.b	(a2)+, d0
	moveq	#7, d4
bf_ubit:
	lsr.b	#1, d0
	bcc	1f
	move.w	(a0)+, d3
	andi.w	#0x7FFF, d3			/* strip the on-disc cold flag */
	move.w	d6, d2
	add.w	d2, d2				/* cell*2 */
	move.w	d3, (a1,d2.w)
1:
	addq.w	#1, d6
	dbra	d4, bf_ubit
	dbra	d5, bf_ubyte
bf_blit:
	/* シャドウ全体(18行x32)を裏NTへ blit (裏は非表示=active可) */
	moveq	#0, d5
	move.w	back_idx, d5
	lsl.l	#8, d5
	lsl.l	#5, d5				/* back_idx*0x2000 */
	add.l	#NT0, d5			/* back_base = 0xC000 or 0xE000 (flipまで保持) */
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
	move.w	md_tcols, d1
	subq.w	#1, d1
bf_bw:
	move.w	(a1)+, (VDP_DATA).l
	dbra	d1, bf_bw
	addq.w	#1, d4
	dbra	d6, bf_row

	/* CRAM総入替は flip と同一VBLANKで行う(bf_flip側)。ここで先に書くと、
	   タイルDMAが複数vblankに渡る間「旧フレーム表示×新パレット」が見える
	   (パレット区間切替の瞬間に実機側だけ明るいゴミタイルが出る実バグ)。 */
bf_dma:
	/* Pass2: 表を順に Word-RAM 直DMA(src→VRAM dst)。VBLANK予算(d7)でランをまたいで分割。
	   Word-RAM源DMAは先頭1ワードが化ける(実測/Sega文書)ため、チャンク毎に
	   先頭1ワードをCPU書き→残り(chunk-1)語を src+2→dst+2 でDMA。 */
	move.w	n_runs, d4
	beq	bf_flip
	lea	RUN_TABLE, a2
	move.w	(VDP_CTRL).l, d0		/* 現vblank内でなければ次vblankへ */
	btst	#3, d0
	bne	1f
	bsr	wait_vb_start
1:
	move.w	md_vbudget, d7			/* d7 = 残VBLANK予算(語, モード別) */
bf_run_lp:
	move.w	(a2)+, d3			/* dst(VRAMバイト) */
	move.w	(a2)+, d1			/* len(語, このランの残) */
	movea.l	(a2)+, a3			/* src(Word-RAM) */
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
	bsr	dma_chunk_wr			/* d6語を a3(Word-RAM)→d3 へ(先頭CPU書き+DMA, 完了待ち) */
	sub.w	d6, d7				/* 予算 -= chunk */
	sub.w	d6, d1				/* ラン残 -= chunk */
	add.w	d6, d6				/* chunk*2 = バイト */
	adda.w	d6, a3				/* src += バイト */
	add.w	d6, d3				/* dst += バイト */
	tst.w	d1
	bne	bf_chunk
	subq.w	#1, d4
	bne	bf_run_lp
bf_flip:
.ifdef DEBUG
	bsr	render_dbg			/* 上黒帯にデバッグ指標を描画(裏バッファ, flip直前) */
.endif
	/* パレット区間切替: CRAM総入替(64語≈0.1ms)→flip を新しいvblank頭で連続実行=
	   同一VBLANK内で原子的。フォント色替え(CPU再着色~1.7ms+DMA)はvblankを
	   食い潰してflipをactiveへ押し出す(=新パレット×旧フレームが上部に露出する
	   再発バグ)ため、**flip後**に回し、そのDMAは次vblankで行う。
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
	move.l	a0, cur_pal_src			/* HUD色替え(dbg_setbright)も同じ源を読む */
	bsr	wait_vb_start			/* 頭から使える新しいvblank(CRAM+flipが確実に収まる) */
	move.l	#0xC0000000, (VDP_CTRL).l	/* CRAM addr 0 */
	move.w	#64-1, d1
1:
	move.w	(a0)+, (VDP_DATA).l
	dbra	d1, 1b
	bsr	do_flip				/* CRAM直後・同vblank内にflip */
.ifdef DEBUG
	bsr	dbg_setbright			/* フォント色替え(CPU=active可, DMAは内部で次vblank) */
.endif
	bra	bf_after_flip
bf_doflip:
	bsr	do_flip
bf_after_flip:
	/* 滑りインジケータ: SPが検出した滑り回数>0なら枠を赤に */
	move.w	(PROBE_BANK+0xAF00).l, d0
	beq	1f
	move.l	#0xC0000000, (VDP_CTRL).l
	move.w	#0x000E, (VDP_DATA).l
1:
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
   Word-RAM源はフェッチが1ワード遅延する(最初のフェッチはsrc-2を返す)ため、
   **源アドレスに+2して全長をそのまま転送**すると全ワードが正しく届く
   (dst側は据置・CPU書き不要)。 */
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
	/* 先頭1ワードはDMA開始ラッチの古い値(ゴミ)が書かれるため、CPUで上書き修復。
	   (src+2補正で2ワード目以降は正しい。ゴミはチャンク先頭の1ワードのみ) */
	move.w	d3, d0
	bsr	set_vram_write
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
	move.w	#CMD_SWAP, (GA_COMCMD0).l
1:
	move.w	(GA_COMSTAT0).l, d0
	cmp.w	#STAT_READY, d0
	beq	2f
	cmp.w	#STAT_END, d0
	bne	1b
2:
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

/* B案: 現区間CRAM(cur_pal_src, 64語=Main-RAMのPALTAB表)でRGB合計最大の色を探し、その
   palette行を dbg_palattr に、その色index(B)へフォントの画素(index1)を塗り替えて予約VRAMへ
   DMA。pal_write時に呼ぶ(vblank内)。 */
dbg_setbright:
	movem.l	d0-d7/a0-a3, -(sp)
	movea.l	cur_pal_src, a0
	moveq	#0, d3				/* best index */
	moveq	#-1, d4				/* best sum */
	moveq	#0, d2				/* i */
dsb_scan:
	move.w	(a0)+, d0			/* CRAM: 0000 BBB0 GGG0 RRR0 */
	move.w	d0, d1
	lsr.w	#1, d1
	andi.w	#7, d1				/* R */
	move.w	d0, d5
	lsr.w	#5, d5
	andi.w	#7, d5				/* G */
	add.w	d5, d1
	move.w	d0, d5
	lsr.w	#8, d5
	lsr.w	#1, d5
	andi.w	#7, d5				/* B */
	add.w	d5, d1				/* sum */
	cmp.w	d4, d1
	ble	1f
	move.w	d1, d4
	move.w	d2, d3
1:
	addq.w	#1, d2
	cmp.w	#64, d2
	blo	dsb_scan
	move.w	d3, d5				/* palrow = idx/16 */
	lsr.w	#4, d5
	lsl.w	#8, d5
	lsl.w	#5, d5				/* attr = palrow<<13 */
	move.w	d5, dbg_palattr
	move.w	d3, d6
	andi.w	#0xF, d6			/* B = colidx */
	/* フォント色替え: base(ROM) の nibble==1 を B へ → DBG_STAGE */
	lea	dbgfont, a0
	lea	DBG_STAGE, a1
	move.w	#DBGFONT_N*16-1, d7
dsb_rc:
	move.w	(a0)+, d0
	bsr	dbg_recolor_word
	move.w	d0, (a1)+
	dbra	d7, dsb_rc
	/* DMA DBG_STAGE → フォントVRAM。CPU再着色でvblankを跨いでいるため、
	   activeへのDMAはみ出しを避けて次のvblank頭で転送する(HUD色が1コマ遅れるだけ) */
	bsr	wait_vb_start
	move.w	#DBGFONT_N*16, d6
	move.w	md_font_addr+2, d3		/* フォントVRAM(下位word, <64KB) */
	lea	DBG_STAGE, a3
	bsr	dma_chunk
	movem.l	(sp)+, d0-d7/a0-a3
	rts

/* d0=1語(4ニブル)の index1 を d6(B) へ置換して返す。trashes d1,d2,d3 */
dbg_recolor_word:
	moveq	#0, d1
	moveq	#3, d2
1:
	rol.w	#4, d0				/* 上位ニブル→下位へ */
	move.w	d0, d3
	andi.w	#0xF, d3
	cmp.w	#1, d3
	bne	2f
	move.w	d6, d3
2:
	lsl.w	#4, d1
	or.w	d3, d1
	dbra	d2, 1b
	move.w	d1, d0
	rts

/* 上黒帯(行2)にデバッグ表示。d5=back_base。裏バッファへ書く(flip直前)。
   表示は F(rame) と P(区間) のみ・左上寄せ・スペース区切りの横並び(ユーザー指定)。
   行0-1はオーバースキャンで切れうるため行2を使用。下黒帯は将来の
   「黒帯走査中のDMA早期開始」用に空けておく。 */
/* --- デバッグHUDレイアウト(左上端・1行) ---
   H32は32列に収めるため pitch=5 (glyph+4桁、空け無し)、H40はpitch=6
   (glyph+4桁+空け1)を使う。OCR側(tools/read_frameno.py)は録画幅から選ぶ。 */
.equ HUD_ROW,   0			/* HUD行(0=最上段) */
.equ HUD_PITCH_H32, 5
.equ HUD_PITCH_H40, 6

render_dbg:
	movem.l	d0-d4/d6-d7/a0-a1, -(sp)
	move.w	dbg_palattr, d7
	move.w	md_hud_pitch, d6
	add.w	d6, d6				/* pitch in name-table bytes */
	move.w	#HUD_ROW*128, a1
	move.w	a1, d2				/* F フレーム番号 */
	move.w	#15, d3				/* glyph 'F'(=hex F) */
	move.w	frame_no, d4
	bsr	dbg_put_row
	add.w	d6, a1
	move.w	a1, d2				/* P パレット区間 */
	move.w	#19, d3				/* glyph 'P' */
	move.w	dbg_seg, d4
	bsr	dbg_put_row
	add.w	d6, a1
	move.w	a1, d2				/* S 滑り=再シーク回復回数(グリッチマーカー) */
	move.w	#23, d3				/* glyph 'S' */
	move.w	(PROBE_BANK+0xAF00).l, d4
	bsr	dbg_put_row
	add.w	d6, a1
	move.w	a1, d2				/* D desync検知回数(通常0) */
	move.w	#13, d3				/* glyph 'D'(=hex D) */
	move.w	(PROBE_BANK+0xAF7E).l, d4
	bsr	dbg_put_row
	add.w	d6, a1
	move.w	a1, d2				/* R 音声re-sync回数(計測) */
	move.w	#16, d3				/* glyph 'R' */
	move.w	(PROBE_BANK+0xAF20).l, d4
	bsr	dbg_put_row
	add.w	d6, a1
	move.w	a1, d2				/* L 現コマの音声リード(計測) */
	move.w	#21, d3				/* glyph 'L' */
	move.w	(PROBE_BANK+0xAF22).l, d4
	bsr	dbg_put_row
	movem.l	(sp)+, d0-d4/d6-d7/a0-a1
	rts

/* 1項目: d2=NT内オフセット(row*128+col*2), d3=ラベルglyph, d4=値(hex4桁), d5=back_base,
   d7=palattr。trashes d0,d1,d2 */
dbg_put_row:
	moveq	#0, d1
	move.w	d2, d1
	add.l	d5, d1
	move.l	d1, d0
	bsr	set_vram_write			/* d0=dst; trashes d0,d2 */
	move.w	d3, d1				/* ラベル */
	add.w	md_font_vtile, d1
	or.w	d7, d1
	move.w	d1, (VDP_DATA).l
	moveq	#3, d2				/* 4 hex桁(上位→下位) */
1:
	rol.w	#4, d4
	move.w	d4, d1
	andi.w	#0xF, d1
	add.w	md_font_vtile, d1
	or.w	d7, d1
	move.w	d1, (VDP_DATA).l
	dbra	d2, 1b
	rts

	.data
	.align 2
palettes:
	.incbin "out/movieplay/palettes.bin"
dbgfont:
	.incbin "dbgfont.bin"

	.bss
	.align 2
shadow:
	.space 1120*2				/* 最大グリッド(H40 40x28)ぶん */
md_mode:
	.space 2
md_hud_pitch:
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
dbg_palattr:
	.space 2
md_nseg:
	.space 2				/* PALTAB区間数(表コピー時にクランプ済み) */
cur_pal_src:
	.space 4				/* 現区間パレットのMain-RAM位置(HUD色替えが読む) */
