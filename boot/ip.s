.equ STACK, 0x00FFFD00

.equ EXVEC_LEVEL6, 0x00FFFD08
.equ BIOS_VBLANK_HANDLER_FLAGS, 0x00FFFE26
.equ BIOS_VBLANK_HANDLER, 0x00000290
.equ BIOS_CLEAR_VRAM, 0x000002A0
.equ BIOS_LOAD_DEFAULT_VDP_REGS, 0x000002AC
.equ BIOS_CLEAR_COMM, 0x00000340

.equ GA_MEMMODE, 0x00A12002
.equ GA_RET_BIT, 0
.equ GA_DMNA_BIT, 1
.equ GA_COMCMD0, 0x00A12010
.equ GA_COMSTAT0, 0x00A12020
.equ WORD_RAM, 0x00200000
.equ MAIN_RAM_ENTRY, 0x00FF8000
.equ M_INIT_COPY_WORDS, (0x4000/2)

.equ CMD_LOAD_M_INIT, 1
.equ CMD_RETURN_WORD_RAM, 2

.text

	.incbin "security.bin"

	/* Some Mega-CD security blocks continue at 0x156, while common BIOS
	   documentation describes post-security execution at 0x584. Keep a
	   branch stub at 0x156 and put the real program at 0x584 so either
	   path reaches the same code. */
	bra.w	ip_entry
	.org	0x584

.global ip_entry
ip_entry:
	move.w	#0x2700, sr
	lea	STACK, sp

	bsr	init_bios_runtime
	bsr	grant_word_ram_to_sub
	move.w	#CMD_LOAD_M_INIT, d0
	bsr	sub_command
	move.w	#CMD_RETURN_WORD_RAM, d0
	bsr	sub_command
	bsr	wait_word_ram_main
	bsr	copy_m_init_to_main_ram
	jmp	MAIN_RAM_ENTRY

init_bios_runtime:
	jsr	BIOS_LOAD_DEFAULT_VDP_REGS
	jsr	BIOS_CLEAR_VRAM
	jsr	BIOS_CLEAR_COMM
	move.b	#0x00, (BIOS_VBLANK_HANDLER_FLAGS).l
	move.l	#BIOS_VBLANK_HANDLER, (EXVEC_LEVEL6).l
	move.w	#0x2000, sr
	rts

grant_word_ram_to_sub:
	bset	#GA_DMNA_BIT, (GA_MEMMODE+1).l
1:
	btst	#GA_DMNA_BIT, (GA_MEMMODE+1).l
	beq	1b
	rts

sub_command:
	move.w	d0, (GA_COMCMD0).l
1:
	tst.w	(GA_COMSTAT0).l
	beq	1b
	move.w	#0, (GA_COMCMD0).l
2:
	tst.w	(GA_COMSTAT0).l
	bne	2b
	rts

wait_word_ram_main:
1:
	btst	#GA_RET_BIT, (GA_MEMMODE+1).l
	beq	1b
	rts

copy_m_init_to_main_ram:
	lea	WORD_RAM, a0
	lea	MAIN_RAM_ENTRY, a1
	move.w	#M_INIT_COPY_WORDS-1, d0
1:
	move.w	(a0)+, (a1)+
	dbra	d0, 1b
	rts
