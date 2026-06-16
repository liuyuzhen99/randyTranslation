from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from application.services.outbox_dispatcher import OutboxDispatcher
from application.services.async_pipeline import AsyncPipelineCommandService
from application.services.review_workflow_service import (
    ReviewWorkflowServices,
    build_review_workflow_services,
)
from application.services.artist_catalog_service import (
    CandidateCatalogService,
    CandidateDiscoveryPayload,
    ArtistCatalogProviders,
)
from application.services.dual_write_reconcile_service import (
    DualWriteReconcileService,
    DualWriteReconcileThresholds,
)
from domain.repositories import JobRepository
from application.services.shadow_write_service import ShadowWriteService
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyArtistRepository,
    SQLAlchemyArtistSyncRunRepository,
    SQLAlchemyArtifactRepository,
    SQLAlchemyAuditLogRepository,
    SQLAlchemyCandidateRepository,
    SQLAlchemyJobRepository,
    SQLAlchemyOutboxRepository,
    SQLAlchemyReviewRepository,
    SQLAlchemySessionFactory,
    SQLAlchemySubtitleRepository,
    SQLAlchemyVideoRepository,
)
from infrastructure.persistence.sqlite_repositories import SQLiteJobRepository
from infrastructure.storage.cos_media_storage import TencentCOSMediaStorage
from infrastructure.storage.local_media_storage import LocalFilesystemMediaStorage
from infrastructure.messaging.rabbitmq_publisher import RabbitMQPublishConfig, RabbitMQPublisher
from infrastructure.vector.qdrant_repository import QdrantVectorRepository

# Current runtime requirements (startup fail-fast scope).
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
    "SHADOW_WRITE_ENABLED",
    "DATABASE_AUTO_CREATE_SCHEMA",
    "DUAL_WRITE_RECONCILE_ENABLED",
    "DUAL_WRITE_RECONCILE_REPORT_PATH",
    "DUAL_WRITE_RECONCILE_MAX_MISSING_JOBS",
    "DUAL_WRITE_RECONCILE_MAX_JOB_FIELD_MISMATCHES",
    "DUAL_WRITE_RECONCILE_MAX_INVALID_OUTBOX_PAYLOADS",
    "DUAL_WRITE_RECONCILE_MAX_OUTBOX_PAYLOAD_MISMATCHES",
    "OUTBOX_DISPATCH_ENABLED",
    "ASYNC_PIPELINE_ENABLED",
    "PIPELINE_SERVICE_WORKER_ENABLED",
    "PIPELINE_SERVICE_WORKER_POLL_SECONDS",
    "PIPELINE_MAX_STAGE_ATTEMPTS",
    "PIPELINE_RETRY_BACKOFF_BASE_SECONDS",
    "ARTIST_SYNC_STALE_AFTER_SECONDS",
    "MEDIA_STORAGE_BACKEND",
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
    "VECTOR_REPOSITORY_BACKEND",
    "VECTOR_EMBEDDING_PROVIDER",
    "VECTOR_EMBEDDING_DIMENSION",
    "QDRANT_COLLECTION_PREFIX",
    "OPENAI_API_KEY",
    "CUTOVER_READ_SOURCE",
    "SCHEMA_FREEZE_ENABLED",
    "ROLLBACK_ENABLED",
    "STABILITY_WINDOW_DAYS",
    "SHADOW_TRAFFIC_ENABLED",
    "S3_ENDPOINT_URL",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_BUCKET",
    "S3_REGION",
    "COS_SECRET_ID",
    "COS_SECRET_KEY",
    "COS_BUCKET",
    "COS_REGION",
    "COS_SCHEME",
    "COS_ENDPOINT",
)

LEGACY_ENV_VAR_ALIASES: dict[str, str] = {
    "PHASE2_SHADOW_WRITE_ENABLED": "SHADOW_WRITE_ENABLED",
    "PHASE2_AUTO_CREATE_SCHEMA": "DATABASE_AUTO_CREATE_SCHEMA",
    "PHASE2_RECONCILE_ENABLED": "DUAL_WRITE_RECONCILE_ENABLED",
    "PHASE2_RECONCILE_REPORT_PATH": "DUAL_WRITE_RECONCILE_REPORT_PATH",
    "PHASE2_RECONCILE_MAX_MISSING_JOBS": "DUAL_WRITE_RECONCILE_MAX_MISSING_JOBS",
    "PHASE2_RECONCILE_MAX_JOB_FIELD_MISMATCHES": "DUAL_WRITE_RECONCILE_MAX_JOB_FIELD_MISMATCHES",
    "PHASE2_RECONCILE_MAX_INVALID_OUTBOX_PAYLOADS": "DUAL_WRITE_RECONCILE_MAX_INVALID_OUTBOX_PAYLOADS",
    "PHASE2_RECONCILE_MAX_OUTBOX_PAYLOAD_MISMATCHES": "DUAL_WRITE_RECONCILE_MAX_OUTBOX_PAYLOAD_MISMATCHES",
    "PHASE2_OUTBOX_DISPATCH_ENABLED": "OUTBOX_DISPATCH_ENABLED",
    "PHASE6_ASYNC_PIPELINE_ENABLED": "ASYNC_PIPELINE_ENABLED",
    "PHASE6_SERVICE_WORKER_ENABLED": "PIPELINE_SERVICE_WORKER_ENABLED",
    "PHASE6_SERVICE_WORKER_POLL_SECONDS": "PIPELINE_SERVICE_WORKER_POLL_SECONDS",
    "PHASE6_MAX_STAGE_ATTEMPTS": "PIPELINE_MAX_STAGE_ATTEMPTS",
    "PHASE6_RETRY_BACKOFF_BASE_SECONDS": "PIPELINE_RETRY_BACKOFF_BASE_SECONDS",
    "PHASE9_CUTOVER_READ_SOURCE": "CUTOVER_READ_SOURCE",
    "PHASE9_SCHEMA_FREEZE_ENABLED": "SCHEMA_FREEZE_ENABLED",
    "PHASE9_ROLLBACK_ENABLED": "ROLLBACK_ENABLED",
    "PHASE9_STABILITY_WINDOW_DAYS": "STABILITY_WINDOW_DAYS",
    "PHASE9_SHADOW_TRAFFIC_ENABLED": "SHADOW_TRAFFIC_ENABLED",
}


def validate_startup_env(environ: Mapping[str, str] | None = None) -> None:
    source = environ if environ is not None else os.environ
    missing = [name for name in REQUIRED_ENV_VARS if not source.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Please configure them in your environment or .env file."
        )


class AppRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        env_ignore_empty=True,
    )

    job_repository_backend: str = "memory"
    job_repository_sqlite_path: str = ""
    database_url: str = ""
    # POSTGRES_* components for assembling DATABASE_URL (consumed by model_validator)
    postgres_host: str = ""
    postgres_port: str = "5432"
    postgres_db: str = ""
    postgres_user: str = ""
    postgres_password: str = ""
    shadow_write_enabled: bool = False
    database_auto_create_schema: bool = False
    dual_write_reconcile_enabled: bool = False
    dual_write_reconcile_report_path: str = ""
    dual_write_reconcile_max_missing_jobs: int = 0
    dual_write_reconcile_max_job_field_mismatches: int = 0
    dual_write_reconcile_max_invalid_outbox_payloads: int = 0
    dual_write_reconcile_max_outbox_payload_mismatches: int = 0
    outbox_dispatch_enabled: bool = False
    async_pipeline_enabled: bool = False
    pipeline_service_worker_enabled: bool = False
    pipeline_service_worker_poll_seconds: float = 1.0
    pipeline_max_stage_attempts: int = 3
    pipeline_retry_backoff_base_seconds: int = 30
    artist_sync_stale_after_seconds: int = 1800
    media_storage_backend: str = "local"
    artifact_temp_retention_days: int = 1
    artifact_final_retention_days: int = 0
    vector_repository_backend: str = "sqlite"
    vector_embedding_provider: str = "bge"
    vector_embedding_dimension: int = 1024
    qdrant_collection_prefix: str = ""
    cutover_read_source: str = "legacy"
    schema_freeze_enabled: bool = False
    rollback_enabled: bool = True
    stability_window_days: int = 7
    shadow_traffic_enabled: bool = False

    @field_validator("job_repository_backend", mode="before")
    @classmethod
    def _validate_job_backend(cls, v: str) -> str:
        val = (v or "memory").strip().lower()
        if val not in {"memory", "sqlite", "sqlalchemy"}:
            raise RuntimeError(
                "Invalid JOB_REPOSITORY_BACKEND. Expected one of: memory, sqlite, sqlalchemy."
            )
        return val

    @field_validator("media_storage_backend", mode="before")
    @classmethod
    def _validate_media_backend(cls, v: str) -> str:
        val = (v or "local").strip().lower()
        if val not in {"local", "cos"}:
            raise RuntimeError("Invalid MEDIA_STORAGE_BACKEND. Expected one of: local, cos.")
        return val

    @field_validator("vector_repository_backend", mode="before")
    @classmethod
    def _validate_vector_backend(cls, v: str) -> str:
        val = (v or "sqlite").strip().lower()
        if val not in {"sqlite", "qdrant"}:
            raise RuntimeError(
                "Invalid VECTOR_REPOSITORY_BACKEND. Expected one of: sqlite, qdrant."
            )
        return val

    @field_validator("vector_embedding_provider", mode="before")
    @classmethod
    def _validate_vector_embedding_provider(cls, v: str) -> str:
        val = (v or "bge").strip().lower()
        if val not in {"bge", "bge-m3", "baai/bge-m3", "hash", "hashing", "fallback"}:
            raise RuntimeError(
                "Invalid VECTOR_EMBEDDING_PROVIDER. Expected one of: bge, hashing."
            )
        return val

    @field_validator("vector_embedding_dimension", mode="before")
    @classmethod
    def _validate_vector_dimension(cls, v) -> int:
        val = int(v) if isinstance(v, str) else v
        if val < 8:
            raise RuntimeError("VECTOR_EMBEDDING_DIMENSION must be at least 8.")
        return val

    @field_validator("pipeline_max_stage_attempts", mode="before")
    @classmethod
    def _validate_max_stage_attempts(cls, v) -> int:
        val = int(v) if isinstance(v, str) else v
        if val < 1:
            raise RuntimeError("PIPELINE_MAX_STAGE_ATTEMPTS must be at least 1.")
        return val

    @field_validator("artist_sync_stale_after_seconds", mode="before")
    @classmethod
    def _validate_artist_sync_stale_after_seconds(cls, v) -> int:
        val = int(v) if isinstance(v, str) else v
        if val < 1:
            raise RuntimeError("ARTIST_SYNC_STALE_AFTER_SECONDS must be at least 1.")
        return val

    @field_validator("dual_write_reconcile_max_missing_jobs", "dual_write_reconcile_max_job_field_mismatches",
                     "dual_write_reconcile_max_invalid_outbox_payloads", "dual_write_reconcile_max_outbox_payload_mismatches",
                     "pipeline_retry_backoff_base_seconds", "artifact_temp_retention_days",
                     "artifact_final_retention_days", "stability_window_days", mode="before")
    @classmethod
    def _validate_non_negative_int(cls, v) -> int:
        if v == "" or v is None:
            return 0
        val = int(v) if isinstance(v, str) else v
        if val < 0:
            raise RuntimeError(f"Value must be a non-negative integer, got {val}.")
        return val

    @field_validator("cutover_read_source", mode="before")
    @classmethod
    def _validate_cutover_read_source(cls, v: str) -> str:
        val = (v or "legacy").strip().lower()
        if val not in {"legacy", "postgres", "qdrant"}:
            raise RuntimeError(
                "Invalid CUTOVER_READ_SOURCE. Expected one of: legacy, postgres, qdrant."
            )
        return val

    @field_validator("job_repository_sqlite_path", mode="after")
    @classmethod
    def _default_sqlite_path(cls, v: str, info) -> str:
        if not v and info.data.get("job_repository_backend") == "sqlite":
            return str(Path.cwd() / "data" / "jobs.db")
        return v

    @model_validator(mode="after")
    def _assemble_database_url(self) -> "AppRuntimeSettings":
        if not self.database_url:
            host = self.postgres_host
            db = self.postgres_db
            user = self.postgres_user
            password = self.postgres_password
            port = self.postgres_port or "5432"
            if host and db and user and password:
                object.__setattr__(
                    self,
                    "database_url",
                    f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}",
                )
        return self


def load_runtime_settings(environ: Mapping[str, str] | None = None) -> AppRuntimeSettings:
    if environ is None:
        load_dotenv(Path.cwd() / ".env", override=False)
        _apply_legacy_env_aliases(os.environ)
        return AppRuntimeSettings()
    # Tests inject a dict with uppercase env-var names.
    # Build settings purely from that dict, bypassing os.environ and .env.
    field_map = {k.lower(): v for k, v in _with_legacy_env_aliases(environ).items()}

    class _DictSettings(AppRuntimeSettings):
        model_config = SettingsConfigDict(
            env_file=None,
            env_ignore_empty=True,
            extra="ignore",
            populate_by_name=True,
        )

        @classmethod
        def settings_customise_sources(cls, settings_cls, init_settings, **kwargs):
            return (init_settings,)

    return _DictSettings(**field_map)


def _with_legacy_env_aliases(environ: Mapping[str, str]) -> dict[str, str]:
    merged = dict(environ)
    for legacy_name, current_name in LEGACY_ENV_VAR_ALIASES.items():
        if current_name not in merged and legacy_name in merged:
            merged[current_name] = merged[legacy_name]
    return merged


def _apply_legacy_env_aliases(environ: os._Environ[str]) -> None:
    for legacy_name, current_name in LEGACY_ENV_VAR_ALIASES.items():
        if current_name not in environ and legacy_name in environ:
            environ[current_name] = environ[legacy_name]


def create_vector_repository(
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
):
    settings = runtime_settings or load_runtime_settings(environ)
    source = environ if environ is not None else os.environ
    if settings.vector_repository_backend == "qdrant":
        from application.services.vector_migration import build_embedding_provider
        embedding_provider = build_embedding_provider(
            settings.vector_embedding_provider,
            dimension=settings.vector_embedding_dimension,
        )
        return QdrantVectorRepository(
            url=source.get("QDRANT_URL", "").strip(),
            api_key=source.get("QDRANT_API_KEY", "").strip(),
            collection_prefix=settings.qdrant_collection_prefix,
            embedding_provider=embedding_provider,
        )
    sqlite_path = source.get("JOB_REPOSITORY_SQLITE_PATH", "").strip() or str(Path.cwd() / "data" / "jobs.db")
    from infrastructure.persistence.sqlite_repositories import SQLiteVectorRepository

    return SQLiteVectorRepository(sqlite_path)


def create_media_storage(
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
):
    settings = runtime_settings or load_runtime_settings(environ)
    source = environ if environ is not None else os.environ
    if settings.media_storage_backend == "cos":
        return TencentCOSMediaStorage(
            temp_root=source.get("MEDIA_TEMP_ROOT", "").strip() or None,
            bucket=source.get("COS_BUCKET", "").strip() or None,
            region=source.get("COS_REGION", "").strip() or None,
            secret_id=source.get("COS_SECRET_ID", "").strip() or None,
            secret_key=source.get("COS_SECRET_KEY", "").strip() or None,
            scheme=source.get("COS_SCHEME", "https").strip() or "https",
            endpoint=source.get("COS_ENDPOINT", "").strip() or None,
        )
    return LocalFilesystemMediaStorage(
        temp_root=source.get("MEDIA_TEMP_ROOT", "").strip() or None,
        output_root=source.get("MEDIA_OUTPUT_ROOT", "").strip() or None,
        bucket=source.get("S3_BUCKET", "").strip() or source.get("COS_BUCKET", "").strip() or None,
    )

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
    if settings.database_auto_create_schema:
        session_factory.create_schema()
    return session_factory


def create_shadow_write_service(
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> ShadowWriteService | None:
    settings = runtime_settings or load_runtime_settings(environ)
    if not settings.shadow_write_enabled:
        return None
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        raise RuntimeError(
            "SHADOW_WRITE_ENABLED requires DATABASE_URL to be configured."
        )
    return ShadowWriteService(active_session_factory)


def create_dual_write_reconcile_service(
    primary_job_repository: JobRepository,
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> DualWriteReconcileService | None:
    settings = runtime_settings or load_runtime_settings(environ)
    if not settings.dual_write_reconcile_enabled:
        return None
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        raise RuntimeError(
            "DUAL_WRITE_RECONCILE_ENABLED requires DATABASE_URL to be configured."
        )
    return DualWriteReconcileService(
        primary_job_repository,
        active_session_factory,
        thresholds=DualWriteReconcileThresholds(
            max_missing_jobs=settings.dual_write_reconcile_max_missing_jobs,
            max_job_field_mismatches=settings.dual_write_reconcile_max_job_field_mismatches,
            max_invalid_outbox_payloads=settings.dual_write_reconcile_max_invalid_outbox_payloads,
            max_outbox_payload_mismatches=settings.dual_write_reconcile_max_outbox_payload_mismatches,
        ),
    )


def create_outbox_dispatcher(
    publisher=None,
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> OutboxDispatcher | None:
    settings = runtime_settings or load_runtime_settings(environ)
    if not settings.outbox_dispatch_enabled and not settings.async_pipeline_enabled:
        return None
    if publisher is None:
        if settings.async_pipeline_enabled:
            rabbitmq_url = (environ if environ is not None else os.environ).get("RABBITMQ_URL", "").strip()
            if rabbitmq_url:
                publisher = RabbitMQPublisher(RabbitMQPublishConfig(url=rabbitmq_url))
        if publisher is None:
            return None
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        raise RuntimeError(
            "OUTBOX_DISPATCH_ENABLED requires DATABASE_URL to be configured."
        )
    return OutboxDispatcher(SQLAlchemyOutboxRepository(active_session_factory), publisher)


def create_async_pipeline_command_service(
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> AsyncPipelineCommandService | None:
    settings = runtime_settings or load_runtime_settings(environ)
    if not settings.async_pipeline_enabled:
        return None
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        raise RuntimeError("ASYNC_PIPELINE_ENABLED requires DATABASE_URL to be configured.")
    outbox_repository = SQLAlchemyOutboxRepository(active_session_factory)
    return AsyncPipelineCommandService(
        outbox_repository=outbox_repository,
        max_attempts=settings.pipeline_max_stage_attempts,
    )


def create_artist_catalog_service(
    providers: ArtistCatalogProviders | None = None,
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> CandidateCatalogService | None:
    settings = runtime_settings or load_runtime_settings(environ)
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        return None
    active_providers = providers or ArtistCatalogProviders(
        followed_artists_lookup=_default_followed_artists_lookup_provider,
        channel_lookup=_default_channel_lookup_provider,
        candidate_lookup=_default_candidate_lookup_provider,
    )
    return CandidateCatalogService(
        artist_repository=SQLAlchemyArtistRepository(active_session_factory),
        artist_sync_run_repository=SQLAlchemyArtistSyncRunRepository(active_session_factory),
        candidate_repository=SQLAlchemyCandidateRepository(active_session_factory),
        providers=active_providers,
        artist_sync_stale_after_seconds=settings.artist_sync_stale_after_seconds,
    )


def create_review_workflow_services(
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> ReviewWorkflowServices | None:
    settings = runtime_settings or load_runtime_settings(environ)
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        return None
    return build_review_workflow_services(
        artist_repository=SQLAlchemyArtistRepository(active_session_factory),
        candidate_repository=SQLAlchemyCandidateRepository(active_session_factory),
        review_repository=SQLAlchemyReviewRepository(active_session_factory),
        audit_log_repository=SQLAlchemyAuditLogRepository(active_session_factory),
        subtitle_repository=SQLAlchemySubtitleRepository(active_session_factory),
        video_repository=SQLAlchemyVideoRepository(active_session_factory),
        artifact_repository=SQLAlchemyArtifactRepository(active_session_factory),
    )


create_phase2_shadow_write_service = create_shadow_write_service
create_phase2_reconcile_service = create_dual_write_reconcile_service
create_phase2_outbox_dispatcher = create_outbox_dispatcher
create_phase6_async_pipeline_services = create_async_pipeline_command_service
create_phase3_catalog_service = create_artist_catalog_service
create_phase4_workflow_services = create_review_workflow_services


def create_artifact_repository(
    environ: Mapping[str, str] | None = None,
    runtime_settings: AppRuntimeSettings | None = None,
    session_factory: SQLAlchemySessionFactory | None = None,
) -> SQLAlchemyArtifactRepository | None:
    settings = runtime_settings or load_runtime_settings(environ)
    active_session_factory = session_factory or create_sqlalchemy_session_factory(environ, settings)
    if active_session_factory is None:
        return None
    return SQLAlchemyArtifactRepository(active_session_factory)


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
