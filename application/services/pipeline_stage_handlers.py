from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from application.services.phase4_workflow_service import Phase4WorkflowServices, ReviewConflictError
from domain.entities import ArtifactRecord
from domain.enums import CandidateStatus, ReviewStatus, ReviewType, StageType
from domain.message_contracts import PipelineStageMessage
from domain.repositories import ArtifactRepository
from domain.storage import MediaStorageService
from domain.time_utils import utc_now
from infrastructure.pipeline.legacy_producer_adapter import ProducerBackendFactory
from utils.logger_manager import log_manager


stage_logger = log_manager.get_task_logger("PIPELINE")


class PipelineStageHandlers:
    def __init__(
        self,
        *,
        media_storage: MediaStorageService,
        producer_backend_factory: ProducerBackendFactory,
        workflow_services: Phase4WorkflowServices | None = None,
        artifact_repository: ArtifactRepository | None = None,
        vector_repository=None,
        final_artifact_retention_days: int = 0,
    ) -> None:
        self.media_storage = media_storage
        self.producer_backend_factory = producer_backend_factory
        self.workflow_services = workflow_services
        self.artifact_repository = artifact_repository
        self.vector_repository = vector_repository
        self.final_artifact_retention_days = final_artifact_retention_days

    def as_mapping(self):
        return {
            StageType.DOWNLOAD: self.download,
            StageType.TRANSCRIBE: self.transcribe,
            StageType.AUDIT: self.audit,
            StageType.MANUAL_REVIEW: self.manual_review_gate,
            StageType.TRANSLATE: self.translate,
            StageType.TRANSLATION_REVIEW: self.translation_review_gate,
            StageType.RENDER: self.render,
        }

    def download(self, message: PipelineStageMessage) -> dict:
        stage_logger.info("开始下载候选视频: %s", message.song_name)
        workspace = self.media_storage.prepare_task_workspace(message.job_id)
        backend = self.producer_backend_factory()
        backend.temp_dir = workspace
        raw_video_path = self.media_storage.resolve_temp_file(message.job_id, "raw_video.mp4")
        video_ref = backend.download_step(message.song_name, output_path=raw_video_path)
        self._set_candidate_status(message, CandidateStatus.PENDING_REVIEW)
        stage_logger.info("下载完成，视频已保存: %s", video_ref)
        return {"video_ref": str(video_ref), "raw_video_path": raw_video_path}

    def transcribe(self, message: PipelineStageMessage) -> dict:
        stage_logger.info("开始转写候选视频音频。")
        video_ref = message.payload.get("video_ref") or self.media_storage.resolve_temp_file(message.job_id, "raw_video.mp4")
        backend = self.producer_backend_factory()
        backend.temp_dir = self.media_storage.prepare_task_workspace(message.job_id)
        audio_path = self.media_storage.resolve_temp_file(message.job_id, "temp_audio.wav")
        segments, english_texts = backend.transcribe_step(video_ref, audio_path)
        stage_logger.info("转写完成，共生成 %s 个歌词片段。", len(segments))
        normalized_segments = [
            {
                "start_time": segment.get("start", segment.get("start_time", 0.0)),
                "end_time": segment.get("end", segment.get("end_time", 0.0)),
                "text": segment.get("text", ""),
            }
            for segment in segments
        ]
        candidate_id = message.review.candidate_id
        if candidate_id and self.workflow_services is not None:
            try:
                self.workflow_services.automation_service.submit_transcript(
                    candidate_id=candidate_id,
                    actor_id="phase6-transcriber",
                    segments=normalized_segments,
                    auto_approve_review=True,
                    comment="Phase 6 async transcription completed.",
                )
            except ReviewConflictError:
                pass
        return {
            "video_ref": video_ref,
            "audio_path": audio_path,
            "segments": segments,
            "english_texts": english_texts,
        }

    def audit(self, message: PipelineStageMessage) -> dict:
        stage_logger.info("开始 AI 品味审计。")
        backend = self.producer_backend_factory()
        english_texts = [
            str(text)
            for text in (message.payload.get("english_texts") or [])
            if str(text).strip()
        ]
        audit_result = {}
        if hasattr(backend, "audit_step"):
            references = message.payload.get("audit_references") or self._search_audit_references(message, english_texts)
            audit_result = backend.audit_step(
                english_texts,
                title=message.song_name,
                references=references,
            )
        score = audit_result.get("score")
        decision = str(audit_result.get("decision", "Reject")).strip().lower()
        approve = decision == "pass" or decision == "approved" or (
            decision not in {"reject", "rejected", "fail", "failed"} and float(score or 0) >= 60
        )
        candidate_id = message.review.candidate_id
        if candidate_id and self.workflow_services is not None:
            try:
                self.workflow_services.automation_service.record_taste_audit(
                    candidate_id=candidate_id,
                    actor_id="phase6-auditor",
                    approve=approve,
                    comment=audit_result.get("reason") or "Phase 6 async taste audit completed.",
                    score=float(score or 0),
                    key_lyrics=audit_result.get("key_lyrics") or english_texts[:3],
                )
            except ReviewConflictError:
                pass
        if not approve:
            stage_logger.warning("AI 品味审计未通过: score=%s decision=%s", score, audit_result.get("decision"))
            return {
                "pause": True,
                "pause_reason": "taste_audit_rejected",
                "audit": audit_result,
            }
        stage_logger.info("AI 品味审计通过: score=%s decision=%s", score, audit_result.get("decision"))
        return {"audit": audit_result}

    def manual_review_gate(self, message: PipelineStageMessage) -> dict:
        return self._review_gate(message, ReviewType.MANUAL_REVIEW, "manual_review_pending")

    def translate(self, message: PipelineStageMessage) -> dict:
        stage_logger.info("开始 AI 歌词翻译并生成双语 SRT。")
        backend = self.producer_backend_factory()
        backend.temp_dir = self.media_storage.prepare_task_workspace(message.job_id)
        segments = message.payload.get("segments") or []
        english_texts = message.payload.get("english_texts") or [
            segment.get("text", "") for segment in segments
        ]
        references = message.payload.get("translation_references") or self._search_translation_references(
            message,
            [str(text) for text in english_texts if str(text).strip()],
        )
        srt_path = self.media_storage.resolve_temp_file(message.job_id, "bilingual.srt")
        subtitle_file = backend.generate_bilingual_srt(
            segments,
            english_texts,
            output_file=srt_path,
            translation_references=references,
        )
        stage_logger.info("翻译完成，双语字幕已生成: %s", subtitle_file)
        candidate_id = message.review.candidate_id
        if candidate_id and self.workflow_services is not None:
            translations = self._read_translation_lines(subtitle_file)
            try:
                self.workflow_services.automation_service.submit_translation(
                    candidate_id=candidate_id,
                    actor_id="phase6-translator",
                    translations=translations,
                    auto_approve_review=False,
                    comment="Phase 6 async translation completed.",
                )
            except ReviewConflictError:
                pass
        return {"subtitle_file": subtitle_file, "translation_references": references}

    def translation_review_gate(self, message: PipelineStageMessage) -> dict:
        return self._review_gate(message, ReviewType.TRANSLATION_REVIEW, "translation_review_pending")

    def render(self, message: PipelineStageMessage) -> dict:
        stage_logger.info("开始压制双语字幕到视频。")
        backend = self.producer_backend_factory()
        backend.temp_dir = self.media_storage.prepare_task_workspace(message.job_id)
        video_ref = message.payload.get("video_ref") or self.media_storage.resolve_temp_file(message.job_id, "raw_video.mp4")
        subtitle_file = message.payload.get("subtitle_file") or self.media_storage.resolve_temp_file(message.job_id, "bilingual.srt")
        final_output = self.media_storage.resolve_final_output(message.job_id)
        backend.burn_video(video_ref, subtitle_file, final_path=final_output)
        stage_logger.info("视频压制完成，开始上传产物: %s", final_output)
        final_video_artifact = self.media_storage.upload_artifact(
            task_id=message.job_id,
            local_path=final_output,
            artifact_type="final_video",
            content_type="video/mp4",
        )
        subtitle_artifact = self.media_storage.upload_artifact(
            task_id=message.job_id,
            local_path=subtitle_file,
            artifact_type="subtitle_srt",
            content_type="application/x-subrip",
        )
        self._record_artifact(message, final_video_artifact)
        self._record_artifact(message, subtitle_artifact)
        stage_logger.info("最终视频和字幕产物已登记。")
        return {"artifact_uri": final_video_artifact.object_uri}

    def _review_gate(
        self,
        message: PipelineStageMessage,
        review_type: ReviewType,
        pause_reason: str,
    ) -> dict:
        candidate_id = message.review.candidate_id
        if not candidate_id or self.workflow_services is None:
            return {}
        reviews = self.workflow_services.audit_service.support.get_reviews_for_candidate(candidate_id)
        review = next((item for item in reviews if item.review_type == review_type), None)
        if review is None or review.status == ReviewStatus.PENDING:
            return {"pause": True, "pause_reason": pause_reason}
        if review.status == ReviewStatus.REJECTED:
            raise RuntimeError(f"{review_type.value} rejected for candidate {candidate_id}")
        return {}

    def _record_artifact(self, message: PipelineStageMessage, stored_object) -> None:
        if self.artifact_repository is None:
            return
        now = utc_now()
        expires_at = (
            now + timedelta(days=self.final_artifact_retention_days)
            if self.final_artifact_retention_days > 0
            else None
        )
        candidate_id = message.review.candidate_id
        owner_type = "candidate" if candidate_id else "job"
        owner_id = candidate_id or message.job_id
        self.artifact_repository.upsert(
            ArtifactRecord(
                artifact_id=f"{owner_type}:{owner_id}:{stored_object.artifact_type}:v1",
                owner_type=owner_type,
                owner_id=owner_id,
                artifact_type=stored_object.artifact_type,
                object_uri=stored_object.object_uri,
                object_key=stored_object.object_key,
                bucket=stored_object.bucket,
                storage_provider=stored_object.storage_provider,
                content_type=stored_object.content_type,
                job_id=message.job_id,
                candidate_id=candidate_id,
                size_bytes=stored_object.size_bytes,
                checksum_sha256=stored_object.checksum_sha256,
                version=1,
                metadata={"song_name": message.song_name},
                created_at=stored_object.created_at,
                updated_at=now,
                expires_at=expires_at,
            )
        )

    def _set_candidate_status(self, message: PipelineStageMessage, status: CandidateStatus) -> None:
        candidate_id = message.review.candidate_id
        if not candidate_id or self.workflow_services is None:
            return
        candidate_repository = self.workflow_services.pipeline_service.support.candidate_repository
        candidate = candidate_repository.get(candidate_id)
        if candidate is None:
            return
        candidate.status = status
        candidate.last_seen_at = utc_now()
        candidate_repository.upsert(candidate)

    def _search_audit_references(self, message: PipelineStageMessage, english_texts: list[str]) -> list[dict]:
        repository = getattr(self, "vector_repository", None)
        if repository is None:
            stage_logger.info("未配置 RAG 向量库，AI 审计将不带相似品味参考。")
            return []
        try:
            from application.services.phase8_vectors import AUDIT_STYLE_MEMORY

            query = "\n".join(english_texts[:20]) or message.song_name
            records = repository.search(AUDIT_STYLE_MEMORY, query, limit=3)
        except Exception as exc:
            stage_logger.warning("RAG 相似品味检索失败，继续无参考审计: %s", exc)
            return []
        references = [
            {
                "title": record.metadata.get("title") or record.metadata.get("artist") or record.vector_id,
                "lyrics": record.text,
                "score": record.score,
            }
            for record in records
        ]
        stage_logger.info("RAG 相似品味检索完成，返回 %s 条参考。", len(references))
        return references

    def _search_translation_references(self, message: PipelineStageMessage, english_texts: list[str]) -> list[dict]:
        repository = getattr(self, "vector_repository", None)
        if repository is None:
            stage_logger.info("未配置 RAG 向量库，AI 翻译将不带相似翻译参考。")
            return []
        try:
            from application.services.phase8_vectors import TRANSLATION_MEMORY

            query = "\n".join(english_texts[:20]) or message.song_name
            records = repository.search(TRANSLATION_MEMORY, query, limit=3)
        except Exception as exc:
            stage_logger.warning("RAG 相似翻译检索失败，继续无参考翻译: %s", exc)
            return []
        references = [
            {
                "title": record.metadata.get("title") or record.metadata.get("artist") or record.vector_id,
                "lyrics": record.text,
                "translation": record.metadata.get("translation")
                or record.metadata.get("zh_text")
                or record.metadata.get("chinese")
                or "",
                "score": record.score,
            }
            for record in records
        ]
        stage_logger.info("RAG 相似翻译检索完成，返回 %s 条参考。", len(references))
        return references

    @staticmethod
    def _read_translation_lines(srt_path: str) -> list[dict]:
        lines = Path(srt_path).read_text(encoding="utf-8").splitlines()
        translations = []
        current_index: int | None = None
        block_text: list[str] = []
        for line in [*lines, ""]:
            if not line.strip():
                if current_index is not None and len(block_text) >= 2:
                    translations.append(
                        {
                            "line_index": current_index - 1,
                            "zh_text": block_text[-1],
                        }
                    )
                current_index = None
                block_text = []
                continue
            if current_index is None:
                try:
                    current_index = int(line.strip())
                except ValueError:
                    current_index = 0
                continue
            if "-->" in line:
                continue
            block_text.append(line)
        return translations
