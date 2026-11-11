from __future__ import annotations

import struct
from collections.abc import Iterable
from dataclasses import dataclass

from frame_proxy.media.audio import frame_boundary_sample


DEFAULT_SCENES = (5, 4, 5, 3)
SCENE_COLORS = (
    (214, 66, 48),
    (44, 151, 168),
    (222, 183, 53),
    (71, 166, 91),
)


@dataclass(frozen=True, slots=True)
class AudioSpec:
    """Requested PCM audio track for generated footage.

    ``total_samples`` defaults to exactly covering the video duration.
    ``declared_samples`` overrides the stream header length so checks can
    fabricate incomplete tracks; it defaults to ``total_samples``.
    """

    sample_rate: int = 48000
    channels: int = 1
    bits_per_sample: int = 16
    total_samples: int | None = None
    chunk_samples: int = 8192
    declared_samples: int | None = None


def _chunk(tag: bytes, payload: bytes) -> bytes:
    if len(tag) != 4:
        raise ValueError("chunk tag must be four bytes")
    padding = b"\x00" if len(payload) & 1 else b""
    return tag + struct.pack("<I", len(payload)) + payload + padding


def _list_chunk(kind: bytes, children: Iterable[bytes]) -> bytes:
    payload = kind + b"".join(children)
    return _chunk(b"LIST", payload)


def _frame_bytes(
    width: int,
    height: int,
    row_stride: int,
    color: tuple[int, int, int],
    phase: int,
) -> bytes:
    rows = bytearray()
    red, green, blue = color
    for y in range(height - 1, -1, -1):
        row = bytearray()
        for x in range(width):
            drift = ((x * 3 + y * 5 + phase * 11) % 19) - 9
            row.extend(
                (
                    max(0, min(255, blue + drift)),
                    max(0, min(255, green + drift // 2)),
                    max(0, min(255, red + drift)),
                )
            )
        row.extend(b"\x00" * (row_stride - len(row)))
        rows.extend(row)
    return bytes(rows)


def _pcm_bytes(spec: AudioSpec, total_samples: int) -> bytes:
    # Sample values derive from the absolute sample index so any dropped,
    # duplicated, or shifted sample is observable downstream.
    output = bytearray()
    if spec.bits_per_sample == 16:
        for index in range(total_samples):
            for channel in range(spec.channels):
                value = ((index * 31 + channel * 9973 + 7) % 65536) - 32768
                output += struct.pack("<h", value)
    elif spec.bits_per_sample == 8:
        for index in range(total_samples):
            for channel in range(spec.channels):
                output.append((index * 31 + channel * 37 + 7) % 256)
    else:
        raise ValueError("generated footage supports 8-bit or 16-bit PCM")
    return bytes(output)


def _audio_stream_header(spec: AudioSpec, declared_samples: int) -> bytes:
    block_align = spec.channels * (spec.bits_per_sample // 8)
    byte_rate = spec.sample_rate * block_align
    strh = struct.pack(
        "<4s4sIHHIIIIIIIIhhhh",
        b"auds",
        b"\x00\x00\x00\x00",
        0,
        0,
        0,
        0,
        block_align,
        byte_rate,
        0,
        declared_samples,
        block_align,
        0,
        block_align,
        0,
        0,
        0,
        0,
    )
    strf = struct.pack(
        "<HHIIHHH",
        1,
        spec.channels,
        spec.sample_rate,
        byte_rate,
        block_align,
        spec.bits_per_sample,
        0,
    )
    return _list_chunk(
        b"strl",
        (
            _chunk(b"strh", strh),
            _chunk(b"strf", strf),
        ),
    )


def build_sample_avi(
    *,
    width: int = 128,
    height: int = 72,
    fps: int = 12,
    scene_lengths: tuple[int, ...] = DEFAULT_SCENES,
    fps_denominator: int = 1,
    audio: AudioSpec | None = None,
) -> bytes:
    if width <= 0 or height <= 0 or fps <= 0 or fps_denominator <= 0:
        raise ValueError("sample geometry and frame rate must be positive")
    if not scene_lengths or any(length <= 0 for length in scene_lengths):
        raise ValueError("scene lengths must be positive")
    row_stride = ((width * 3 + 3) // 4) * 4
    frame_size = row_stride * height
    frame_count = sum(scene_lengths)

    pcm = b""
    declared_samples = 0
    if audio is not None:
        if audio.sample_rate <= 0 or audio.channels <= 0:
            raise ValueError("audio sample rate and channels must be positive")
        if audio.chunk_samples <= 0:
            raise ValueError("audio chunk size must be positive")
        total_samples = audio.total_samples
        if total_samples is None:
            total_samples = frame_boundary_sample(
                frame_count,
                fps_numerator=fps,
                fps_denominator=fps_denominator,
                sample_rate=audio.sample_rate,
            )
        if total_samples < 0:
            raise ValueError("audio sample count must not be negative")
        pcm = _pcm_bytes(audio, total_samples)
        declared_samples = (
            audio.declared_samples if audio.declared_samples is not None else total_samples
        )
        if declared_samples <= 0:
            raise ValueError("declared audio samples must be positive")

    avih = struct.pack(
        "<14I",
        1_000_000 * fps_denominator // fps,
        frame_size * fps // fps_denominator,
        0,
        0x10,
        frame_count,
        0,
        2 if audio is not None else 1,
        frame_size,
        width,
        height,
        0,
        0,
        0,
        0,
    )
    strh = struct.pack(
        "<4s4sIHHIIIIIIIIhhhh",
        b"vids",
        b"DIB ",
        0,
        0,
        0,
        0,
        fps_denominator,
        fps,
        0,
        frame_count,
        frame_size,
        0,
        frame_size,
        0,
        0,
        width,
        height,
    )
    strf = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height,
        1,
        24,
        0,
        frame_size,
        2835,
        2835,
        0,
        0,
    )
    stream_header = _list_chunk(
        b"strl",
        (
            _chunk(b"strh", strh),
            _chunk(b"strf", strf),
        ),
    )
    header_children = [_chunk(b"avih", avih), stream_header]
    if audio is not None:
        header_children.append(_audio_stream_header(audio, declared_samples))
    header_list = _list_chunk(b"hdrl", header_children)

    audio_chunks: list[bytes] = []
    if audio is not None:
        block_align = audio.channels * (audio.bits_per_sample // 8)
        chunk_bytes = audio.chunk_samples * block_align
        for start in range(0, len(pcm), chunk_bytes):
            audio_chunks.append(_chunk(b"01wb", pcm[start : start + chunk_bytes]))

    media_chunks: list[bytes] = []
    frame_number = 0
    audio_emitted = 0
    for scene_index, length in enumerate(scene_lengths):
        color = SCENE_COLORS[scene_index % len(SCENE_COLORS)]
        for _ in range(length):
            payload = _frame_bytes(
                width,
                height,
                row_stride,
                color,
                frame_number,
            )
            media_chunks.append(_chunk(b"00db", payload))
            frame_number += 1
            if audio_chunks:
                target = frame_number * len(audio_chunks) // frame_count
                while audio_emitted < target:
                    media_chunks.append(audio_chunks[audio_emitted])
                    audio_emitted += 1
    while audio_emitted < len(audio_chunks):
        media_chunks.append(audio_chunks[audio_emitted])
        audio_emitted += 1
    media_list = _list_chunk(b"movi", media_chunks)

    body = b"AVI " + header_list + media_list
    return b"RIFF" + struct.pack("<I", len(body)) + body
