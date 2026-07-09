/*
 * PRG-RAM 書込テスト(Sub側)。CD読込の有無で高位PRGへCPU書込できるかを切り分ける。
 * 進捗を COMSTAT0 に記録: 書込がハングすればその段階で止まる=MD側で色として観測できる。
 *   1=開始 2=0x20000(読込無) 3=0x40000(読込無) 4=0x60000(読込無=全部OK)
 *   5=読込開始中 6=読込active 7=0x20000(読込中) 8=0x40000(読込中) 9=0x60000(読込中=全部OK)
 *   0x80=読み戻し不一致
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
.equ COMSTAT0,    SUB_GA_BASE+0x0020
.equ SUB_BANK_1M, 0x000C0000

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
	rts

.global sp_main
sp_main:
	movea.l	#0x0007FF00, sp
	move.w	#1, (COMSTAT0).l
	/* --- 読込 無し で高位PRGへ書込 --- */
	movea.l	#0x00020000, a0
	bsr	wrtest
	bne	fail
	move.w	#2, (COMSTAT0).l
	movea.l	#0x00040000, a0
	bsr	wrtest
	bne	fail
	move.w	#3, (COMSTAT0).l
	movea.l	#0x00060000, a0
	bsr	wrtest
	bne	fail
	move.w	#4, (COMSTAT0).l
	/* --- CD読込を開始 --- */
	bsr	start_cd_read
	move.w	#6, (COMSTAT0).l
	/* --- TEST: 1回ドレイン後 CDC_STOP で連続読込を止めてから append バースト --- */
	move.l	#0x000552A0, ring_t
	move.l	#10000, remain
	bsr	drain5
	BIOSCALL BIOS_CDC_STOP			/* 連続読込を止める */
	move.w	#50-1, d5
frmloop:
	movea.l	#0x00010000, a0
	movea.l	ring_t, a1
	move.w	#264-1, d2
aploop:
	move.w	#16-1, d3
2:
	move.w	(a0)+, (a1)+
	dbra	d3, 2b
	cmpa.l	#0x0007B000, a1
	blo	3f
	movea.l	#0x0000A000, a1
3:
	dbra	d2, aploop
	move.l	a1, ring_t
	dbra	d5, frmloop
	move.w	#9, (COMSTAT0).l		/* 完走=読込ドレイン中のPRG書込OK */
hold:
	bra	hold
fail:
	move.w	#0x80, (COMSTAT0).l
	bra	hold

/* a0=addr: 0xA5A5 を書いて読み戻す。一致でZ。書込不能ならここでハング(戻らない)。 */
wrtest:
	move.w	#0xA5A5, (a0)
	cmp.w	#0xA5A5, (a0)
	rts

/* CDを5セクタ STAGING(0x8000) へドレイン(アクティブ読込を模す)。 */
drain5:
	movem.l	d0-d7/a0-a6, -(sp)
	move.l	#0x000C0000, dr_dest		/* Word-RAMへドレイン(PRGでなく) */
	move.w	#5, dr_cnt
ds_sec:
	lea	bios_packet, a5
1:
	BIOSCALL BIOS_CDC_STAT
	bcs	1b
2:
	BIOSCALL BIOS_CDC_READ
	bcc	2b
3:
	movea.l	dr_dest, a0
	lea	12(a5), a1
	BIOSCALL BIOS_CDC_TRN
	bcc	3b
	BIOSCALL BIOS_CDC_ACK
	add.l	#0x0800, dr_dest
	subq.l	#1, remain
	subq.w	#1, dr_cnt
	bne	ds_sec
	movem.l	(sp)+, d0-d7/a0-a6
	rts

start_cd_read:
	move.w	#5, (COMSTAT0).l
	andi.b	#0xFA, (MEMMODE+1).l
	lea	drv_init_tracklist, a0
	BIOSCALL BIOS_DRV_INIT
1:
	BIOSCALL BIOS_CDB_STAT
	andi.b	#0xF0, (CDB_STAT).w
	bne	1b
	bset	#2, (MEMMODE+1).l
	lea	bios_packet, a5
	move.l	#16, (a5)			/* LBA 16 から */
	move.l	#10000, 4(a5)			/* 大量に=連続読み継続 */
	move.l	#SUB_BANK_1M, 8(a5)
	movea.l	a5, a0
	BIOSCALL BIOS_CDC_STOP
	BIOSCALL BIOS_ROM_READN
	rts

.global sp_int2
sp_int2:
	rts
.global sp_user
sp_user:
	rts

drv_init_tracklist:
	.byte	1, 0xFF
	.align	2
bios_packet:
	.long	0, 0, 0, 0, 0
ring_t:
	.long	0
remain:
	.long	0
dr_dest:
	.long	0
dr_cnt:
	.word	0
sp_end:
