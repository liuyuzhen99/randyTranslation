import os
import tempfile
import unittest

from application.services.job_service import JobService
from application.services.pipeline_orchestrator import PipelineOrchestrator
from domain.entities import Job
from domain.enums import JobStatus
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository
from infrastructure.storage.local_media_storage import LocalFilesystemMediaStorage


class FakeProducerBackend:
    def __init__(self):
        self.temp_dir = ""

    def download_step(self, song_name: str, output_path: str):
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("video")
        return output_path

    def transcribe_step(self, video_ref, audio_path: str):
        with open(audio_path, "w", encoding="utf-8") as f:
            f.write("audio")
        return [{"start": 0.0, "end": 1.0, "text": "hello"}], ["hello"]

    def generate_bilingual_srt(self, segments, english_texts, output_file: str):
        with open(output_file, "w", encoding="utf-8") as f:
            f.write("1\n00:00:00,000 --> 00:00:01,000\nhello\n你好\n")
        return output_file

    def burn_video(self, video_ref, srt_file: str, final_path: str):
        with open(final_path, "w", encoding="utf-8") as f:
            f.write("final")


class FailingProducerBackend(FakeProducerBackend):
    def download_step(self, song_name: str, output_path: str):
        raise RuntimeError("download failed")


class Phase1LayeredArchitectureTests(unittest.TestCase):
    def test_job_service_create_and_list(self):
        repo = InMemoryJobRepository()
        service = JobService(repo)

        job = service.create_job("test song")

        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertIn(job.job_id, service.list_jobs())

    def test_pipeline_orchestrator_success(self):
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as output_root:
            repo = InMemoryJobRepository()
            job = Job(job_id="abc12345", song_name="song")
            repo.create(job)
            storage = LocalFilesystemMediaStorage(temp_root=temp_root, output_root=output_root)
            orchestrator = PipelineOrchestrator(repo, storage, FakeProducerBackend())

            orchestrator.run(job.job_id, job.song_name)

            updated = repo.get(job.job_id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, JobStatus.COMPLETED)
            self.assertTrue(updated.result)
            self.assertTrue(os.path.exists(updated.result))
            self.assertFalse(os.path.exists(os.path.join(temp_root, job.job_id)))

    def test_pipeline_orchestrator_failure(self):
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as output_root:
            repo = InMemoryJobRepository()
            job = Job(job_id="def67890", song_name="song")
            repo.create(job)
            storage = LocalFilesystemMediaStorage(temp_root=temp_root, output_root=output_root)
            orchestrator = PipelineOrchestrator(repo, storage, FailingProducerBackend())

            orchestrator.run(job.job_id, job.song_name)

            updated = repo.get(job.job_id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, JobStatus.FAILED)
            self.assertIn("错误", updated.progress)


if __name__ == "__main__":
    unittest.main()
