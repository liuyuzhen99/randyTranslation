import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from infrastructure.persistence.alembic_runtime_config import resolve_database_url


class AlembicEnvTests(unittest.TestCase):
    def test_resolve_database_url_prefers_environment(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:"}, clear=False):
            resolved = resolve_database_url("sqlite:///fallback.db")

        self.assertEqual(resolved, "sqlite:///:memory:")

    def test_resolve_database_url_loads_dotenv_before_fallback(self):
        with TemporaryDirectory() as temp_root:
            project_root = Path(temp_root)
            (project_root / ".env").write_text(
                "DATABASE_URL=sqlite:///./dotenv-core.db\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                resolved = resolve_database_url(
                    "sqlite:///fallback.db",
                    project_root=project_root,
                )

        self.assertEqual(resolved, "sqlite:///./dotenv-core.db")

    def test_resolve_database_url_uses_fallback_when_env_missing(self):
        with TemporaryDirectory() as temp_root:
            with patch.dict(os.environ, {}, clear=True):
                resolved = resolve_database_url(
                    "sqlite:///fallback.db",
                    project_root=Path(temp_root),
                )

        self.assertEqual(resolved, "sqlite:///fallback.db")


if __name__ == "__main__":
    unittest.main()
