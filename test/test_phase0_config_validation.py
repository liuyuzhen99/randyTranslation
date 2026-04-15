import unittest
from tempfile import TemporaryDirectory

from api.config import create_job_repository, load_runtime_settings, validate_startup_env
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository
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


if __name__ == "__main__":
    unittest.main()
