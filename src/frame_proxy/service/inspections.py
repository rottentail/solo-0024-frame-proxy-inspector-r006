from __future__ import annotations

import hashlib
from dataclasses import dataclass

from frame_proxy.config import Settings
from frame_proxy.domain.errors import (
    ApplicationError,
    ErrorCode,
    MediaValidationError,
    NotFoundError,
    StateError,
    ValidationError,
)
from frame_proxy.domain.models import (
    ClipRecord,
    InspectionAudioRecord,
    InspectionRecord,
)
from frame_proxy.domain.policies import (
    InspectionRequest,
    validate_clip_id,
    validate_identifier,
)
from frame_proxy.domain.transitions import transition_inspection
from frame_proxy.media.audio import (
    encode_wav,
    extract_pcm_range,
    frame_boundary_sample,
    pcm_stream_digest,
)
from frame_proxy.media.avi import AviDescriptor, probe_avi
from frame_proxy.media.detector import analyze_frames
from frame_proxy.media.fingerprint import fingerprint_frame
from frame_proxy.media.frames import decode_frame_at
from frame_proxy.storage.repository import FileRepository


@dataclass(frozen=True, slots=True)
class SceneAudio:
    scene_index: int
    sample_start: int
    sample_end: int
    payload: bytes
    digest: str
    clip_digest: str


class InspectionService:
    def __init__(self, repository: FileRepository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings

    def create(
        self,
        clip_id: str,
        request: InspectionRequest,
    ) -> InspectionRecord:
        normalized_clip_id = validate_clip_id(clip_id)
        clip = self._repository.get_clip(normalized_clip_id)
        if clip.state != "registered":
            raise MediaValidationError(
                "clip is not ready for inspection",
                code=ErrorCode.INVALID_STATE,
            )

        record = InspectionRecord(
            inspection_id=request.inspection_id,
            clip_id=clip.clip_id,
            state="pending",
            sensitivity=request.sensitivity,
            sample_stride=request.sample_stride,
            min_scene_frames=request.min_scene_frames,
        )
        self._repository.create_inspection(record)

        transition_inspection(record, "scanning", reason="frame scan started")
        self._repository.update_inspection(record)
        try:
            media = self._repository.get_clip_media(clip.clip_id)
            if hashlib.sha256(media).hexdigest() != clip.digest:
                raise MediaValidationError(
                    "stored clip digest does not match its record",
                    code=ErrorCode.FRAME_DECODE,
                )
            descriptor = probe_avi(media)
            sampled_frames = tuple(
                range(0, descriptor.frame_count, request.sample_stride)
            )
            fingerprints = tuple(
                fingerprint_frame(decode_frame_at(media, descriptor, frame_index))
                for frame_index in sampled_frames
            )
            result = analyze_frames(
                descriptor.frame_count,
                sampled_frames,
                fingerprints,
                sensitivity=request.sensitivity,
                min_scene_frames=request.min_scene_frames,
            )
            record.sampled_frames = list(result.sampled_frames)
            record.cut_frames = list(result.cut_frames)
            record.scenes = list(result.scenes)
            if descriptor.audio is not None:
                self._bind_audio(record, clip, descriptor, media)
            transition_inspection(record, "indexed", reason="scene ranges verified")
            self._repository.update_inspection(record)
            return record
        except ApplicationError as exc:
            self._mark_failed(record, exc.code.value, exc.message)
            raise
        except Exception as exc:
            self._mark_failed(record, ErrorCode.INTERNAL.value, str(exc))
            raise ApplicationError(
                ErrorCode.INTERNAL,
                "shot indexing failed",
                status=500,
            ) from exc

    def _bind_audio(
        self,
        record: InspectionRecord,
        clip: ClipRecord,
        descriptor: AviDescriptor,
        media: bytes,
    ) -> None:
        audio = descriptor.audio
        if audio is None:
            return
        clip_audio = clip.audio
        if clip_audio is None:
            raise ApplicationError(
                ErrorCode.INTERNAL,
                "clip record is missing its audio track",
                status=500,
            )
        pcm_digest = pcm_stream_digest(media, audio)
        if pcm_digest != clip_audio.digest:
            raise MediaValidationError(
                "stored audio does not match the clip record",
                code=ErrorCode.FRAME_DECODE,
            )
        record.audio = InspectionAudioRecord(
            codec="pcm",
            channels=audio.channels,
            sample_rate=audio.sample_rate,
            bits_per_sample=audio.bits_per_sample,
            total_samples=audio.total_samples,
            pcm_digest=pcm_digest,
            clip_digest=clip.digest,
        )
        for scene in record.scenes:
            scene.sample_start = frame_boundary_sample(
                scene.start_frame,
                fps_numerator=descriptor.fps_numerator,
                fps_denominator=descriptor.fps_denominator,
                sample_rate=audio.sample_rate,
            )
            scene.sample_end = frame_boundary_sample(
                scene.end_frame,
                fps_numerator=descriptor.fps_numerator,
                fps_denominator=descriptor.fps_denominator,
                sample_rate=audio.sample_rate,
            )

    def _mark_failed(
        self,
        record: InspectionRecord,
        code: str,
        message: str,
    ) -> None:
        record.error_code = code
        record.error_message = message
        if record.state in {"pending", "scanning"}:
            transition_inspection(record, "failed", reason=code)
        try:
            self._repository.update_inspection(record)
        except Exception:
            pass

    def get(self, inspection_id: str) -> InspectionRecord:
        normalized = validate_identifier(inspection_id, "inspection_id")
        return self._repository.get_inspection(normalized)

    def get_scene_audio(self, inspection_id: str, scene_index: int) -> SceneAudio:
        inspection = self.get(inspection_id)
        if inspection.state != "indexed":
            raise StateError(
                "scene audio requires an indexed inspection",
                details={"state": inspection.state},
            )
        audio = inspection.audio
        if audio is None:
            raise NotFoundError(
                ErrorCode.AUDIO_NOT_FOUND,
                "inspection has no verified audio track",
                details={"inspection_id": inspection.inspection_id},
            )
        if scene_index < 0 or scene_index >= len(inspection.scenes):
            raise ValidationError(
                "scene index is outside the inspection",
                details={
                    "scene_index": scene_index,
                    "scene_count": len(inspection.scenes),
                },
            )
        scene = inspection.scenes[scene_index]
        clip = self._repository.get_clip(inspection.clip_id)
        media = self._repository.get_clip_media(clip.clip_id)
        media_digest = hashlib.sha256(media).hexdigest()
        if media_digest != clip.digest or media_digest != audio.clip_digest:
            raise MediaValidationError(
                "stored clip digest does not match its records",
                code=ErrorCode.FRAME_DECODE,
            )
        descriptor = probe_avi(media)
        if descriptor.audio is None:
            raise ApplicationError(
                ErrorCode.INTERNAL,
                "stored clip no longer presents its audio track",
                status=500,
            )
        sample_start = frame_boundary_sample(
            scene.start_frame,
            fps_numerator=descriptor.fps_numerator,
            fps_denominator=descriptor.fps_denominator,
            sample_rate=descriptor.audio.sample_rate,
        )
        sample_end = frame_boundary_sample(
            scene.end_frame,
            fps_numerator=descriptor.fps_numerator,
            fps_denominator=descriptor.fps_denominator,
            sample_rate=descriptor.audio.sample_rate,
        )
        if scene.sample_start is not None and (
            scene.sample_start != sample_start or scene.sample_end != sample_end
        ):
            raise ApplicationError(
                ErrorCode.INTERNAL,
                "stored scene sample range disagrees with the clip timing",
                status=500,
            )
        pcm = extract_pcm_range(media, descriptor.audio, sample_start, sample_end)
        payload = encode_wav(
            pcm,
            channels=descriptor.audio.channels,
            sample_rate=descriptor.audio.sample_rate,
            bits_per_sample=descriptor.audio.bits_per_sample,
        )
        return SceneAudio(
            scene_index=scene_index,
            sample_start=sample_start,
            sample_end=sample_end,
            payload=payload,
            digest=hashlib.sha256(payload).hexdigest(),
            clip_digest=clip.digest,
        )
