import os
import tempfile
import threading
import unittest

from application.services.job_service import JobService
from application.services.pipeline_orchestrator import PipelineOrchestrator
from domain.entities import Job
from domain.enums import JobStatus
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository
from infrastructure.persistence.sqlite_repositories import SQLiteJobRepository
from infrastructure.pipeline.legacy_producer_adapter import MissingProducerBackend
from infrastructure.storage.local_media_storage import LocalFilesystemMediaStorage


class FakeProducerBackend:
    def __init__(self):
        self.temp_dir = ""

    def download_step(self, song_name: str, output_path: str):
        with open(output_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(song_name)
        return output_path

    def transcribe_step(self, video_ref, audio_path: str):
        with open(audio_path, "w", encoding="utf-8") as file_obj:
            file_obj.write("audio")
        return [{"start": 0.0, "end": 1.0, "text": "hello"}], ["hello"]

    def generate_bilingual_srt(self, segments, english_texts, output_file: str):
        with open(output_file, "w", encoding="utf-8") as file_obj:
            file_obj.write("1\n00:00:00,000 --> 00:00:01,000\nhello\n你好\n")
        return output_file

    def burn_video(self, video_ref, srt_file: str, final_path: str):
        with open(final_path, "w", encoding="utf-8") as file_obj:
            file_obj.write("final")


class RecordingProducerBackend(FakeProducerBackend):
    def __init__(self, created_backends: list["RecordingProducerBackend"]):
        super().__init__()
        self.created_backends = created_backends
        self.observed_temp_dirs: list[str] = []
        created_backends.append(self)

    def download_step(self, song_name: str, output_path: str):
        self.observed_temp_dirs.append(self.temp_dir)
        return super().download_step(song_name, output_path)


class FailingProducerBackend(FakeProducerBackend):
    def download_step(self, song_name: str, output_path: str):
        raise RuntimeError("download failed")


class CleanupFailingStorage(LocalFilesystemMediaStorage):
    def cleanup_task_workspace(self, task_id: str) -> None:
        super().cleanup_task_workspace(task_id)
        raise RuntimeError("cleanup failed")


class Phase1LayeredArchitectureTests(unittest.TestCase):
    def test_job_service_create_and_list(self):
        repo = InMemoryJobRepository()
        service = JobService(repo)

        job = service.create_job("test song")

        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertIn(job.job_id, service.list_jobs())

    def test_in_memory_repository_returns_copies(self):
        repo = InMemoryJobRepository()
        repo.create(Job(job_id="copy0001", song_name="song"))

        fetched_job = repo.get("copy0001")
        assert fetched_job is not None
        fetched_job.progress = "mutated locally"

        refetched_job = repo.get("copy0001")
        assert refetched_job is not None
        self.assertEqual(refetched_job.progress, "已加入队列")

    def test_sqlite_job_repository_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_root:
            repo = SQLiteJobRepository(os.path.join(temp_root, "jobs.db"))
            job = Job(job_id="sqlite01", song_name="song", status=JobStatus.PROCESSING, progress="running")

            repo.create(job)
            fetched_job = repo.get(job.job_id)
            listed_jobs = repo.list_all()

            self.assertIsNotNone(fetched_job)
            assert fetched_job is not None
            self.assertEqual(fetched_job.status, JobStatus.PROCESSING)
            self.assertEqual(listed_jobs[job.job_id].song_name, "song")

    def test_local_media_storage_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as output_root:
            storage = LocalFilesystemMediaStorage(temp_root=temp_root, output_root=output_root)

            workspace = storage.prepare_task_workspace("job12345")
            temp_file = storage.resolve_temp_file("job12345", "raw_video.mp4")
            final_output = storage.resolve_final_output("job12345")

            self.assertTrue(os.path.isdir(workspace))
            self.assertEqual(temp_file, os.path.join(workspace, "raw_video.mp4"))
            self.assertEqual(final_output, os.path.join(output_root, "MV_job12345.mp4"))

            storage.cleanup_task_workspace("job12345")
            self.assertFalse(os.path.exists(workspace))

    def test_missing_producer_backend_marks_job_failed(self):
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as output_root:
            repo = InMemoryJobRepository()
            job = Job(job_id="missing01", song_name="song")
            repo.create(job)
            storage = LocalFilesystemMediaStorage(temp_root=temp_root, output_root=output_root)
            orchestrator = PipelineOrchestrator(repo, storage, MissingProducerBackend)

            orchestrator.run(job.job_id, job.song_name)

            updated = repo.get(job.job_id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, JobStatus.FAILED)
            self.assertIn("producer backend is unavailable", updated.progress)
            self.assertFalse(os.path.exists(os.path.join(temp_root, job.job_id)))

    def test_pipeline_orchestrator_success(self):
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as output_root:
            repo = InMemoryJobRepository()
            job = Job(job_id="abc12345", song_name="song")
            repo.create(job)
            storage = LocalFilesystemMediaStorage(temp_root=temp_root, output_root=output_root)
            orchestrator = PipelineOrchestrator(repo, storage, FakeProducerBackend)

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
            orchestrator = PipelineOrchestrator(repo, storage, FailingProducerBackend)

            orchestrator.run(job.job_id, job.song_name)

            updated = repo.get(job.job_id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, JobStatus.FAILED)
            self.assertIn("错误", updated.progress)
            self.assertFalse(os.path.exists(os.path.join(temp_root, job.job_id)))

    def test_pipeline_orchestrator_cleanup_failure_does_not_stick_job(self):
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as output_root:
            repo = InMemoryJobRepository()
            job = Job(job_id="clean001", song_name="song")
            repo.create(job)
            storage = CleanupFailingStorage(temp_root=temp_root, output_root=output_root)
            orchestrator = PipelineOrchestrator(repo, storage, FakeProducerBackend)

            orchestrator.run(job.job_id, job.song_name)

            updated = repo.get(job.job_id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, JobStatus.COMPLETED)
            self.assertIn("清理临时文件失败", updated.progress)
            self.assertTrue(updated.result)
            self.assertTrue(os.path.exists(updated.result))

    def test_pipeline_orchestrator_uses_isolated_backend_instances_per_job(self):
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as output_root:
            repo = InMemoryJobRepository()
            storage = LocalFilesystemMediaStorage(temp_root=temp_root, output_root=output_root)
            created_backends: list[RecordingProducerBackend] = []

            def producer_factory() -> RecordingProducerBackend:
                return RecordingProducerBackend(created_backends)

            orchestrator = PipelineOrchestrator(repo, storage, producer_factory)

            jobs = [
                Job(job_id="job00001", song_name="song one"),
                Job(job_id="job00002", song_name="song two"),
            ]
            for job in jobs:
                repo.create(job)

            threads = [
                threading.Thread(target=orchestrator.run, args=(job.job_id, job.song_name))
                for job in jobs
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(len(created_backends), 2)
            observed_dirs = {backend.observed_temp_dirs[0] for backend in created_backends}
            self.assertEqual(
                observed_dirs,
                {os.path.join(temp_root, "job00001"), os.path.join(temp_root, "job00002")},
            )

            for job in jobs:
                updated = repo.get(job.job_id)
                self.assertIsNotNone(updated)
                assert updated is not None
                self.assertEqual(updated.status, JobStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
