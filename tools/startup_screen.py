#!/usr/bin/env python3
"""Generate the profile-specific Mega-CD preload status-screen include."""
from __future__ import annotations

import argparse
from fractions import Fraction
from pathlib import Path

from PIL import Image

import av_config
import encode_config
import player_constants


GLYPHS = "".join(chr(value) for value in range(32, 127))
GLYPH_INDEX = {char: index for index, char in enumerate(GLYPHS)}
MAX_COLS = 32
MAX_ROWS = 28


def _font_bits(font_path: Path) -> bytes:
    """Extract the used ASCII glyphs from SGDK's default 16x6 tile sheet."""
    sheet = Image.open(font_path)
    if sheet.size != (128, 48):
        raise ValueError(
            f"SGDK default font must be 128x48, got {sheet.size} from {font_path}")
    out = bytearray()
    for char in GLYPHS:
        ascii_index = ord(char) - 32
        if not 0 <= ascii_index < 96:
            raise ValueError(f"glyph is outside SGDK's ASCII sheet: {char!r}")
        sx = ascii_index % 16 * 8
        sy = ascii_index // 16 * 8
        for y in range(8):
            value = 0
            for x in range(8):
                value = (value << 1) | int(bool(sheet.getpixel((sx + x, sy + y))))
            out.append(value)
    return bytes(out)


def _sanitize(text: str) -> str:
    return "".join(char if char in GLYPH_INDEX else " " for char in str(text))


def _kib(patterns: int) -> str:
    value = int(patterns) * 32 / 1024
    return f"{value:g}"


def _duration(value: str) -> str:
    seconds = float(value)
    minutes, second = divmod(seconds, 60)
    return f"{int(minutes):02d}:{second:05.2f}"


def _version(path: Path) -> str:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip()
    return f"{values['date']}.E{values['e']}.P{values['p']}"


def screen_lines(profile: encode_config.EncodeProfile,
                 constants: player_constants.PlayerConstants,
                 version: str) -> list[tuple[int, int, int, str]]:
    """Return row, column, palette, text records for the 32-column screen."""
    video = profile.data["video"]
    source = profile.data["source"]
    encoder = profile.data.get("encoder", {})
    palette = profile.data["palette"]
    audio = profile.data["audio"]
    width = int(video["width"])
    height = int(video["height"])
    active_tiles = int(video.get("active_tiles", width * height // 64))
    cap = av_config.cold_cap_for_fps(
        float(Fraction(str(source["fps"]))), video["mode"], active_tiles)
    audio_label = "ADPCM22 22.05k mono" if audio["kind"] == "adpcm22" \
        else "PCM13 13.3k mono"
    flags = [
        name for name, enabled in (
            ("DITH", encoder.get("dither", False)),
            ("SEGPAL", encoder.get("segment_palettes", False)),
            ("NEAR", encoder.get("near", False)),
            ("COA", encoder.get("coa", False)),
        ) if enabled
    ]
    fit = str(video["fit"]).lower()
    if fit == "crop":
        fit = "cover"
    title = profile.data.get("metadata", {}).get("title", profile.artifact_stem)
    dash = "-" * MAX_COLS
    lines = [
        (0, max(0, (MAX_COLS - len(_sanitize(title)[:MAX_COLS])) // 2),
         0, _sanitize(title)[:MAX_COLS]),
        (1, 3, 0, f"PROFILE  {profile.artifact_stem}"[:29]),
        (2, 0, 0, dash),
        (3, 1, 1, "VIDEO / STREAM"),
        (4, 1, 0, f"Mode      {video['mode'].upper()} {width}x{height} "
                    f"{constants.tcols}x{constants.trows}"),
        (5, 1, 0, f"Active    {active_tiles}/{width * height // 64} tiles"),
        (6, 1, 0, f"Timing    {source['fps']}fps {_duration(source['duration'])[:5]} "
                    f"{constants.frames}fr"),
        (7, 1, 0, f"Codec     TTRC e{version.split('.E')[1].split('.P')[0]} "
                    f"{encoder.get('rate_kib', 0)} KiB/s"),
        (8, 1, 0, f"Audio     {audio_label}"),
        (9, 1, 0, f"Palette   {str(palette['algorithm']).lower()} {constants.nseg} seg"),
        (10, 1, 0, f"Filter    {fit} / {video.get('resize_filter', 'lanczos')} / "
                     f"{'dith' if encoder.get('dither', False) else 'plain'}"),
        (11, 1, 0, f"VRAM      {encoder.get('vram_tiles', 0)} Cold {cap} "
                     f"N+C {'on' if encoder.get('near') and encoder.get('coa') else 'off'}"),
        (12, 0, 0, dash),
        (13, 1, 1, "STARTUP PRELOAD"),
        (14, 1, 2, f"PalTab  {constants.paltab_sec * 2:02d}/{constants.paltab_sec * 2:02d} "
                     "KiB ........   "),
        (15, 1, 2, f"ADPCM   {constants.adpcm_table_sectors * 2:02d}/"
                     f"{constants.adpcm_table_sectors * 2:02d} KiB ........   "),
        (16, 1, 2, f"Audio   {constants.audio_preload_sec * 2:02d}/"
                     f"{constants.audio_preload_sec * 2:02d} KiB ........   "),
        (17, 1, 2, f"Frame0 C{constants.f0_ctrl_sec * 2:02d}/P"
                     f"{constants.f0_pat_sec * 2:02d} KiB ......   "),
        (18, 1, 2, f"Routing {constants.routing_sec * 2:02d}/"
                     f"{constants.routing_sec * 2:02d} KiB ........   "),
        (19, 1, 3, f"PrgBuf 000/{av_config.PRG_BUF_CAP_KB} KiB LOADING"),
        (20, 0, 3, "[" + "-" * 30 + "]"),
        (22, 0, 0, f"Physical ring {av_config.RING_SIZE_KB} / safe "
                     f"{av_config.PRG_BUF_CAP_KB} KiB"),
        (24, 5, 0, "Preparing playback..."),
    ]
    for row, col, _palette, text in lines:
        if not 0 <= row < MAX_ROWS or not 0 <= col < MAX_COLS:
            raise ValueError(f"startup line origin is outside the screen: {row},{col}")
        if col + len(text) > MAX_COLS:
            raise ValueError(
                f"startup line exceeds {MAX_COLS} columns at row {row}: {text!r}")
        unsupported = set(text) - set(GLYPHS)
        if unsupported:
            raise ValueError(f"unsupported startup glyphs: {sorted(unsupported)!r}")
    return lines


def _bytes(lines: list[tuple[int, int, int, str]]) -> bytes:
    out = bytearray()
    for row, col, palette, text in lines:
        encoded = bytes(GLYPH_INDEX[char] for char in text)
        out += bytes((row, col, palette, len(encoded))) + encoded
    out.append(0xFF)
    return bytes(out)


def _asm_bytes(label: str, data: bytes, width: int = 16) -> list[str]:
    lines = [f"{label}:"]
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        lines.append("\t.byte\t" + ",".join(f"0x{value:02X}" for value in chunk))
    return lines


def render_include(profile: encode_config.EncodeProfile,
                   constants: player_constants.PlayerConstants,
                   version: str, font_path: Path) -> str:
    lines = screen_lines(profile, constants, version)
    prg = next(item for item in lines if item[3].startswith("PrgBuf "))
    bar = next(item for item in lines if item[3].startswith("["))
    prefix = [item for item in lines if item[0] >= 14 and item[3].startswith(
        ("PalTab ", "ADPCM ", "Audio ", "Frame0 ", "Routing "))]
    value_col = prg[1] + len("PrgBuf ")
    status_col = prg[1] + prg[3].index("LOADING")
    text = [
        "/* Generated by tools/startup_screen.py. Do not edit. */",
        f".equ STARTUP_FONT_N, {len(GLYPHS)}",
        f".equ STARTUP_GLYPH_0, {GLYPH_INDEX['0']}",
        f".equ STARTUP_GLYPH_HASH, {GLYPH_INDEX['#']}",
        f".equ STARTUP_GLYPH_DASH, {GLYPH_INDEX['-']}",
        f".equ STARTUP_GLYPH_O, {GLYPH_INDEX['O']}",
        f".equ STARTUP_GLYPH_K, {GLYPH_INDEX['K']}",
        f".equ STARTUP_PRG_VALUE_ADDR, 0x{0xE000 + prg[0] * 128 + value_col * 2:04X}",
        f".equ STARTUP_PRG_STATUS_ADDR, 0x{0xE000 + prg[0] * 128 + status_col * 2:04X}",
        f".equ STARTUP_PRG_BAR_ADDR, 0x{0xE000 + bar[0] * 128 + (bar[1] + 1) * 2:04X}",
        f".equ STARTUP_PRG_CAP_KB, {av_config.PRG_BUF_CAP_KB}",
        f".equ STARTUP_PREFIX_OK_N, {len(prefix)}",
        "",
        "startup_palette:",
        # Four palettes: black background plus white/cyan/green/amber ink.
        "\t.word\t0x0000,0x0EE0" + ",0x0000" * 14,
        "\t.word\t0x0000,0x0EEE" + ",0x0000" * 14,
        "\t.word\t0x0000,0x00E0" + ",0x0000" * 14,
        "\t.word\t0x0000,0x00AE" + ",0x0000" * 14,
        "startup_nibble_words:",
    ]
    for value in range(16):
        word = 0
        for bit in range(3, -1, -1):
            word = (word << 4) | ((value >> bit) & 1)
        text.append(f"\t.word\t0x{word:04X}")
    text += _asm_bytes("startup_font_bits", _font_bits(font_path))
    text += _asm_bytes("startup_lines", _bytes(lines))
    prefix_addresses = bytearray()
    for row, col, _palette, line in prefix:
        addr = 0xE000 + row * 128 + (col + len(line) - 2) * 2
        prefix_addresses += addr.to_bytes(2, "big")
    text += _asm_bytes("startup_prefix_ok_addrs", bytes(prefix_addresses))
    text += _asm_bytes(
        "startup_ok_glyphs",
        bytes(GLYPH_INDEX[char] for char in "OK     "))
    text.append("")
    return "\n".join(text)


def generate(config_path: Path, header_path: Path, version_path: Path, font_path: Path,
             output_path: Path) -> None:
    profile = encode_config.load_profile(config_path)
    with header_path.open("rb") as src:
        constants = player_constants.parse_header_sector(src.read(player_constants.SECTOR))
    rendered = render_include(
        profile, constants, _version(version_path), font_path)
    if not output_path.exists() or output_path.read_text() != rendered:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--header", type=Path, required=True)
    parser.add_argument("--version", type=Path, default=Path("tools/av_version.txt"))
    parser.add_argument("--font", type=Path, required=True,
                        help="SGDK res/image/font_default.png")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generate(args.config, args.header, args.version, args.font, args.output)
    print(f"startup_screen: {args.output}")


if __name__ == "__main__":
    main()
