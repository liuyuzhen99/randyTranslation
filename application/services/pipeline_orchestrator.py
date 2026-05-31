from __future__ import annotations

import logging
from datetime import timedelta

from domain.entities import ArtifactRecord, Job
from domain.enums import JobStatus, StageType
from domain.job_lifecycle import transition_job
from domain.repositories import ArtifactRepository, JobRepository
from domain.storage import MediaStorageService
from domain.time_utils import utc_now
from infrastructure.pipeline.legacy_producer_adapter import ProducerBackendFactory

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """Application service that owns pipeline state transitions."""

    def __init__(
        self,
        job_repository: JobRepository,
        media_storage: MediaStorageService,
        producer_backend_factory: ProducerBackendFactory,
        shadow_write_service=None,
        artifact_repository: ArtifactRepository | None = None,
        final_artifact_retention_days: int = 0,
    ) -> None:
        self.job_repository = job_repository
        self.media_storage = media_storage
        self.producer_backend_factory = producer_backend_factory
        self.shadow_write_service = shadow_write_service
        self.artifact_repository = artifact_repository
        self.final_artifact_retention_days = final_artifact_retention_days

    def run(self, task_id: str, song_name: str, candidate_id: str | None = None) -> None:
        job = self.job_repository.get(task_id)
        if not job:
            job = Job(job_id=task_id, song_name=song_name)
            self.job_repository.create(job)

        job = self._update_job(
            job,
            JobStatus.PROCESSING,
            "📥 正在搜索并下载视频...",
            stage=StageType.DOWNLOAD,
        )
        workspace = self.media_storage.prepare_task_workspace(task_id)
        producer_backend = self.producer_backend_factory()
        # A fresh backend instance per job avoids cross-task temp path races under concurrency.
        producer_backend.temp_dir = workspace

        try:
            raw_video_path = self.media_storage.resolve_temp_file(task_id, "raw_video.mp4")
            video_ref = producer_backend.download_step(song_name, output_path=raw_video_path)

            job = self._update_job(
                job,
                JobStatus.PROCESSING,
                "🎙️ 正在分离人声并识别歌词 (耗时较长)...",
                stage=StageType.TRANSCRIBE,
            )
            temp_audio_path = self.media_storage.resolve_temp_file(task_id, "temp_audio.wav")
            segments, english_texts = producer_backend.transcribe_step(video_ref, temp_audio_path)

            job = self._update_job(
                job,
                JobStatus.PROCESSING,
                "🤖 正在调用 Qwen 进行微调模型翻译...",
                stage=StageType.TRANSLATE,
            )
            srt_path = self.media_storage.resolve_temp_file(task_id, "bilingual.srt")
            subtitle_file = producer_backend.generate_bilingual_srt(
                segments, english_texts, output_file=srt_path
            )

            job = self._update_job(
                job,
                JobStatus.PROCESSING,
                "🎬 正在合成双语字幕视频...",
                stage=StageType.RENDER,
            )
            final_output = self.media_storage.resolve_final_output(task_id)
            producer_backend.burn_video(video_ref, subtitle_file, final_path=final_output)
            final_video_artifact = self.media_storage.upload_artifact(
                task_id=task_id,
                local_path=final_output,
                artifact_type="final_video",
                content_type="video/mp4",
            )
            subtitle_artifact = self.media_storage.upload_artifact(
                task_id=task_id,
                local_path=subtitle_file,
                artifact_type="subtitle_srt",
                content_type="application/x-subrip",
            )
            self._record_artifact(job, final_video_artifact, candidate_id=candidate_id)
            self._record_artifact(job, subtitle_artifact, candidate_id=candidate_id)

            job = self._update_job(
                job,
                JobStatus.COMPLETED,
                "✨ 制作完成！",
                final_video_artifact.object_uri,
                stage=StageType.RENDER,
            )
        except Exception as exc:
            job = self._update_job(job, JobStatus.FAILED, f"❌ 错误: {exc}")
        finally:
            try:
                self.media_storage.cleanup_task_workspace(task_id)
            except Exception as exc:
                logger.exception("Failed to cleanup task workspace for %s", task_id)
                if job.status != JobStatus.FAILED:
                    job = self._update_job(job, job.status, f"{job.progress} (清理临时文件失败: {exc})")

    def _update_job(
        self,
        job: Job,
        status: JobStatus,
        progress: str,
        result: str | None = None,
        stage: StageType | None = None,
    ) -> Job:
        updated_job = transition_job(
            job,
            status,
            progress=progress,
            stage=stage if stage is not None else job.current_stage,
            result=result,
        )
        self.job_repository.update(updated_job)
        if self.shadow_write_service is not None:
            try:
                self.shadow_write_service.record_job_update(job, updated_job)
            except Exception as exc:
                logger.error(
                    "event=shadow_write_failure op=job_updated job_id=%s error=%s",
                    job.job_id,
                    exc,
                    exc_info=True,
                )
        return updated_job

    def _record_artifact(self, job: Job, stored_object, candidate_id: str | None = None) -> None:
        if self.artifact_repository is None:
            return
        now = utc_now()
        expires_at = (
            now + timedelta(days=self.final_artifact_retention_days)
            if self.final_artifact_retention_days > 0
            else None
        )
        owner_type = "candidate" if candidate_id else "job"
        owner_id = candidate_id or job.job_id
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
                job_id=job.job_id,
                candidate_id=candidate_id,
                size_bytes=stored_object.size_bytes,
                checksum_sha256=stored_object.checksum_sha256,
                version=1,
                metadata={"song_name": job.song_name},
                created_at=stored_object.created_at,
                updated_at=now,
                expires_at=expires_at,
            )
        )
