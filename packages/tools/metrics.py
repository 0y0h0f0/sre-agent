"""Prometheus metrics tool."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from math import ceil, isfinite
from statistics import fmean
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from packages.common.time import ensure_utc
from packages.tools.base import ToolResult, compact_summary, elapsed_ms, start_timer
from packages.tools.cache import RequestLocalToolCache, build_cache_key

MetricType = Literal[
    "latency",
    "error_rate",
    "qps",
    "cpu",
    "memory",
    "db_connections",
    "cache_hit_rate",
]


class MetricsQuery(BaseModel):
    service: str = Field(min_length=1)
    metric_type: MetricType
    start: datetime
    end: datetime

    @field_validator("service")
    @classmethod
    def _strip_service(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_window(self) -> MetricsQuery:
        self.start = ensure_utc(self.start)
        self.end = ensure_utc(self.end)
        if self.end <= self.start:
            msg = "end must be after start"
            raise ValueError(msg)
        return self


class MetricsTool:
    name = "metrics"

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = 2.0,
        cache: RequestLocalToolCache | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.cache = cache

    def run(self, query: BaseModel) -> ToolResult:
        metrics_query = MetricsQuery.model_validate(query)
        started_at = start_timer()
        cache_key = build_cache_key(
            tool_name=self.name,
            service=metrics_query.service,
            query=metrics_query,
            start=metrics_query.start,
            end=metrics_query.end,
            bucket_seconds=60,
        )
        cached = self.cache.get(cache_key) if self.cache else None
        if cached is not None:
            return cached.model_copy(update={"duration_ms": elapsed_ms(started_at)})

        promql = _promql(metrics_query.metric_type, metrics_query.service)
        try:
            payload = self._query_range(metrics_query, promql)
            values = _extract_values(payload)
            if not values:
                result = ToolResult(
                    status="degraded",
                    data={"query": promql, "stats": None, "sample_count": 0},
                    summary=(
                        "no Prometheus samples for "
                        f"{metrics_query.service} {metrics_query.metric_type}"
                    ),
                    evidence=[],
                    cache_key=cache_key,
                    duration_ms=elapsed_ms(started_at),
                    error_message="empty prometheus result",
                )
            else:
                stats = _series_stats(values)
                result = ToolResult(
                    status="succeeded",
                    data={"query": promql, "stats": stats, "sample_count": len(values)},
                    summary=compact_summary(
                        {
                            "service": metrics_query.service,
                            "metric": metrics_query.metric_type,
                            "avg": round(stats["avg"], 4),
                            "p95": round(stats["p95"], 4),
                            "last": round(stats["last"], 4),
                        }
                    ),
                    evidence=[
                        {
                            "type": "metric",
                            "source": "prometheus",
                            "title": f"{metrics_query.metric_type} for {metrics_query.service}",
                            "payload": {"query": promql, "stats": stats},
                        }
                    ],
                    cache_key=cache_key,
                    duration_ms=elapsed_ms(started_at),
                )
        except httpx.TimeoutException as exc:
            result = ToolResult(
                status="timeout",
                data={"query": promql},
                summary=f"Prometheus query timed out for {metrics_query.service}",
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=str(exc),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
            result = ToolResult(
                status="degraded",
                data={"query": promql},
                summary=(
                    f"Prometheus unavailable for {metrics_query.service}; "
                    "continuing with degraded metrics"
                ),
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=str(exc),
            )

        if self.cache and result.status in {"succeeded", "degraded"}:
            self.cache.set(cache_key, result)
        return result

    def _query_range(self, query: MetricsQuery, promql: str) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "query": promql,
            "start": int(query.start.timestamp()),
            "end": int(query.end.timestamp()),
            "step": "30s",
        }
        if self.client is not None:
            response = self.client.get(
                "/api/v1/query_range",
                params=params,
                timeout=self.timeout_seconds,
            )
        else:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as client:
                response = client.get("/api/v1/query_range", params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if payload.get("status") != "success":
            msg = f"prometheus status={payload.get('status')}"
            raise ValueError(msg)
        return payload


def _promql(metric_type: MetricType, service: str) -> str:
    escaped = service.replace("\\", "\\\\").replace('"', '\\"')
    templates: dict[str, str] = {
        "latency": (
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket"
            '{{service="{service}"}}[5m])) by (le))'
        ),
        "error_rate": (
            'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[5m])) '
            '/ clamp_min(sum(rate(http_requests_total{{service="{service}"}}[5m])), 1)'
        ),
        "qps": 'sum(rate(http_requests_total{{service="{service}"}}[5m]))',
        "cpu": 'sum(rate(process_cpu_seconds_total{{service="{service}"}}[5m]))',
        "memory": 'process_resident_memory_bytes{{service="{service}"}}',
        "db_connections": 'db_connections_active{{service="{service}"}}',
        "cache_hit_rate": 'redis_cache_hit_rate{{service="{service}"}}',
    }
    return templates[metric_type].format(service=escaped)


def _extract_values(payload: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for series in payload["data"].get("result", []):
        for raw_value in series.get("values", []):
            value = float(raw_value[1])
            if isfinite(value):
                values.append(value)
    return values


def _series_stats(values: Iterable[float]) -> dict[str, float]:
    ordered = list(values)
    sorted_values = sorted(ordered)
    p95_index = max(0, ceil(len(sorted_values) * 0.95) - 1)
    first = ordered[0]
    last = ordered[-1]
    change_ratio = 0.0 if first == 0 else (last - first) / abs(first)
    return {
        "min": min(ordered),
        "max": max(ordered),
        "avg": fmean(ordered),
        "p95": sorted_values[p95_index],
        "first": first,
        "last": last,
        "change_ratio": change_ratio,
    }
