/*
 * Sega CD boot sector for the ASIC 2x-upscale self-test.
 * Same layout as boot.s; embeds the asictest IP/SP (SP carries ASIC.DAT).
 */

DiscHeader:
DiscType:
	.ascii "SEGADISCSYSTEM  "
VolumeName:
	.asciz "SCFMV_ASIC "
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
	.long 0x00001000
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
	.ascii "SCFMV ASIC 2X UPSCALE SELFTEST                  "
OverseasName:
	.ascii "SCFMV ASIC 2X UPSCALE SELFTEST                  "
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
	.incbin "out/asictest_ip.bin"
IPEnd:

	.org 0x1000
SPStart:
	.incbin "out/asictest_sp.bin"
SPEnd:

	.align 0x8000
