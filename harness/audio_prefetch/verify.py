#!/usr/bin/env python3
"""Verify the exact PCM queue order from a real HEADER.DAT + BODY.DAT pair."""

from __future__ import annotations

import argparse
import struct
import wave
from pathlib import Path

import numpy as np


SECTOR = 2048
DBG_LEN = 22


def sign_magnitude_audio(path: Path, target_len: int) -> bytes:
    with wave.open(str(path), "rb") as src:
        if src.getsampwidth() != 1 or src.getnchannels() != 1:
            raise SystemExit("source WAV must be mono unsigned 8-bit PCM")
        raw = src.readframes(src.getnframes())
    if not raw:
        raw = b"\x80"
    if len(raw) != target_len:
        samples = np.frombuffer(raw, np.uint8).astype(np.float64)
        source_x = np.arange(len(samples), dtype=np.float64)
        target_x = np.linspace(0.0, float(len(samples) - 1), target_len)
        raw = np.rint(np.interp(target_x, source_x, samples)).clip(0, 255).astype(np.uint8).tobytes()
    out = bytearray(len(raw))
    for index, value in enumerate(raw):
        sample = value - 128
        out[index] = sample if sample >= 0 else (0x80 | min(-sample, 0x7E))
    return bytes(out)


def control_audio(block: bytes, cells: int, audio_bytes: int) -> bytes:
    if len(block) < 8:
        raise SystemExit("truncated control block")
    n_upd = struct.unpack_from(">H", block, 4)[0]
    dbg = block[7]
    pos = 8 + (DBG_LEN if dbg else 0) + ((cells + 7) // 8) + n_upd * 2
    chunk = block[pos:pos + audio_bytes]
    if len(chunk) != audio_bytes:
        raise SystemExit(
            f"truncated control audio: {len(chunk)} / {audio_bytes} bytes")
    return chunk


def split_blocks(data: bytes, count: int) -> list[bytes]:
    blocks = []
    pos = 0
    for frame in range(count):
        if pos + 2 > len(data):
            raise SystemExit(f"control stream ended before frame {frame}")
        total = struct.unpack_from(">H", data, pos)[0]
        if total < 8 or total & 1 or pos + total > len(data):
            raise SystemExit(
                f"invalid control length {total} at frame {frame}, offset {pos}")
        block = data[pos:pos + total]
        sequence = struct.unpack_from(">H", block, 2)[0]
        if sequence != (frame & 0xFFFF):
            raise SystemExit(
                f"control sequence {sequence} != frame {frame} at offset {pos}")
        blocks.append(block)
        pos += total
    return blocks


def body_control_bytes(body: bytes, routing: bytes, nframes: int, fps: int) -> bytes:
    pos = 0
    sec_acc = 0
    lead = 0
    chunks = []
    for frame in range(1, nframes):
        n_pay = routing[frame * 2]
        n_ctrl = routing[frame * 2 + 1]
        sec_acc += 75
        rate_delta, sec_acc = divmod(sec_acc, fps)
        actual = n_pay + n_ctrl
        due = max(0, rate_delta - lead)
        fsec = max(actual, due)
        lead += fsec - rate_delta
        end = pos + fsec * SECTOR
        if end > len(body):
            raise SystemExit(f"BODY.DAT ended inside frame {frame}")
        chunks.append(body[pos:pos + n_ctrl * SECTOR])
        pos = end
    if pos != len(body):
        raise SystemExit(f"BODY.DAT walk ended at {pos}, file has {len(body)} bytes")
    return b"".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("header", type=Path)
    parser.add_argument("body", type=Path)
    parser.add_argument("audio", type=Path, help="encoder mono pcm_u8 WAV")
    args = parser.parse_args()

    header = args.header.read_bytes()
    body = args.body.read_bytes()
    if len(header) < SECTOR or header[:4] != b"TTRC":
        raise SystemExit("not a TTRC HEADER.DAT")
    if len(header) % SECTOR or len(body) % SECTOR:
        raise SystemExit("HEADER.DAT and BODY.DAT must be sector aligned")

    nframes = struct.unpack_from(">H", header, 6)[0]
    cells = struct.unpack_from(">H", header, 12)[0]
    routing_sec = struct.unpack_from(">L", header, 26)[0]
    prebuf_sec = struct.unpack_from(">L", header, 30)[0]
    f0_ctrl_sec = struct.unpack_from(">L", header, 40)[0]
    f0_pat_sec = struct.unpack_from(">L", header, 44)[0]
    paltab_sec = struct.unpack_from(">L", header, 48)[0]
    audio_bytes = struct.unpack_from(">H", header, 54)[0]
    fps = struct.unpack_from(">H", header, 56)[0]
    skip_frames = struct.unpack_from(">H", header, 58)[0]
    audio_pre_sec = struct.unpack_from(">H", header, 60)[0]
    if not nframes or not cells or not audio_bytes or not fps:
        raise SystemExit("invalid zero header field")

    cursor = SECTOR + paltab_sec * SECTOR
    audio_prefix = header[cursor:cursor + audio_pre_sec * SECTOR]
    if len(audio_prefix) != audio_pre_sec * SECTOR:
        raise SystemExit("truncated STARTUP_AUDIO")
    preload_chunks = [
        audio_prefix[i * SECTOR:i * SECTOR + audio_bytes]
        for i in range(audio_pre_sec)
    ]
    cursor += audio_pre_sec * SECTOR

    f0_region = header[cursor:cursor + f0_ctrl_sec * SECTOR]
    cursor += (f0_ctrl_sec + f0_pat_sec) * SECTOR
    routing = header[cursor:cursor + routing_sec * SECTOR]
    cursor += (routing_sec + prebuf_sec) * SECTOR
    if cursor != len(header):
        raise SystemExit(f"HEADER.DAT walk ended at {cursor}, file has {len(header)} bytes")
    if len(routing) < nframes * 2:
        raise SystemExit("truncated routing table")

    if len(f0_region) < 2:
        raise SystemExit("missing frame-0 control block")
    f0_len = struct.unpack_from(">H", f0_region, 0)[0]
    control_stream = f0_region[:f0_len] + body_control_bytes(
        body, routing, nframes, fps)
    blocks = split_blocks(control_stream, nframes)
    live_chunks = [control_audio(block, cells, audio_bytes) for block in blocks]

    if skip_frames > len(live_chunks):
        raise SystemExit("audio skip count exceeds the number of control blocks")
    queued = b"".join(preload_chunks + live_chunks[skip_frames:])
    expected_len = nframes * audio_bytes
    expected = sign_magnitude_audio(args.audio, expected_len)
    if len(queued) < expected_len:
        raise SystemExit(
            f"PCM queue is short: {len(queued)} / {expected_len} samples")
    actual = queued[:expected_len]
    if actual != expected:
        mismatch = next(i for i, (a, b) in enumerate(zip(actual, expected)) if a != b)
        raise SystemExit(
            f"PCM order mismatch at sample {mismatch} "
            f"(actual=0x{actual[mismatch]:02X}, expected=0x{expected[mismatch]:02X})")
    if any(queued[expected_len:]):
        raise SystemExit("PCM queue tail after the source is not silent")

    print(
        f"audio prefetch proof: OK  frames={nframes} audio={audio_bytes}B "
        f"preload={audio_pre_sec} skip={skip_frames} verified={expected_len} samples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
