import unittest
from tempfile import TemporaryDirectory

from api.config import (
    create_job_repository,
    create_phase2_shadow_write_service,
    create_sqlalchemy_session_factory,
    load_runtime_settings,
    validate_startup_env,
)
from application.services.phase2_shadow_write_service import Phase2ShadowWriteService
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository
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


if __name__ == "__main__":
    unittest.main()
