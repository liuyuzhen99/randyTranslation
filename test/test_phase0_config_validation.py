import os
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from api.config import (
    create_job_repository,
    create_phase2_outbox_dispatcher,
    create_phase2_reconcile_service,
    create_phase2_shadow_write_service,
    create_sqlalchemy_session_factory,
    load_runtime_settings,
    validate_startup_env,
)
from application.services.outbox_dispatcher import OutboxDispatcher
from application.services.phase2_reconcile_service import Phase2ReconcileService
from application.services.phase2_shadow_write_service import Phase2ShadowWriteService
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyJobRepository
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemySessionFactory
from infrastructure.persistence.sqlite_repositories import SQLiteJobRepository


class Phase0ConfigValidationTests(unittest.TestCase):
    def test_validate_startup_env_raises_when_required_missing(self):
        with self.assertRaises(RuntimeError) as ctx:
            validate_startup_env({})
        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))
        self.assertIn("DEEPSEEK_BASE_URL", str(ctx.exception))

    def test_validate_startup_env_accepts_required_values(self):
        validate_startup_env(
            {
                "DEEPSEEK_API_KEY": "demo",
                "DEEPSEEK_BASE_URL": "https://example.local",
            }
        )

    def test_load_runtime_settings_defaults_to_in_memory_repository(self):
        settings = load_runtime_settings({})
        self.assertEqual(settings.job_repository_backend, "memory")
        self.assertEqual(settings.job_repository_sqlite_path, "")
        self.assertEqual(settings.database_url, "")
        self.assertFalse(settings.phase2_shadow_write_enabled)
        self.assertFalse(settings.phase2_reconcile_enabled)
        self.assertEqual(settings.phase2_reconcile_report_path, "")
        self.assertEqual(settings.phase2_reconcile_max_missing_jobs, 0)
        self.assertEqual(settings.phase2_reconcile_max_job_field_mismatches, 0)
        self.assertEqual(settings.phase2_reconcile_max_invalid_outbox_payloads, 0)
        self.assertEqual(settings.phase2_reconcile_max_outbox_payload_mismatches, 0)
        self.assertFalse(settings.phase2_outbox_dispatch_enabled)

    def test_load_runtime_settings_reads_reconcile_report_path(self):
        settings = load_runtime_settings(
            {"PHASE2_RECONCILE_REPORT_PATH": "./data/reports/phase2.json"}
        )
        self.assertEqual(settings.phase2_reconcile_report_path, "./data/reports/phase2.json")

    def test_load_runtime_settings_reads_reconcile_thresholds(self):
        settings = load_runtime_settings(
            {
                "PHASE2_RECONCILE_MAX_MISSING_JOBS": "2",
                "PHASE2_RECONCILE_MAX_JOB_FIELD_MISMATCHES": "3",
                "PHASE2_RECONCILE_MAX_INVALID_OUTBOX_PAYLOADS": "4",
                "PHASE2_RECONCILE_MAX_OUTBOX_PAYLOAD_MISMATCHES": "5",
            }
        )
        self.assertEqual(settings.phase2_reconcile_max_missing_jobs, 2)
        self.assertEqual(settings.phase2_reconcile_max_job_field_mismatches, 3)
        self.assertEqual(settings.phase2_reconcile_max_invalid_outbox_payloads, 4)
        self.assertEqual(settings.phase2_reconcile_max_outbox_payload_mismatches, 5)

    def test_load_runtime_settings_rejects_negative_reconcile_threshold(self):
        with self.assertRaises(RuntimeError):
            load_runtime_settings({"PHASE2_RECONCILE_MAX_MISSING_JOBS": "-1"})

    def test_load_runtime_settings_reads_project_dotenv_when_environ_not_provided(self):
        with TemporaryDirectory() as temp_root:
            env_path = Path(temp_root) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "JOB_REPOSITORY_BACKEND=sqlalchemy",
                        "DATABASE_URL=sqlite:///:memory:",
                        "PHASE2_SHADOW_WRITE_ENABLED=true",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with patch("api.config.Path.cwd", return_value=Path(temp_root)):
                    settings = load_runtime_settings()

        self.assertEqual(settings.job_repository_backend, "sqlalchemy")
        self.assertEqual(settings.database_url, "sqlite:///:memory:")
        self.assertTrue(settings.phase2_shadow_write_enabled)

    def test_load_runtime_settings_builds_database_url_from_postgres_parts(self):
        settings = load_runtime_settings(
            {
                "POSTGRES_HOST": "localhost",
                "POSTGRES_PORT": "5432",
                "POSTGRES_DB": "randy_translation",
                "POSTGRES_USER": "app",
                "POSTGRES_PASSWORD": "secret",
            }
        )
        self.assertEqual(
            settings.database_url,
            "postgresql+psycopg://app:secret@localhost:5432/randy_translation",
        )

    def test_load_runtime_settings_defaults_sqlite_path_when_backend_enabled(self):
        settings = load_runtime_settings({"JOB_REPOSITORY_BACKEND": "sqlite"})
        self.assertEqual(settings.job_repository_backend, "sqlite")
        self.assertTrue(settings.job_repository_sqlite_path.endswith("data/jobs.db"))

    def test_load_runtime_settings_rejects_unknown_backend(self):
        with self.assertRaises(RuntimeError):
            load_runtime_settings({"JOB_REPOSITORY_BACKEND": "redis"})

    def test_create_job_repository_builds_in_memory_backend(self):
        repo = create_job_repository({})
        self.assertIsInstance(repo, InMemoryJobRepository)

    def test_create_job_repository_builds_sqlite_backend(self):
        with TemporaryDirectory() as temp_root:
            repo = create_job_repository(
                {
                    "JOB_REPOSITORY_BACKEND": "sqlite",
                    "JOB_REPOSITORY_SQLITE_PATH": f"{temp_root}/jobs.db",
                }
            )
            self.assertIsInstance(repo, SQLiteJobRepository)

    def test_create_job_repository_builds_sqlalchemy_backend(self):
        repo = create_job_repository(
            {
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": "sqlite:///:memory:",
            }
        )
        self.assertIsInstance(repo, SQLAlchemyJobRepository)

    def test_create_sqlalchemy_session_factory_returns_none_without_database_url(self):
        self.assertIsNone(create_sqlalchemy_session_factory({}))

    def test_create_sqlalchemy_session_factory_builds_factory(self):
        session_factory = create_sqlalchemy_session_factory({"DATABASE_URL": "sqlite:///:memory:"})
        self.assertIsInstance(session_factory, SQLAlchemySessionFactory)

    def test_create_phase2_shadow_write_service_requires_database_url(self):
        with self.assertRaises(RuntimeError):
            create_phase2_shadow_write_service({"PHASE2_SHADOW_WRITE_ENABLED": "true"})

    def test_create_phase2_shadow_write_service_builds_service(self):
        service = create_phase2_shadow_write_service(
            {
                "PHASE2_SHADOW_WRITE_ENABLED": "true",
                "DATABASE_URL": "sqlite:///:memory:",
            }
        )
        self.assertIsInstance(service, Phase2ShadowWriteService)

    def test_create_phase2_reconcile_service_requires_database_url(self):
        with self.assertRaises(RuntimeError):
            create_phase2_reconcile_service(
                InMemoryJobRepository(),
                {"PHASE2_RECONCILE_ENABLED": "true"},
            )

    def test_create_phase2_reconcile_service_builds_service(self):
        service = create_phase2_reconcile_service(
            InMemoryJobRepository(),
            {
                "PHASE2_RECONCILE_ENABLED": "true",
                "DATABASE_URL": "sqlite:///:memory:",
                "PHASE2_RECONCILE_MAX_MISSING_JOBS": "1",
            },
        )
        self.assertIsInstance(service, Phase2ReconcileService)
        self.assertEqual(service.thresholds.max_missing_jobs, 1)

    def test_create_phase2_outbox_dispatcher_requires_database_url(self):
        with self.assertRaises(RuntimeError):
            create_phase2_outbox_dispatcher(
                publisher=object(),
                environ={"PHASE2_OUTBOX_DISPATCH_ENABLED": "true"},
            )

    def test_create_phase2_outbox_dispatcher_returns_none_without_publisher(self):
        dispatcher = create_phase2_outbox_dispatcher(
            environ={
                "PHASE2_OUTBOX_DISPATCH_ENABLED": "true",
                "DATABASE_URL": "sqlite:///:memory:",
            },
        )
        self.assertIsNone(dispatcher)

    def test_create_phase2_outbox_dispatcher_builds_dispatcher(self):
        class Publisher:
            def publish(self, topic: str, payload: str, correlation_id=None) -> None:
                return None

        dispatcher = create_phase2_outbox_dispatcher(
            publisher=Publisher(),
            environ={
                "PHASE2_OUTBOX_DISPATCH_ENABLED": "true",
                "DATABASE_URL": "sqlite:///:memory:",
            },
        )
        self.assertIsInstance(dispatcher, OutboxDispatcher)


if __name__ == "__main__":
    unittest.main()
