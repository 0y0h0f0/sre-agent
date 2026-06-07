"""Loki logs tool."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from packages.common.time import ensure_utc
from packages.tools.base import ToolResult, ToolStatus, compact_summary, elapsed_ms, start_timer
from packages.tools.cache import RequestLocalToolCache, build_cache_key


class LogsQuery(BaseModel):
    service: str = Field(min_length=1)
    start: datetime
    end: datetime
    keywords: list[str] = Field(default_factory=list)
    limit: int = Field(default=100, ge=1, le=1000)

    @field_validator("service")
    @classmethod
    def _strip_service(cls, value: str) -> str:
        return value.strip()

    @field_validator("keywords")
    @classmethod
    def _strip_keywords(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    @model_validator(mode="after")
    def _validate_window(self) -> LogsQuery:
        self.start = ensure_utc(self.start)
        self.end = ensure_utc(self.end)
        if self.end <= self.start:
            msg = "end must be after start"
            raise ValueError(msg)
        return self


class LogsTool:
    name = "logs"

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = 2.0,
        cache: RequestLocalToolCache | None = None,
        service_label: str = "service",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.cache = cache
        self.service_label = service_label

    def run(self, query: BaseModel) -> ToolResult:
        logs_query = LogsQuery.model_validate(query)
        started_at = start_timer()
        cache_key = build_cache_key(
            tool_name=self.name,
            service=logs_query.service,
            query=logs_query,
            start=logs_query.start,
            end=logs_query.end,
            bucket_seconds=60,
        )
        cached = self.cache.get(cache_key) if self.cache else None
        if cached is not None:
            return cached.model_copy(update={"duration_ms": elapsed_ms(started_at)})

        try:
            lines = self._query_loki(logs_query)
            aggregate = _aggregate_logs(lines)
            status: ToolStatus = "succeeded" if lines else "degraded"
            result = ToolResult(
                status=status,
                data=aggregate,
                summary=(
                    compact_summary(
                        {
                            "service": logs_query.service,
                            "lines": len(lines),
                            "top_error": aggregate["top_error_type"],
                        }
                    )
                    if lines
                    else f"no Loki log lines for {logs_query.service}"
                ),
                evidence=[
                    {
                        "type": "log",
                        "source": "loki",
                        "title": f"log samples for {logs_query.service}",
                        "payload": aggregate,
                    }
                ]
                if lines
                else [],
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=None if lines else "empty loki result",
            )
        except httpx.TimeoutException as exc:
            result = ToolResult(
                status="timeout",
                data={},
                summary=f"Loki query timed out for {logs_query.service}",
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=str(exc),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            result = ToolResult(
                status="degraded",
                data={},
                summary=f"Loki unavailable for {logs_query.service}; continuing with degraded logs",
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=str(exc),
            )

        if self.cache and result.status in {"succeeded", "degraded"}:
            self.cache.set(cache_key, result)
        return result

    def _query_loki(self, query: LogsQuery) -> list[dict[str, Any]]:
        keywords: list[str | None] = list(query.keywords)[:10] if query.keywords else [None]
        collected: dict[tuple[Any, ...], dict[str, Any]] = {}
        for keyword in keywords:
            logql = _logql(query.service, keyword, self.service_label)
            params: dict[str, str | int] = {
                "query": logql,
                "start": int(query.start.timestamp() * 1_000_000_000),
                "end": int(query.end.timestamp() * 1_000_000_000),
                "limit": query.limit,
                "direction": "backward",
            }
            if self.client is not None:
                response = self.client.get(
                    "/loki/api/v1/query_range",
                    params=params,
                    timeout=self.timeout_seconds,
                )
            else:
                with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as client:
                    response = client.get("/loki/api/v1/query_range", params=params)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            if payload.get("status") != "success":
                msg = f"loki status={payload.get('status')}"
                raise ValueError(msg)
            for stream in payload["data"].get("result", []):
                labels = stream.get("stream", {})
                for timestamp, line in stream.get("values", []):
                    labels_key = tuple(sorted(labels.items()))
                    collected[(str(timestamp), str(line), labels_key)] = {
                        "timestamp": str(timestamp),
                        "line": str(line),
                        "labels": labels,
                    }
        return list(collected.values())[: query.limit]


def _logql(service: str, keyword: str | None, service_label: str = "service") -> str:
    escaped_service = service.replace("\\", "\\\\").replace('"', '\\"')
    query = f'{{{service_label}="{escaped_service}"}}'
    if keyword:
        escaped_keyword = keyword.replace("\\", "\\\\").replace('"', '\\"')
        query = f'{query} |= "{escaped_keyword}"'
    return query


def _aggregate_logs(lines: list[dict[str, Any]]) -> dict[str, Any]:
    error_types: Counter[str] = Counter()
    signatures: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    for line in lines:
        parsed = _parse_line(line["line"])
        error_type = _error_type(parsed)
        error_types[error_type] += 1
        signature = _signature(parsed)
        if signature:
            signatures[signature] += 1
        if len(samples) < 5:
            samples.append(
                {
                    "timestamp": line["timestamp"],
                    "message": parsed.get("message") or line["line"][:300],
                    "level": parsed.get("level"),
                    "labels": line["labels"],
                }
            )
    top_error = error_types.most_common(1)[0][0] if error_types else None
    top_signature = signatures.most_common(1)[0][0] if signatures else None
    return {
        "line_count": len(lines),
        "error_type_counts": dict(error_types),
        "top_error_type": top_error,
        "top_stack_signature": top_signature,
        "samples": samples,
    }


def _parse_line(line: str) -> dict[str, Any]:
    try:
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {"message": line}


def _error_type(parsed: dict[str, Any]) -> str:
    for key in ("error_type", "exception", "event"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    message = str(parsed.get("message", "")).lower()
    if "timeout" in message:
        return "timeout"
    if "connection" in message and ("exhaust" in message or "refused" in message):
        return "connection_error"
    if "redis" in message or "cache" in message:
        return "cache_error"
    if "oom" in message or "restart" in message:
        return "pod_restart"
    if "5xx" in message or "http 500" in message:
        return "http_5xx"
    return str(parsed.get("level") or "unknown")


def _signature(parsed: dict[str, Any]) -> str | None:
    stack = parsed.get("stack") or parsed.get("traceback")
    if isinstance(stack, str) and stack:
        lines = stack.splitlines()
        if not lines:
            return None
        first = lines[0].strip()
        # Java/Node.js: exception on first line ("Exception: msg", "Error: msg")
        # Python: exception on last line (traceback ends with "TypeError: ...")
        if ":" in first and not first.startswith("at "):
            return first[:160]
        return lines[-1][:160]
    message = parsed.get("message")
    if isinstance(message, str) and message:
        return message[:160]
    return None
