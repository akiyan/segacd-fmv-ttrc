#!/usr/bin/env python3
"""Build the generic/specialized player matrix for issue #21."""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import av_config  # noqa: E402
import pattern_supply  # noqa: E402
import player_constants  # noqa: E402
import ttrc_routing  # noqa: E402


@dataclass(frozen=True)
class Case:
    name: str
    mode: int
    fps: int
    adpcm22: bool = False
    pattern_supply: bool = False


CASES = (
    Case("h32-15", 0, 15),
    Case("h32-24", 0, 24),
    Case("h32-30", 0, 30),
    Case("h40-15", 1, 15),
    Case("h40-24", 1, 24),
    Case("h40-30", 1, 30),
    Case("h40-15-adpcm", 1, 15, True),
    Case("h40-30-adpcm", 1, 30, True),
    Case("h32-30-supply", 0, 30, False, True),
    Case("h40-30-adpcm-supply", 1, 30, True, True),
)


def find_tool(name: str) -> Path:
    found = shutil.which(name)
    if found:
        return Path(found)
    candidate = Path.home() / "toolchains/mars/m68k-elf/bin" / name
    if candidate.is_file():
        return candidate
    raise SystemExit(f"missing tool: {name}")


def make_header(case: Case) -> bytes:
    tcols = 32 if case.mode == 0 else 40
    trows = 28
    cells = tcols * trows
    frames = 600
    features = ttrc_routing.FEATURE_COLD_RUNS
    if av_config.uses_fixed_n2_cadence(case.fps):
        features |= ttrc_routing.FEATURE_FIXED_N2
    if case.adpcm22:
        _rate, audio, _control = av_config.audio_frame_layout(
            "adpcm22", case.fps)
        features |= ttrc_routing.FEATURE_ADPCM22
    else:
        audio = av_config.pcm_frame_bytes(case.fps, 13_300)
    if case.pattern_supply:
        features |= ttrc_routing.FEATURE_PATTERN_SUPPLY
    audio_fd = av_config.rf5c164_fd(
        audio, av_config.playback_fps_for_content(case.fps))
    prefix = struct.pack(
        ">4s9H4LBB3L6H",
        b"TTRC", ttrc_routing.VERSION, frames, tcols, trows, cells,
        1400, 1, ttrc_routing.FRAME_SECTORS, 1,
        12416, ttrc_routing.routing_sector_count(frames), 194, 12416,
        case.mode, 0, 2, 18 if case.mode else 14, 1,
        av_config.vsync_n_for_fps(case.fps), audio, case.fps,
        audio_fd, 30, features,
    )
    sector = bytearray(
        prefix + bytes(128) + bytes(player_constants.SECTOR - 192))
    if case.pattern_supply:
        player_constants.PATTERN_SUPPLY_STRUCT.pack_into(
            sector, player_constants.PATTERN_SUPPLY_OFFSET,
            player_constants.PATTERN_SUPPLY_MAGIC,
            player_constants.PATTERN_SUPPLY_VERSION, 0,
            pattern_supply.WORD_BUF_PATTERNS,
            pattern_supply.WORD_BUF_PATTERNS,
            pattern_supply.MAIN_BUF_PATTERNS,
            (pattern_supply.WORD_BUF_PATTERNS + 63) // 64,
            (pattern_supply.WORD_BUF_PATTERNS + 63) // 64,
            (pattern_supply.MAIN_BUF_PATTERNS + 63) // 64,
        )
    return player_constants.stamp_header_sector(sector)


@dataclass(frozen=True)
class Build:
    ip_text: int
    ip_bin: int
    sp_text: int
    sp_bin: int


def run(command: list[str]) -> str:
    result = subprocess.run(
        command, cwd=ROOT, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.stdout


def text_size(size: Path, obj: Path) -> int:
    for line in run([str(size), "-A", str(obj)]).splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == ".text":
            return int(fields[1])
    raise AssertionError(f"no .text size in {obj}")


def verify_flip_control_flow(objdump: Path, obj: Path) -> None:
    """Keep flip branches local and prove the final VBlank guard ordering."""
    disassembly = run([str(objdump), "-d", str(obj)])
    start_match = re.search(r"^([0-9a-f]+) <bf_doflip>:$", disassembly, re.MULTILINE)
    end_match = re.search(r"^([0-9a-f]+) <bf_after_flip>:$", disassembly, re.MULTILINE)
    if not start_match or not end_match:
        raise AssertionError(f"{obj}: missing bf_doflip symbols")
    start = int(start_match.group(1), 16)
    end = int(end_match.group(1), 16)
    block = disassembly[start_match.end():end_match.start()]
    branches = re.findall(
        r"^\s*[0-9a-f]+:\s+(?:[0-9a-f]{4}\s+)+"
        r"(?!bsr)(b[a-z]+)\s+([0-9a-f]+)\s+<",
        block,
        re.MULTILINE,
    )
    escaped = [
        (mnemonic, int(target, 16))
        for mnemonic, target in branches
        if not start <= int(target, 16) <= end
    ]
    if escaped:
        details = ", ".join(f"{op}->0x{target:X}" for op, target in escaped)
        raise AssertionError(f"{obj}: bf_doflip branch escaped its region: {details}")

    guard_match = re.search(
        r"^([0-9a-f]+) <do_flip>:$", disassembly, re.MULTILINE)
    guard_end_match = re.search(
        r"^([0-9a-f]+) <dma_chunk_wr>:$", disassembly, re.MULTILINE)
    if not guard_match or not guard_end_match:
        raise AssertionError(f"{obj}: missing do_flip guard symbols")
    guard_start = int(guard_match.group(1), 16)
    guard_end = int(guard_end_match.group(1), 16)
    guard = disassembly[guard_match.end():guard_end_match.start()]
    status_reads = list(re.finditer(
        r"\bmovew\s+(?:00)?c00004 <VDP_CTRL>,%d0", guard))
    hv_read = re.search(r"\bmovew\s+(?:00)?c00008 <VDP_HV>,%d0", guard)
    tail_check = re.search(r"\bcmpiw\s+#-1024,%d0", guard)
    fresh_wait = re.search(r"\bbsr\w*\s+[^\n]*<wait_vb_start>", guard)
    plane_write = re.search(
        r"\bmovew\s+%d5,(?:00)?c00004 <VDP_CTRL>", guard)
    if (len(status_reads) != 2 or hv_read is None or tail_check is None or
            fresh_wait is None or plane_write is None):
        raise AssertionError(f"{obj}: incomplete final VBlank guard")
    positions = (
        status_reads[0].start(), hv_read.start(), tail_check.start(),
        status_reads[1].start(), fresh_wait.start(), plane_write.start(),
    )
    if positions != tuple(sorted(positions)):
        raise AssertionError(f"{obj}: final VBlank guard is out of order")

    guard_branches = re.findall(
        r"^\s*[0-9a-f]+:\s+(?:[0-9a-f]{4}\s+)+"
        r"(?!bsr)(b[a-z]+)\s+([0-9a-f]+)\s+<",
        guard,
        re.MULTILINE,
    )
    if len(guard_branches) < 3:
        raise AssertionError(f"{obj}: final VBlank guard branches are missing")
    escaped = [
        (mnemonic, int(target, 16))
        for mnemonic, target in guard_branches
        if not guard_start <= int(target, 16) < guard_end
    ]
    if escaped:
        details = ", ".join(f"{op}->0x{target:X}" for op, target in escaped)
        raise AssertionError(f"{obj}: do_flip branch escaped its region: {details}")


def verify_adpcm_decode_pump(
    objdump: Path, obj: Path, *, expected: bool,
) -> None:
    """Require the low-rate specialized decoder to service the CDC mid-chunk."""
    disassembly = run([str(objdump), "-d", str(obj)])
    start_match = re.search(
        r"^[0-9a-f]+ <decode_adpcm_chunk>:$", disassembly, re.MULTILINE)
    end_match = re.search(
        r"^[0-9a-f]+ <write_wave_chunk>:$", disassembly, re.MULTILINE)
    if not start_match or not end_match:
        raise AssertionError(f"{obj}: missing ADPCM decoder symbols")
    block = disassembly[start_match.end():end_match.start()]
    found = bool(re.search(r"\bbsr\w*\s+[^\n]*<pump_poll>", block))
    if found != expected:
        state = "present" if found else "absent"
        wanted = "present" if expected else "absent"
        raise AssertionError(
            f"{obj}: decoder pump is {state}, expected {wanted}")


def build_case(
    case: Case, case_dir: Path, *, specialized: bool,
    assembler: Path, linker: Path, size: Path, objdump: Path,
) -> Build:
    tag = "specialized" if specialized else "generic"
    common = [
        str(assembler), "-m68000", "--register-prefix-optional", "--bitwise-or",
        "--defsym", "DEBUG=1",
    ]
    fixed = ["--defsym", "PLAYER_SPECIALIZED=1"] if specialized else []
    includes = ["-I", str(case_dir), "-I", str(ROOT / "boot")]

    ip_obj = case_dir / f"ip-{tag}.o"
    ip_bin = case_dir / f"ip-{tag}.bin"
    run(common + [
        "--defsym", "MAIN_CODEGEN=1", "--defsym", "DMA_RUN_FASTPATH=1",
    ] + fixed + includes + [str(ROOT / "boot/movieplay_ip.s"), "-o", str(ip_obj)])
    run([
        str(linker), "-nostdlib", "--oformat", "binary",
        "-T", str(ROOT / "cfg/ip.ld"), "-o", str(ip_bin), str(ip_obj),
    ])
    if specialized:
        verify_flip_control_flow(objdump, ip_obj)

    sp_obj = case_dir / f"sp-{tag}.o"
    sp_bin = case_dir / f"sp-{tag}.bin"
    run(common + fixed + includes + [
        str(ROOT / "boot/movieplay_sp.s"), "-o", str(sp_obj),
    ])
    run([
        str(linker), "-nostdlib", "--oformat", "binary",
        "-T", str(ROOT / "cfg/sp.ld"), "-o", str(sp_bin), str(sp_obj),
    ])
    if specialized and case.adpcm22:
        verify_adpcm_decode_pump(
            objdump, sp_obj, expected=case.fps < 24)

    return Build(
        ip_text=text_size(size, ip_obj),
        ip_bin=ip_bin.stat().st_size,
        sp_text=text_size(size, sp_obj),
        sp_bin=sp_bin.stat().st_size,
    )


def main() -> None:
    assembler = find_tool("m68k-elf-as")
    linker = find_tool("m68k-elf-ld")
    size = find_tool("m68k-elf-size")
    objdump = find_tool("m68k-elf-objdump")
    tmp_root = ROOT / "tmp"
    tmp_root.mkdir(exist_ok=True)

    print("case      IP generic->specialized   SP generic->specialized")
    with tempfile.TemporaryDirectory(prefix="player_constants_", dir=tmp_root) as td:
        matrix_dir = Path(td)
        for case in CASES:
            case_dir = matrix_dir / case.name
            case_dir.mkdir()
            header = make_header(case)
            header_path = case_dir / "HEADER.DAT"
            header_path.write_bytes(header)
            (case_dir / "palettes.bin").write_bytes(bytes(128))
            constants = player_constants.generate_include(
                header_path, case_dir / "player_constants.inc")

            generic = build_case(
                case, case_dir, specialized=False,
                assembler=assembler, linker=linker, size=size, objdump=objdump)
            specialized = build_case(
                case, case_dir, specialized=True,
                assembler=assembler, linker=linker, size=size, objdump=objdump)

            if specialized.ip_bin > generic.ip_bin:
                raise AssertionError(
                    f"{case.name}: specialized IP grew {generic.ip_bin}->{specialized.ip_bin}")
            if specialized.sp_bin > generic.sp_bin:
                raise AssertionError(
                    f"{case.name}: specialized SP grew {generic.sp_bin}->{specialized.sp_bin}")
            if specialized.sp_bin > 4096:
                raise AssertionError(
                    f"{case.name}: specialized SP is {specialized.sp_bin} bytes")

            sp_bytes = (case_dir / "sp-specialized.bin").read_bytes()
            if struct.pack(">L", constants.signature) not in sp_bytes:
                raise AssertionError(
                    f"{case.name}: SP does not contain HEADER signature immediate")
            if struct.pack(">H", 0xBAD1) not in sp_bytes:
                raise AssertionError(f"{case.name}: SP has no mismatch diagnostic")

            print(
                f"{case.name:<9} "
                f"{generic.ip_bin:4}->{specialized.ip_bin:4}B "
                f"(text {generic.ip_text:4}->{specialized.ip_text:4})   "
                f"{generic.sp_bin:4}->{specialized.sp_bin:4}B "
                f"(text {generic.sp_text:4}->{specialized.sp_text:4})")

    print("player constant build matrix: OK")


if __name__ == "__main__":
    main()
