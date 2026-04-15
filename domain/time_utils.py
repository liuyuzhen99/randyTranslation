from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return a UTC timestamp stored as a naive datetime for DB compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)
