from __future__ import annotations

import hashlib
import math
import struct
from fractions import Fraction

from frame_proxy.media.avi_writer import AudioSpec, build_sample_avi
from tools.harness import RunningService, require, require_status, success


def _extract_pcm(payload: bytes) -> bytes:
    """Independently collect the 01wb payloads from an AVI container."""
    require(payload[:4] == b"RIFF" and payload[8:12] == b"AVI ", "not an AVI")
    pcm = bytearray()
    cursor = 12
    while cursor + 8 <= len(payload):
        tag = payload[cursor : cursor + 4]
        size = struct.unpack_from("<I", payload, cursor + 4)[0]
        body = cursor + 8
        if tag == b"LIST" and payload[body : body + 4] == b"movi":
            inner = body + 4
            end = body + size
            while inner + 8 <= end:
                inner_tag = payload[inner : inner + 4]
                inner_size = struct.unpack_from("<I", payload, inner + 4)[0]
                if inner_tag == b"01wb":
                    pcm += payload[inner + 8 : inner + 8 + inner_size]
                inner += 8 + inner_size + (inner_size & 1)
        cursor = body + size + (size & 1)
    return bytes(pcm)


def _parse_wav(data: bytes) -> tuple[tuple[int, ...], bytes]:
    require(data[:4] == b"RIFF", "WAV RIFF tag is missing")
    require(data[8:12] == b"WAVE", "WAVE tag is missing")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    require(riff_size + 8 == len(data), "WAV size does not match its byte count")
    fmt: tuple[int, ...] | None = None
    pcm: bytes | None = None
    cursor = 12
    while cursor + 8 <= len(data):
        tag = data[cursor : cursor + 4]
        size = struct.unpack_from("<I", data, cursor + 4)[0]
        body = data[cursor + 8 : cursor + 8 + size]
        if tag == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", body, 0)
        elif tag == b"data":
            pcm = body
        cursor += 8 + size + (size & 1)
    require(fmt is not None and pcm is not None, "WAV chunks are incomplete")
    return fmt, pcm


def _boundary(frame: int, fps_num: int, fps_den: int, rate: int) -> int:
    """Exact round-half-up of the frame timestamp in samples."""
    return math.floor(Fraction(frame * fps_den * rate, fps_num) + Fraction(1, 2))


def _check_scene_audio_cycle(
    service: RunningService,
    *,
    clip_id: str,
    inspection_id: str,
    payload: bytes,
    fps_num: int,
    fps_den: int,
    sample_rate: int,
    channels: int,
    bits: int,
) -> None:
    align = channels * (bits // 8)
    pcm = _extract_pcm(payload)
    total_samples = len(pcm) // align
    pcm_digest = hashlib.sha256(pcm).hexdigest()
    clip_digest = hashlib.sha256(payload).hexdigest()

    response = service.upload_clip(clip_id, payload)
    require_status(response, 201, "audio clip intake")
    clip = response.json()["clip"]
    audio_info = clip["audio"]
    require(audio_info["codec"] == "pcm", "clip audio codec changed")
    require(audio_info["sample_rate"] == sample_rate, "clip sample rate changed")
    require(audio_info["channels"] == channels, "clip channel count changed")
    require(audio_info["bits_per_sample"] == bits, "clip bit depth changed")
    require(
        audio_info["total_samples"] == total_samples,
        "clip sample count disagrees with the source",
    )
    require(
        audio_info["digest"] == pcm_digest,
        "clip audio digest does not match the source PCM",
    )
    require(
        clip["fps"] == {"numerator": fps_num, "denominator": fps_den},
        "clip frame rate ratio changed",
    )

    stored_clip = service.request("GET", f"/v1/clips/{clip_id}")
    require_status(stored_clip, 200, "audio clip lookup")
    require(
        stored_clip.json()["clip"]["audio"]["digest"] == pcm_digest,
        "stored clip audio digest changed",
    )

    created = service.request_json(
        "POST",
        f"/v1/clips/{clip_id}/inspections",
        {
            "inspection_id": inspection_id,
            "sensitivity": 0.55,
            "sample_stride": 1,
            "min_scene_frames": 2,
        },
    )
    require_status(created, 201, "audio inspection")
    inspection = created.json()["inspection"]
    require(inspection["state"] == "indexed", "audio inspection did not index")
    bound = inspection["audio"]
    require(bound["pcm_digest"] == pcm_digest, "inspection PCM digest mismatch")
    require(bound["clip_digest"] == clip_digest, "inspection clip digest mismatch")
    require(bound["total_samples"] == total_samples, "inspection samples changed")

    scenes = inspection["scenes"]
    cursor = 0
    for scene in scenes:
        require(
            scene["sample_start"] == _boundary(
                scene["start_frame"], fps_num, fps_den, sample_rate
            ),
            "scene sample start disagrees with exact frame timing",
        )
        require(
            scene["sample_end"]
            == _boundary(scene["end_frame"], fps_num, fps_den, sample_rate),
            "scene sample end disagrees with exact frame timing",
        )
        require(scene["sample_start"] == cursor, "scene sample ranges overlap or gap")
        cursor = scene["sample_end"]
    require(
        cursor == total_samples,
        "stitched scenes do not cover the inspected video time range",
    )

    stored = service.request("GET", f"/v1/inspections/{inspection_id}")
    require_status(stored, 200, "audio inspection lookup")
    require(
        stored.json()["inspection"]["audio"]["pcm_digest"] == pcm_digest,
        "stored inspection audio changed",
    )

    stitched = bytearray()
    for scene in scenes:
        result = service.request(
            "GET",
            f"/v1/inspections/{inspection_id}/scenes/{scene['index']}/audio",
        )
        require_status(result, 200, "scene audio fetch")
        require(result.content_type == "audio/x-wav", "scene audio type changed")
        require(
            result.headers.get("etag")
            == f'"{hashlib.sha256(result.body).hexdigest()}"',
            "scene audio ETag does not match its payload",
        )
        require(
            result.headers.get("x-frame-proxy-clip-digest") == clip_digest,
            "scene audio is not traceable to the inspected clip",
        )
        fmt, wav_pcm = _parse_wav(result.body)
        require(
            fmt
            == (
                1,
                channels,
                sample_rate,
                sample_rate * align,
                align,
                bits,
            ),
            "scene WAV format disagrees with the source track",
        )
        start = scene["sample_start"] * align
        end = scene["sample_end"] * align
        require(
            wav_pcm == pcm[start:end],
            "scene audio does not match the inspected source samples",
        )
        stitched += wav_pcm
    require(
        bytes(stitched) == pcm[: total_samples * align],
        "stitched scene audio dropped, duplicated, or reordered samples",
    )


def main() -> int:
    with RunningService() as service:
        _check_scene_audio_cycle(
            service,
            clip_id="clip-scene-audio-a",
            inspection_id="inspection-scene-audio-a",
            payload=build_sample_avi(
                audio=AudioSpec(sample_rate=48000, chunk_samples=8192)
            ),
            fps_num=12,
            fps_den=1,
            sample_rate=48000,
            channels=1,
            bits=16,
        )

        _check_scene_audio_cycle(
            service,
            clip_id="clip-scene-audio-b",
            inspection_id="inspection-scene-audio-b",
            payload=build_sample_avi(
                fps=30000,
                fps_denominator=1001,
                audio=AudioSpec(sample_rate=48000, chunk_samples=8192),
            ),
            fps_num=30000,
            fps_den=1001,
            sample_rate=48000,
            channels=1,
            bits=16,
        )

        plain = service.upload_clip("clip-scene-audio-c")
        require_status(plain, 201, "video-only clip intake")
        require(
            plain.json()["clip"]["audio"] is None,
            "video-only clip gained an audio track",
        )
        plain_inspection = service.request_json(
            "POST",
            "/v1/clips/clip-scene-audio-c/inspections",
            {
                "inspection_id": "inspection-scene-audio-c",
                "sensitivity": 0.55,
                "sample_stride": 1,
                "min_scene_frames": 2,
            },
        )
        require_status(plain_inspection, 201, "video-only inspection")
        plain_record = plain_inspection.json()["inspection"]
        require(
            plain_record["audio"] is None,
            "video-only inspection gained an audio track",
        )
        require(
            "sample_start" not in plain_record["scenes"][0],
            "video-only scenes gained sample ranges",
        )
        missing = service.request(
            "GET",
            "/v1/inspections/inspection-scene-audio-c/scenes/0/audio",
        )
        require_status(missing, 404, "video-only scene audio")
        require(
            missing.json()["error"]["code"] == "audio_not_found",
            "video-only scene audio returned the wrong error code",
        )

        short = service.upload_clip(
            "clip-scene-audio-d",
            build_sample_avi(audio=AudioSpec(total_samples=1000)),
        )
        require_status(short, 422, "incomplete audio rejection")
        require(
            service.request("GET", "/v1/clips/clip-scene-audio-d").status == 404,
            "incomplete audio left a visible clip",
        )

        overstated = service.upload_clip(
            "clip-scene-audio-e",
            build_sample_avi(audio=AudioSpec(declared_samples=999999)),
        )
        require_status(overstated, 422, "overstated audio rejection")
        require(
            service.request("GET", "/v1/clips/clip-scene-audio-e").status == 404,
            "overstated audio left a visible clip",
        )

        out_of_range = service.request(
            "GET",
            "/v1/inspections/inspection-scene-audio-a/scenes/9/audio",
        )
        require_status(out_of_range, 400, "out-of-range scene audio")
        malformed = service.request(
            "GET",
            "/v1/inspections/inspection-scene-audio-a/scenes/abc/audio",
        )
        require_status(malformed, 400, "malformed scene audio index")

    success(
        "read-scene-audio",
        {
            "frame_rates": ["12/1", "30000/1001"],
            "scenes": 4,
            "incomplete_status": short.status,
            "video_only_status": missing.status,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
