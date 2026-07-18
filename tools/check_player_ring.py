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

SP = Path(__file__).resolve().parent.parent / "boot" / "movieplay_sp.s"
text = SP.read_text()


def _equ(source, name, path):
    m = re.search(r"^\.equ\s+%s,\s*(0x[0-9A-Fa-f]+|\d+)" % name, source, re.M)
    if not m:
        sys.exit(f"check_player_ring: could not find `.equ {name}` in {path}")
    return int(m.group(1), 0)


ring_bytes = _equ(text, "RING_SIZE", SP)
want_bytes = av_config.RING_SIZE_KB * 1024
if ring_bytes != want_bytes:
    sys.exit(
        f"check_player_ring: player RING_SIZE={ring_bytes} (0x{ring_bytes:X}) "
        f"!= av_config.RING_SIZE_KB={av_config.RING_SIZE_KB} "
        f"({want_bytes} / 0x{want_bytes:X}). Update one so they agree "
        f"(single source of truth = tools/av_config.py).")
print(f"check_player_ring: OK  RING_SIZE={ring_bytes//1024}KB "
      f"== av_config.RING_SIZE_KB (cap {av_config.RING_CAP_KB}KB, tank {av_config.TANK_KB}KB)")

# --- Boot-time PRG staging and resident Word-RAM routing map ---
# Frame 0 is allowed to load the whole H40 raster, unlike timed frames. Keep its
# sector-rounded pattern block in the physical ring's jitter-only tail, then
# reuse that memory only after frame 0 has expanded. ROUTING_TMP borrows APPLY
# before steady streaming starts. The validated table is duplicated at the end
# of both 128 KiB Word-RAM banks, so routing remains visible after every swap.
route_bytes = 16 * 1024
sector = 2048
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
        "check_player_ring: payload ring must be sector- and pattern-aligned: "
        f"base={ring_base:#x}, size={ring_bytes:#x}")
if ring_end > apply_base:
    sys.exit(
        f"check_player_ring: RING_END={ring_end:#x} overlaps "
        f"APPLY_BASE={apply_base:#x}")
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

# --- CRAM pre-load (PALTAB) consistency ---
# The pack sizes the PALTAB to av_config.PALTAB_MAX_SEG; the Main player copies it
# into a fixed Main-RAM table sized by its own `.equ PALTAB_MAX_SEG`. And both CPUs
# must agree on the Word-RAM staging offset (`.equ PALTAB_OFF`). Drift = wrong
# palettes on segment switches, so fail the build instead.
IP = Path(__file__).resolve().parent.parent / "boot" / "movieplay_ip.s"
ip_text = IP.read_text()

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
