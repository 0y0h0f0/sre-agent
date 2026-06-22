from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from packages.tools.cache import RequestLocalToolCache, build_cache_key
from packages.tools.git_changes import GitChangeQuery, GitChangeTool
from packages.tools.logs import LogsQuery, LogsTool
from packages.tools.metrics import MetricsQuery, MetricsTool
from packages.tools.traces import TraceQuery, TraceTool

START = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
END = datetime(2026, 6, 1, 0, 10, tzinfo=UTC)


def test_metrics_tool_queries_prometheus_and_summarizes_stats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query_range"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {"values": [[1, "1"], [2, "2"], [3, "4"]]},
                    ]
                },
            },
        )

    client = httpx.Client(base_url="http://prometheus", transport=httpx.MockTransport(handler))
    tool = MetricsTool(base_url="http://prometheus", client=client)

    result = tool.run(
        MetricsQuery(service="checkout", metric_type="error_rate", start=START, end=END)
    )

    assert result.status == "succeeded"
    assert result.data["stats"]["avg"] == 7 / 3
    assert result.data["stats"]["p95"] == 4.0
    assert result.evidence[0]["source"] == "prometheus"


def test_logs_tool_queries_loki_and_aggregates_sample_logs() -> None:
    line = json.dumps(
        {
            "level": "error",
            "event": "http_5xx_after_deploy",
            "message": "5xx spike after deploy",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/loki/api/v1/query_range"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "checkout"},
                            "values": [["1780272000000000000", line]],
                        }
                    ]
                },
            },
        )

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client)

    result = tool.run(LogsQuery(service="checkout", start=START, end=END, keywords=["error"]))

    assert result.status == "succeeded"
    assert result.data["top_error_type"] == "http_5xx_after_deploy"
    assert result.data["samples"][0]["message"] == "5xx spike after deploy"


def test_logs_tool_falls_back_to_unfiltered_logs_when_keywords_match_nothing() -> None:
    inner = json.dumps(
        {
            "level": "info",
            "msg": "access",
            "service": "api-gateway",
            "status": 401,
            "path": "/api/v1/projects",
        }
    )
    line = json.dumps({"log": f"{inner}\n", "stream": "stderr"})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params["query"])
        seen.append(query)
        result = []
        if query == '{service="api-gateway"}':
            result = [
                {
                    "stream": {"service": "api-gateway"},
                    "values": [["1780272000000000000", line]],
                }
            ]
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client)

    result = tool.run(
        LogsQuery(
            service="api-gateway",
            start=START,
            end=END,
            keywords=["5xx", "error", "deploy"],
        )
    )

    assert result.status == "succeeded"
    assert result.data["top_error_type"] == "http_4xx"
    assert result.data["samples"][0]["message"] == "access"
    assert any("|=" in query for query in seen)
    assert '{service="api-gateway"}' in seen


def test_logs_tool_degrades_when_loki_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("loki down", request=request)

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client)

    result = tool.run(LogsQuery(service="checkout", start=START, end=END, keywords=["error"]))

    assert result.status == "degraded"
    assert "Loki unavailable" in result.summary
    assert result.error_message is not None


def test_trace_tool_reads_fixture_and_returns_slow_error_spans(tmp_path) -> None:
    fixture = tmp_path / "traces.json"
    fixture.write_text(
        json.dumps(
            {
                "spans": [
                    {
                        "trace_id": "trace-1",
                        "span_id": "span-1",
                        "service": "checkout",
                        "name": "POST /checkout",
                        "start": "2026-06-01T00:01:00Z",
                        "duration_ms": 900,
                        "status": "error",
                        "downstream_service": "payments",
                    }
                ]
            }
        )
    )

    result = TraceTool(fixture_path=fixture).run(
        TraceQuery(service="checkout", start=START, end=END, min_duration_ms=500)
    )

    assert result.status == "succeeded"
    assert result.data["duration_p95_ms"] == 900
    assert result.data["downstream_services"] == ["payments"]


def test_git_change_tool_reads_fixture_and_filters_window(tmp_path) -> None:
    fixture = tmp_path / "git_changes.json"
    fixture.write_text(
        json.dumps(
            {
                "changes": [
                    {
                        "service": "checkout",
                        "deployed_at": "2026-06-01T00:01:00Z",
                        "commit_sha": "a1b2c3d",
                        "summary": "deploy checkout",
                    },
                    {
                        "service": "billing",
                        "deployed_at": "2026-06-01T00:01:00Z",
                        "commit_sha": "ignored",
                        "summary": "deploy billing",
                    },
                ]
            }
        )
    )

    result = GitChangeTool(fixture_path=fixture).run(
        GitChangeQuery(service="checkout", start=START, end=END)
    )

    assert result.status == "succeeded"
    assert result.data["change_count"] == 1
    assert result.data["changes"][0]["commit_sha"] == "a1b2c3d"


def test_logs_query_rejects_blank_keywords() -> None:
    q = LogsQuery(
        service="checkout",
        start=START,
        end=END,
        keywords=["  error  ", "", "  timeout\n"],
    )
    assert q.keywords == ["error", "timeout"]


def test_logs_query_rejects_invalid_window() -> None:
    import pytest

    with pytest.raises(ValueError, match="end must be after start"):
        LogsQuery(service="checkout", start=END, end=START)


def test_logs_tool_handles_loki_timeout() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("loki timed out", request=request)

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client, timeout_seconds=0.1)

    result = tool.run(LogsQuery(service="checkout", start=START, end=END))

    assert result.status == "timeout"
    assert "timed out" in result.summary
    assert result.error_message is not None


def test_logs_tool_handles_non_json_log_lines() -> None:
    import httpx

    plain = "2026-06-01T00:05:00Z [ERROR] OOM killed process pid=1234"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "checkout"},
                            "values": [["1780272000000000000", plain]],
                        }
                    ]
                },
            },
        )

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client)

    result = tool.run(LogsQuery(service="checkout", start=START, end=END, keywords=["OOM"]))

    assert result.status == "succeeded"
    assert result.data["samples"][0]["message"] == plain


def test_logs_tool_classifies_error_types() -> None:
    import httpx

    cases = [
        ("connection pool exhausted", "connection_error"),
        ("redis cache miss spike", "cache_error"),
        ("OOM killed", "pod_restart"),
        ("http 500 response", "http_5xx"),
        ('{"level":"warn","message":"slow query"}', "warn"),
    ]

    def _make_handler(line_value: str) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "result": [
                            {
                                "stream": {"service": "checkout"},
                                "values": [["1780272000000000000", line_value]],
                            }
                        ]
                    },
                },
            )

        return handler

    for msg, expected_type in cases:
        line = msg if msg.startswith("{") else f'{{"message":"{msg}"}}'

        client = httpx.Client(
            base_url="http://loki", transport=httpx.MockTransport(_make_handler(line))
        )
        tool = LogsTool(base_url="http://loki", client=client)
        result = tool.run(LogsQuery(service="checkout", start=START, end=END))
        assert result.data["top_error_type"] == expected_type, f"msg={msg}"


def test_logs_tool_extracts_signature_from_stack() -> None:
    import httpx

    line = (
        '{"level":"error","message":"something broke",'
        '"stack":"File \\"app.py\\", line 42, in handle\\nValueError: bad input"}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "checkout"},
                            "values": [["1780272000000000000", line]],
                        }
                    ]
                },
            },
        )

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client)
    result = tool.run(LogsQuery(service="checkout", start=START, end=END))

    assert result.data["top_stack_signature"] == "ValueError: bad input"


def test_logs_tool_deduplicates_on_multiple_keywords() -> None:
    import httpx

    line = '{"level":"error","event":"db_timeout","message":"database pool exhausted"}'

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "checkout"},
                            "values": [["1780272000000000000", line]],
                        }
                    ]
                },
            },
        )

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client)
    result = tool.run(
        LogsQuery(
            service="checkout",
            start=START,
            end=END,
            keywords=["database", "exhausted", "timeout"],
        )
    )

    assert result.status == "succeeded"
    assert result.data["line_count"] == 1
    assert call_count == 3


def test_request_local_cache_marks_reused_tool_results() -> None:
    cache = RequestLocalToolCache()
    query = LogsQuery(service="checkout", start=START, end=END)
    key = build_cache_key(
        tool_name="logs",
        service="checkout",
        query=query,
        start=START,
        end=END,
        bucket_seconds=60,
    )
    tool = LogsTool(
        base_url="http://loki",
        client=httpx.Client(
            base_url="http://loki",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"status": "success", "data": {"result": []}}
                )
            ),
        ),
        cache=cache,
    )

    first = tool.run(query)
    second = tool.run(query)

    assert first.cache_key == key
    assert first.cache_hit is False
    assert second.cache_hit is True
