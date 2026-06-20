"""Timezone helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Normalize datetimes to timezone-aware UTC."""
    if value.tzinfo is None:
        # Treat naive datetimes at system boundaries as already-UTC. Callers that
        # know a local timezone should localize before passing values here.
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
