from __future__ import annotations

import os
from collections.abc import Mapping

# Current runtime requirements (Phase 0 fail-fast scope).
REQUIRED_ENV_VARS: tuple[str, ...] = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
)

# Current and target infrastructure variables for documentation/template coverage.
KNOWN_ENV_VARS: tuple[str, ...] = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
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
