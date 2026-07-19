#!/usr/bin/env python3
"""Checkpointed continuous IMA ADPCM for the SEGA-CD player.

The encoder state is continuous across the movie, but every fixed-size chunk
starts with a four-byte checkpoint so it can be decoded independently:

    s16 predictor (big endian), u8 step index, u8 reserved zero

Two samples are packed per byte, low nibble first.  The checkpoint does not
reset the codec; it records the exact continuous state at the chunk boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import struct

import numpy as np


STEP_TABLE = np.array([
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
    598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707,
    1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871,
    5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635,
    13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
], dtype=np.int32)

INDEX_TABLE = np.array(
    [-1, -1, -1, -1, 2, 4, 6, 8,
     -1, -1, -1, -1, 2, 4, 6, 8],
    dtype=np.int32,
)

CHECKPOINT = struct.Struct(">hBB")
CHECKPOINT_BYTES = CHECKPOINT.size
FULL_INDEX_BYTES = 89 * 16 * 2
FULL_DELTA_BYTES = 89 * 16 * 4
OUTPUT_LUT_BYTES = 256
FULL_TABLE_BYTES = FULL_INDEX_BYTES + FULL_DELTA_BYTES + OUTPUT_LUT_BYTES


@dataclass(frozen=True)
class State:
    predictor: int = 0
    index: int = 0

    def checked(self) -> "State":
        if not -32768 <= int(self.predictor) <= 32767:
            raise ValueError(f"IMA predictor is outside s16: {self.predictor}")
        if not 0 <= int(self.index) <= 88:
            raise ValueError(f"IMA index is outside 0..88: {self.index}")
        return State(int(self.predictor), int(self.index))


def encoded_bytes(sample_count: int) -> int:
    """On-disc bytes for one independently decodable chunk."""
    count = int(sample_count)
    if count <= 0 or count & 1:
        raise ValueError(f"IMA chunk sample count must be positive and even: {count}")
    return CHECKPOINT_BYTES + count // 2


def _delta(step: int, code: int) -> int:
    value = step >> 3
    if code & 4:
        value += step
    if code & 2:
        value += step >> 1
    if code & 1:
        value += step >> 2
    return -value if code & 8 else value


def encode_samples(pcm16, state: State = State()) -> tuple[bytes, State]:
    """Encode s16 samples from ``state`` and return bytes plus final state."""
    samples = np.asarray(pcm16, dtype=np.int16).astype(np.int32, copy=False)
    current = state.checked()
    predictor = current.predictor
    index = current.index
    out = bytearray((len(samples) + 1) // 2)
    for n, sample in enumerate(samples):
        step = int(STEP_TABLE[index])
        diff = int(sample) - predictor
        code = 0
        if diff < 0:
            code = 8
            diff = -diff
        magnitude = step >> 3
        if diff >= step:
            code |= 4
            diff -= step
            magnitude += step
        if diff >= (step >> 1):
            code |= 2
            diff -= step >> 1
            magnitude += step >> 1
        if diff >= (step >> 2):
            code |= 1
            magnitude += step >> 2
        predictor += -magnitude if code & 8 else magnitude
        predictor = max(-32768, min(32767, predictor))
        index += int(INDEX_TABLE[code])
        index = max(0, min(88, index))
        if n & 1:
            out[n >> 1] |= code << 4
        else:
            out[n >> 1] = code
    return bytes(out), State(predictor, index)


def decode_samples(
        data: bytes, sample_count: int | None = None,
        state: State = State()) -> tuple[np.ndarray, State]:
    """Decode packed nibbles from ``state`` and return s16 plus final state."""
    count = len(data) * 2 if sample_count is None else int(sample_count)
    if count < 0 or count > len(data) * 2:
        raise ValueError(
            f"sample count {count} exceeds {len(data) * 2} packed samples")
    current = state.checked()
    predictor = current.predictor
    index = current.index
    out = np.empty(count, dtype=np.int16)
    for n in range(count):
        byte = data[n >> 1]
        code = (byte & 0x0F) if not (n & 1) else (byte >> 4)
        predictor += _delta(int(STEP_TABLE[index]), code)
        predictor = max(-32768, min(32767, predictor))
        index += int(INDEX_TABLE[code])
        index = max(0, min(88, index))
        out[n] = predictor
    return out, State(predictor, index)


def encode_chunk(pcm16, state: State = State()) -> tuple[bytes, State]:
    """Encode one even-sized chunk with its continuous-state checkpoint."""
    samples = np.asarray(pcm16, dtype=np.int16)
    if len(samples) <= 0 or len(samples) & 1:
        raise ValueError(
            f"IMA chunk sample count must be positive and even: {len(samples)}")
    initial = state.checked()
    packed, final = encode_samples(samples, initial)
    checkpoint = CHECKPOINT.pack(initial.predictor, initial.index, 0)
    chunk = checkpoint + packed
    if len(chunk) != encoded_bytes(len(samples)):
        raise AssertionError("IMA chunk size disagrees with encoded_bytes()")
    return chunk, final


def decode_chunk(chunk: bytes, sample_count: int) -> tuple[np.ndarray, State]:
    """Decode one checkpointed chunk independently."""
    expected = encoded_bytes(sample_count)
    if len(chunk) != expected:
        raise ValueError(f"IMA chunk has {len(chunk)} bytes, expected {expected}")
    predictor, index, reserved = CHECKPOINT.unpack_from(chunk)
    if reserved != 0:
        raise ValueError(f"IMA checkpoint reserved byte is nonzero: {reserved}")
    return decode_samples(
        chunk[CHECKPOINT_BYTES:], sample_count, State(predictor, index))


def retime_pcm_s16(pcm16, target_len: int) -> np.ndarray:
    """Stretch mono s16 PCM evenly to an exact fixed-chunk sample count."""
    samples = np.asarray(pcm16, dtype=np.int16)
    count = int(target_len)
    if count <= 0:
        return np.empty(0, dtype=np.int16)
    if not len(samples):
        return np.zeros(count, dtype=np.int16)
    if len(samples) == count:
        return samples.copy()
    src_x = np.arange(len(samples), dtype=np.float64)
    dst_x = np.linspace(0.0, float(len(samples) - 1), count)
    return np.rint(np.interp(
        dst_x, src_x, samples.astype(np.float64))).clip(
            -32768, 32767).astype(np.int16)


def encode_decode_chunks(
        pcm16, samples_per_chunk: int) -> tuple[list[bytes], list[bytes]]:
    """Encode continuous IMA chunks and return their exact RF5C164 output.

    The returned pair is ``(checkpointed_control_chunks, signmag_pcm_chunks)``.
    This is the shared reference path for both disc packing and sim playback
    audio, so the analysis movie cannot accidentally audition the clean source.
    """
    samples = np.asarray(pcm16, dtype=np.int16)
    chunk_samples = int(samples_per_chunk)
    if chunk_samples <= 0 or chunk_samples & 1:
        raise ValueError(
            f"IMA chunk sample count must be positive and even: {chunk_samples}")
    if len(samples) % chunk_samples:
        raise ValueError(
            f"PCM sample count {len(samples)} is not a multiple of "
            f"chunk size {chunk_samples}")

    controls: list[bytes] = []
    reconstructed: list[bytes] = []
    state = State()
    for frame, start in enumerate(range(0, len(samples), chunk_samples)):
        chunk, state = encode_chunk(samples[start:start + chunk_samples], state)
        decoded, decoded_state = decode_chunk(chunk, chunk_samples)
        if decoded_state != state:
            raise AssertionError(f"IMA state mismatch after chunk {frame}")
        controls.append(chunk)
        reconstructed.append(pcm16_to_sign_magnitude(decoded))
    return controls, reconstructed


def pcm16_to_sign_magnitude(pcm16) -> bytes:
    """Convert reconstructed s16 PCM to RF5C164 sign-magnitude u8 samples."""
    samples = np.asarray(pcm16, dtype=np.int16).astype(np.int32, copy=False)
    high = samples >> 8
    out = np.empty(len(high), dtype=np.uint8)
    positive = high >= 0
    out[positive] = np.minimum(high[positive], 0x7F).astype(np.uint8)
    negative = np.minimum(-high[~positive], 0x7E)
    out[~positive] = (0x80 | negative).astype(np.uint8)
    return out.tobytes()


def sign_magnitude_to_pcm16(data: bytes) -> np.ndarray:
    """Convert RF5C164 sign-magnitude bytes to linear s16 for WAV playback."""
    encoded = np.frombuffer(data, dtype=np.uint8).astype(np.int16)
    magnitude = encoded & 0x7F
    signed = np.where(encoded & 0x80, -magnitude, magnitude)
    return (signed << 8).astype(np.int16)


def output_lut() -> bytes:
    """Return the 256-byte offset-high to RF5C164 conversion table."""
    signed_high = np.arange(256, dtype=np.int16) - 128
    return pcm16_to_sign_magnitude(signed_high << 8)


def full_tables() -> bytes:
    """Return the Sub decoder's 8,800-byte full lookup table image.

    Layout is ``new_index_x32[u16][89][16]``, then
    ``signed_delta[s32][89][16]``, then the 256-byte output LUT.  Values are
    big endian so the 68000 can use them directly from Word RAM.
    """
    indices = bytearray()
    deltas = bytearray()
    for index in range(89):
        step = int(STEP_TABLE[index])
        for code in range(16):
            new_index = max(0, min(88, index + int(INDEX_TABLE[code])))
            indices += struct.pack(">H", new_index * 32)
            deltas += struct.pack(">l", _delta(step, code))
    blob = bytes(indices + deltas) + output_lut()
    if len(blob) != FULL_TABLE_BYTES:
        raise AssertionError(
            f"full IMA table is {len(blob)} bytes, expected {FULL_TABLE_BYTES}")
    return blob


def _selftest() -> None:
    count = 22_050
    t = np.arange(count)
    signal = (
        0.6 * np.sin(2 * np.pi * 440 * t / count)
        + 0.3 * np.sin(2 * np.pi * (300 + 2000 * t / count) * t / count)
    )
    pcm = np.clip(signal * 32767, -32768, 32767).astype(np.int16)
    if len(pcm) & 1:
        pcm = pcm[:-1]
    chunk, _state = encode_chunk(pcm)
    decoded, _state = decode_chunk(chunk, len(pcm))
    error = pcm.astype(np.int32) - decoded.astype(np.int32)
    signal_rms = np.sqrt(np.mean(pcm.astype(np.float64) ** 2))
    error_rms = np.sqrt(np.mean(error.astype(np.float64) ** 2))
    snr = 20 * np.log10(signal_rms / max(1e-9, error_rms))
    print(
        f"samples={len(pcm)} bytes={len(chunk)} SNR={snr:.1f}dB "
        f"table={len(full_tables())}B")
    if snr <= 20:
        raise SystemExit("IMA self-test SNR is too low")


if __name__ == "__main__":
    _selftest()
