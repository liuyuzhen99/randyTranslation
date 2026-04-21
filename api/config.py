from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from application.services.outbox_dispatcher import OutboxDispatcher
from application.services.phase3_catalog_service import (
    CandidateCatalogService,
    CandidateDiscoveryPayload,
    Phase3Providers,
)
from application.services.phase2_reconcile_service import (
    Phase2ReconcileService,
    Phase2ReconcileThresholds,
)
from domain.repositories import JobRepository
from application.services.phase2_shadow_write_service import Phase2ShadowWriteService
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyArtistRepository,
    SQLAlchemyArtistSyncRunRepository,
    SQLAlchemyCandidateRepository,
    SQLAlchemyJobRepository,
    SQLAlchemyOutboxRepository,
    SQLAlchemySessionFactory,
)
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
    "PHASE2_RECONCILE_ENABLED",
    "PHASE2_RECONCILE_REPORT_PATH",
    "PHASE2_RECONCILE_MAX_MISSING_JOBS",
    "PHASE2_RECONCILE_MAX_JOB_FIELD_MISMATCHES",
    "PHASE2_RECONCILE_MAX_INVALID_OUTBOX_PAYLOADS",
    "PHASE2_RECONCILE_MAX_OUTBOX_PAYLOAD_MISMATCHES",
    "PHASE2_OUTBOX_DISPATCH_ENABLED",
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
    phase2_reconcile_enabled: bool = False
    phase2_reconcile_report_path: str = ""
    phase2_reconcile_max_missing_jobs: int = 0
    phase2_reconcile_max_job_field_mismatches: int = 0
    phase2_reconcile_max_invalid_outbox_payloads: int = 0
    phase2_reconcile_max_outbox_payload_mismatches: int = 0
    phase2_outbox_dispatch_enabled: bool = False


def load_runtime_settings(environ: Mapping[str, str] | None = None) -> AppRuntimeSettings:
    if environ is None:
        load_dotenv(Path.cwd() / ".env", override=False)
    source = environ if environ is not None else os.environ
    backend = source.get("JOB_REPOSITORY_BACKEND", "memory").strip().lower() or "memory"

    if backend not in {"memory", "sqlite", "sqlalchemy"}:
        raise RuntimeError(
            "Invalid JOB_REPOSITORY_BACKEND. Expected one of: memory, sqlite, sqlalchemy."
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
    reconcile_enabled = source.get("PHASE2_RECONCILE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    reconcile_report_path = source.get("PHASE2_RECONCILE_REPORT_PATH", "").strip()
    reconcile_max_missing_jobs = _read_non_negative_int(
        source,
        "PHASE2_RECONCILE_MAX_MISSING_JOBS",
    )
    reconcile_max_job_field_mismatches = _read_non_negative_int(
        source,
        "PHASE2_RECONCILE_MAX_JOB_FIELD_MISMATCHES",
    )
    reconcile_max_invalid_outbox_payloads = _read_non_negative_int(
        source,
        "PHASE2_RECONCILE_MAX_INVALID_OUTBOX_PAYLOADS",
    )
    reconcile_max_outbox_payload_mismatches = _read_non_negative_int(
        source,
        "PHASE2_RECONCILE_MAX_OUTBOX_PAYLOAD_MISMATCHES",
    )
    outbox_dispatch_enabled = source.get("PHASE2_OUTBOX_DISPATCH_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not database_url:
        postgres_host = source.get("POSTGRES_HOST", "").strip()
        postgres_port = source.get("POSTGRES_PORT", "").strip() or "5432"
        postgres_db = source.get("POSTGRES_DB", "").strip()
        postgres_user = source.get("POSTGRES_USER", "").strip()
        postgres_password = source.get("POSTGRES_PASSWORD", "").strip()
        if postgres_host and postgres_db and postgres_user and postgres_password:
            database_url = (
                f"postgresql+psycopg://{postgres_user}:{postgres_password}"
                f"@{postgres_host}:{postgres_port}/{postgres_db}"
            )

    return AppRuntimeSettings(
        job_repository_backend=backend,
        job_repository_sqlite_path=sqlite_path,
        database_url=database_url,
        phase2_shadow_write_enabled=shadow_write_enabled,
        phase2_auto_create_schema=auto_create_schema,
        phase2_reconcile_enabled=reconcile_enabled,
        phase2_reconcile_report_path=reconcile_report_path,
        phase2_reconcile_max_missing_jobs=reconcile_max_missing_jobs,
        phase2_reconcile_max_job_field_mismatches=reconcile_max_job_field_mismatches,
        phase2_reconcile_max_invalid_outbox_payloads=reconcile_max_invalid_outbox_payloads,
        phase2_reconcile_max_outbox_payload_mismatches=reconcile_max_outbox_payload_mismatches,
        phase2_outbox_dispatch_enabled=outbox_dispatch_enabled,
    )


def _read_non_negative_int(source: Mapping[str, str], key: str) -> int:
    raw_value = source.get(key, "").strip()
    if not raw_value:
        return 0
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{key} must be a non-negative integer.") from exc
    if value < 0:
        raise RuntimeError(f"{key} must be a non-negative integer.")
    return value


def create_job_repository(
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> JobRepository:
    settings = runtime_settings or load_runtime_settings(environ)
    if settings.job_repository_backend == "sqlite":
        sqlite_path = Path(settings.job_repository_sqlite_path)
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        return SQLiteJobRepository(str(sqlite_path))
    if settings.job_repository_backend == "sqlalchemy":
        active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
        if active_session_factory is None:
            raise RuntimeError(
                "JOB_REPOSITORY_BACKEND=sqlalchemy requires DATABASE_URL to be configured."
            )
        return SQLAlchemyJobRepository(active_session_factory)
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
    session_factory: SQLAlchemySessionFactory | None = None,
) -> Phase2ShadowWriteService | None:
    settings = runtime_settings or load_runtime_settings(environ)
    if not settings.phase2_shadow_write_enabled:
        return None
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        raise RuntimeError(
            "PHASE2_SHADOW_WRITE_ENABLED requires DATABASE_URL to be configured."
        )
    return Phase2ShadowWriteService(active_session_factory)


def create_phase2_reconcile_service(
    primary_job_repository: JobRepository,
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> Phase2ReconcileService | None:
    settings = runtime_settings or load_runtime_settings(environ)
    if not settings.phase2_reconcile_enabled:
        return None
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        raise RuntimeError(
            "PHASE2_RECONCILE_ENABLED requires DATABASE_URL to be configured."
        )
    return Phase2ReconcileService(
        primary_job_repository,
        active_session_factory,
        thresholds=Phase2ReconcileThresholds(
            max_missing_jobs=settings.phase2_reconcile_max_missing_jobs,
            max_job_field_mismatches=settings.phase2_reconcile_max_job_field_mismatches,
            max_invalid_outbox_payloads=settings.phase2_reconcile_max_invalid_outbox_payloads,
            max_outbox_payload_mismatches=settings.phase2_reconcile_max_outbox_payload_mismatches,
        ),
    )


def create_phase2_outbox_dispatcher(
    publisher=None,
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> OutboxDispatcher | None:
    settings = runtime_settings or load_runtime_settings(environ)
    if not settings.phase2_outbox_dispatch_enabled:
        return None
    if publisher is None:
        return None
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        raise RuntimeError(
            "PHASE2_OUTBOX_DISPATCH_ENABLED requires DATABASE_URL to be configured."
        )
    return OutboxDispatcher(SQLAlchemyOutboxRepository(active_session_factory), publisher)


def create_phase3_catalog_service(
    providers: Phase3Providers | None = None,
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> CandidateCatalogService | None:
    settings = runtime_settings or load_runtime_settings(environ)
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        return None
    active_providers = providers or Phase3Providers(
        followed_artists_lookup=_default_followed_artists_lookup_provider,
        channel_lookup=_default_channel_lookup_provider,
        candidate_lookup=_default_candidate_lookup_provider,
    )
    return CandidateCatalogService(
        artist_repository=SQLAlchemyArtistRepository(active_session_factory),
        artist_sync_run_repository=SQLAlchemyArtistSyncRunRepository(active_session_factory),
        candidate_repository=SQLAlchemyCandidateRepository(active_session_factory),
        providers=active_providers,
    )


def _default_followed_artists_lookup_provider():
    from domain.entities import Artist
    from services.getSpotifyFollowingList import get_all_followed_artists

    return [
        Artist(spotify_id=item["id"], name=item["name"])
        for item in get_all_followed_artists(open_browser=False)
    ]


def _default_channel_lookup_provider(artist) -> str | None:
    if artist.yt_channel_id:
        return artist.yt_channel_id

    from services.getChannelIDfromFollowingList import fetch_youtube_channel_ids

    return fetch_youtube_channel_ids([artist.name]).get(artist.name)


def _default_candidate_lookup_provider(artist, days: int) -> list[CandidateDiscoveryPayload]:
    if not artist.yt_channel_id:
        return []

    from datetime import datetime, timedelta
    import time

    import feedparser

    from services.getLatestMVfromRss import is_valid_mv

    deadline = datetime.now() - timedelta(days=days)
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={artist.yt_channel_id}"
    feed = feedparser.parse(rss_url)
    payloads: list[CandidateDiscoveryPayload] = []
    for entry in getattr(feed, "entries", []):
        published_parsed = getattr(entry, "published_parsed", None)
        if not published_parsed:
            continue
        published_at = datetime.fromtimestamp(time.mktime(published_parsed))
        if published_at <= deadline:
            continue
        is_valid, _reason = is_valid_mv(entry.title, entry.link)
        if not is_valid:
            continue
        video_id = getattr(entry, "yt_videoid", None)
        if not video_id:
            continue
        payloads.append(
            CandidateDiscoveryPayload(
                video_id=video_id,
                title=entry.title,
                source_url=entry.link,
                published_at=published_at,
            )
        )
    return payloads
