"""Prometheus metrics tool."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
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
    # Phase 2.4 fault catalog expansion.
    "cpu_throttle",
    "disk_avail",
    "cert_expiry_days",
    "dns_error_rate",
    "queue_lag",
    "rate_limit_hits",
    "slo_burn_rate",
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
        service_label: str = "service",
        step_seconds: int = 30,
        max_window_seconds: int = 3600,
        max_shards: int = 6,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.cache = cache
        self.service_label = service_label
        self.step_seconds = step_seconds
        self.max_window_seconds = max_window_seconds
        self.max_shards = max_shards

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

        promql_candidates = _promql_candidates(
            metrics_query.metric_type,
            metrics_query.service,
            self.service_label,
        )
        promql = promql_candidates[0]
        attempted_queries: list[str] = []
        try:
            values: list[float] = []
            for candidate in promql_candidates:
                attempted_queries.append(candidate)
                payload = self._query_range(metrics_query, candidate)
                values = _extract_values(payload)
                promql = candidate
                if values:
                    break
            if not values:
                result = ToolResult(
                    status="degraded",
                    data={
                        "query": promql,
                        "queries": attempted_queries,
                        "stats": None,
                        "sample_count": 0,
                    },
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
                    data={
                        "query": promql,
                        "queries": attempted_queries,
                        "stats": stats,
                        "sample_count": len(values),
                    },
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
                data={"query": promql, "queries": attempted_queries},
                summary=f"Prometheus query timed out for {metrics_query.service}",
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=str(exc),
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
            result = ToolResult(
                status="degraded",
                data={"query": promql, "queries": attempted_queries},
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
        # Split large windows into bounded shards so a single request never asks
        # Prometheus for an unbounded series (Phase 2.1 query safety). Values
        # from all shards are merged into one Prometheus-shaped payload.
        shards, step = self._plan_shards(query.start, query.end)
        merged: list[float] = []
        for shard_start, shard_end in shards:
            payload = self._query_shard(promql, shard_start, shard_end, step)
            merged.extend(_extract_values(payload))
        return {"data": {"result": [{"values": [[0, value] for value in merged]}]}}

    def _plan_shards(
        self, start: datetime, end: datetime
    ) -> tuple[list[tuple[datetime, datetime]], int]:
        """Plan shards covering the *entire* window.

        When the window needs more than ``max_shards`` shards we widen each
        shard and coarsen the step rather than truncating — covering the whole
        window with bounded request count and points-per-request. Truncating
        would silently drop data outside the first N shards and mislabel the
        result as a complete success.
        """
        total = (end - start).total_seconds()
        shard_window = float(self.max_window_seconds)
        step = self.step_seconds
        if total > shard_window:
            needed = ceil(total / shard_window)
            if needed > self.max_shards:
                shard_window = total / self.max_shards
                # Coarsen the step proportionally to keep points-per-shard bounded.
                step = max(step, ceil(step * shard_window / self.max_window_seconds))
        shards: list[tuple[datetime, datetime]] = []
        cursor = start
        span = timedelta(seconds=shard_window)
        while cursor < end:
            shard_end = min(cursor + span, end)
            shards.append((cursor, shard_end))
            cursor = shard_end
        return shards, step

    def _query_shard(
        self, promql: str, start: datetime, end: datetime, step: int
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "query": promql,
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "step": f"{step}s",
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


def _promql(metric_type: MetricType, service: str, service_label: str = "service") -> str:
    escaped = service.replace("\\", "\\\\").replace('"', '\\"')
    label = service_label
    sel = f'{label}="{escaped}"'
    templates: dict[str, str] = {
        "latency": (
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket"
            f"{{{sel}}}[5m])) by (le))"
        ),
        "error_rate": (
            f'sum(rate(http_requests_total{{{sel},status=~"5.."}}[5m])) '
            f"/ clamp_min(sum(rate(http_requests_total{{{sel}}}[5m])), 1)"
        ),
        "qps": f"sum(rate(http_requests_total{{{sel}}}[5m]))",
        "cpu": f"sum(rate(process_cpu_seconds_total{{{sel}}}[5m]))",
        "memory": f"demo_process_resident_memory_bytes{{{sel}}}",
        "db_connections": f"db_connections_active{{{sel}}}",
        "cache_hit_rate": f"redis_cache_hit_rate{{{sel}}}",
        # Phase 2.4 fault catalog.
        "cpu_throttle": (
            f"sum(rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[5m])) "
            f"/ clamp_min(sum(rate(container_cpu_cfs_periods_total{{{sel}}}[5m])), 1)"
        ),
        "disk_avail": (
            f"min(node_filesystem_avail_bytes{{{sel}}}) "
            f"/ clamp_min(min(node_filesystem_size_bytes{{{sel}}}), 1)"
        ),
        "cert_expiry_days": f"min(tls_cert_expiry_seconds{{{sel}}}) / 86400",
        "dns_error_rate": (
            f'sum(rate(coredns_dns_responses_total{{{sel},rcode!="NOERROR"}}[5m])) '
            f"/ clamp_min(sum(rate(coredns_dns_responses_total{{{sel}}}[5m])), 1)"
        ),
        "queue_lag": f"max(kafka_consumergroup_lag{{{sel}}})",
        "rate_limit_hits": f"sum(rate(rate_limit_hits_total{{{sel}}}[5m]))",
        "slo_burn_rate": (
            f'sum(rate(http_requests_total{{{sel},status=~"5.."}}[1h])) '
            f"/ clamp_min(sum(rate(http_requests_total{{{sel}}}[1h])), 1) / 0.001"
        ),
    }
    return templates[metric_type]


def _promql_candidates(
    metric_type: MetricType, service: str, service_label: str = "service"
) -> list[str]:
    candidates = [_promql(metric_type, service, service_label)]
    for selector in _service_selector_candidates(service, service_label):
        candidates.extend(_promql_templates_for_selector(metric_type, selector))
    return _dedupe(candidates)


def _promql_templates_for_selector(metric_type: MetricType, selector: str) -> list[str]:
    templates: dict[str, list[str]] = {
        "latency": [
            (
                "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket"
                f"{{{selector}}}[5m])) by (le))"
            ),
            (
                "histogram_quantile(0.95, sum(rate(grpc_server_request_duration_seconds_bucket"
                f"{{{selector}}}[5m])) by (le))"
            ),
        ],
        "error_rate": [
            (
                f'sum(rate(http_requests_total{{{selector},status=~"5.."}}[5m])) '
                f"/ clamp_min(sum(rate(http_requests_total{{{selector}}}[5m])), 1)"
            ),
            (
                f'sum(rate(http_server_requests_seconds_count{{{selector},status=~"5.."}}[5m])) '
                f"/ clamp_min(sum(rate(http_server_requests_seconds_count{{{selector}}}[5m])), 1)"
            ),
            f"clamp_min(1 - avg(up{{{selector}}}), 0)",
        ],
        "qps": [
            f"sum(rate(http_requests_total{{{selector}}}[5m]))",
            f"sum(rate(http_server_requests_seconds_count{{{selector}}}[5m]))",
            f"sum(rate(grpc_server_requests_total{{{selector}}}[5m]))",
        ],
        "cpu": [
            f"sum(rate(process_cpu_seconds_total{{{selector}}}[5m]))",
            f"sum(rate(container_cpu_usage_seconds_total{{{selector}}}[5m]))",
        ],
        "memory": [
            f"demo_process_resident_memory_bytes{{{selector}}}",
            f"process_resident_memory_bytes{{{selector}}}",
            f"container_memory_working_set_bytes{{{selector}}}",
        ],
        "db_connections": [
            f"db_connections_active{{{selector}}}",
            f"db_pool_in_use{{{selector}}}",
            f"db_pool_open_connections{{{selector}}}",
            f"hikaricp_connections_active{{{selector}}}",
        ],
        "cache_hit_rate": [
            f"redis_cache_hit_rate{{{selector}}}",
            f"cache_hit_rate{{{selector}}}",
        ],
        "cpu_throttle": [
            (
                f"sum(rate(container_cpu_cfs_throttled_periods_total{{{selector}}}[5m])) "
                f"/ clamp_min(sum(rate(container_cpu_cfs_periods_total{{{selector}}}[5m])), 1)"
            ),
        ],
        "disk_avail": [
            (
                f"min(node_filesystem_avail_bytes{{{selector}}}) "
                f"/ clamp_min(min(node_filesystem_size_bytes{{{selector}}}), 1)"
            ),
        ],
        "cert_expiry_days": [f"min(tls_cert_expiry_seconds{{{selector}}}) / 86400"],
        "dns_error_rate": [
            (
                f'sum(rate(coredns_dns_responses_total{{{selector},rcode!="NOERROR"}}[5m])) '
                f"/ clamp_min(sum(rate(coredns_dns_responses_total{{{selector}}}[5m])), 1)"
            ),
        ],
        "queue_lag": [f"max(kafka_consumergroup_lag{{{selector}}})"],
        "rate_limit_hits": [f"sum(rate(rate_limit_hits_total{{{selector}}}[5m]))"],
        "slo_burn_rate": [
            (
                f'sum(rate(http_requests_total{{{selector},status=~"5.."}}[1h])) '
                f"/ clamp_min(sum(rate(http_requests_total{{{selector}}}[1h])), 1) / 0.001"
            ),
        ],
    }
    return templates[metric_type]


def _service_selector_candidates(service: str, service_label: str) -> list[str]:
    labels = [
        service_label,
        "service",
        "app",
        "job",
        "container",
        "deployment",
        "app_kubernetes_io_name",
        "kubernetes_pod_name",
        "pod",
    ]
    escaped = service.replace("\\", "\\\\").replace('"', '\\"')
    regex = _service_alias_regex(service)
    selectors: list[str] = []
    for label in _dedupe(labels):
        selectors.append(f'{label}="{escaped}"')
        selectors.append(f'{label}=~"{regex}"')
    return _dedupe(selectors)


def _service_alias_regex(service: str) -> str:
    escaped = re.escape(service).replace("\\-", "-")
    return f"{escaped}($|[-_].*)"


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


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
