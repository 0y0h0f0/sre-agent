"""Stable cache-key helpers for tool queries."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from packages.common.time import ensure_utc
from packages.tools.base import ToolResult


class RequestLocalToolCache:
    """Small in-memory cache intended for a single agent run."""

    def __init__(self) -> None:
        self._items: dict[str, ToolResult] = {}

    def get(self, key: str) -> ToolResult | None:
        cached = self._items.get(key)
        if cached is None:
            return None
        return cached.model_copy(update={"cache_hit": True})

    def set(self, key: str, result: ToolResult) -> None:
        self._items[key] = result.model_copy(update={"cache_hit": False})


def build_cache_key(
    *,
    tool_name: str,
    service: str,
    query: BaseModel,
    start: datetime,
    end: datetime,
    bucket_seconds: int,
) -> str:
    normalized = _normalized_query_payload(query)
    normalized.pop("service", None)
    normalized.pop("start", None)
    normalized.pop("end", None)
    query_hash = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    start_bucket = _bucket_datetime(start, bucket_seconds, round_up=False)
    end_bucket = _bucket_datetime(end, bucket_seconds, round_up=True)
    return (
        f"tool:{tool_name}:{service}:{query_hash}:"
        f"{start_bucket.isoformat()}:{end_bucket.isoformat()}"
    )


def _normalized_query_payload(query: BaseModel) -> dict[str, Any]:
    payload = query.model_dump(mode="json", exclude_none=True)
    if payload.get("keywords") == []:
        payload.pop("keywords")
    return payload


def _bucket_datetime(value: datetime, bucket_seconds: int, *, round_up: bool) -> datetime:
    normalized = ensure_utc(value).astimezone(UTC)
    timestamp = int(normalized.timestamp())
    if round_up:
        bucketed = ((timestamp + bucket_seconds - 1) // bucket_seconds) * bucket_seconds
    else:
        bucketed = (timestamp // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(bucketed, tz=UTC).replace(microsecond=0)


def expand_window(start: datetime, end: datetime, before: timedelta) -> tuple[datetime, datetime]:
    return ensure_utc(start) - before, ensure_utc(end)
