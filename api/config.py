from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from domain.repositories import JobRepository
from application.services.phase2_shadow_write_service import Phase2ShadowWriteService
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemySessionFactory
from infrastructure.persistence.sqlite_repositories import SQLiteJobRepository

# Current runtime requirements (Phase 0 fail-fast scope).
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
)

# Current and target infrastructure variables for documentation/template coverage.
KNOWN_ENV_VARS: tuple[str, ...] = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "JOB_REPOSITORY_BACKEND",
    "JOB_REPOSITORY_SQLITE_PATH",
    "PHASE2_SHADOW_WRITE_ENABLED",
    "PHASE2_AUTO_CREATE_SCHEMA",
    "MEDIA_TEMP_ROOT",
    "MEDIA_OUTPUT_ROOT",
    "SPOTIPY_CLIENT_ID",
    "SPOTIPY_CLIENT_SECRET",
    "SPOTIPY_REDIRECT_URI",
    "DATABASE_URL",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "RABBITMQ_URL",
    "RABBITMQ_HOST",
    "RABBITMQ_PORT",
    "RABBITMQ_USER",
    "RABBITMQ_PASSWORD",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_BUCKET",
    "S3_REGION",
)


def validate_startup_env(environ: Mapping[str, str] | None = None) -> None:
    source = environ if environ is not None else os.environ
    missing = [name for name in REQUIRED_ENV_VARS if not source.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Please configure them in your environment or .env file."
        )


@dataclass(frozen=True)
class AppRuntimeSettings:
    job_repository_backend: str = "memory"
    job_repository_sqlite_path: str = ""
    database_url: str = ""
    phase2_shadow_write_enabled: bool = False
    phase2_auto_create_schema: bool = False


def load_runtime_settings(environ: Mapping[str, str] | None = None) -> AppRuntimeSettings:
    source = environ if environ is not None else os.environ
    backend = source.get("JOB_REPOSITORY_BACKEND", "memory").strip().lower() or "memory"

    if backend not in {"memory", "sqlite"}:
        raise RuntimeError(
            "Invalid JOB_REPOSITORY_BACKEND. Expected one of: memory, sqlite."
        )

    sqlite_path = source.get("JOB_REPOSITORY_SQLITE_PATH", "").strip()
    if backend == "sqlite" and not sqlite_path:
        sqlite_path = str(Path.cwd() / "data" / "jobs.db")
    database_url = source.get("DATABASE_URL", "").strip()
    shadow_write_enabled = source.get("PHASE2_SHADOW_WRITE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    auto_create_schema = source.get("PHASE2_AUTO_CREATE_SCHEMA", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    return AppRuntimeSettings(
        job_repository_backend=backend,
        job_repository_sqlite_path=sqlite_path,
        database_url=database_url,
        phase2_shadow_write_enabled=shadow_write_enabled,
        phase2_auto_create_schema=auto_create_schema,
    )


def create_job_repository(
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
) -> JobRepository:
    settings = runtime_settings or load_runtime_settings(environ)
    if settings.job_repository_backend == "sqlite":
        sqlite_path = Path(settings.job_repository_sqlite_path)
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return SQLiteJobRepository(str(sqlite_path))
    return InMemoryJobRepository()


def create_sqlalchemy_session_factory(
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
) -> SQLAlchemySessionFactory | None:
    settings = runtime_settings or load_runtime_settings(environ)
    if not settings.database_url:
        return None
    session_factory = SQLAlchemySessionFactory(settings.database_url)
    if settings.phase2_auto_create_schema:
        session_factory.create_schema()
    return session_factory


def create_phase2_shadow_write_service(
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
) -> Phase2ShadowWriteService | None:
    settings = runtime_settings or load_runtime_settings(environ)
    if not settings.phase2_shadow_write_enabled:
        return None
    session_factory = create_sqlalchemy_session_factory(environ, settings)
    if session_factory is None:
        raise RuntimeError(
            "PHASE2_SHADOW_WRITE_ENABLED requires DATABASE_URL to be configured."
        )
    return Phase2ShadowWriteService(session_factory)
