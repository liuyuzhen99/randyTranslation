import os
import unittest
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import api.service as api_service
from application.services.job_service import JobService
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository


class ApiBaselineTests(unittest.TestCase):
    def _base_env(self, **overrides):
        env = {
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_BASE_URL": "https://example.local",
            "DATABASE_URL": "",
            "RABBITMQ_URL": "",
            "VECTOR_REPOSITORY_BACKEND": "sqlite",
            "SHADOW_WRITE_ENABLED": "false",
            "DUAL_WRITE_RECONCILE_ENABLED": "false",
            "OUTBOX_DISPATCH_ENABLED": "false",
            "ASYNC_PIPELINE_ENABLED": "false",
            "PIPELINE_SERVICE_WORKER_ENABLED": "false",
        }
        env.update(overrides)
        return env

    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.dict(os.environ, self._base_env(), clear=False))

        self.repo = InMemoryJobRepository()
        self.job_service = JobService(self.repo)

        api_service.app.state.job_service = self.job_service
        api_service.app.state.async_pipeline_command_service = None
        api_service.app.state.async_pipeline_services = None
        api_service.app.state.outbox_dispatcher = None
        api_service.app.state.reconcile_service = None
        api_service.job_service = self.job_service
        self.client = TestClient(api_service.app)
        self.stack.enter_context(self.client)

    def tearDown(self):
        self.stack.close()

    def test_create_task_contract(self):
        response = self.client.post("/create_task", json={"song_name": "N95"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {"detail": "Async pipeline is required; legacy in-process execution has been removed"},
        )

    def test_check_status_contract(self):
        task_id = self.job_service.create_job("DNA").job_id

        response = self.client.get(f"/check_status/{task_id}")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertEqual(set(data.keys()), {"status", "progress", "result", "song_name"})
        self.assertEqual(data["song_name"], "DNA")

    def test_check_status_not_found(self):
        response = self.client.get("/check_status/unknown-id")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"code": "not_found", "resource": "job", "id": "unknown-id"})

    def test_list_tasks_contract(self):
        first = self.job_service.create_job("HUMBLE").job_id
        second = self.job_service.create_job("Money Trees").job_id

        response = self.client.get("/list_tasks")
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertIn(first, data)
        self.assertIn(second, data)
        self.assertEqual(data[first]["song_name"], "HUMBLE")
        self.assertEqual(data[second]["song_name"], "Money Trees")

    def test_sqlite_backend_persists_jobs_across_app_rebuild(self):
        with TemporaryDirectory() as temp_root:
            env = self._base_env(**{
                "JOB_REPOSITORY_BACKEND": "sqlite",
                "JOB_REPOSITORY_SQLITE_PATH": os.path.join(temp_root, "jobs.db"),
                "LOG_FILE_PATH": os.path.join(temp_root, "app.log"),
            })

            with patch.dict(os.environ, env, clear=False):
                first_app = api_service.create_app()
                with TestClient(first_app) as first_client:
                    task_id = first_app.state.job_service.create_job("Count Me Out").job_id

                second_app = api_service.create_app()
                with TestClient(second_app) as second_client:
                    response = second_client.get(f"/check_status/{task_id}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["song_name"], "Count Me Out")

    def test_sqlalchemy_backend_persists_jobs_across_app_rebuild(self):
        with TemporaryDirectory() as temp_root:
            env = self._base_env(**{
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'core.db')}",
                "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "LOG_FILE_PATH": os.path.join(temp_root, "app.log"),
            })

            with patch.dict(os.environ, env, clear=False):
                first_app = api_service.create_app()
                with TestClient(first_app) as first_client:
                    task_id = first_app.state.job_service.create_job("Worldwide Steppers").job_id

                second_app = api_service.create_app()
                with TestClient(second_app) as second_client:
                    response = second_client.get(f"/check_status/{task_id}")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["song_name"], "Worldwide Steppers")

    def test_dual_write_reconcile_endpoint_returns_report_and_writes_file(self):
        with TemporaryDirectory() as temp_root:
            report_path = os.path.join(temp_root, "reports", "dual-write-reconcile.json")
            env = self._base_env(**{
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'core.db')}",
                "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "SHADOW_WRITE_ENABLED": "true",
                "DUAL_WRITE_RECONCILE_ENABLED": "true",
                "DUAL_WRITE_RECONCILE_REPORT_PATH": report_path,
                "DUAL_WRITE_RECONCILE_MAX_MISSING_JOBS": "0",
                "LOG_FILE_PATH": os.path.join(temp_root, "app.log"),
            })

            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app()
                with TestClient(app) as client:
                    app.state.job_service.create_job("Silent Hill")
                    response = client.get("/internal/dual-write/reconcile")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["report_path"], report_path)
            self.assertTrue(os.path.exists(report_path))
            self.assertIn("is_consistent", payload["report"])
            self.assertIn("is_within_threshold", payload["report"])

    def test_outbox_dispatch_endpoint_returns_503_without_real_publisher(self):
        with TemporaryDirectory() as temp_root:
            env = self._base_env(**{
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'core.db')}",
                "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "SHADOW_WRITE_ENABLED": "true",
                "OUTBOX_DISPATCH_ENABLED": "true",
                "ASYNC_PIPELINE_ENABLED": "false",
                "LOG_FILE_PATH": os.path.join(temp_root, "app.log"),
            })

            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app()
                with TestClient(app) as client:
                    app.state.job_service.create_job("Element")
                    response = client.post("/internal/outbox/dispatch")

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": "Outbox dispatcher is not enabled"})

    def test_outbox_dispatch_endpoint_works_with_injected_publisher(self):
        with TemporaryDirectory() as temp_root:
            published_calls = []

            class Publisher:
                def publish(self, topic: str, payload: str, correlation_id=None) -> None:
                    published_calls.append((topic, payload, correlation_id))

            env = self._base_env(**{
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'core.db')}",
                "DATABASE_AUTO_CREATE_SCHEMA": "true",
                "SHADOW_WRITE_ENABLED": "true",
                "OUTBOX_DISPATCH_ENABLED": "true",
                "ASYNC_PIPELINE_ENABLED": "false",
                "LOG_FILE_PATH": os.path.join(temp_root, "app.log"),
            })

            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app(outbox_publisher=Publisher())
                with TestClient(app) as client:
                    app.state.job_service.create_job("Element")
                    response = client.post("/internal/outbox/dispatch")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["attempted"], 1)
            self.assertEqual(payload["published"], 1)
            self.assertEqual(payload["failed"], 0)
            self.assertEqual(payload["pending_after"], 0)
            self.assertEqual(len(published_calls), 1)


if __name__ == "__main__":
    unittest.main()
