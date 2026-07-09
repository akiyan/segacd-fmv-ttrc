/*
 * Mega-CD boot sector for the 1M Word RAM swap self-test.
 *
 * Same layout as boot.s but embeds the test IP/SP instead of the movie ones.
 */

DiscHeader:
DiscType:
	.ascii "SEGADISCSYSTEM  "
VolumeName:
	.asciz "SCFMV_T1M  "
VolumeSystem:
	.word 0x0100, 0x0001
SystemName:
	.asciz "SEGASYSTEM "
SystemVersion:
	.word 0x0000, 0x0000
IP_Addr:
	.long 0x00000800
IP_Size:
	.long IPEnd-IPStart
IP_Entry:
	.long 0x00000000
IP_WorkRAM:
	.long 0x00000000
SP_Addr:
	.long 0x00007000
SP_Size:
	.long SPEnd-SPStart
SP_Entry:
	.long 0x00000000
SP_WorkRAM:
	.long 0x00000000
	.ascii "                "
	.ascii "                "
	.ascii "                "
	.ascii "                "
	.ascii "                "
	.ascii "                "
	.ascii "                "
	.ascii "                "
	.ascii "                "
	.ascii "                "
	.ascii "                "

HardwareType:
	.ascii "SEGA MEGA DRIVE "
Copyright:
	.ascii "(C) AKIYAN 2026 "
DomesticName:
	.ascii "SCFMV 1M WORD RAM SWAP SELFTEST                 "
OverseasName:
	.ascii "SCFMV 1M WORD RAM SWAP SELFTEST                 "
ProductCode:
	.ascii "GM 00-0000-00   "
IoSupport:
	.ascii "J               "
	.ascii "                "
	.ascii "                "
	.ascii "                "
	.ascii "                "
	.ascii "                "
Region:
	.ascii "J               "

IPStart:
	.incbin "out/test1m_ip.bin"
IPEnd:

	.org 0x7000
SPStart:
	.incbin "out/test1m_sp.bin"
SPEnd:

	.align 0x8000
