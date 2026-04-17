from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def resolve_database_url(fallback_url: str, *, project_root: Path | None = None) -> str:
    """Resolve the active database URL for Alembic from env/.env before falling back."""
    active_project_root = project_root or Path(__file__).resolve().parents[2]
    load_dotenv(active_project_root / ".env", override=False)
    return os.environ.get("DATABASE_URL", "").strip() or fallback_url
