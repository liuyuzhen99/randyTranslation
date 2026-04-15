from __future__ import annotations

import logging

from domain.entities import Job
from domain.enums import JobStatus
from domain.repositories import JobRepository
from domain.storage import MediaStorageService
from infrastructure.pipeline.legacy_producer_adapter import ProducerBackendFactory

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """Application service that owns pipeline state transitions."""

    def __init__(
        self,
        job_repository: JobRepository,
        media_storage: MediaStorageService,
        producer_backend_factory: ProducerBackendFactory,
    ) -> None:
        self.job_repository = job_repository
        self.media_storage = media_storage
        self.producer_backend_factory = producer_backend_factory

    def run(self, task_id: str, song_name: str) -> None:
        job = self.job_repository.get(task_id)
        if not job:
            job = Job(job_id=task_id, song_name=song_name)
            self.job_repository.create(job)

        self._update_job(job, JobStatus.PROCESSING, "📥 正在搜索并下载视频...")
        workspace = self.media_storage.prepare_task_workspace(task_id)
        producer_backend = self.producer_backend_factory()
        # A fresh backend instance per job avoids cross-task temp path races under concurrency.
        producer_backend.temp_dir = workspace

        try:
            raw_video_path = self.media_storage.resolve_temp_file(task_id, "raw_video.mp4")
            video_ref = producer_backend.download_step(song_name, output_path=raw_video_path)

            self._update_job(job, JobStatus.PROCESSING, "🎙️ 正在分离人声并识别歌词 (耗时较长)...")
            temp_audio_path = self.media_storage.resolve_temp_file(task_id, "temp_audio.wav")
            segments, english_texts = producer_backend.transcribe_step(video_ref, temp_audio_path)

            self._update_job(job, JobStatus.PROCESSING, "🤖 正在调用 Qwen 进行微调模型翻译...")
            srt_path = self.media_storage.resolve_temp_file(task_id, "bilingual.srt")
            subtitle_file = producer_backend.generate_bilingual_srt(
                segments, english_texts, output_file=srt_path
            )

            self._update_job(job, JobStatus.PROCESSING, "🎬 正在合成双语字幕视频...")
            final_output = self.media_storage.resolve_final_output(task_id)
            producer_backend.burn_video(video_ref, subtitle_file, final_path=final_output)

            self._update_job(job, JobStatus.COMPLETED, "✨ 制作完成！", final_output)
        except Exception as exc:
            self._update_job(job, JobStatus.FAILED, f"❌ 错误: {exc}")
        finally:
            try:
                self.media_storage.cleanup_task_workspace(task_id)
            except Exception as exc:
                logger.exception("Failed to cleanup task workspace for %s", task_id)
                if job.status != JobStatus.FAILED:
                    self._update_job(job, job.status, f"{job.progress} (清理临时文件失败: {exc})")

    def _update_job(
        self,
        job: Job,
        status: JobStatus,
        progress: str,
        result: str | None = None,
    ) -> None:
        job.status = status
        job.progress = progress
        if result is not None:
            job.result = result
        self.job_repository.update(job)
