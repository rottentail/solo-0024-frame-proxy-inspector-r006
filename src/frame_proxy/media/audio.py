from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Iterator

from frame_proxy.domain.errors import ErrorCode, MediaValidationError


@dataclass(frozen=True, slots=True)
class AudioDescriptor:
    """Verified layout of one uncompressed PCM audio stream inside an AVI."""

    channels: int
    sample_rate: int
    bits_per_sample: int
    block_align: int
    total_samples: int
    byte_size: int
    chunk_offsets: tuple[int, ...]
    chunk_sizes: tuple[int, ...]


def frame_boundary_sample(
    frame_index: int,
    *,
    fps_numerator: int,
    fps_denominator: int,
    sample_rate: int,
) -> int:
    """Map a video frame boundary to an exact PCM sample index.

    The result is round-half-up of
    ``frame_index * fps_denominator * sample_rate / fps_numerator`` computed
    with integer arithmetic only. Every boundary is derived from its absolute
    frame index, so adjacent scenes share exactly one boundary and never
    accumulate drift, even for non-integer frame rates such as 30000/1001.
    """
    if frame_index < 0:
        raise ValueError("frame index must not be negative")
    if fps_numerator <= 0 or fps_denominator <= 0 or sample_rate <= 0:
        raise ValueError("frame rate and sample rate must be positive")
    numerator = frame_index * fps_denominator * sample_rate
    return (2 * numerator + fps_numerator) // (2 * fps_numerator)


def iter_pcm_payloads(buffer: bytes, audio: AudioDescriptor) -> Iterator[bytes]:
    """Yield each audio chunk payload in stream order."""
    for offset, size in zip(audio.chunk_offsets, audio.chunk_sizes):
        end = offset + size
        if offset < 0 or end > len(buffer):
            raise MediaValidationError(
                "audio chunk extends beyond the payload",
                code=ErrorCode.FRAME_DECODE,
            )
        yield buffer[offset:end]


def pcm_stream_digest(buffer: bytes, audio: AudioDescriptor) -> str:
    """Digest the concatenated PCM stream without materializing it."""
    hasher = hashlib.sha256()
    for payload in iter_pcm_payloads(buffer, audio):
        hasher.update(payload)
    return hasher.hexdigest()


def extract_pcm_range(
    buffer: bytes,
    audio: AudioDescriptor,
    start_sample: int,
    end_sample: int,
) -> bytes:
    """Copy the half-open sample range out of the chunked PCM stream.

    Audio chunks may span scene boundaries, so slicing happens at sample
    granularity after logically concatenating every chunk in order.
    """
    if not 0 <= start_sample <= end_sample <= audio.total_samples:
        raise MediaValidationError(
            "sample range is outside the audio stream",
            code=ErrorCode.MEDIA_BOUNDS,
            details={
                "start_sample": start_sample,
                "end_sample": end_sample,
                "total_samples": audio.total_samples,
            },
        )
    align = audio.block_align
    start_byte = start_sample * align
    end_byte = end_sample * align
    parts: list[bytes] = []
    cursor = 0
    for offset, size in zip(audio.chunk_offsets, audio.chunk_sizes):
        chunk_start = cursor
        chunk_end = cursor + size
        low = max(start_byte, chunk_start)
        high = min(end_byte, chunk_end)
        if low < high:
            parts.append(buffer[offset + (low - chunk_start) : offset + (high - chunk_start)])
        cursor = chunk_end
        if cursor >= end_byte:
            break
    return b"".join(parts)


def encode_wav(
    pcm: bytes,
    *,
    channels: int,
    sample_rate: int,
    bits_per_sample: int,
) -> bytes:
    """Wrap verbatim PCM bytes in a canonical RIFF/WAVE header."""
    if channels <= 0 or sample_rate <= 0 or bits_per_sample <= 0:
        raise ValueError("WAV parameters must be positive")
    block_align = channels * (bits_per_sample // 8)
    if block_align <= 0 or len(pcm) % block_align != 0:
        raise ValueError("PCM payload is not aligned to whole samples")
    fmt = struct.pack(
        "<HHIIHH",
        1,
        channels,
        sample_rate,
        sample_rate * block_align,
        block_align,
        bits_per_sample,
    )
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )
