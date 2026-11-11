from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Iterator

from frame_proxy.domain.errors import ErrorCode, MediaValidationError
from frame_proxy.media.audio import AudioDescriptor, frame_boundary_sample


@dataclass(frozen=True, slots=True)
class AviDescriptor:
    width: int
    height: int
    frame_count: int
    fps_numerator: int
    fps_denominator: int
    row_stride: int
    frame_payload_size: int
    top_down: bool
    frame_offsets: tuple[int, ...]
    handler: str
    audio: AudioDescriptor | None = None


@dataclass(frozen=True, slots=True)
class _Chunk:
    tag: bytes
    payload_offset: int
    payload_size: int
    end_offset: int


def _fail(message: str, code: ErrorCode = ErrorCode.MEDIA_FORMAT) -> None:
    raise MediaValidationError(message, code=code)


def _require_bounds(buffer: bytes, offset: int, size: int) -> None:
    if offset < 0 or size < 0 or offset + size > len(buffer):
        _fail("AVI chunk extends beyond the payload")


def _iter_chunks(buffer: bytes, start: int, end: int) -> Iterator[_Chunk]:
    cursor = start
    while cursor < end:
        if cursor + 8 > end:
            _fail("AVI chunk header is truncated")
        tag = buffer[cursor : cursor + 4]
        size = struct.unpack_from("<I", buffer, cursor + 4)[0]
        payload_offset = cursor + 8
        payload_end = payload_offset + size
        if payload_end > end:
            _fail("AVI chunk payload is truncated")
        padded_end = payload_end + (size & 1)
        if padded_end > end:
            _fail("AVI chunk padding is truncated")
        yield _Chunk(tag, payload_offset, size, padded_end)
        cursor = padded_end


def _find_chunk(
    buffer: bytes,
    start: int,
    end: int,
    wanted: bytes,
) -> _Chunk | None:
    for chunk in _iter_chunks(buffer, start, end):
        if chunk.tag == wanted:
            return chunk
    return None


def _list_payload(buffer: bytes, chunk: _Chunk, list_type: bytes) -> tuple[int, int]:
    if chunk.payload_size < 4:
        _fail("AVI list is missing its type")
    actual = buffer[chunk.payload_offset : chunk.payload_offset + 4]
    if actual != list_type:
        _fail(f"expected AVI list {list_type!r}, found {actual!r}")
    return chunk.payload_offset + 4, chunk.payload_offset + chunk.payload_size


def _parse_avih(buffer: bytes, chunk: _Chunk) -> tuple[int, int, int, int]:
    if chunk.payload_size < 56:
        _fail("AVI main header is too short")
    fields = struct.unpack_from("<14I", buffer, chunk.payload_offset)
    total_frames = fields[4]
    streams = fields[6]
    width = fields[8]
    height = fields[9]
    if streams not in (1, 2):
        _fail("one video stream with an optional audio stream is supported")
    if total_frames <= 0:
        _fail("AVI frame count must be positive")
    return total_frames, streams, width, height


def _parse_strh(buffer: bytes, chunk: _Chunk) -> tuple[int, int, int, int]:
    if chunk.payload_size < 56:
        _fail("AVI stream header is too short")
    fields = struct.unpack_from("<4s4sIHHIIIIIIIIhhhh", buffer, chunk.payload_offset)
    stream_type = fields[0]
    scale = fields[6]
    rate = fields[7]
    length = fields[9]
    if stream_type != b"vids":
        _fail("the first stream must be video")
    if scale <= 0 or rate <= 0:
        _fail("AVI frame rate ratio must be positive")
    if length <= 0:
        _fail("AVI stream length must be positive")
    return scale, rate, length, fields[2]


def _parse_strf(
    buffer: bytes,
    chunk: _Chunk,
) -> tuple[int, int, int, int, int, int]:
    if chunk.payload_size < 40:
        _fail("AVI bitmap header is too short")
    fields = struct.unpack_from("<IiiHHIIiiII", buffer, chunk.payload_offset)
    header_size = fields[0]
    width = fields[1]
    signed_height = fields[2]
    planes = fields[3]
    bit_count = fields[4]
    compression = fields[5]
    image_size = fields[6]
    if header_size < 40:
        _fail("unsupported bitmap header")
    if width <= 0 or signed_height == 0:
        _fail("AVI image dimensions must be non-zero")
    if planes != 1:
        _fail("AVI image plane count must be one")
    if bit_count != 24:
        _fail("only 24-bit BGR frames are supported")
    if compression != 0:
        _fail("compressed AVI frames are not supported")
    return width, abs(signed_height), bit_count, compression, image_size, signed_height


def _parse_audio_strh(buffer: bytes, chunk: _Chunk) -> tuple[int, int, int]:
    if chunk.payload_size < 56:
        _fail("AVI audio stream header is too short")
    fields = struct.unpack_from("<4s4sIHHIIIIIIIIhhhh", buffer, chunk.payload_offset)
    stream_type = fields[0]
    scale = fields[6]
    rate = fields[7]
    length = fields[9]
    if stream_type != b"auds":
        _fail("the second stream must be audio")
    if scale <= 0 or rate <= 0:
        _fail("AVI audio stream rate must be positive")
    if length <= 0:
        _fail("AVI audio stream length must be positive")
    return scale, rate, length


def _parse_audio_strf(buffer: bytes, chunk: _Chunk) -> tuple[int, int, int, int, int]:
    if chunk.payload_size < 16:
        _fail("AVI audio format header is too short")
    fields = struct.unpack_from("<HHIIHH", buffer, chunk.payload_offset)
    format_tag = fields[0]
    channels = fields[1]
    sample_rate = fields[2]
    avg_bytes_per_second = fields[3]
    block_align = fields[4]
    bits_per_sample = fields[5]
    if format_tag != 1:
        _fail("only uncompressed PCM audio is supported")
    if channels <= 0:
        _fail("AVI audio channel count must be positive")
    if sample_rate <= 0:
        _fail("AVI audio sample rate must be positive")
    if bits_per_sample not in (8, 16, 24, 32):
        _fail("unsupported PCM bit depth")
    expected_align = channels * (bits_per_sample // 8)
    if block_align != expected_align:
        _fail("AVI audio block align does not match PCM geometry")
    if avg_bytes_per_second != sample_rate * block_align:
        _fail("AVI audio byte rate does not match PCM geometry")
    return channels, sample_rate, bits_per_sample, block_align, avg_bytes_per_second


def _simplify_ratio(numerator: int, denominator: int) -> tuple[int, int]:
    divisor = math.gcd(numerator, denominator)
    return numerator // divisor, denominator // divisor


def probe_avi(buffer: bytes) -> AviDescriptor:
    if len(buffer) < 12:
        _fail("payload is too short to be an AVI container")
    if buffer[0:4] != b"RIFF" or buffer[8:12] != b"AVI ":
        _fail("payload must be a RIFF/AVI container")
    riff_size = struct.unpack_from("<I", buffer, 4)[0]
    if riff_size + 8 != len(buffer):
        _fail(
            "RIFF size does not match the uploaded byte count",
            code=ErrorCode.MEDIA_BOUNDS,
        )

    hdrl = _find_chunk(buffer, 12, len(buffer), b"LIST")
    if hdrl is None:
        _fail("AVI header list is missing")
    hdrl_start, hdrl_end = _list_payload(buffer, hdrl, b"hdrl")

    avih = _find_chunk(buffer, hdrl_start, hdrl_end, b"avih")
    if avih is None:
        _fail("AVI main header chunk is missing")
    total_frames, stream_count, avih_width, avih_height = _parse_avih(buffer, avih)

    stream_lists: list[_Chunk] = []
    for chunk in _iter_chunks(buffer, hdrl_start, hdrl_end):
        if chunk.tag == b"LIST" and chunk.payload_size >= 4:
            if buffer[chunk.payload_offset : chunk.payload_offset + 4] == b"strl":
                stream_lists.append(chunk)
    if len(stream_lists) != stream_count:
        _fail("AVI stream lists do not match the main header")

    stream_start, stream_end = _list_payload(buffer, stream_lists[0], b"strl")
    strh = _find_chunk(buffer, stream_start, stream_end, b"strh")
    strf = _find_chunk(buffer, stream_start, stream_end, b"strf")
    if strh is None or strf is None:
        _fail("AVI stream headers are incomplete")
    scale, rate, stream_length, _flags = _parse_strh(buffer, strh)
    (
        width,
        height,
        _bit_count,
        _compression,
        image_size,
        signed_height,
    ) = _parse_strf(buffer, strf)

    audio_format: tuple[int, int, int, int] | None = None
    audio_length = 0
    if stream_count == 2:
        audio_start, audio_end = _list_payload(buffer, stream_lists[1], b"strl")
        audio_strh = _find_chunk(buffer, audio_start, audio_end, b"strh")
        audio_strf = _find_chunk(buffer, audio_start, audio_end, b"strf")
        if audio_strh is None or audio_strf is None:
            _fail("AVI audio stream headers are incomplete")
        audio_scale, audio_rate, audio_length = _parse_audio_strh(buffer, audio_strh)
        (
            audio_channels,
            audio_sample_rate,
            audio_bits,
            audio_block_align,
            audio_byte_rate,
        ) = _parse_audio_strf(buffer, audio_strf)
        if audio_scale != audio_block_align:
            _fail("AVI audio stream scale must be the PCM block align")
        if audio_rate != audio_byte_rate:
            _fail("AVI audio stream rate must be the PCM byte rate")
        audio_format = (
            audio_channels,
            audio_sample_rate,
            audio_bits,
            audio_block_align,
        )

    if width != avih_width or height != avih_height:
        _fail("AVI main and stream dimensions disagree")
    if total_frames != stream_length:
        _fail("AVI main and stream frame counts disagree")

    row_stride = ((width * 3 + 3) // 4) * 4
    frame_payload_size = row_stride * height
    if image_size not in (0, frame_payload_size):
        _fail("AVI bitmap image size does not match frame geometry")

    movi: _Chunk | None = None
    for chunk in _iter_chunks(buffer, 12, len(buffer)):
        if chunk.tag == b"LIST":
            actual = buffer[chunk.payload_offset : chunk.payload_offset + 4]
            if actual == b"movi":
                movi = chunk
                break
    if movi is None:
        _fail("AVI media list is missing")
    movi_start, movi_end = _list_payload(buffer, movi, b"movi")

    offsets: list[int] = []
    audio_offsets: list[int] = []
    audio_sizes: list[int] = []
    for chunk in _iter_chunks(buffer, movi_start, movi_end):
        if chunk.tag == b"JUNK":
            continue
        if chunk.tag == b"00db":
            if chunk.payload_size != frame_payload_size:
                _fail("AVI frame payload size does not match geometry")
            offsets.append(chunk.payload_offset)
            continue
        if chunk.tag == b"01wb" and audio_format is not None:
            if chunk.payload_size % audio_format[3] != 0:
                _fail("AVI audio chunk is not aligned to whole samples")
            audio_offsets.append(chunk.payload_offset)
            audio_sizes.append(chunk.payload_size)
            continue
        _fail(f"unsupported AVI media chunk {chunk.tag!r}")

    if len(offsets) != total_frames:
        _fail(
            "declared AVI frame count does not match media chunks",
            code=ErrorCode.MEDIA_BOUNDS,
        )

    fps_num, fps_den = _simplify_ratio(rate, scale)
    audio: AudioDescriptor | None = None
    if audio_format is not None:
        channels, sample_rate, bits_per_sample, block_align = audio_format
        audio_bytes = sum(audio_sizes)
        total_samples = audio_bytes // block_align
        if total_samples != audio_length:
            _fail(
                "declared AVI audio length does not match media chunks",
                code=ErrorCode.MEDIA_BOUNDS,
            )
        needed_samples = frame_boundary_sample(
            total_frames,
            fps_numerator=fps_num,
            fps_denominator=fps_den,
            sample_rate=sample_rate,
        )
        if total_samples < needed_samples:
            _fail(
                "AVI audio stream does not cover the video duration",
                code=ErrorCode.MEDIA_BOUNDS,
            )
        audio = AudioDescriptor(
            channels=channels,
            sample_rate=sample_rate,
            bits_per_sample=bits_per_sample,
            block_align=block_align,
            total_samples=total_samples,
            byte_size=audio_bytes,
            chunk_offsets=tuple(audio_offsets),
            chunk_sizes=tuple(audio_sizes),
        )

    return AviDescriptor(
        width=width,
        height=height,
        frame_count=total_frames,
        fps_numerator=fps_num,
        fps_denominator=fps_den,
        row_stride=row_stride,
        frame_payload_size=frame_payload_size,
        top_down=signed_height < 0,
        frame_offsets=tuple(offsets),
        handler="DIB",
        audio=audio,
    )

