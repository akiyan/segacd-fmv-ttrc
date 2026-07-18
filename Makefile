PROJECT := SCFMV_MCD
OUT_DIR := out
DISC_DIR := $(OUT_DIR)/disc
BOOT_DIR := boot
CFG_DIR := cfg
SECURITY_REGION ?= jp
CONFIG ?=
PYTHON ?= tools/python.sh

# A movie build is identified by its TOML filename.  Keep packed streams under
# out/<toml-stem>/, transient build/staging files under tmp/<toml-stem>/, and
# the bootable pair at out/<toml-stem>.iso + .cue.  Standalone hardware tests
# keep their fixed names.
ifeq ($(strip $(MAKECMDGOALS)),)
MOVIEPLAY_REQUESTED := all
else
MOVIEPLAY_REQUESTED := $(filter all disc movieplay test1m,$(MAKECMDGOALS))
endif
ifneq ($(strip $(MOVIEPLAY_REQUESTED)),)
ifeq ($(strip $(CONFIG)),)
$(error CONFIG is required; for example: make disc CONFIG=configs/bad-apple-h32.toml)
endif
endif

ifneq ($(strip $(CONFIG)),)
CONFIG_STEM := $(shell $(PYTHON) tools/encode_config.py "$(CONFIG)" --print-stem)
ifeq ($(strip $(CONFIG_STEM)),)
$(error invalid CONFIG: $(CONFIG))
endif
else
CONFIG_STEM := movieplay
endif

MOVIEPLAY_STREAM_DIR := $(OUT_DIR)/$(CONFIG_STEM)
MOVIEPLAY_TMP_DIR := tmp/$(CONFIG_STEM)
MOVIEPLAY_BUILD_DIR := $(MOVIEPLAY_TMP_DIR)/build
MOVIEPLAY_DISC := $(MOVIEPLAY_TMP_DIR)/disc
MOVIEPLAY_ISO := $(OUT_DIR)/$(CONFIG_STEM).iso
MOVIEPLAY_CUE := $(OUT_DIR)/$(CONFIG_STEM).cue
PLAYER_CONSTANTS := $(MOVIEPLAY_STREAM_DIR)/player_constants.inc

MARSDEV ?= $(HOME)/toolchains/mars
M68K_PREFIX ?= $(MARSDEV)/m68k-elf/bin/m68k-elf-

AS := $(M68K_PREFIX)as
CC := $(M68K_PREFIX)gcc
LD := $(M68K_PREFIX)ld
OBJCOPY := $(M68K_PREFIX)objcopy
MKISOFS := $(shell command -v mkisofs 2>/dev/null || command -v genisoimage 2>/dev/null || true)

ASFLAGS := -m68000 --register-prefix-optional --bitwise-or
CFLAGS_M68K := -m68000 -ffreestanding -fno-builtin -fomit-frame-pointer -O2 -Wall -Wextra
LDFLAGS := -nostdlib --oformat binary

.PHONY: all disc setup movieplay-setup clean check-tools test1m cdcbench still256 movieplay dmabench streamtest pcmtest adpcmtest upscaletest asictest prgtest movieplay-force

all: disc

setup:
	@mkdir -p $(OUT_DIR) $(DISC_DIR)

movieplay-setup: setup
	@mkdir -p $(MOVIEPLAY_STREAM_DIR) $(MOVIEPLAY_BUILD_DIR) $(MOVIEPLAY_DISC)

# 本番ディスク = movieplay(HEADER.DAT + BODY.DAT)。旧PROBE.BIN/CD-DA画面パスは撤去済み。
disc: movieplay

check-tools:
	@test -x "$(PYTHON)" || (echo "missing project Python launcher: $(PYTHON). Run tools/bootstrap_python.sh --cpu" && exit 1)
	@test -x "$(AS)" || (echo "missing assembler: $(AS). Set MARSDEV=/path/to/mars or M68K_PREFIX=m68k-elf-" && exit 1)
	@test -x "$(CC)" || (echo "missing compiler: $(CC). Set MARSDEV=/path/to/mars or M68K_PREFIX=m68k-elf-" && exit 1)
	@test -x "$(LD)" || (echo "missing linker: $(LD). Set MARSDEV=/path/to/mars or M68K_PREFIX=m68k-elf-" && exit 1)
	@test -x "$(OBJCOPY)" || (echo "missing objcopy: $(OBJCOPY). Set MARSDEV=/path/to/mars or M68K_PREFIX=m68k-elf-" && exit 1)
	@test -n "$(MKISOFS)" || (echo "missing mkisofs/genisoimage" && exit 1)

$(BOOT_DIR)/security.bin: $(BOOT_DIR)/sec_$(SECURITY_REGION).bin
	@cp $< $@

# --- 1M Word RAM swap self-test (standalone, no CD / no M_INIT) ---
TEST1M_DISC := $(OUT_DIR)/disc_test1m

test1m: check-tools $(OUT_DIR)/TEST1M.iso $(OUT_DIR)/TEST1M.cue

$(OUT_DIR)/test1m_ip.o: $(BOOT_DIR)/test1m_ip.s $(BOOT_DIR)/security.bin | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/test1m_ip.bin: $(OUT_DIR)/test1m_ip.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<

$(OUT_DIR)/test1m_sp.o: $(BOOT_DIR)/test1m_sp.s | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/test1m_sp.bin: $(OUT_DIR)/test1m_sp.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/sp.ld -o $@ $<

$(OUT_DIR)/test1m_boot.bin: $(OUT_DIR)/test1m_ip.bin $(OUT_DIR)/test1m_sp.bin $(BOOT_DIR)/test1m_boot.s
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $(BOOT_DIR)/test1m_boot.s -o $(OUT_DIR)/test1m_boot.out
	$(OBJCOPY) -O binary $(OUT_DIR)/test1m_boot.out $@

$(OUT_DIR)/TEST1M.iso: $(OUT_DIR)/test1m_boot.bin $(MOVIEPLAY_STREAM_DIR)/MOVIE.DAT
	@mkdir -p $(TEST1M_DISC)
	@printf "1M Word RAM swap self-test\n" > $(TEST1M_DISC)/README.TXT
	@cp $(MOVIEPLAY_STREAM_DIR)/MOVIE.DAT $(TEST1M_DISC)/MOVIE.DAT
	@rm -f $@ $(OUT_DIR)/TEST1M.cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "SCFMV_T1M" -o $@ $(TEST1M_DISC)

$(OUT_DIR)/TEST1M.cue: $(OUT_DIR)/TEST1M.iso
	@rm -f $@
	@printf 'FILE "TEST1M.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

# --- Isolated CDC throughput test (standalone, IP+SP only, BENCH.DAT on disc) ---
CDCBENCH_DISC := $(OUT_DIR)/disc_cdcbench
CDCBENCH_DAT_SECTORS ?= 1536

cdcbench: check-tools $(OUT_DIR)/CDCBENCH.iso $(OUT_DIR)/CDCBENCH.cue

$(BOOT_DIR)/hexfont.bin: tools/gen_hexfont.py
	$(PYTHON) tools/gen_hexfont.py

$(OUT_DIR)/cdcbench_ip.o: $(BOOT_DIR)/cdcbench_ip.s $(BOOT_DIR)/security.bin $(BOOT_DIR)/hexfont.bin | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/cdcbench_ip.bin: $(OUT_DIR)/cdcbench_ip.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<

$(OUT_DIR)/cdcbench_sp.o: $(BOOT_DIR)/cdcbench_sp.s | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/cdcbench_sp.bin: $(OUT_DIR)/cdcbench_sp.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/sp.ld -o $@ $<

$(OUT_DIR)/cdcbench_boot.bin: $(OUT_DIR)/cdcbench_ip.bin $(OUT_DIR)/cdcbench_sp.bin $(BOOT_DIR)/cdcbench_boot.s
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $(BOOT_DIR)/cdcbench_boot.s -o $(OUT_DIR)/cdcbench_boot.out
	$(OBJCOPY) -O binary $(OUT_DIR)/cdcbench_boot.out $@

$(OUT_DIR)/CDCBENCH.iso: $(OUT_DIR)/cdcbench_boot.bin
	@mkdir -p $(CDCBENCH_DISC)
	@printf "CDC throughput test\n" > $(CDCBENCH_DISC)/README.TXT
	dd if=/dev/urandom of=$(CDCBENCH_DISC)/BENCH.DAT bs=2048 count=$(CDCBENCH_DAT_SECTORS) status=none
	@rm -f $@ $(OUT_DIR)/CDCBENCH.cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "SCFMV_CDCB" -o $@ $(CDCBENCH_DISC)

$(OUT_DIR)/CDCBENCH.cue: $(OUT_DIR)/CDCBENCH.iso
	@rm -f $@
	@printf 'FILE "CDCBENCH.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

# --- Phase A: H32 256x144 静止画レンダラ(描画土台検証, CD読み無し/CPU書き込みのみ) ---
STILL256_DISC := $(OUT_DIR)/disc_still256
STILL256_DATA ?= $(shell $(PYTHON) -c 'import sys; sys.path.insert(0, "tools"); from cbr_paths import sim_work_dir; print(sim_work_dir() / "still256.bin")')

still256: check-tools $(OUT_DIR)/STILL256.iso $(OUT_DIR)/STILL256.cue

$(OUT_DIR)/still256_ip.o: $(BOOT_DIR)/still256_ip.s $(BOOT_DIR)/security.bin $(STILL256_DATA) | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/still256_ip.bin: $(OUT_DIR)/still256_ip.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<

$(OUT_DIR)/still256_boot.bin: $(OUT_DIR)/still256_ip.bin $(OUT_DIR)/cdcbench_sp.bin $(BOOT_DIR)/still256_boot.s
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $(BOOT_DIR)/still256_boot.s -o $(OUT_DIR)/still256_boot.out
	$(OBJCOPY) -O binary $(OUT_DIR)/still256_boot.out $@

$(OUT_DIR)/STILL256.iso: $(OUT_DIR)/still256_boot.bin
	@mkdir -p $(STILL256_DISC)
	@printf "still256 phase A\n" > $(STILL256_DISC)/README.TXT
	@rm -f $@ $(OUT_DIR)/STILL256.cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "SCFMV_ST256" -o $@ $(STILL256_DISC)

$(OUT_DIR)/STILL256.cue: $(OUT_DIR)/STILL256.iso
	@rm -f $@
	@printf 'FILE "STILL256.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

# --- dmabench: 表示モード別 VRAM DMA スループット実測(再利用可能) ---
# 使い方: make dmabench DMABENCH_MODE=0|1|2  (0=H32, 1=H40, 2=mode4)
# 右下に W=語/vblank T=タイル/vblank F=タイル/コマ(3vblank換算) を表示。結果は BUDGETS.md 参照。
DMABENCH_MODE ?= 0
DMABENCH_TAG := mode$(DMABENCH_MODE)
dmabench: check-tools $(OUT_DIR)/DMABENCH_$(DMABENCH_TAG).iso $(OUT_DIR)/DMABENCH_$(DMABENCH_TAG).cue
	@cp $(OUT_DIR)/DMABENCH_$(DMABENCH_TAG).iso $(OUT_DIR)/DMABENCH.iso
	@cp $(OUT_DIR)/DMABENCH_$(DMABENCH_TAG).cue $(OUT_DIR)/DMABENCH.cue

$(OUT_DIR)/dmabench_ip_$(DMABENCH_TAG).o: $(BOOT_DIR)/dmabench_ip.s $(BOOT_DIR)/security.bin $(BOOT_DIR)/dbgfont.bin | setup
	$(AS) $(ASFLAGS) --defsym MODE=$(DMABENCH_MODE) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/dmabench_ip_$(DMABENCH_TAG).bin: $(OUT_DIR)/dmabench_ip_$(DMABENCH_TAG).o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<

$(OUT_DIR)/dmabench_boot_$(DMABENCH_TAG).bin: $(OUT_DIR)/dmabench_ip_$(DMABENCH_TAG).bin $(OUT_DIR)/cdcbench_sp.bin $(BOOT_DIR)/dmabench_boot.s
	cp $(OUT_DIR)/dmabench_ip_$(DMABENCH_TAG).bin $(OUT_DIR)/dmabench_ip.bin
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $(BOOT_DIR)/dmabench_boot.s -o $(OUT_DIR)/dmabench_boot_$(DMABENCH_TAG).out
	$(OBJCOPY) -O binary $(OUT_DIR)/dmabench_boot_$(DMABENCH_TAG).out $@

$(OUT_DIR)/DMABENCH_$(DMABENCH_TAG).iso: $(OUT_DIR)/dmabench_boot_$(DMABENCH_TAG).bin
	@mkdir -p $(OUT_DIR)/disc_dmabench
	@printf "dmabench\n" > $(OUT_DIR)/disc_dmabench/README.TXT
	@rm -f $@ $(OUT_DIR)/DMABENCH_$(DMABENCH_TAG).cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "DMABENCH" -o $@ $(OUT_DIR)/disc_dmabench

$(OUT_DIR)/DMABENCH_$(DMABENCH_TAG).cue: $(OUT_DIR)/DMABENCH_$(DMABENCH_TAG).iso
	@rm -f $@
	@printf 'FILE "DMABENCH_$(DMABENCH_TAG).iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

# --- Phase B2: 差分ストリーム再生(単バッファ, BODY.DAT を連続供給) ---
# HEADER.DAT/BODY.DAT は事前に同じ CONFIG で pack する。

movieplay: check-tools $(MOVIEPLAY_ISO) $(MOVIEPLAY_CUE)

# 既定はリリースビルド。DEBUG=1 でデバッグオーバーレイを有効化する。
# ストリーム側のデバッグ欄は CBRSIM_PACK_DEBUG=1 で pack した時だけ載せる。
DEBUG ?= 0
ISO_HOLD_N ?= 0
# Issue #27 Main-CPU straight-line bitmap handlers and fixed-geometry NT
# blitters. H32/H40 full-playback validation is complete; MAIN_CODEGEN=0 keeps
# the byte-identical reference player available for fallback/A-B diagnostics.
MAIN_CODEGEN ?= 1
# Main-CPU pattern-transfer fast path.  Set to 0 only for reproducible A/B
# diagnostics against the former all-DMA run path.
DMA_RUN_FASTPATH ?= 1
# DEBUG changes assembler flags without changing a source timestamp. Force this
# small object to rebuild so `make disc DEBUG=1` can never reuse a release object
# (or vice versa).
movieplay-force:

$(PLAYER_CONSTANTS): $(MOVIEPLAY_STREAM_DIR)/HEADER.DAT tools/player_constants.py tools/ttrc_routing.py | movieplay-setup
	$(PYTHON) tools/player_constants.py $< --output $@

$(MOVIEPLAY_BUILD_DIR)/movieplay_ip.o: $(BOOT_DIR)/movieplay_ip.s $(BOOT_DIR)/security.bin $(MOVIEPLAY_STREAM_DIR)/palettes.bin $(PLAYER_CONSTANTS) $(BOOT_DIR)/dbgfont.bin tools/av_config.py tools/ttrc_routing.py tools/check_player_ring.py $(CONFIG) movieplay-force | movieplay-setup
	$(PYTHON) tools/check_player_ring.py
	$(AS) $(ASFLAGS) $(if $(filter 1,$(DEBUG)),--defsym DEBUG=1) $(if $(filter 1,$(MAIN_CODEGEN)),--defsym MAIN_CODEGEN=1) $(if $(filter 1,$(DMA_RUN_FASTPATH)),--defsym DMA_RUN_FASTPATH=1) -I$(MOVIEPLAY_STREAM_DIR) -I$(BOOT_DIR) $< -o $@

$(BOOT_DIR)/dbgfont.bin: tools/gen_debugfont.py
	$(PYTHON) tools/gen_debugfont.py

$(MOVIEPLAY_BUILD_DIR)/movieplay_ip.bin: $(MOVIEPLAY_BUILD_DIR)/movieplay_ip.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<

$(MOVIEPLAY_BUILD_DIR)/movieplay_sp.o: $(BOOT_DIR)/movieplay_sp.s $(PLAYER_CONSTANTS) tools/av_config.py tools/ttrc_routing.py tools/check_player_ring.py $(CONFIG) movieplay-force | movieplay-setup
	$(PYTHON) tools/check_player_ring.py
	$(AS) $(ASFLAGS) $(if $(filter 1,$(DEBUG)),--defsym DEBUG=1) $(if $(filter-out 0,$(ISO_HOLD_N)),--defsym ISO_HOLD_N=$(ISO_HOLD_N)) -I$(MOVIEPLAY_STREAM_DIR) -I$(BOOT_DIR) $< -o $@

$(MOVIEPLAY_BUILD_DIR)/movieplay_sp.bin: $(MOVIEPLAY_BUILD_DIR)/movieplay_sp.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/sp.ld -o $@ $<
	@bytes=$$(wc -c < $@); \
		if [ "$$bytes" -gt 4096 ]; then \
			echo "ERROR: $@ is $$bytes bytes; the Sega CD boot SP area is limited to 4096 bytes" >&2; \
			rm -f $@; \
			exit 1; \
		fi

$(MOVIEPLAY_BUILD_DIR)/movieplay_boot.bin: $(MOVIEPLAY_BUILD_DIR)/movieplay_ip.bin $(MOVIEPLAY_BUILD_DIR)/movieplay_sp.bin $(BOOT_DIR)/movieplay_boot.s
	$(AS) $(ASFLAGS) -I$(MOVIEPLAY_BUILD_DIR) -I$(BOOT_DIR) $(BOOT_DIR)/movieplay_boot.s -o $(MOVIEPLAY_BUILD_DIR)/movieplay_boot.out
	$(OBJCOPY) -O binary $(MOVIEPLAY_BUILD_DIR)/movieplay_boot.out $@

$(MOVIEPLAY_ISO): $(MOVIEPLAY_BUILD_DIR)/movieplay_boot.bin $(MOVIEPLAY_STREAM_DIR)/HEADER.DAT $(MOVIEPLAY_STREAM_DIR)/BODY.DAT | movieplay-setup
	@mkdir -p $(MOVIEPLAY_DISC)
	@printf "delta stream phase B2\n" > $(MOVIEPLAY_DISC)/README.TXT
	@rm -f $(MOVIEPLAY_DISC)/MOVIE.DAT $(MOVIEPLAY_DISC)/HEADER.DAT $(MOVIEPLAY_DISC)/BODY.DAT
	cp $(MOVIEPLAY_STREAM_DIR)/HEADER.DAT $(MOVIEPLAY_DISC)/HEADER.DAT
	cp $(MOVIEPLAY_STREAM_DIR)/BODY.DAT $(MOVIEPLAY_DISC)/BODY.DAT
	@rm -f $@ $(MOVIEPLAY_CUE)
	$(MKISOFS) -iso-level 1 -G $< -pad -V "SCFMV_DLT" -o $@ $(MOVIEPLAY_DISC)

$(MOVIEPLAY_CUE): $(MOVIEPLAY_ISO)
	@rm -f $@
	@printf 'FILE "$(notdir $(MOVIEPLAY_ISO))" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

# --- Continuous-stream self-test (standalone, IP+SP only, STREAM.DAT on disc) ---
# NOTE: STREAM_FRAMES / STREAM_FRAME_SECTORS must match NUM_FRAMES / FRAME_SECTORS
# in boot/streamtest_sp.s and boot/streamtest_ip.s.
STREAMTEST_DISC := $(OUT_DIR)/disc_streamtest
STREAM_FRAMES ?= 256
STREAM_FRAME_SECTORS ?= 5

streamtest: check-tools $(OUT_DIR)/STREAMTEST.iso $(OUT_DIR)/STREAMTEST.cue

$(OUT_DIR)/streamtest_ip.o: $(BOOT_DIR)/streamtest_ip.s $(BOOT_DIR)/security.bin $(BOOT_DIR)/hexfont.bin | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/streamtest_ip.bin: $(OUT_DIR)/streamtest_ip.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<

$(OUT_DIR)/streamtest_sp.o: $(BOOT_DIR)/streamtest_sp.s | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/streamtest_sp.bin: $(OUT_DIR)/streamtest_sp.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/sp.ld -o $@ $<

$(OUT_DIR)/streamtest_boot.bin: $(OUT_DIR)/streamtest_ip.bin $(OUT_DIR)/streamtest_sp.bin $(BOOT_DIR)/streamtest_boot.s
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $(BOOT_DIR)/streamtest_boot.s -o $(OUT_DIR)/streamtest_boot.out
	$(OBJCOPY) -O binary $(OUT_DIR)/streamtest_boot.out $@

$(OUT_DIR)/STREAMTEST.iso: $(OUT_DIR)/streamtest_boot.bin tools/gen_streamtest.py
	@mkdir -p $(STREAMTEST_DISC)
	@printf "Continuous stream self-test\n" > $(STREAMTEST_DISC)/README.TXT
	$(PYTHON) tools/gen_streamtest.py --frames $(STREAM_FRAMES) --frame-sectors $(STREAM_FRAME_SECTORS) --output $(STREAMTEST_DISC)/STREAM.DAT
	@rm -f $@ $(OUT_DIR)/STREAMTEST.cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "SCFMV_STRM" -o $@ $(STREAMTEST_DISC)

$(OUT_DIR)/STREAMTEST.cue: $(OUT_DIR)/STREAMTEST.iso
	@rm -f $@
	@printf 'FILE "STREAMTEST.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

# --- RF5C164 PCM self-test (standalone, IP+SP only, looping tone) ---
PCMTEST_DISC := $(OUT_DIR)/disc_pcmtest

pcmtest: check-tools $(OUT_DIR)/PCMTEST.iso $(OUT_DIR)/PCMTEST.cue

$(OUT_DIR)/pcmtest_ip.o: $(BOOT_DIR)/pcmtest_ip.s $(BOOT_DIR)/security.bin $(BOOT_DIR)/hexfont.bin | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/pcmtest_ip.bin: $(OUT_DIR)/pcmtest_ip.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<

$(OUT_DIR)/pcmtest_sp.o: $(BOOT_DIR)/pcmtest_sp.s | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/pcmtest_sp.bin: $(OUT_DIR)/pcmtest_sp.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/sp.ld -o $@ $<

$(OUT_DIR)/pcmtest_boot.bin: $(OUT_DIR)/pcmtest_ip.bin $(OUT_DIR)/pcmtest_sp.bin $(BOOT_DIR)/pcmtest_boot.s
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $(BOOT_DIR)/pcmtest_boot.s -o $(OUT_DIR)/pcmtest_boot.out
	$(OBJCOPY) -O binary $(OUT_DIR)/pcmtest_boot.out $@

$(OUT_DIR)/PCMTEST.iso: $(OUT_DIR)/pcmtest_boot.bin
	@mkdir -p $(PCMTEST_DISC)
	@printf "RF5C164 PCM self-test\n" > $(PCMTEST_DISC)/README.TXT
	@rm -f $@ $(OUT_DIR)/PCMTEST.cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "SCFMV_PCM" -o $@ $(PCMTEST_DISC)

$(OUT_DIR)/PCMTEST.cue: $(OUT_DIR)/PCMTEST.iso
	@rm -f $@
	@printf 'FILE "PCMTEST.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

# --- ADPCM decoder smoke test (standalone, IP+SP only, embedded IMA stream) ---
ADPCMTEST_DISC := $(OUT_DIR)/disc_adpcmtest

adpcmtest: check-tools $(OUT_DIR)/ADPCMTEST.iso $(OUT_DIR)/ADPCMTEST.cue

$(BOOT_DIR)/adpcmtest_font.bin: tools/gen_adpcmtest_font.py
	$(PYTHON) tools/gen_adpcmtest_font.py

$(OUT_DIR)/adpcmtest_ip.o: $(BOOT_DIR)/adpcmtest_ip.s $(BOOT_DIR)/security.bin $(BOOT_DIR)/adpcmtest_font.bin | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/adpcmtest_ip.bin: $(OUT_DIR)/adpcmtest_ip.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<

$(OUT_DIR)/adpcmtest_sp_shell.o: $(BOOT_DIR)/adpcmtest_sp.s | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/adpcmtest_adpcm.o: $(BOOT_DIR)/adpcmtest_adpcm.c | setup
	$(CC) $(CFLAGS_M68K) -c $< -o $@

$(OUT_DIR)/adpcmtest_tone_ima.bin: tools/gen_adpcmtest_audio.py | setup
	$(PYTHON) tools/gen_adpcmtest_audio.py --pattern --out $@ --rate 22050 --samples 8192

$(OUT_DIR)/adpcmtest_audio.o: $(BOOT_DIR)/adpcmtest_audio.s $(OUT_DIR)/adpcmtest_tone_ima.bin | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/adpcmtest_sp.bin: $(OUT_DIR)/adpcmtest_sp_shell.o $(OUT_DIR)/adpcmtest_adpcm.o $(OUT_DIR)/adpcmtest_audio.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/sp.ld -o $@ $^

$(OUT_DIR)/adpcmtest_boot.bin: $(OUT_DIR)/adpcmtest_ip.bin $(OUT_DIR)/adpcmtest_sp.bin $(BOOT_DIR)/adpcmtest_boot.s
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $(BOOT_DIR)/adpcmtest_boot.s -o $(OUT_DIR)/adpcmtest_boot.out
	$(OBJCOPY) -O binary $(OUT_DIR)/adpcmtest_boot.out $@

$(OUT_DIR)/ADPCMTEST.iso: $(OUT_DIR)/adpcmtest_boot.bin
	@mkdir -p $(ADPCMTEST_DISC)
	@printf "ADPCM decoder smoke test\n" > $(ADPCMTEST_DISC)/README.TXT
	@rm -f $@ $(OUT_DIR)/ADPCMTEST.cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "SCFMV_ADPCM" -o $@ $(ADPCMTEST_DISC)

$(OUT_DIR)/ADPCMTEST.cue: $(OUT_DIR)/ADPCMTEST.iso
	@rm -f $@
	@printf 'FILE "ADPCMTEST.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

# --- 2x CPU-upscale 320x160 / 4-VBlank DMA verification (reuses boot.bin, SP,
#     and the movie PROBE.BIN; only M_INIT.PRG is the upscale Main) ---
UPSCALE_DISC := $(OUT_DIR)/disc_upscale

upscaletest: check-tools setup $(OUT_DIR)/UPSCALE.iso $(OUT_DIR)/UPSCALE.cue

$(OUT_DIR)/upscaletest_main.o: $(BOOT_DIR)/upscaletest_main.s | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/upscaletest_main.bin: $(OUT_DIR)/upscaletest_main.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/handoff.ld -o $@ $^

$(OUT_DIR)/UPSCALE.iso: $(OUT_DIR)/boot.bin $(OUT_DIR)/upscaletest_main.bin $(DISC_DIR)/PROBE.BIN
	@mkdir -p $(UPSCALE_DISC)
	cp $(OUT_DIR)/upscaletest_main.bin $(UPSCALE_DISC)/M_INIT.PRG
	cp $(DISC_DIR)/PROBE.BIN $(UPSCALE_DISC)/PROBE.BIN
	@printf "upscale 320x160 4-VBlank test\n" > $(UPSCALE_DISC)/README.TXT
	@rm -f $@ $(OUT_DIR)/UPSCALE.cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "SCFMV_UPSC" -o $@ $(UPSCALE_DISC)

$(OUT_DIR)/UPSCALE.cue: $(OUT_DIR)/UPSCALE.iso
	@rm -f $@
	@printf 'FILE "UPSCALE.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

# --- ASIC 2x-upscale verification (static frame, Graphics ASIC scaler) ---
ASIC_FRAME ?= 00100
ASIC_DISC := $(OUT_DIR)/disc_asic
# asictest is fixed at 160x80 (20x10 tiles); it keeps its own probe root so it is
# unaffected by the main build's resolution.
ASIC_PROBE_ROOT := $(OUT_DIR)/video/061_asic_160x80

asictest: check-tools setup $(OUT_DIR)/ASIC.iso $(OUT_DIR)/ASIC.cue

$(ASIC_PROBE_ROOT)/palettes.bin: $(OP_SRC) tools/quantize_global4_tiles.py tools/quantize_md_video.py
	$(PYTHON) tools/quantize_global4_tiles.py --input $(OP_SRC) --start 0 --duration 152.866667 --fps 15 --scale-width 160 --scale-height 80 --output-dir $(ASIC_PROBE_ROOT)

$(OUT_DIR)/asic/ASIC.DAT: $(ASIC_PROBE_ROOT)/palettes.bin tools/make_asic_stamps.py | setup
	@mkdir -p $(OUT_DIR)/asic
	$(PYTHON) tools/make_asic_stamps.py \
		--tiles $(ASIC_PROBE_ROOT)/tile/$(ASIC_FRAME).tile \
		--pmap $(ASIC_PROBE_ROOT)/pmap/$(ASIC_FRAME).pmap \
		--pal $(ASIC_PROBE_ROOT)/palettes.bin \
		--out $(OUT_DIR)/asic --dat $(OUT_DIR)/asic/ASIC.DAT

$(OUT_DIR)/asictest_ip.o: $(BOOT_DIR)/asictest_ip.s $(BOOT_DIR)/security.bin | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@
$(OUT_DIR)/asictest_ip.bin: $(OUT_DIR)/asictest_ip.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<

$(OUT_DIR)/asictest_sp.o: $(BOOT_DIR)/asictest_sp.s $(OUT_DIR)/asic/ASIC.DAT | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@
$(OUT_DIR)/asictest_sp.bin: $(OUT_DIR)/asictest_sp.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/sp.ld -o $@ $<

$(OUT_DIR)/asictest_boot.bin: $(OUT_DIR)/asictest_ip.bin $(OUT_DIR)/asictest_sp.bin $(BOOT_DIR)/asictest_boot.s
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $(BOOT_DIR)/asictest_boot.s -o $(OUT_DIR)/asictest_boot.out
	$(OBJCOPY) -O binary $(OUT_DIR)/asictest_boot.out $@

$(OUT_DIR)/ASIC.iso: $(OUT_DIR)/asictest_boot.bin
	@mkdir -p $(ASIC_DISC)
	@printf "asic 2x upscale test\n" > $(ASIC_DISC)/README.TXT
	@rm -f $@ $(OUT_DIR)/ASIC.cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "SCFMV_ASIC" -o $@ $(ASIC_DISC)

$(OUT_DIR)/ASIC.cue: $(OUT_DIR)/ASIC.iso
	@rm -f $@
	@printf 'FILE "ASIC.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

clean:
	@rm -rf $(OUT_DIR)
	@rm -f $(BOOT_DIR)/security.bin

# --- PRG-RAM 書込テスト(CD読込の有無で高位PRGへCPU書込できるか) ---
prgtest: check-tools $(OUT_DIR)/PRGTEST.iso $(OUT_DIR)/PRGTEST.cue

$(OUT_DIR)/prgtest_ip.o: $(BOOT_DIR)/prgtest_ip.s $(BOOT_DIR)/security.bin | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@
$(OUT_DIR)/prgtest_ip.bin: $(OUT_DIR)/prgtest_ip.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<
$(OUT_DIR)/prgtest_sp.o: $(BOOT_DIR)/prgtest_sp.s | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@
$(OUT_DIR)/prgtest_sp.bin: $(OUT_DIR)/prgtest_sp.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/sp.ld -o $@ $<
$(OUT_DIR)/prgtest_boot.bin: $(OUT_DIR)/prgtest_ip.bin $(OUT_DIR)/prgtest_sp.bin $(BOOT_DIR)/prgtest_boot.s
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $(BOOT_DIR)/prgtest_boot.s -o $(OUT_DIR)/prgtest_boot.out
	$(OBJCOPY) -O binary $(OUT_DIR)/prgtest_boot.out $@
$(OUT_DIR)/PRGTEST.iso: $(OUT_DIR)/prgtest_boot.bin
	@mkdir -p $(OUT_DIR)/disc_prgtest
	@printf "prg write test\n" > $(OUT_DIR)/disc_prgtest/README.TXT
	@rm -f $@ $(OUT_DIR)/PRGTEST.cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "PRGTEST" -o $@ $(OUT_DIR)/disc_prgtest
$(OUT_DIR)/PRGTEST.cue: $(OUT_DIR)/PRGTEST.iso
	@rm -f $@
	@printf 'FILE "PRGTEST.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@
