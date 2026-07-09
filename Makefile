PROJECT := SCFMV_MCD
OUT_DIR := out
DISC_DIR := $(OUT_DIR)/disc
BOOT_DIR := boot
CFG_DIR := cfg
SECURITY_REGION ?= jp

MARSDEV ?= $(HOME)/toolchains/mars
M68K_PREFIX ?= $(MARSDEV)/m68k-elf/bin/m68k-elf-

AS := $(M68K_PREFIX)as
LD := $(M68K_PREFIX)ld
OBJCOPY := $(M68K_PREFIX)objcopy
MKISOFS := $(shell command -v mkisofs 2>/dev/null || command -v genisoimage 2>/dev/null || true)

ASFLAGS := -m68000 --register-prefix-optional --bitwise-or
LDFLAGS := -nostdlib --oformat binary

.PHONY: all disc setup clean check-tools test1m cdcbench still256 movieplay dmabench streamtest pcmtest upscaletest asictest prgtest

all: disc

setup:
	@mkdir -p $(OUT_DIR) $(DISC_DIR)

# 本番ディスク = movieplay(MOVIE.DAT)。旧PROBE.BIN/CD-DA画面パスは撤去済み。
disc: movieplay

check-tools:
	@test -x "$(AS)" || (echo "missing assembler: $(AS). Set MARSDEV=/path/to/mars or M68K_PREFIX=m68k-elf-" && exit 1)
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

$(OUT_DIR)/TEST1M.iso: $(OUT_DIR)/test1m_boot.bin out/movieplay/MOVIE.DAT
	@mkdir -p $(TEST1M_DISC)
	@printf "1M Word RAM swap self-test\n" > $(TEST1M_DISC)/README.TXT
	@cp out/movieplay/MOVIE.DAT $(TEST1M_DISC)/MOVIE.DAT
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
	python3 tools/gen_hexfont.py

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
STILL256_DATA ?= $(shell python3 -c 'import sys; sys.path.insert(0, "tools"); from cbr_paths import sim_work_dir; print(sim_work_dir() / "still256.bin")')

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

# --- Phase B2: 差分ストリーム再生(単バッファ, MOVIE.DAT を連続供給) ---
# MOVIE.DAT/palettes.bin は事前に: python3 tools/pack_stream.py --frames N --raw-dir out/movieplay
MOVIEPLAY_DISC := $(OUT_DIR)/disc_movieplay

movieplay: check-tools $(OUT_DIR)/MOVIEPLAY.iso $(OUT_DIR)/MOVIEPLAY.cue

# RELEASE=1 でデバッグオーバーレイを除去(既定はデバッグビルド)。
# ストリーム側のデバッグ欄除去は CBRSIM_PACK_DEBUG=0 で pack する。
RELEASE ?= 0
$(OUT_DIR)/movieplay_ip.o: $(BOOT_DIR)/movieplay_ip.s $(BOOT_DIR)/security.bin out/movieplay/palettes.bin $(BOOT_DIR)/dbgfont.bin | setup
	$(AS) $(ASFLAGS) $(if $(filter 1,$(RELEASE)),--defsym RELEASE=1) -I$(BOOT_DIR) $< -o $@

$(BOOT_DIR)/dbgfont.bin: tools/gen_debugfont.py
	python3 tools/gen_debugfont.py

$(OUT_DIR)/movieplay_ip.bin: $(OUT_DIR)/movieplay_ip.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/ip.ld -o $@ $<

$(OUT_DIR)/movieplay_sp.o: $(BOOT_DIR)/movieplay_sp.s | setup
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $< -o $@

$(OUT_DIR)/movieplay_sp.bin: $(OUT_DIR)/movieplay_sp.o
	$(LD) $(LDFLAGS) -T $(CFG_DIR)/sp.ld -o $@ $<

$(OUT_DIR)/movieplay_boot.bin: $(OUT_DIR)/movieplay_ip.bin $(OUT_DIR)/movieplay_sp.bin $(BOOT_DIR)/movieplay_boot.s
	$(AS) $(ASFLAGS) -I$(BOOT_DIR) $(BOOT_DIR)/movieplay_boot.s -o $(OUT_DIR)/movieplay_boot.out
	$(OBJCOPY) -O binary $(OUT_DIR)/movieplay_boot.out $@

$(OUT_DIR)/MOVIEPLAY.iso: $(OUT_DIR)/movieplay_boot.bin out/movieplay/MOVIE.DAT
	@mkdir -p $(MOVIEPLAY_DISC)
	@printf "delta stream phase B2\n" > $(MOVIEPLAY_DISC)/README.TXT
	cp out/movieplay/MOVIE.DAT $(MOVIEPLAY_DISC)/MOVIE.DAT
	@rm -f $@ $(OUT_DIR)/MOVIEPLAY.cue
	$(MKISOFS) -iso-level 1 -G $< -pad -V "SCFMV_DLT" -o $@ $(MOVIEPLAY_DISC)

$(OUT_DIR)/MOVIEPLAY.cue: $(OUT_DIR)/MOVIEPLAY.iso
	@rm -f $@
	@printf 'FILE "MOVIEPLAY.iso" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n' > $@

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
	python3 tools/gen_streamtest.py --frames $(STREAM_FRAMES) --frame-sectors $(STREAM_FRAME_SECTORS) --output $(STREAMTEST_DISC)/STREAM.DAT
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
	python3 tools/quantize_global4_tiles.py --input $(OP_SRC) --start 0 --duration 152.866667 --fps 15 --scale-width 160 --scale-height 80 --output-dir $(ASIC_PROBE_ROOT)

$(OUT_DIR)/asic/ASIC.DAT: $(ASIC_PROBE_ROOT)/palettes.bin tools/make_asic_stamps.py | setup
	@mkdir -p $(OUT_DIR)/asic
	python3 tools/make_asic_stamps.py \
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
