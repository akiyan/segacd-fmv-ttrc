#!/usr/bin/env python3
"""Build-time guards for the player's PRG/Word-RAM streaming memory map.

Run by the Makefile before assembling boot/movieplay_sp.s. Fails the build if the
player's physical ring and the pipeline's single-source-of-truth ring drift apart,
or if boot-only frame/routing staging overlaps a live buffer.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import av_config
import ima_adpcm
import pattern_supply
import ttrc_routing

SP = Path(__file__).resolve().parent.parent / "boot" / "movieplay_sp.s"
text = SP.read_text()
IP = Path(__file__).resolve().parent.parent / "boot" / "movieplay_ip.s"
ip_text = IP.read_text()


def _equ(source, name, path):
    m = re.search(r"^\.equ\s+%s,\s*(0x[0-9A-Fa-f]+|\d+)" % name, source, re.M)
    if not m:
        sys.exit(f"check_player_ring: could not find `.equ {name}` in {path}")
    return int(m.group(1), 0)


def _require_asm(pattern, description):
    if not re.search(pattern, text, re.M):
        sys.exit(f"check_player_ring: ASM does not use {description}")


# --- Current TTRC contract, retaining the v7+ packed-routing layout ---
# The Python codec is the format source of truth. The Sub-CPU player keeps
# literal `.equ` values because the assembler cannot import Python; compare all
# of them before every player build and also require the copy loops to use the
# named values rather than disconnected immediate constants.
routing_equ_contract = {
    "ROUTING_VERSION": ttrc_routing.VERSION,
    "ROUTING_BYTES": ttrc_routing.ROUTE_BYTES,
    "ROUTING_MAX_FRAMES": ttrc_routing.MAX_FRAMES,
    "ROUTING_SECTOR_BYTES": ttrc_routing.SECTOR_BYTES,
    "ROUTING_CTRL_MASK": ttrc_routing.CTRL_MASK,
    "ROUTING_TOTAL_SHIFT": ttrc_routing.TOTAL_SHIFT,
    "ROUTING_MAX_ENTRY": ttrc_routing.MAX_ENTRY,
    "FEATURE_COLD_RUNS_BIT": ttrc_routing.FEATURE_COLD_RUNS.bit_length() - 1,
    "FEATURE_FIXED_N2_BIT": ttrc_routing.FEATURE_FIXED_N2.bit_length() - 1,
    "FEATURE_ADPCM22_BIT": ttrc_routing.FEATURE_ADPCM22.bit_length() - 1,
    "FEATURE_PATTERN_SUPPLY_BIT": (
        ttrc_routing.FEATURE_PATTERN_SUPPLY.bit_length() - 1),
    "FEATURE_SHADOW_UPDATE_LISTS_BIT": (
        ttrc_routing.FEATURE_SHADOW_UPDATE_LISTS.bit_length() - 1),
}
for equ_name, expected in routing_equ_contract.items():
    actual = _equ(text, equ_name, SP)
    if actual != expected:
        sys.exit(
            f"check_player_ring: player {equ_name}={actual} (0x{actual:X}) != "
            f"ttrc_routing={expected} (0x{expected:X})")

route_copy_longs = _equ(text, "ROUTING_COPY_LONGS", SP)
route_bank_copies = _equ(text, "ROUTING_BANK_COPIES", SP)
if ttrc_routing.ROUTE_BYTES != 16 * 1024:
    sys.exit(
        "check_player_ring: resident routing allocation must remain 16KB, got "
        f"{ttrc_routing.ROUTE_BYTES} bytes")
if route_copy_longs * 4 != ttrc_routing.ROUTE_BYTES:
    sys.exit(
        "check_player_ring: routing MOVE.L copy does not cover the complete "
        f"table: {route_copy_longs} longs vs {ttrc_routing.ROUTE_BYTES} bytes")
if route_bank_copies != 2:
    sys.exit(
        "check_player_ring: routing must be copied into both physical 1M Word-RAM "
        f"banks, got {route_bank_copies} copies")
_require_asm(
    r"^\s*move\.w\s+#ROUTING_COPY_LONGS-1,\s*d0\s*$",
    "ROUTING_COPY_LONGS in the MOVE.L copy loop")
_require_asm(
    r"^\s*moveq\s+#ROUTING_BANK_COPIES-1,\s*d1\s*$",
    "ROUTING_BANK_COPIES in the Word-RAM bank loop")
print(
    "check_player_ring: OK  TTRC routing "
    f"v{ttrc_routing.VERSION}, {ttrc_routing.ROUTE_BYTES // 1024}KB, "
    f"{ttrc_routing.MAX_FRAMES} frames, {route_copy_longs} MOVE.L x "
    f"{route_bank_copies} banks")

# --- v9+ ADPCM full table duplicated into both physical Word-RAM banks ---
adpcm_table = _equ(text, "ADPCM_TABLE", SP)
adpcm_table_bytes = _equ(text, "ADPCM_TABLE_BYTES", SP)
adpcm_table_sectors = _equ(text, "ADPCM_TABLE_SECTORS", SP)
adpcm_bank_copies = _equ(text, "ADPCM_BANK_COPIES", SP)
pcm_dec_buf = _equ(text, "PCM_DEC_BUF", SP)
if adpcm_table_bytes != ima_adpcm.FULL_TABLE_BYTES:
    sys.exit(
        f"check_player_ring: ADPCM_TABLE_BYTES={adpcm_table_bytes} != "
        f"reference full table {ima_adpcm.FULL_TABLE_BYTES}")
expected_table_sectors = (
    ima_adpcm.FULL_TABLE_BYTES + ttrc_routing.SECTOR_BYTES - 1
) // ttrc_routing.SECTOR_BYTES
if adpcm_table_sectors != expected_table_sectors:
    sys.exit(
        f"check_player_ring: ADPCM_TABLE_SECTORS={adpcm_table_sectors} != "
        f"{expected_table_sectors}")
if adpcm_bank_copies != 2:
    sys.exit(
        f"check_player_ring: ADPCM table needs two physical-bank copies, got "
        f"{adpcm_bank_copies}")
if adpcm_table % 4 or pcm_dec_buf % 4:
    sys.exit("check_player_ring: ADPCM table and PCM buffer must be long-aligned")
if adpcm_table + adpcm_table_bytes > pcm_dec_buf:
    sys.exit(
        f"check_player_ring: ADPCM table ends at "
        f"{adpcm_table + adpcm_table_bytes:#x}, overlapping PCM buffer "
        f"{pcm_dec_buf:#x}")
if pcm_dec_buf + 1536 > _equ(text, "ROUTING", SP):
    sys.exit("check_player_ring: ADPCM PCM buffer overlaps resident routing")
_require_asm(
    r"^\s*move\.w\s+#ADPCM_TABLE_LONGS-1,\s*d0\s*$",
    "ADPCM_TABLE_LONGS in the full-table copy loop")
_require_asm(
    r"^\s*moveq\s+#ADPCM_BANK_COPIES-1,\s*d1\s*$",
    "ADPCM_BANK_COPIES in the Word-RAM bank loop")
print(
    "check_player_ring: OK  ADPCM full table "
    f"{adpcm_table:#x}..{adpcm_table + adpcm_table_bytes:#x}, "
    f"{adpcm_table_sectors} sectors x {adpcm_bank_copies} banks, "
    f"PCM buffer {pcm_dec_buf:#x}..{pcm_dec_buf + 1536:#x}")


ring_bytes = _equ(text, "RING_SIZE", SP)
want_bytes = av_config.RING_SIZE_KB * 1024
if ring_bytes != want_bytes:
    sys.exit(
        f"check_player_ring: player RING_SIZE={ring_bytes} (0x{ring_bytes:X}) "
        f"!= av_config.RING_SIZE_KB={av_config.RING_SIZE_KB} "
        f"({want_bytes} / 0x{want_bytes:X}). Update one so they agree "
        f"(single source of truth = tools/av_config.py).")
print(f"check_player_ring: OK  RING_SIZE={ring_bytes//1024}KB "
      f"== av_config.RING_SIZE_KB (PrgBuf cap {av_config.PRG_BUF_CAP_KB}KB, "
      f"quality budget {av_config.QUALITY_BUDGET_KB}KB)")

# --- Boot-time PRG staging and resident Word-RAM routing map ---
# Frame 0 is allowed to load the whole H40 raster, unlike timed frames. Keep its
# sector-rounded pattern block in the physical ring's jitter-only tail, then
# reuse that memory only after frame 0 has expanded. ROUTING_TMP borrows APPLY
# before steady streaming starts. The validated table is duplicated at the end
# of both 128 KiB Word-RAM banks, so routing remains visible after every swap.
route_bytes = ttrc_routing.ROUTE_BYTES
sector = ttrc_routing.SECTOR_BYTES
max_f0_bytes = ((40 * 28 * 32 + sector - 1) // sector) * sector
ring_base = _equ(text, "RING_BASE", SP)
ring_cap_end = _equ(text, "RING_CAP_END", SP)
f0pat_tmp = _equ(text, "F0PAT_TMP", SP)
apply_base = _equ(text, "APPLY_BASE", SP)
apply_size = _equ(text, "APPLY_SIZE", SP)
routing_tmp = _equ(text, "ROUTING_TMP", SP)
sub_bank = _equ(text, "SUB_BANK_1M", SP)
routing = _equ(text, "ROUTING", SP)

expected_cap_end = ring_base + av_config.RING_CAP_KB * 1024
if ring_cap_end != expected_cap_end or f0pat_tmp != ring_cap_end:
    sys.exit(
        "check_player_ring: frame-0 staging must start exactly at the usable "
        f"ring cap: RING_CAP_END={ring_cap_end:#x}, F0PAT_TMP={f0pat_tmp:#x}, "
        f"expected={expected_cap_end:#x}")
ring_end = ring_base + ring_bytes
if ring_base % sector or ring_bytes % sector or ring_bytes % 32:
    sys.exit(
        "check_player_ring: physical PrgBuf ring must be sector- and pattern-aligned: "
        f"base={ring_base:#x}, size={ring_bytes:#x}")
if ring_end > apply_base:
    sys.exit(
        f"check_player_ring: RING_END={ring_end:#x} overlaps "
        f"APPLY_BASE={apply_base:#x}")
if ring_end != apply_base:
    sys.exit(
        f"check_player_ring: relocated routing leaves reclaimable PRG RAM: "
        f"RING_END={ring_end:#x}, APPLY_BASE={apply_base:#x}")
if ring_end - ring_cap_end != av_config.RING_JITTER_MARGIN_KB * 1024:
    sys.exit(
        "check_player_ring: physical-to-usable ring gap does not match the "
        f"configured jitter margin: {ring_end - ring_cap_end} bytes")
if f0pat_tmp + max_f0_bytes > ring_end:
    sys.exit(
        f"check_player_ring: H40 frame-0 staging ends at "
        f"{f0pat_tmp + max_f0_bytes:#x}, beyond RING_END={ring_end:#x}")
if routing_tmp != apply_base or routing_tmp + route_bytes > apply_base + apply_size:
    sys.exit(
        "check_player_ring: boot routing staging must fit in the unused APPLY "
        f"ring: ROUTING_TMP={routing_tmp:#x}, APPLY={apply_base:#x}.."
        f"{apply_base + apply_size:#x}")
word_bank_end = sub_bank + 0x20000
if routing + route_bytes != word_bank_end:
    sys.exit(
        "check_player_ring: resident routing must occupy the final 16 KiB of "
        f"the owned Word-RAM bank: ROUTING={routing:#x}, bank end={word_bank_end:#x}")
print(
    "check_player_ring: OK  frame0 boot staging "
    f"{f0pat_tmp:#x}..{f0pat_tmp + max_f0_bytes:#x}, routing temp "
    f"{routing_tmp:#x}..{routing_tmp + route_bytes:#x}, Word routing "
    f"{routing:#x}..{routing + route_bytes:#x}, PRG ring end {ring_end:#x}")

# --- v12 boot-only pattern supply map ---
word_buf = _equ(text, "WORD_BUF", SP)
word_buf_patterns = _equ(text, "WORD_BUF_PATTERNS", SP)
ip_word_buf_off = _equ(ip_text, "WORD_BUF_OFF", IP)
ip_word_buf_end = _equ(ip_text, "WORD_BUF_END", IP)
ip_word_buf_patterns = _equ(ip_text, "WORD_BUF_PATTERNS", IP)
dic_stage_off = _equ(ip_text, "DIC_STAGE_OFF", IP)
dic_stage_patterns = _equ(text, "DIC_STAGE_PATTERNS", SP)
dic_buf = _equ(ip_text, "DIC_BUF", IP)
dic_buf_patterns = _equ(ip_text, "DIC_BUF_PATTERNS", IP)
run_table = _equ(ip_text, "RUN_TABLE", IP)

if word_buf != sub_bank + pattern_supply.WORD_BUF_OFFSET:
    sys.exit(
        f"check_player_ring: SP WORD_BUF={word_buf:#x} != owned bank + "
        f"Python offset {pattern_supply.WORD_BUF_OFFSET:#x}")
if (ip_word_buf_off, ip_word_buf_end, ip_word_buf_patterns) != (
        pattern_supply.WORD_BUF_OFFSET,
        pattern_supply.WORD_BUF_END,
        pattern_supply.WORD_BUF_PATTERNS):
    sys.exit(
        "check_player_ring: Main WordBuf layout does not match pattern_supply.py: "
        f"{ip_word_buf_off:#x}..{ip_word_buf_end:#x}, {ip_word_buf_patterns} patterns")
if word_buf_patterns != pattern_supply.WORD_BUF_PATTERNS:
    sys.exit(
        f"check_player_ring: Sub WORD_BUF_PATTERNS={word_buf_patterns} != "
        f"Python {pattern_supply.WORD_BUF_PATTERNS}")
if pcm_dec_buf + 1536 != word_buf or word_buf + word_buf_patterns * 32 != routing:
    sys.exit(
        "check_player_ring: WordBuf must exactly fill the stable gap between "
        f"PCM_DEC_BUF and ROUTING: pcm_end={pcm_dec_buf + 1536:#x}, "
        f"WordBuf={word_buf:#x}..{word_buf + word_buf_patterns * 32:#x}, "
        f"routing={routing:#x}")
if dic_stage_off != pattern_supply.DIC_STAGE_OFFSET:
    sys.exit(
        f"check_player_ring: DIC_STAGE_OFF={dic_stage_off:#x} != "
        f"Python {pattern_supply.DIC_STAGE_OFFSET:#x}")
if dic_stage_patterns != pattern_supply.DIC_BUF_PATTERNS:
    sys.exit(
        f"check_player_ring: DIC_STAGE_PATTERNS={dic_stage_patterns} != "
        f"Python DicBuf capacity {pattern_supply.DIC_BUF_PATTERNS}")
if not re.search(
        r"^\.equ\s+DIC_STAGE,\s*SUB_BANK_1M\+0xD000\b", text, re.M):
    sys.exit("check_player_ring: SP DIC_STAGE must use the shared +0xD000 offset")
if dic_stage_off + dic_stage_patterns * 32 > 0x10000:
    sys.exit("check_player_ring: DicBuf Word-RAM staging overlaps CTRL at +0x10000")
if (dic_buf, run_table, dic_buf_patterns) != (
        pattern_supply.DIC_BUF_BASE,
        pattern_supply.DIC_BUF_END,
        pattern_supply.DIC_BUF_PATTERNS):
    sys.exit(
        "check_player_ring: DicBuf layout does not match pattern_supply.py: "
        f"{dic_buf:#x}..{run_table:#x}, {dic_buf_patterns} patterns")
if not re.search(
        r"^\.equ\s+MAIN_CODEGEN_LIMIT,\s*DIC_BUF\s*$", ip_text, re.M):
    sys.exit("check_player_ring: Main code generation must stop at DIC_BUF")
print(
    "check_player_ring: OK  pattern supply "
    f"Wr0/Wr1 each {word_buf_patterns} patterns at bank+{ip_word_buf_off:#x}, "
    f"Dic {dic_buf_patterns} patterns at {dic_buf:#x}..{run_table:#x}")

# --- CRAM pre-load (PALTAB) consistency ---
# The pack sizes the PALTAB to av_config.PALTAB_MAX_SEG; the Main player copies it
# into a fixed Main-RAM table sized by its own `.equ PALTAB_MAX_SEG`. And both CPUs
# must agree on the Word-RAM staging offset (`.equ PALTAB_OFF`). Drift = wrong
# palettes on segment switches, so fail the build instead.
ip_max_seg = _equ(ip_text, "PALTAB_MAX_SEG", IP)
if ip_max_seg != av_config.PALTAB_MAX_SEG:
    sys.exit(
        f"check_player_ring: player PALTAB_MAX_SEG={ip_max_seg} != "
        f"av_config.PALTAB_MAX_SEG={av_config.PALTAB_MAX_SEG}. Update both together "
        f"(single source of truth = tools/av_config.py).")
sp_off = _equ(text, "PALTAB_OFF", SP)
ip_off = _equ(ip_text, "PALTAB_OFF", IP)
if sp_off != ip_off:
    sys.exit(
        f"check_player_ring: PALTAB_OFF mismatch sp={sp_off:#x} ip={ip_off:#x} "
        f"(the Word-RAM staging offset must agree between the two CPUs).")
print(f"check_player_ring: OK  PALTAB_MAX_SEG={ip_max_seg} "
      f"({ip_max_seg * 128 // 1024}KB Main-RAM table), staging offset {sp_off:#x}")
