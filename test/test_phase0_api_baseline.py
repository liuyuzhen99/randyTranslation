import os
import unittest
from contextlib import ExitStack
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

        self.stack.enter_context(patch.object(api_service, "job_service", self.job_service))
        self.stack.enter_context(patch.object(api_service, "orchestrator", self.orchestrator))
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


if __name__ == "__main__":
    unittest.main()
