/*
 * Phase B5: delta stream player - Sub(SP)側, TTRC = B方式(payload/control分離)。
 *
 * 絶対ルール: CD連続読み(ONE ROM_READN, シーク無し)を維持する。
 *   連続読み中の CPU→PRG バースト書込は Sub-CPU を固めるため(実証済み)、リングへの
 *   payload 書込を完全 DMA(CDC_TRN)化する。CPU は PRG へ書かない(読みのみ=安全)。
 *
 * MOVIE.DAT(TTRC): Header(1sec) + routing(2B/frame) + prebuffer(payload先頭) + frames(5sec)。
 *   各 frame = [n_pay_sec payloadセクタ][n_ctrl_sec controlセクタ][pad]。
 * 起動: routing table→PRG, prebuffer→リングへ CDC_TRN。
 * 毎フレーム: 5セクタを routing 通り CDC_TRN で振分(payload→リング循環, control→apply循環,
 *   pad→捨て) → control block を apply(PRG循環)から Word-RAM スクラッチへコピー(折返し線形化,
 *   PRG読み+WordRAM書き=安全) → 旧フラット形式+CRAM を Word-RAM 出力へ展開(cold は
 *   リングpop=PRG読み安全) + audio→PCM → swap。
 */

.equ CDBIOS,      0x00005F22
.equ CDB_STAT,    0x00005E80
.equ BIOS_DRV_INIT, 0x0010
.equ BIOS_CDB_STAT, 0x0081
.equ BIOS_CDC_STOP, 0x0089
.equ BIOS_CDC_STAT, 0x008A
.equ BIOS_CDC_READ, 0x008B
.equ BIOS_CDC_TRN,  0x008C
.equ BIOS_CDC_ACK,  0x008D
.equ BIOS_ROM_READN,0x0020

.equ SUB_GA_BASE, 0x00FF8000
.equ MEMMODE,     SUB_GA_BASE+0x0002
.equ COMCMD0,     SUB_GA_BASE+0x0010
.equ COMSTAT0,    SUB_GA_BASE+0x0020
.equ COMSTAT1,    SUB_GA_BASE+0x0022

.equ SUB_BANK_1M, 0x000C0000

/* --- PRG-RAM レイアウト(program 0x6000〜, <0x1000) --- */
/* 0x6800-0x8000 は連続読み中にBIOSが踏む(実証)。0x8000以上は安全(マーカー実証)。 */
.equ ROUTING,     0x00075000        /* routing table(最大8KB=4096フレーム) 0x75000-0x77000
                                       旧0x8000-0x9800(6KB)は3072フレームまでしか入らないため
                                       安全な高位PRGへ移設(0x9800-0xC000はBIOSが踏むので拡張不可) */
.equ ISO_BUF,     0x00007000        /* ISO初期化用(streaming前のみ・BIOS領域を一時利用) */
.equ SP_STACK,    0x0007FF00        /* スタック最上位(apply端0x7F800の上, 1.8KB) */
/* 0x9800-0xC000は連続読み中にBIOSが踏む(回収を試みたら化けた)。RINGは0xC000から。 */
.equ RING_BASE,   0x0000C000
.equ RING_SIZE,   0x00069000        /* 420KB(全機能ON tank414のdecode peak<=410KB向け) */
.equ RING_END,    RING_BASE+RING_SIZE     /* 0x75000 */
.equ APPLY_BASE,  0x00077000        /* routing(8KB)の直後 */
.equ APPLY_SIZE,  0x00008800        /* 34KB(16KBは頭詰まり→滑りを実測。42KB→34KBはrouting移設分) */
.equ APPLY_END,   APPLY_BASE+APPLY_SIZE   /* 0x7F800 */

/* --- Word-RAM スクラッチ(SPバンク内, 毎フレーム再利用=スワップ影響なし) --- */
.equ CTRL_SCR,    0x000D0000        /* control block 線形化(<=2246B) */
.equ PAD_SCR,     0x000D2000        /* pad セクタ捨て場 */
.equ F0PAT_SCR,   0x000D4000        /* frame0 patterns 一時置場(32KB, リング外=streamingと非干渉)。
                                       frame0展開のcold popはここから(f0_expand=1でring wrap/occ迂回)。 */

/* --- Word-RAM 出力(MDが読む) ---
   フル画面H40(最大1120セル)対応: loads は最大 1120*32B+ランヘッダ ≈ 36.5KB。
   O_LOADS を 0x84..0x9800(約38.8KB) に拡大(MD側と一致必須)。upds=1120*4B。 */
.equ O_PALW,   SUB_BANK_1M+0x0000   /* v3: 区間番号+1(0=切替なし)。MDはMain-RAMのPALTAB表を引く */
.equ O_CRAM,   SUB_BANK_1M+0x0002   /* 予約(v3でin-stream CRAM廃止。offsetは互換のため空けたまま) */
.equ O_NLOAD,  SUB_BANK_1M+0x0082
.equ O_LOADS,  SUB_BANK_1M+0x0084
.equ O_NUPD,   SUB_BANK_1M+0x9800
.equ O_UPDS,   SUB_BANK_1M+0x9802
.equ O_SLIP,   SUB_BANK_1M+0xAF00   /* slip_count(=再シーク回復回数=グリッチ) */
.equ O_DSY,    SUB_BANK_1M+0xAF7E   /* desync_count(同期マーカー不一致=フォールバック) */
.equ O_DBG,    SUB_BANK_1M+0xAF02   /* 22Bデバッグブロック転写先(raw,same,near,coa,flbk,buf,miss,予約4)
                                       control の dbg==1 のとき転写、dbg==0 はゼロ埋め */
.equ O_RESYNC, SUB_BANK_1M+0xAF20   /* 計測: 音声re-sync回数(リード下限/上限逸脱で書込ジャンプ=乱れの元) */
.equ O_LEAD,   SUB_BANK_1M+0xAF22   /* 計測: 現コマの音声リード(write-play, バイト)。SYNC_MINに近づく=枯渇 */
.equ O_HDR,    SUB_BANK_1M+0xAF80   /* ヘッダ先頭64Bの写し(MDがmode/tcols/trows/pool/baseを読む) */
.equ PALTAB_OFF, 0xB000             /* PALTAB(全区間パレット)のWord-RAMステージ位置。boot時に
                                       frame0と同じバンクへ置き、MDがMain-RAM表へ一度だけコピー。
                                       0xB000..0x10000(CTRL_SCR手前)=20KB=160区間が物理上限。
                                       ip.s の PALTAB_OFF と一致必須(check_player_ring.pyが検証) */
.equ O_PALTAB, SUB_BANK_1M+PALTAB_OFF

/* --- RF5C164 PCM (13.3kHz) --- */
.equ AUDIO_BYTES, 887
.equ PCM_ENV,   0x00FF0001
.equ PCM_PAN,   0x00FF0003
.equ PCM_FDL,   0x00FF0005
.equ PCM_FDH,   0x00FF0007
.equ PCM_LSL,   0x00FF0009
.equ PCM_LSH,   0x00FF000B
.equ PCM_ST,    0x00FF000D
.equ PCM_CTRL,  0x00FF000F
.equ PCM_ONOFF, 0x00FF0011
.equ PCM_WAVE,  0x00FF2001
.equ PCM_PLAY_H, 0x00FF0023
.equ WAVE_RING_END, 0x8000
.equ RING_MASK, WAVE_RING_END-1
/* 音声リード: リング先頭の無音を SYNC_LEAD ぶん再生してから実音声に到達=起動遅延。
   タイトル同期優先(遅延を出さない)。大バッファ/FD追従は使わず素直なリード 0x1800(0.46s)。
   SYNC_MIN(リード下限)を割ると書込を play+SYNC_LEAD へジャンプ=re-sync(古い音をまたぐ乱れ)。
   重いシーン転換クラスタで映像が数コマ遅れリードが一瞬凹むが、O_LEAD計測で底≈0x5BB(machi_op
   F1056)と実測。SYNC_MIN=0x400(≈1.6コマ)はその底より下・追い越し(0)より十分上に置き、
   安全な一瞬の凹みでは re-sync させない(=乱れゼロ・無劣化)。真に枯渇した時だけ最後の砦として発火。 */
.equ SYNC_LEAD, 0x1800
.equ SYNC_MIN,  0x0400
.equ SYNC_MAX,  0x6800

.equ HEADER_SECTORS,  1
/* frames/tcols/trows/cells/pool/base/frame_sectors/prebuf/routing/mode は
   MOVIE.DAT ヘッダから起動時に読む(h_* 変数)。焼き込み定数の手動更新は廃止。 */

.equ CMD_STREAM, 0x50
.equ CMD_SWAP,   0x51
.equ STAT_READY, 0x8003
.equ STAT_END,   0x8004			/* 全フレーム再生完了(MDは15秒待って CMD_STREAM 再送) */

.macro BIOSCALL code
	move.w	#\code, d0
	jsr	CDBIOS
.endm

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
	andi.w	#0xFFFA, (MEMMODE).l
	move.w	#0, (COMSTAT0).l
	move.w	#0, (COMSTAT1).l
	rts

.global sp_main
sp_main:
command_loop:
	tst.w	(COMCMD0).l
	bne	1f
	bra	command_loop
1:
	cmp.w	#CMD_STREAM, (COMCMD0).l
	beq	do_stream
	move.w	(COMCMD0).l, (COMSTAT0).l
2:
	tst.w	(COMCMD0).l
	bne	2b
	move.w	#0, (COMSTAT0).l
	bra	sp_main

do_stream:
	movea.l	#SP_STACK, sp
	move.w	#CMD_STREAM, (COMSTAT0).l
	/* BIOSのCDCセクタ管理は割り込み駆動。IENを立てsrを下げないと、連続読みで
	   ポーリングの隙間にセクタが化け/欠ける(実証: XOR不一致)。元プレイヤと同じ設定。 */
	ori.b	#0x04, (SUB_GA_BASE+0x37).l	/* HOCK: enable CDD communication */
	ori.b	#0x3C, (SUB_GA_BASE+0x33).l	/* IEN: enable INT2-5 (timer/CDD/CDC) */
	move.w	#0x2000, sr			/* enable ints BEFORE the BIOS calls */
	andi.b	#0xFA, (MEMMODE+1).l
	lea	drv_init_tracklist, a0
	BIOSCALL BIOS_DRV_INIT
1:
	BIOSCALL BIOS_CDB_STAT
	andi.b	#0xF0, (CDB_STAT).w
	bne	1b
	bsr	init_iso9660
	lea	file_stream, a0
	bsr	find_file		/* d0 = LBA */
	move.l	d0, stream_lba
	bset	#2, (MEMMODE+1).l
stream_start:
	/* ループ再生の再入口(ここから下は毎ループ実行)。1ループにつきシークは
	   ここでの ROM_READN 1回だけ=再生中のCD連続読みルールは維持 */
	clr.l	prev_msf
	clr.w	slip_count
	bsr	init_pcm
	bsr	issue_rom_readn			/* ファイル全体を連続読み(以降シーク無し) */
	/* ヘッダ1secをSTAGEへ取り込み、マジック "TTRC" を検証(MOVIE.md) */
	move.w	#HEADER_SECTORS, d0
	lea	PAD_SCR, a0
	bsr	drain_lin
	cmpi.l	#0x54545243, (PAD_SCR).l	/* "TTRC" */
	beq	1f
	move.w	#0xBAD0, (COMSTAT1).l		/* 不一致: 診断マーカーを出して停止 */
bad_magic:
	bra	bad_magic
1:
	/* ヘッダ解析(>4sHHHHHHHHH + >LLLL + mode@38)。焼き込み定数を廃し実行時に読む */
	lea	PAD_SCR, a0
	move.w	6(a0), h_frames
	move.w	18(a0), h_fsec
	move.w	12(a0), d0			/* cells */
	addq.w	#7, d0
	lsr.w	#3, d0
	move.w	d0, h_bmbytes			/* ceil(cells/8) */
	move.l	26(a0), d0
	move.w	d0, h_routing_sec
	move.l	30(a0), d0
	move.w	d0, h_prebuf_sec
	move.l	22(a0), h_prebuf_pat
	move.l	40(a0), d0			/* v2: frame0 control sectors @offset40 */
	move.w	d0, h_f0_ctrl_sec
	move.l	44(a0), d0			/* v2: frame0 pattern sectors @offset44 */
	move.w	d0, h_f0_pat_sec
	move.l	48(a0), d0			/* v3: PALTAB sectors @offset48 (v2ディスクは0) */
	cmpi.w	#10, d0				/* 壊れたヘッダ対策: ステージ上限20KB=10secにクランプ */
	bls	1f
	moveq	#10, d0
1:
	move.w	d0, h_paltab_sec
	/* v4: 可変フレーム。N(vsync/コマ)@52, AUDIO(1コマ音声B)@54。v2/v3(=0)なら15fps既定へ */
	move.w	52(a0), d0			/* N */
	bne	1f
	moveq	#4, d0				/* v2/v3(0)は15fps=N4 */
1:
	move.w	d0, h_vsync_n
	move.w	54(a0), d0			/* AUDIO(1コマ音声B) */
	bne	1f
	move.w	#AUDIO_BYTES, d0		/* v2/v3(0)は887 */
1:
	move.w	d0, h_audio_bytes
	/* v4: 名目fps@56(レートマッチpadding用)。v2/v3(=0)は15。CD 1x=75sec/s を fps で割った整数
	   割当(累積器)まで各コマをpad=ディスク読み速度を表示速度に一致(過剰配送/CDCスリップ防止)。
	   15fpsでは常に5(=v3固定と一致)、30fpsは2/3平均。sec_acc は累積器の初期化。 */
	move.w	56(a0), d0			/* 名目fps(15/30) */
	bne	1f
	moveq	#15, d0				/* v2/v3(0)は15 */
1:
	move.w	d0, h_fps_int
	clr.w	sec_acc
	clr.w	lead
	/* v4: h_stream_total は find_file がファイルサイズ(実セクタ数)から初期化済み。可変フレーム
	   では frames*fsec 計算は過大(=paddingぶん多い)なので、ここでの上書きは行わない。
	   ROM_READN の連続読み長・スリップ回復の残量は find_file のファイルサイズ値を使う。 */
	/* MDへヘッダ写しを渡す(frame0と同じバンクに書く=swap後にMDが読める) */
	lea	(O_HDR).l, a1
	moveq	#32-1, d1			/* 64B */
1:
	move.w	(a0)+, (a1)+
	dbra	d1, 1b
	/* PALTAB(ヘッダ直後, paltab_sec) → Word-RAM O_PALTAB へ(frame0と同じバンク)。
	   MDはSTAT_READY後に一度だけMain-RAM表へコピーする(以降palバイトは表参照のみ)。 */
	moveq	#0, d0
	move.w	h_paltab_sec, d0
	beq	1f
	lea	(O_PALTAB).l, a0
	bsr	drain_lin_staged		/* CDC_TRN直行を避けSTAGE経由(スリップ防止) */
1:
	/* === v2: frame0 は DAT冒頭の専用ヘッダブロック(control+patterns)。boot中に別ロード
	   してVRAMへ展開・表示する。ストリーミングのリングは一切経由しない(=boot時リングが
	   RING_CAP以下=back-pressure非接触)。frame0の大バーストによる後続枯渇(崩壊)を根絶。 */
	/* frame0 control(f0_ctrl_sec) を CTRL_SCR へ。CDC_TRN直行を避け STAGE経由(スリップ防止) */
	moveq	#0, d0
	move.w	h_f0_ctrl_sec, d0
	lea	CTRL_SCR, a0
	bsr	drain_lin_staged
	/* frame0 patterns を Word-RAM 一時置場(F0PAT_SCR)へ。リング外なので streaming と一切
	   干渉しない(リングは PREBUF1 0xC000-0x63800 + streamed 0x63800↑ の連続=穴なし)。 */
	move.l	#F0PAT_SCR, f0_pat_addr
	moveq	#0, d0
	move.w	h_f0_pat_sec, d0
	movea.l	f0_pat_addr, a0
	bsr	drain_lin_staged		/* CDC_TRN直行を避け STAGE経由(PRG直行スリップ防止) */
	/* routing table → STAGE経由で PRG へ */
	move.w	h_routing_sec, d7
	lea	ROUTING, a1
rt_lp:
	movem.l	d7/a1, -(sp)
	lea	PAD_SCR, a0
	bsr	drain1
	movem.l	(sp)+, d7/a1
	movem.l	d7/a1, -(sp)
	bsr	stage_copy
	movem.l	(sp)+, d7/a1
	lea	0x800(a1), a1
	subq.w	#1, d7
	bne	rt_lp
	/* prebuffer(PREBUF1=frame1満タン) → STAGE経由でリング下部(RING_BASE)へ */
	move.l	#RING_BASE, ring_tail
	move.w	h_prebuf_sec, d7
pb_lp:
	movem.l	d7, -(sp)
	lea	PAD_SCR, a0
	bsr	drain1
	movea.l	ring_tail, a1
	bsr	stage_copy
	movem.l	(sp)+, d7
	movea.l	ring_tail, a0
	lea	0x800(a0), a0
	move.l	a0, ring_tail
	subq.w	#1, d7
	bne	pb_lp
	bsr	pcm_on
	/* === streaming状態を frame0展開の「前」に確立する。frame0展開は数ms要り、その間CDを
	   吸わないとCDCが溢れて数セクタを落とす(実測: +3セクタ desync → frame1のcontrolがズレて
	   全面化け)。展開中も expand_frame 内の pump_poll が drain_frame=1 で frame1+ を正しく
	   リング/applyへバッファするので溢れない。frame0のpop域(f0pat 0x63800-0x6B800)と pump の
	   書込先(ring_tail=0x6B800 以上=f0patの上)は重ならない=競合なし。 === */
	move.l	#APPLY_BASE, apply_tail
	move.l	#APPLY_BASE, apply_cur
	move.w	#1, drain_frame			/* FRAMES先頭=frame1(routing[0]=0,0はスキップ) */
	clr.w	drain_k
	/* frame0展開: coldは Word-RAM の f0pat(F0PAT_SCR)から pop(f0_expand=1でwrap/occ迂回)。
	   ring_tail=PREBUF1末尾(0x63800)=streamingの書込開始点。展開中 pump_poll が frame1+ を
	   0x63800↑へ連続バッファ(CDC溢れ=desync防止)。f0patはリング外なので競合なし・穴なし。 */
	move.l	#RING_BASE, ring_head		/* pump_pollのocc計算用(0xC000)。frame0のpopはf0_pat_addr */
	move.l	h_prebuf_pat, d0
	lsl.l	#5, d0
	add.l	#RING_BASE, d0
	move.l	d0, ring_tail			/* 0x63800 = PREBUF1末尾 = streaming tail */
	move.w	#1, f0_expand
	move.w	#1, frame_idx			/* frame0処理済み(旧playerと同じframe_idx=1) */
	bsr	expand_frame
	clr.w	f0_expand
	/* frame1用: ring_head=PREBUF1先頭。ring_tail/drain_frame/drain_k/apply は pump が
	   進めた値をそのまま維持(連続=正しいストリーム位置、穴なし)。 */
	move.l	#RING_BASE, ring_head
	/* frame0 を表示(swap)。 */
	bchg	#0, (MEMMODE+1).l
	bsr	swap_settle
	move.w	#STAT_READY, (COMSTAT0).l
2:
	bsr	pump_poll
	tst.w	(COMCMD0).l
	bne	2b
	move.w	#0, (COMSTAT0).l
.equ ISO_HOLD_F0, 0			/* ISO診断: frame0 表示直後に静止(frame0単体の健全性確認) */
.if ISO_HOLD_F0
f0h1:
	bsr	dump_pats			/* 毎周 PREBUF1[0..] を O_LOADS へ(pumpしない=0xC000 pristine維持) */
f0hw:
	cmp.w	#CMD_SWAP, (COMCMD0).l
	bne	f0hw
	bchg	#0, (MEMMODE+1).l
	bsr	swap_settle
	move.w	#STAT_READY, (COMSTAT0).l
f0h2:
	tst.w	(COMCMD0).l
	bne	f0h2
	move.w	#0, (COMSTAT0).l
	bra	f0h1
.endif
.equ ISO_HOLD_N, 0			/* ISO診断: frame N を表示した状態で静止(0=無効) */
.equ ISO_HOLD_DUMP, 0			/* 0=クリーン静止(全画面=実フレームN) 1=内部状態ダンプ */
stream_loop:
	move.w	frame_idx, d0			/* 全フレーム処理済み=映画終端 */
	cmp.w	h_frames, d0
	bhs	movie_end
	bsr	process_frame
3:
	bsr	pump_poll			/* MD待ち中もCDを吸い上げ(溢れ防止) */
	cmp.w	#CMD_SWAP, (COMCMD0).l
	bne	3b
	bchg	#0, (MEMMODE+1).l
	bsr	swap_settle
	move.w	#STAT_READY, (COMSTAT0).l
4:
	bsr	pump_poll
	tst.w	(COMCMD0).l
	bne	4b
	move.w	#0, (COMSTAT0).l
.if ISO_HOLD_N
	cmp.w	#ISO_HOLD_N+1, frame_idx	/* frame N 処理済み=表示中 */
	bne	stream_loop
.if ISO_HOLD_DUMP
	/* ISO診断: ring_head(現pop位置)から576パターンをダンプした擬似フレームを1回出して静止 */
	bsr	dump_ring_head
.else
	/* クリーン静止: 更新0を渡す=MDは現シャドウ(=実フレームN)を再描画し続ける */
	move.w	#0, (O_NLOAD).l
	move.w	#0, (O_NUPD).l
.endif
1:
	cmp.w	#CMD_SWAP, (COMCMD0).l
	bne	1b
	bchg	#0, (MEMMODE+1).l
	bsr	swap_settle
	move.w	#STAT_READY, (COMSTAT0).l
2:
	tst.w	(COMCMD0).l
	bne	2b
	move.w	#0, (COMSTAT0).l
hold_n:
	bra	hold_n

dump_ring_head:
	/* ISO診断: 内部状態5ロング値を各32bit=32タイル(白=1,黒=0)で表示。
	   loadsはラン形式: slot 0..159 連番 = 先頭に1ランのヘッダを置く。 */
	movem.l	d0-d7/a0-a6, -(sp)
	lea	dsv_vals, a3
	moveq	#0, d0
	move.w	frame_idx, d0
	move.l	d0, (a3)			/* [0]=frame_idx(=desyncしたコマ) */
	moveq	#0, d0
	move.w	(CTRL_SCR+2).l, d0
	move.l	d0, 4(a3)			/* [1]=読んだ frame_seq(実測) */
	move.l	apply_cur, 8(a3)		/* [2]=apply_cur */
	moveq	#0, d0
	move.w	slip_count, d0
	move.l	d0, 12(a3)			/* [3]=slip_count(CDCスリップ=グリッチ検出数) */
	moveq	#0, d0
	move.w	drain_frame, d0
	swap	d0
	move.w	desync_count, d0
	move.l	d0, 16(a3)			/* [4]=drain_frame|desync_count */
	/* パレット: pal0[0]=黒, pal0[15]=白(2色でクリア表示) */
	move.w	#1, (O_PALW).l
	lea	(O_CRAM).l, a1
	move.w	#64-1, d0
1:
	clr.w	(a1)+
	dbra	d0, 1b
	move.w	#0x0EEE, (O_CRAM+30).l		/* pal0 index15 = 白 */
	/* O_LOADS: slot0=黒(0x0000), slot1=白(0xFFFF) */
	lea	(O_LOADS).l, a1
	move.w	#0, (a1)+			/* slot_start=0 */
	move.w	#2, (a1)+			/* count=2 */
	move.w	#16-1, d0
1:
	clr.w	(a1)+
	dbra	d0, 1b
	move.w	#16-1, d0
1:
	move.w	#0xFFFF, (a1)+
	dbra	d0, 1b
	/* O_UPDS: 全1120セル。row v(0..4)のcol b(0..31)= value[v] bit(31-b)。他は黒。 */
	lea	(O_UPDS).l, a2
	moveq	#0, d6				/* cell c */
uh_lp:
	moveq	#0, d0
	move.w	d6, d0
	divu	#40, d0				/* lo=v, hi=b */
	move.w	d0, d1				/* v */
	swap	d0
	move.w	d0, d2				/* b */
	moveq	#1, d3				/* 既定 ent=1 (slot0=黒) */
	cmp.w	#5, d1
	bhs	uh_put
	cmp.w	#32, d2
	bhs	uh_put
	lsl.w	#2, d1				/* v*4 */
	move.l	(a3,d1.w), d4
	moveq	#31, d5
	sub.w	d2, d5				/* bit = 31-b */
	btst	d5, d4
	beq	uh_put
	moveq	#2, d3				/* 白 slot1, ent=2 */
uh_put:
	move.w	d6, (a2)+
	move.w	d3, (a2)+
	addq.w	#1, d6
	cmp.w	#1120, d6
	blo	uh_lp
	move.w	#2, (O_NLOAD).l
	move.w	#1120, (O_NUPD).l
	movem.l	(sp)+, d0-d7/a0-a6
	rts
/* ISO診断: ring_head から 1120 パターンを VRAM slot 0.. へ生ロードし、cell c→slot c で
   そのまま並べて表示。PREBUF1 が正しくロードされていれば(scrambleでも)実タイル片が見える。
   全面ノイズなら 0xC000 のリング内容が壊れている。 */
dump_pats:
	movem.l	d0-d7/a0-a6, -(sp)
	/* グレースケール ramp を CRAM pal0 に(色ではなく生インデックス構造を見るため) */
	move.w	#1, (O_PALW).l
	lea	(O_CRAM).l, a1
	moveq	#0, d1
gp_lp:
	move.w	d1, d2
	lsr.w	#1, d2				/* L = i>>1 (0..7) */
	move.w	d2, d3
	add.w	d3, d3				/* L<<1 (R) */
	move.w	d2, d4
	lsl.w	#5, d4				/* L<<5 (G) */
	or.w	d4, d3
	move.w	d2, d4
	lsl.w	#8, d4
	add.w	d4, d4				/* L<<9 (B) */
	or.w	d4, d3
	move.w	d3, (a1)+
	addq.w	#1, d1
	cmp.w	#64, d1
	blo	gp_lp
	lea	(O_LOADS).l, a1
	move.w	#0, (a1)+			/* slot_start=0 */
	move.w	#1120, (a1)+			/* count=1120 */
	movea.l	#RING_BASE, a4			/* PREBUF1先頭(frame1 cold)を固定で見る */
	move.w	#1120*8-1, d0			/* 1120*32B = 8960 long */
1:
	move.l	(a4)+, (a1)+
	cmpa.l	#RING_END, a4
	blo	2f
	movea.l	#RING_BASE, a4
2:
	dbra	d0, 1b
	lea	(O_UPDS).l, a2
	moveq	#0, d6
3:
	move.w	d6, (a2)+			/* cell */
	move.w	d6, d0
	addq.w	#1, d0				/* ent = slot+base(=1), pal0 */
	move.w	d0, (a2)+
	addq.w	#1, d6
	cmp.w	#1120, d6
	blo	3b
	move.w	#1, (O_NLOAD).l
	move.w	#1120, (O_NUPD).l
	movem.l	(sp)+, d0-d7/a0-a6
	rts
dsv_vals:
	.long	0,0,0,0,0
sec_count:
	.long	0
xor_acc:
	.long	0
.endif
	bra	stream_loop

/* 映画終端: 最終フレームのswap要求に応えて表示→STAT_ENDをMDへ→PCM停止→
   CMD_STREAM(再開)を待って stream_start へ(=15秒待ちはMD側が数える) */
movie_end:
1:
	cmp.w	#CMD_SWAP, (COMCMD0).l
	bne	1b
	bchg	#0, (MEMMODE+1).l
	bsr	swap_settle
	move.w	#STAT_END, (COMSTAT0).l
2:
	tst.w	(COMCMD0).l
	bne	2b
	move.w	#0, (COMSTAT0).l
	move.b	#0xFF, (PCM_ONOFF).l		/* 全chオフ(静音) */
3:
	cmp.w	#CMD_STREAM, (COMCMD0).l	/* MDの再開指示を待つ */
	bne	3b
	bra	stream_start

issue_rom_readn:
	movem.l	d0-d7/a0-a6, -(sp)
	lea	bios_packet, a5
	move.l	stream_lba, (a5)
	move.l	h_stream_total, 4(a5)		/* find_fileでファイルサイズから初期化済み */
	move.l	#SUB_BANK_1M, 8(a5)
	movea.l	a5, a0
	BIOSCALL BIOS_CDC_STOP
	BIOSCALL BIOS_ROM_READN
	move.l	h_stream_total, stream_remaining
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* 滑り回復用の再シーク: d0=絶対LBA, d1=残セクタ数 で CDC_STOP+ROM_READN 再発行。
   連続読みの原則は破るが、滑り(MSFギャップ)は稀なので、失われたセクタを読み直して
   ストリームを厳密に継続する(=品質無劣化の回復)。 */
reseek_readn:
	movem.l	d0-d7/a0-a6, -(sp)
	lea	bios_packet, a5
	move.l	d0, (a5)
	move.l	d1, 4(a5)
	move.l	#SUB_BANK_1M, 8(a5)
	movea.l	a5, a0
	BIOSCALL BIOS_CDC_STOP
	BIOSCALL BIOS_ROM_READN
	move.l	d1, stream_remaining
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* 1セクタを (a0) へ CDC_TRN。連続読みをドレイン。宛先a0はBIOS呼びで壊れるので保存・再ロード。 */
drain1:
	move.l	a0, dr_dest
	lea	bios_packet, a5
1:
	BIOSCALL BIOS_CDC_STAT
	bcs	1b
2:
	BIOSCALL BIOS_CDC_READ
	bcc	2b
	move.w	#20000, d6			/* TRN有限リトライ(固着防止)。重フレームのバス競合で
						   2000では尽きてセクタ滑り→desyncしていた。転送は本来数回で
						   成功するので、上限を大きくしても正常時のコストは増えない。 */
3:
	movea.l	dr_dest, a0			/* CDC_TRN直前に宛先を再ロード */
	lea	12(a5), a1
	BIOSCALL BIOS_CDC_TRN
	bcs	4f
	subq.w	#1, d6
	bne	3b
	addq.w	#1, slip_count			/* 回復: このセクタは失われた */
	BIOSCALL BIOS_CDC_ACK
	movea.l	dr_dest, a0
	bra	drain1
4:
	BIOSCALL BIOS_CDC_ACK
	subq.l	#1, stream_remaining
	/* 滑り検出: セクタヘッダ(BCD分秒フレーム)の連番検査。飛び=slip_count++ */
	movem.l	d0-d2/a0, -(sp)
	lea	bios_packet+12, a0
	moveq	#0, d1
	move.b	(a0)+, d0
	bsr	bcd2bin
	move.w	d0, d1
	mulu	#60, d1
	move.b	(a0)+, d0
	bsr	bcd2bin
	add.w	d0, d1
	mulu	#75, d1
	move.b	(a0)+, d0
	bsr	bcd2bin
	add.l	d0, d1
	move.l	prev_msf, d2
	bne	d1_check
	move.l	d1, base_msf			/* 初回セクタ: ファイル先頭MSFを基準に記録 */
	bra	d1_ok
d1_check:
	addq.l	#1, d2				/* 期待 = prev_msf+1 */
	cmp.l	d1, d2
	beq	d1_ok				/* 連番=OK */
	/* MSFギャップ=滑り: 失われたセクタ(prev_msf+1=d2)へ再シークして読み直す(厳密回復)。
	   apply は total_len 前置きの可変長ストリームで、payloadだけ「穴」を空けても control が
	   落ちると parse がズレて永久desync(F273フリーズ実測)。かつ再シークの CDC_STOP は CDC を
	   リセットして後続の連鎖滑りを抑える(全payload-skipにすると滑り数が 38→59 に増える実測)。
	   絶対LBA = stream_lba + (d2 - base_msf), 残 = h_stream_total - (d2 - base_msf)。 */
	addq.w	#1, slip_count
	move.l	d2, d0
	sub.l	base_msf, d0			/* ファイル相対セクタ */
	move.l	h_stream_total, d1
	sub.l	d0, d1				/* 残セクタ数 */
	add.l	stream_lba, d0			/* 絶対LBA */
	bsr	reseek_readn
	movem.l	(sp)+, d0-d2/a0			/* 保存レジスタ復帰 */
	movea.l	dr_dest, a0			/* drain1再入用に宛先を復元 */
	bra	drain1				/* 再読み: CDCは今度 d2 を返す→連番に戻る */
d1_ok:
	move.l	d1, prev_msf
	movem.l	(sp)+, d0-d2/a0
	rts

bcd2bin:
	move.w	d0, -(sp)
	lsr.w	#4, d0
	mulu	#10, d0
	move.w	d0, d2
	move.w	(sp)+, d0
	andi.w	#0x0F, d0
	add.w	d2, d0
	rts

/* d0=セクタ数, a0=先頭(線形)。d0 セクタを a0.. へドレイン。 */
drain_lin:
	movem.l	d0-d7/a0-a6, -(sp)
	move.w	d0, d7
	subq.w	#1, d7
dl_loop:
	movem.l	d7/a0, -(sp)
	bsr	drain1				/* (a0)へ1セクタ */
	movem.l	(sp)+, d7/a0
	lea	0x800(a0), a0
	dbra	d7, dl_loop
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* d0=セクタ数, a0=先頭(線形)。CDC_TRNをPRG直行させず STAGE(PAD_SCR=Word-RAM)経由で
   CPUコピー。AGENTS.md: CDC_TRN→PRG直行はリトライ中にセクタが滑る(frame0ヘッダの
   直読みで frame0 全面ノイズ化の実証)。boot のヘッダブロック読みはこの安全経路を使う。 */
drain_lin_staged:
	movem.l	d0-d7/a0-a6, -(sp)
	move.w	d0, d7
	subq.w	#1, d7
dls_loop:
	movem.l	d7/a0, -(sp)
	lea	PAD_SCR, a0
	bsr	drain1				/* CD→PAD_SCR(Word-RAM STAGE) */
	movem.l	(sp)+, d7/a0
	movem.l	d7/a0, -(sp)
	movea.l	a0, a1				/* stage_copy: PAD_SCR→a1(dest) */
	bsr	stage_copy
	movem.l	(sp)+, d7/a0
	lea	0x800(a0), a0
	dbra	d7, dls_loop
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* d0=セクタ数。ring_tail へ CDC_TRN(循環)。 */
drain_ring:
	movem.l	d0-d7/a0-a6, -(sp)
	move.w	d0, d7
	subq.w	#1, d7
dr_loop:
	movea.l	ring_tail, a0
	movem.l	d7, -(sp)
	bsr	drain1
	movem.l	(sp)+, d7
	movea.l	ring_tail, a0
	lea	0x800(a0), a0
	cmpa.l	#RING_END, a0
	blo	1f
	movea.l	#RING_BASE, a0
1:
	move.l	a0, ring_tail
	dbra	d7, dr_loop
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* d0=セクタ数。apply_tail へ CDC_TRN(循環)。 */
drain_apply:
	movem.l	d0-d7/a0-a6, -(sp)
	move.w	d0, d7
	subq.w	#1, d7
da_loop:
	movea.l	apply_tail, a0
	movem.l	d7, -(sp)
	bsr	drain1
	movem.l	(sp)+, d7
	movea.l	apply_tail, a0
	lea	0x800(a0), a0
	cmpa.l	#APPLY_END, a0
	blo	1f
	movea.l	#APPLY_BASE, a0
1:
	move.l	a0, apply_tail
	dbra	d7, da_loop
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* ---- ポンプ方式ドレイン ----
   CDは止まらず75セクタ/秒を吐き続けるので、取り込みをフレーム処理のテンポに縛ると
   MD側が重いフレームでCDC内部バッファが溢れセクタが失われる(以降ずっとズレる)。
   → セクタ単位カーソル(drain_frame, drain_k)で「届いたら即取り込む」。
   MD待ちループ中も pump_poll で吸い上げ、受け側(リング/apply)の余裕だけ確認する。 */

/* 1セクタを取り込む(ブロッキング)。CD→常にWord-RAM STAGE(実績ある経路)→
   CPUコピーで routing に従い PRG(リング/apply)へ振り分け。
   (CDC_TRN→PRG直行はリトライ中にセクタが滑る事故が起きる: 実測+1/2フレーム) */
/* v4 レートマッチpadding。各フレーム = fsec = max(n_pay+n_ctrl, ratedelta) セクタ。
   ratedelta = CD 1x(75 sec/s)を fps で割った整数割当(累積器 sec_acc)= ディスク読み速度を
   表示速度に一致させる pad。15fpsでは常に5(=v3固定)、30fpsは2/3平均。n_pay+n_ctrl を超える
   ぶん(pad)は読んで捨てる。fsec はコマ先頭(drain_k==0)で1回計算し cur_fsec に保持。
   routing読みは drain1(BIOS呼びで d1等破壊)の後に再読み込み。 */
pump1:
	movem.l	d0-d7/a0-a6, -(sp)
p1_top:
	moveq	#0, d0
	move.w	drain_frame, d0
	cmp.w	h_frames, d0
	bhs	p1_ret				/* ストリーム終端: 読まずに戻る */
	tst.w	drain_k
	bne	p1_read				/* コマ途中: cur_fsec は計算済み */
	/* --- コマ先頭: cur_fsec = max(total, ratedelta-lead) を計算(pack と同一の有界累積器) --- */
	add.w	d0, d0
	lea	ROUTING, a0
	move.b	(a0,d0.w), d1			/* n_pay */
	andi.w	#0xFF, d1
	move.b	1(a0,d0.w), d2			/* n_ctrl */
	andi.w	#0xFF, d2
	add.w	d1, d2				/* d2 = total = n_pay+n_ctrl */
	moveq	#0, d0				/* ratedelta = (sec_acc+75)/h_fps_int, sec_acc=余り */
	move.w	sec_acc, d0
	addi.w	#75, d0
	divu.w	h_fps_int, d0			/* d0低語=商(ratedelta), 高語=余り */
	move.w	d0, d5				/* d5 = ratedelta */
	swap	d0
	move.w	d0, sec_acc			/* sec_acc = 余り */
	move.w	d5, d6				/* delta = ratedelta - lead(先行ぶん, 負可) */
	sub.w	lead, d6
	cmp.w	d6, d2				/* cur_fsec = max(total, delta) 符号付き */
	bge	1f				/* total >= delta → cur_fsec = total */
	move.w	d6, d2				/* else cur_fsec = delta */
1:
	move.w	d2, cur_fsec
	move.w	lead, d6			/* lead += cur_fsec - ratedelta (常に≥0) */
	add.w	d2, d6
	sub.w	d5, d6
	move.w	d6, lead
	tst.w	cur_fsec			/* fsec==0(total=0かつ先行中)= ディスク上0セクタ */
	bne	p1_read
	addq.w	#1, drain_frame			/* 前進のみ、read無し(データは先行配送済み) */
	clr.w	drain_k
	bra	p1_top
p1_read:
	lea	PAD_SCR, a0			/* STAGE = Word-RAM */
	bsr	drain1				/* 1セクタ取り込み(d1/d2破壊) */
	moveq	#0, d0				/* drain1後に routing 再読み込み */
	move.w	drain_frame, d0
	add.w	d0, d0
	lea	ROUTING, a0
	move.b	(a0,d0.w), d1			/* n_pay */
	andi.w	#0xFF, d1
	move.b	1(a0,d0.w), d2			/* n_ctrl */
	andi.w	#0xFF, d2
	move.w	d1, d4
	add.w	d2, d4				/* d4 = total = n_pay+n_ctrl (stage_copyでも不変) */
	move.w	drain_k, d3
	cmp.w	d1, d3
	blo	p1_ring				/* k < n_pay */
	cmp.w	d4, d3
	blo	p1_apply			/* k < total */
	bra	p1_adv				/* k >= total: pad セクタ(捨て) */
p1_ring:
	movea.l	ring_tail, a1
	bsr	stage_copy			/* STAGE→PRG 2048B CPUコピー */
	movea.l	ring_tail, a0
	lea	0x800(a0), a0
	cmpa.l	#RING_END, a0
	blo	1f
	movea.l	#RING_BASE, a0
1:
	move.l	a0, ring_tail
	bra	p1_adv
p1_apply:
	movea.l	apply_tail, a1
	bsr	stage_copy
	movea.l	apply_tail, a0
	lea	0x800(a0), a0
	cmpa.l	#APPLY_END, a0
	blo	1f
	movea.l	#APPLY_BASE, a0
1:
	move.l	a0, apply_tail
p1_adv:
	addq.w	#1, drain_k
	move.w	drain_k, d3
	cmp.w	cur_fsec, d3			/* v4: fsec(=max(total,rate) レートマッチ)セクタで1コマ完了 */
	blo	p1_ret
	clr.w	drain_k
	addq.w	#1, drain_frame
p1_ret:
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* STAGE(PAD_SCR)の2048Bを a1 へCPUコピー。trashes d0/a0/a1。 */
stage_copy:
	lea	PAD_SCR, a0
	move.w	#128-1, d0
1:
	move.l	(a0)+, (a1)+
	move.l	(a0)+, (a1)+
	move.l	(a0)+, (a1)+
	move.l	(a0)+, (a1)+
	dbra	d0, 1b
	rts

/* ノンブロッキング: CDCにセクタが用意できていて、受け側に余裕があれば1セクタ取り込む。
   MD待ちループから毎回呼ぶ。 */
pump_poll:
	movem.l	d0-d7/a0-a6, -(sp)
	move.w	drain_frame, d0
	beq	pp_done				/* v2: frame0展開中は drain_frame=0。ここで pump すると
						   routing[0]=(0,0) によりframe1の実セクタをpad扱いで捨て、
						   CD位置とdrain_k/frameが N セクタ desync → frame1が化ける。
						   streaming state(drain_frame>=1)確立まで pump しない。 */
	cmp.w	h_frames, d0
	bcc	pp_done				/* ストリーム終端 */
	/* リング余裕: occupied = (tail-head) mod SIZE が SIZE-0x1000 以上なら見送り */
	move.l	ring_tail, d0
	sub.l	ring_head, d0
	bpl	1f
	add.l	#RING_SIZE, d0
1:
	cmp.l	#RING_SIZE-0x1000, d0
	bcc	pp_done
	/* apply余裕 */
	move.l	apply_tail, d0
	sub.l	apply_cur, d0
	bpl	2f
	add.l	#APPLY_SIZE, d0
2:
	cmp.l	#APPLY_SIZE-0x1000, d0
	bcc	pp_done
	/* CDCにセクタ準備できてる? (CDC_STAT: キャリー=未準備) */
	BIOSCALL BIOS_CDC_STAT
	bcs	pp_done
	bsr	pump1
pp_done:
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* 1フレーム: frame_idx のセクタが全部届くまでポンプ → control取り出し → expand → 音声。 */
process_frame:
	movem.l	d0-d7/a0-a6, -(sp)
	move.w	frame_idx, d0
pf_pump:
	cmp.w	drain_frame, d0
	blo	pf_ready			/* drain_frame > frame_idx = このフレーム分完了 */
	bsr	pump1				/* ブロッキングで取り込む */
	bra	pf_pump
pf_ready:
	addq.w	#1, frame_idx
	bsr	fetch_control			/* apply循環 → CTRL_SCR 線形化 */
	/* 同期チェック: control先頭の frame_seq(CTRL_SCR+2) が 期待値(frame_idx-1) と一致するか。
	   ズレ=desync(CDCセクタ落ち等)。破棄して前コマ維持(0更新)し、診断カウントを出す。
	   持続desyncは前コマ静止に化ける(=青赤崩壊より軽い)。 */
	move.w	(CTRL_SCR+2).l, d0		/* 実 frame_seq */
	move.w	frame_idx, d1
	subq.w	#1, d1				/* 期待 seq */
	cmp.w	d1, d0
	bne	pf_desync
	bsr	pump_poll
	bsr	expand_frame			/* CTRL_SCR → Word-RAM 出力 + 音声 */
	movem.l	(sp)+, d0-d7/a0-a6
	rts
pf_desync:
	addq.w	#1, desync_count
	move.w	desync_count, (COMSTAT1).l	/* 診断: desync回数 */
.equ DESYNC_DUMP, 0				/* 1: 初回desyncで[frame_idx,seq,apply_cur,apply_tail,drainF|cnt]を表示して静止 */
.if DESYNC_DUMP
	cmp.w	#1, desync_count
	bne	1f
	bsr	dump_ring_head
dsd_lp:
	cmp.w	#CMD_SWAP, (COMCMD0).l
	bne	dsd_lp
	bchg	#0, (MEMMODE+1).l
	bsr	swap_settle
	move.w	#STAT_READY, (COMSTAT0).l
dsd_w:
	tst.w	(COMCMD0).l
	bne	dsd_w
	move.w	#0, (COMSTAT0).l
	bra	dsd_lp
1:
.endif
	move.w	#0, (O_NLOAD).l			/* このコマは更新破棄=前コマ維持 */
	move.w	#0, (O_NUPD).l
	bsr	pump_poll
	movem.l	(sp)+, d0-d7/a0-a6
	rts

/* apply-buffer(PRG循環, apply_cur) から control block を CTRL_SCR(Word-RAM) へコピー(折返し線形化)。
   block先頭 >H total_len。apply_cur を total_len 進める。 */
fetch_control:
	/* issue#15: 呼び出し元 process_frame が既に d0-d7/a0-a6 を退避済み(唯一の呼び出し元)。
	   戻り後は process_frame が d0/d1 を再ロードするので、ここでの退避は冗長=削除。 */
	movea.l	apply_cur, a0
	move.w	(a0), d7			/* total_len */
	/* コピー: total_len バイトを a0(循環) → CTRL_SCR */
	lea	CTRL_SCR, a1
	move.w	d7, d6				/* 残バイト */
	/* first_part = min(total_len, APPLY_END - a0) */
	move.l	#APPLY_END, d0
	sub.l	a0, d0				/* d0 = APPLY_END - a0 (bytes) */
	cmp.w	d6, d0
	bcc	fc_nowrap			/* d0 >= 残 なら折返し無し */
	/* 折返し: first=d0 バイト, 残り d6-d0 */
	move.w	d0, d5				/* first bytes */
	sub.w	d5, d6				/* 残り */
	lsr.w	#1, d5				/* words */
	subq.w	#1, d5
1:
	move.w	(a0)+, (a1)+
	dbra	d5, 1b
	movea.l	#APPLY_BASE, a0
fc_nowrap:
	/* 残 d6 バイトをコピー */
	tst.w	d6
	beq	fc_done
	move.w	d6, d5
	lsr.w	#1, d5
	subq.w	#1, d5
2:
	move.w	(a0)+, (a1)+
	dbra	d5, 2b
fc_done:
	/* apply_cur += total_len, 折返し */
	movea.l	apply_cur, a0
	adda.w	d7, a0
	cmpa.l	#APPLY_END, a0
	blo	3f
	suba.l	#APPLY_SIZE, a0
3:
	move.l	a0, apply_cur
	rts					/* issue#15: 冗長movem削除(process_frameが退避済み) */

/* CTRL_SCR(線形 control block) を Word-RAM へ展開。cold は ring pop。
   block = >H total_len >H frame_seq >H n_upd >B pal >B dbg [22B DEBUG if dbg]
           72 bitmap n_upd*(entry) 887 audio [even pad]   (MOVIE.md 準拠)
   v3: pal = 区間番号+1(0=切替なし)。CRAM本体はboot時にMain-RAM表へ渡し済み(PALTAB)。
   loads はラン形式: [slot_start.w count.w pattern(count*32B)] の列。エンコーダが
   フレーム内coldを連番スロットに割当てるので、MDは1ランを1回の大DMAで転送できる。 */
expand_frame:
	movem.l	d0-d7/a0-a6, -(sp)
	lea	CTRL_SCR, a0
	addq.l	#4, a0				/* skip total_len(2) + frame_seq(2) */
	move.w	(a0)+, d5			/* n_upd (使わないが読み飛ばし用に保持) */
	move.w	(a0)+, d0			/* pal(hi) dbg(lo) */
	lea	(O_DBG).l, a1
	move.w	d0, d4
	andi.w	#0xFF, d4			/* dbg フラグ */
	beq	ef_nodbg
	moveq	#11-1, d1			/* 22B デバッグブロックを MD へ転写 */
1:
	move.w	(a0)+, (a1)+
	dbra	d1, 1b
	bra	ef_pal
ef_nodbg:
	moveq	#11-1, d1			/* デバッグ欄なし: MD表示をゼロで消す */
1:
	clr.w	(a1)+
	dbra	d1, 1b
ef_pal:
	move.w	d0, d4
	lsr.w	#8, d4				/* pal = 区間番号+1(0=切替なし) — MDはMain-RAM表を引く */
	move.w	d4, (O_PALW).l
ef_bm:
.equ ISO_DUMP_OFF, 0
	movea.l	a0, a3				/* bitmap(ceil(cells/8)B) */
	move.w	h_bmbytes, d0
	adda.w	d0, a0				/* entries */
	lea	(O_LOADS).l, a1
	lea	(O_UPDS).l, a2
	movea.l	ring_head, a4			/* pop ptr(PRG読み) */
	tst.w	f0_expand
	beq	1f
	movea.l	f0_pat_addr, a4			/* frame0: popは Word RAM f0pat から(ring_headはpump occ用に0xC000維持) */
1:
	moveq	#0, d4				/* n_load */
	moveq	#0, d5				/* n_upd */
	moveq	#0, d6				/* cell base */
	suba.l	a5, a5				/* a5=開いているランのcountワード位置(0=無し) */
	movea.w	#-1, a6				/* a6=次に連結できるスロット(無効値で開始) */
	clr.w	run_cnt
	move.w	h_bmbytes, d7
	subq.w	#1, d7
ef_byte:
	/* pump_poll は重い(15レジ退避+BIOS_CDC_STAT)。CDは166kサイクルに1セクタしか来ないので
	   毎バイト(全画面140回/コマ)は過剰。8バイト毎(~18回/コマ)でも十分取りこぼさない。
	   空振りpollを削り Sub時間を空ける=重コマのfps落ちを詰めて滑り天井を上げる狙い。 */
	move.w	d7, d0
	andi.w	#31, d0				/* issue#15: 32バイト毎。展開全体で~6.5ms<<1セクタ22msなのでCDC溢れ無し */
	bne	ef_nopump
	bsr	pump_poll			/* 長い展開中もCDを取りこぼさない(頻度は落としても間に合う) */
ef_nopump:
	move.b	(a3)+, d0
	beq	ef_next
	moveq	#0, d1
ef_bit:
	btst	d1, d0
	beq	ef_skip
	move.w	(a0)+, d2			/* entry */
	move.w	d6, d3
	add.w	d1, d3				/* cell */
	move.w	d3, (a2)+
	move.w	d2, d3
	andi.w	#0x7FFF, d3			/* ent */
	move.w	d3, (a2)+
	addq.w	#1, d5
	btst	#15, d2
	beq	ef_skip
	tst.w	f0_expand
	bne	ef_no_occ			/* frame0(Word RAM f0pat): occ迂回(1008<=1024で十分) */
	/* graceful underrun: cold は ring pop(32B)が要る。リングが枯渇(available<32B)なら、
	   ゴミを読まず「このコマの以降の更新を打ち切る」=残り全セルは前コマ維持(stale/残像)。
	   持続重シーンがCD帯域を超えても崩壊でなく軽い残像に化ける。全レジスタ使用中なので
	   occ計算はd0退避で。 */
	move.l	d0, -(sp)
	move.l	ring_tail, d0
	sub.l	a4, d0				/* occ = ring_tail - a4 (mod RING_SIZE) */
	bge	1f
	add.l	#RING_SIZE, d0
1:
	cmp.l	#32, d0
	bhs	2f				/* >=32B: 1パターン分ある→pop可 */
	move.l	(sp)+, d0			/* 枯渇: このセルのupdを取消(stale維持) */
	subq.l	#4, a2
	subq.w	#1, d5
	bra	ef_finalize			/* 開いているランを閉じてコマ確定(残りは stale) */
2:
	move.l	(sp)+, d0
ef_no_occ:
	move.w	d3, d2
	andi.w	#0x07FF, d2
	subq.w	#1, d2				/* slot */
	cmpa.w	d2, a6				/* 直前ランの続き(=期待スロット)か? */
	beq	ef_cont
	cmpa.w	#0, a5				/* 前のランを閉じる(countを書き戻す) */
	beq	1f
	move.w	run_cnt, (a5)
1:
	move.w	d2, (a1)+			/* slot_start */
	movea.l	a1, a5				/* countワードの位置を覚える */
	clr.w	(a1)+
	clr.w	run_cnt
ef_cont:
	addq.w	#1, run_cnt
	movea.w	d2, a6
	addq.l	#1, a6				/* 期待スロット = slot+1 */
	/* ring pop 32B: RING/O_LOADSとも4バイト整列・パターンはリング末端を跨がない
	   (RING_SIZEはセクタ倍数, パターンは32B整列)ので move.l×8 で一括コピー */
	move.l	(a4)+, (a1)+
	move.l	(a4)+, (a1)+
	move.l	(a4)+, (a1)+
	move.l	(a4)+, (a1)+
	move.l	(a4)+, (a1)+
	move.l	(a4)+, (a1)+
	move.l	(a4)+, (a1)+
	move.l	(a4)+, (a1)+
	tst.w	f0_expand
	bne	1f				/* frame0: Word RAM連続=wrap不要 */
	cmpa.l	#RING_END, a4
	blo	1f
	movea.l	#RING_BASE, a4
1:
	addq.w	#1, d4
ef_skip:
	addq.w	#1, d1
	cmp.w	#8, d1
	blo	ef_bit
ef_next:
	addq.w	#8, d6
	dbra	d7, ef_byte
ef_finalize:					/* 通常終了 or リング枯渇での打ち切り合流点 */
	cmpa.w	#0, a5				/* 最後のランを閉じる */
	beq	1f
	move.w	run_cnt, (a5)
1:
	move.w	d4, (O_NLOAD).l
	move.w	d5, (O_NUPD).l
	move.w	slip_count, (O_SLIP).l	/* 滑り(=再シーク回復)回数をMDへ=グリッチマーカー */
	move.w	desync_count, (O_DSY).l	/* desync検知回数をMDへ(再シーク回復が効けば0のまま) */
	move.w	resync_count, (O_RESYNC).l	/* 計測: 音声re-sync回数をMDへ */
	move.w	cur_lead, (O_LEAD).l		/* 計測: 現コマの音声リードをMDへ */
	tst.w	f0_expand
	bne	1f
	move.l	a4, ring_head			/* frame0はring_head書き戻さない(0xC000維持=frame1がPREBUF1から) */
1:
	/* 音声: entries の直後(a0) に 887B */
	movea.l	a0, a5
	movea.l	a5, a0
	bsr	write_wave_chunk
	movem.l	(sp)+, d0-d7/a0-a6
	rts

swap_settle:
	movem.l	d0, -(sp)
	move.w	#0x0400, d0
1:
	dbra	d0, 1b
	movem.l	(sp)+, d0
	rts

read_cd:
	movem.l	d0-d7/a0-a6, -(sp)
	lea	bios_packet, a5
	move.l	d0, (a5)
	move.l	d1, 4(a5)
	move.l	a0, 8(a5)
	movea.l	a5, a0
	BIOSCALL BIOS_CDC_STOP
	BIOSCALL BIOS_ROM_READN
wait_stat:
	BIOSCALL BIOS_CDC_STAT
	bcs	wait_stat
wait_read:
	BIOSCALL BIOS_CDC_READ
	bcc	wait_read
wait_transfer:
	movea.l	8(a5), a0
	lea	12(a5), a1
	BIOSCALL BIOS_CDC_TRN
	bcc	wait_transfer
	BIOSCALL BIOS_CDC_ACK
	addq.l	#1, (a5)
	addi.l	#0x0800, 8(a5)
	subq.l	#1, 4(a5)
	bne	wait_stat
	movem.l	(sp)+, d0-d7/a0-a6
	rts

init_iso9660:
	movem.l	d0-d7/a0-a6, -(sp)
	move.l	#0x10, d0
	move.l	#2, d1
	lea	ISO_BUF, a0
	bsr	read_cd
	lea	ISO_BUF, a0
	lea	156(a0), a1
	moveq	#0, d0
	move.b	6(a1), d0
	lsl.l	#8, d0
	move.b	7(a1), d0
	lsl.l	#8, d0
	move.b	8(a1), d0
	lsl.l	#8, d0
	move.b	9(a1), d0
	move.l	#0x20, d1
	lea	ISO_BUF, a0
	bsr	read_cd
	movem.l	(sp)+, d0-d7/a0-a6
	rts

find_file:
	movem.l	a1-a2/a6, -(sp)
	lea	ISO_BUF, a1
read_filename_start:
	movea.l	a0, a6
	move.b	(a6)+, d0
find_first_char:
	movea.l	a1, a2
	cmp.b	(a1)+, d0
	bne	find_first_char
check_chars:
	move.b	(a6)+, d0
	beq	get_info
	cmp.b	(a1)+, d0
	bne	read_filename_start
	bra	check_chars
get_info:
	sub.l	#33, a2
	moveq	#0, d0
	move.b	6(a2), d0
	lsl.l	#8, d0
	move.b	7(a2), d0
	lsl.l	#8, d0
	move.b	8(a2), d0
	lsl.l	#8, d0
	move.b	9(a2), d0
	/* ファイルサイズ(BE @14) → 総セクタ数を h_stream_total に初期化。
	   ROM_READN はヘッダ解析より先に走るため、初回はこの値を使う
	   (解析後に同値で再計算される)。 */
	moveq	#0, d1
	move.b	14(a2), d1
	lsl.l	#8, d1
	move.b	15(a2), d1
	lsl.l	#8, d1
	move.b	16(a2), d1
	lsl.l	#8, d1
	move.b	17(a2), d1
	add.l	#2047, d1
	moveq	#11, d2
	lsr.l	d2, d1
	move.l	d1, h_stream_total
	movem.l	(sp)+, a1-a2/a6
	rts

/* ---- RF5C164 PCM ---- */
init_pcm:
	movem.l	d0-d2/a0, -(sp)
	moveq	#0, d2
ip_loop:
	move.w	d2, d1
	andi.w	#0x0FFF, d1
	bne	1f
	move.w	d2, d0
	lsr.w	#8, d0
	lsr.w	#4, d0
	ori.b	#0x80, d0
	move.b	d0, (PCM_CTRL).l
1:
	lea	(PCM_WAVE).l, a0
	add.w	d1, d1
	adda.w	d1, a0
	move.b	#0x00, (a0)
	addq.w	#1, d2
	cmp.w	#WAVE_RING_END, d2
	blo	ip_loop
	move.b	#0x88, (PCM_CTRL).l
	move.b	#0xFF, (PCM_WAVE).l
	move.b	#0xC0, (PCM_CTRL).l
	move.b	#0xFF, (PCM_ENV).l
	nop
	nop
	move.b	#0xFF, (PCM_PAN).l
	nop
	nop
	move.b	#0x45, (PCM_FDL).l
	nop
	nop
	move.b	#0x03, (PCM_FDH).l
	nop
	nop
	move.b	#0x00, (PCM_LSL).l
	nop
	nop
	move.b	#0x00, (PCM_LSH).l
	nop
	nop
	move.b	#0x00, (PCM_ST).l
	nop
	nop
	move.w	#0, write_ptr
	movem.l	(sp)+, d0-d2/a0
	rts

pcm_on:
	move.b	#0xFE, (PCM_ONOFF).l
	rts

write_wave_chunk:
	movem.l	d0-d5/a0-a1, -(sp)
	moveq	#0, d5
	move.b	(PCM_PLAY_H).l, d5
	lsl.w	#8, d5
	move.w	write_ptr, d2
	move.w	d2, d0
	sub.w	d5, d0
	andi.w	#RING_MASK, d0			/* d0 = lead(書込-再生) */
	move.w	d0, cur_lead			/* 計測: 現リードを記録(HUD表示) */
	cmp.w	#SYNC_MIN, d0
	blo	1f
	cmp.w	#SYNC_MAX, d0
	bls	2f
1:
	addq.w	#1, resync_count		/* 計測: re-sync発生(リード逸脱で書込ジャンプ=乱れ) */
	move.w	d5, d2
	add.w	#SYNC_LEAD, d2
	andi.w	#RING_MASK, d2
2:
	move.w	h_audio_bytes, d3		/* v4: 1コマ音声B(可変) */
	subq.w	#1, d3
	/* issue#15: 初期バンク設定＋走行ポインタ a1 を用意。以降は毎バイト a1+=2 だけ(lea/add/adda
	   をバイト毎に再計算しない)。バンク境界(0x1000毎)とリング折返しで a1 を PCM_WAVE に戻す。 */
	move.w	d2, d0
	lsr.w	#8, d0
	lsr.w	#4, d0
	ori.b	#0x80, d0
	move.b	d0, (PCM_CTRL).l		/* 初期バンク */
	move.w	d2, d4
	andi.w	#0x0FFF, d4
	add.w	d4, d4
	lea	(PCM_WAVE).l, a1
	adda.w	d4, a1				/* a1 = 初期wave書込ポインタ */
wwc_loop:
	move.w	d2, d0
	andi.w	#0xFF, d0			/* issue#15: 0x100バイト毎(音声全体~443Bで~2回、CDC溢れ無し) */
	bne	3f
	bsr	pump_poll			/* 音声書込中もCDを取りこぼさない(pump_pollはa1保存) */
3:
	move.b	(a0)+, (a1)			/* 書込 */
	addq.w	#2, a1				/* 次wave slot(奇数バイト窓=×2) */
	addq.w	#1, d2
	cmp.w	#WAVE_RING_END, d2
	blo	4f
	moveq	#0, d2				/* リング折返し: bank0, a1=先頭 */
	move.b	#0x80, (PCM_CTRL).l
	lea	(PCM_WAVE).l, a1
	bra	2f
4:
	move.w	d2, d4
	andi.w	#0x0FFF, d4
	bne	2f				/* バンク境界でなければそのまま */
	move.w	d2, d0				/* バンク境界: バンク更新＋a1を先頭へ */
	lsr.w	#8, d0
	lsr.w	#4, d0
	ori.b	#0x80, d0
	move.b	d0, (PCM_CTRL).l
	lea	(PCM_WAVE).l, a1
2:
	dbra	d3, wwc_loop
	move.w	d2, write_ptr
	move.b	#0xC0, (PCM_CTRL).l
	movem.l	(sp)+, d0-d5/a0-a1
	rts

.global sp_int2
sp_int2:
	rts
.global sp_user
sp_user:
	rts

drv_init_tracklist:
	.byte	1, 0xFF
file_stream:
	.asciz	"MOVIE.DAT"
	.align	2

bios_packet:
	.long	0, 0, 0, 0, 0
stream_lba:
	.long	0
stream_remaining:
	.long	0
dr_dest:
	.long	0
prev_msf:
	.long	0
base_msf:
	.long	0				/* ファイル先頭セクタのMSF-linear(=stream_lba相当)。滑り時の再シーク基準 */
slip_count:
	.word	0
	.word	0
ring_head:
	.long	0
ring_tail:
	.long	0
apply_tail:
	.long	0
apply_cur:
	.long	0
frame_idx:
	.word	0
drain_frame:
	.word	0
h_frames:
	.space 2
h_fsec:
	.space 2
h_bmbytes:
	.space 2
h_routing_sec:
	.space 2
h_prebuf_sec:
	.space 2
h_prebuf_pat:
	.space 4
h_f0_ctrl_sec:
	.space 2
h_f0_pat_sec:
	.space 2
h_paltab_sec:
	.space 2
h_vsync_n:
	.space 2				/* v4: 1コマの表示VBLANK数(30fps=2, 15fps=4) */
h_audio_bytes:
	.space 2				/* v4: 1コマの音声バイト(30fps=443, 15fps=887) */
h_fps_int:
	.space 2				/* v4: 名目fps(15/30)。レートマッチpadding累積器の除数 */
sec_acc:
	.space 2				/* v4: CD 1x レート累積器の余り(0..fps-1) */
cur_fsec:
	.space 2				/* v4: 現コマのディスクセクタ数 fsec=max(total,ratedelta-lead) */
lead:
	.space 2				/* v4: ディスクがCD 1x予定より先行しているセクタ数(≥0) */
f0_pat_addr:
	.space 4
h_stream_total:
	.space 4
drain_k:
	.word	0
write_ptr:
	.word	0
run_cnt:
	.word	0
f0_expand:
	.word	0				/* !=0: expand_frameでcold popのring wrap/occ迂回(frame0=Word RAM) */
desync_count:
	.word	0				/* control同期マーカー不一致の累積(診断) */
resync_count:
	.word	0				/* 計測: 音声re-sync累積(リード逸脱=書込ジャンプ=乱れ) */
cur_lead:
	.word	0				/* 計測: 現コマの音声リード(write-play) */

sp_end:
