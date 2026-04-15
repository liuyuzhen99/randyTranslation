import os
import unittest
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import api.service as api_service
from application.services.job_service import JobService
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository


class NoopOrchestrator:
    def run(self, task_id: str, song_name: str) -> None:
        return None


class Phase0ApiBaselineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_BASE_URL": "https://example.local",
        }, clear=False))

        self.repo = InMemoryJobRepository()
        self.job_service = JobService(self.repo)
        self.orchestrator = NoopOrchestrator()

        api_service.app.state.job_service = self.job_service
        api_service.app.state.orchestrator = self.orchestrator
        api_service.job_service = self.job_service
        api_service.orchestrator = self.orchestrator
        self.client = TestClient(api_service.app)
        self.stack.enter_context(self.client)

    def tearDown(self):
        self.stack.close()

    def test_create_task_contract(self):
        response = self.client.post("/create_task", json={"song_name": "N95"})
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertIn("task_id", data)
        self.assertIsInstance(data["task_id"], str)
        self.assertEqual(len(data["task_id"]), 8)
        self.assertEqual(data["message"], "任务已启动，请稍后通过 ID 查询进度")

    def test_check_status_contract(self):
        create_res = self.client.post("/create_task", json={"song_name": "DNA"})
        task_id = create_res.json()["task_id"]

        response = self.client.get(f"/check_status/{task_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(set(data.keys()), {"status", "progress", "result", "song_name"})
        self.assertEqual(data["song_name"], "DNA")

    def test_check_status_not_found(self):
        response = self.client.get("/check_status/unknown-id")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "任务不存在"})

    def test_list_tasks_contract(self):
        first = self.client.post("/create_task", json={"song_name": "HUMBLE"}).json()["task_id"]
        second = self.client.post("/create_task", json={"song_name": "Money Trees"}).json()["task_id"]

        response = self.client.get("/list_tasks")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertIn(first, data)
        self.assertIn(second, data)
        self.assertEqual(data[first]["song_name"], "HUMBLE")
        self.assertEqual(data[second]["song_name"], "Money Trees")

    def test_sqlite_backend_persists_jobs_across_app_rebuild(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlite",
                "JOB_REPOSITORY_SQLITE_PATH": os.path.join(temp_root, "jobs.db"),
                "LOG_FILE_PATH": os.path.join(temp_root, "app.log"),
            }

            with patch.dict(os.environ, env, clear=False):
                first_app = api_service.create_app()
                with TestClient(first_app) as first_client:
                    task_id = first_client.post("/create_task", json={"song_name": "Count Me Out"}).json()["task_id"]

                second_app = api_service.create_app()
                with TestClient(second_app) as second_client:
                    response = second_client.get(f"/check_status/{task_id}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["song_name"], "Count Me Out")


if __name__ == "__main__":
    unittest.main()
