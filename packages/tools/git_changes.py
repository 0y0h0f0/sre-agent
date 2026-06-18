"""Git/deployment change lookup tool backed by a pluggable backend (Phase 2.1).

The fixture backend keeps MVP behaviour; GitHub/Argo CD backends query real
deployment history. Window filtering and summarization live here.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from packages.common.redaction import redact_text
from packages.common.time import ensure_utc
from packages.tools.base import ToolResult, compact_summary, elapsed_ms, start_timer
from packages.tools.cache import RequestLocalToolCache, build_cache_key
from packages.tools.deployment_backends import DeploymentBackend, FixtureDeploymentBackend


class GitChangeQuery(BaseModel):
    service: str = Field(min_length=1)
    start: datetime
    end: datetime

    @field_validator("service")
    @classmethod
    def _strip_service(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_window(self) -> GitChangeQuery:
        self.start = ensure_utc(self.start)
        self.end = ensure_utc(self.end)
        if self.end <= self.start:
            msg = "end must be after start"
            raise ValueError(msg)
        return self


class GitChangeTool:
    name = "git_changes"

    def __init__(
        self,
        *,
        backend: DeploymentBackend | None = None,
        fixture_path: str | None = None,
        timeout_seconds: float = 2.0,
        cache: RequestLocalToolCache | None = None,
    ) -> None:
        if backend is None:
            backend = FixtureDeploymentBackend(
                fixture_path=fixture_path or "demo/faults/git_changes.json"
            )
        self.backend = backend
        self.timeout_seconds = timeout_seconds
        self.cache = cache

    def run(self, query: BaseModel) -> ToolResult:
        git_query = GitChangeQuery.model_validate(query)
        public_service = _redact_query_text(git_query.service)
        started_at = start_timer()
        cache_key = build_cache_key(
            tool_name=self.name,
            service=public_service,
            query=git_query,
            start=git_query.start,
            end=git_query.end,
            bucket_seconds=600,
            datasource=self.backend.name,
        )
        cached = self.cache.get(cache_key) if self.cache else None
        if cached is not None:
            return cached.model_copy(update={"duration_ms": elapsed_ms(started_at)})

        try:
            changes = self.backend.fetch_changes(public_service, git_query.start, git_query.end)
            matching = [
                change
                for change in changes
                if change.get("service") == public_service
                and _within_window(
                    git_query.start,
                    ensure_utc(_parse_datetime(str(change["deployed_at"]))),
                    git_query.end,
                )
            ]
            public_changes = [_public_change(change) for change in matching[:10]]
            data = {"change_count": len(matching), "changes": public_changes}
            result = ToolResult(
                status="succeeded" if matching else "degraded",
                data=data,
                summary=compact_summary(
                    {
                        "service": public_service,
                        "changes": len(matching),
                        "latest": public_changes[0].get("commit_sha") if public_changes else None,
                    }
                )
                if matching
                else f"no deployment changes for {public_service}",
                evidence=[
                    {
                        "type": "git",
                        "source": self.backend.name,
                        "title": f"deployment changes for {public_service}",
                        "payload": data,
                    }
                ]
                if matching
                else [],
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
            )
        except httpx.TimeoutException as exc:
            result = ToolResult(
                status="timeout",
                data={},
                summary=f"deployment backend timed out for {public_service}",
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=_redact_exception(exc),
            )
        except (
            httpx.HTTPError,
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            result = ToolResult(
                status="degraded",
                data={},
                summary=f"deployment backend unavailable for {public_service}",
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=_redact_exception(exc),
            )

        if self.cache and result.status in {"succeeded", "degraded"}:
            self.cache.set(cache_key, result)
        return result


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _within_window(start: datetime, value: datetime, end: datetime) -> bool:
    return start <= value <= end


def _public_change(change: dict[str, Any]) -> dict[str, Any]:
    return {
        "service": _redact_public_text(change.get("service")),
        "deployed_at": _redact_public_text(change.get("deployed_at")),
        "commit_sha": _redact_identifier(change.get("commit_sha")),
        "author": _redact_public_text(change.get("author")),
        "summary": _redact_public_text(change.get("summary")),
        "files": _public_files(change.get("files")),
    }


def _public_files(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_redact_public_text(item) for item in value if isinstance(item, str)]


def _redact_exception(exc: BaseException) -> str:
    return redact_text(str(exc)).redacted_text


def _redact_query_text(value: str) -> str:
    return redact_text(value).redacted_text


def _redact_public_text(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value).redacted_text
    return value


def _redact_identifier(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    result = redact_text(value)
    if any(redaction_type != "raw_token" for redaction_type in result.redaction_types):
        return result.redacted_text
    return value
