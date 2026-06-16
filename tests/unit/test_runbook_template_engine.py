"""Tests for deterministic RunbookTemplateEngine — PR 6.1."""

from __future__ import annotations

from packages.discovery.models import CapabilityMatrix, DiscoveryResult, MetricMapping
from packages.discovery.runbook_template_engine import (
    INCIDENT_TYPE_TO_TEMPLATE,
    RunbookTemplateContext,
    RunbookTemplateEngine,
)


def _make_capability(
    service_name: str = "test-service",
    *,
    metrics: bool = True,
    logs: bool = True,
    traces: bool = True,
    k8s: bool = True,
    gaps: list[str] | None = None,
) -> CapabilityMatrix:
    return CapabilityMatrix(
        service_name=service_name,
        metrics_available=metrics,
        logs_available=logs,
        traces_available=traces,
        k8s_accessible=k8s,
        capability_gaps=gaps or [],
    )


def _make_latency_mapping() -> MetricMapping:
    return MetricMapping(
        semantic_type="latency",
        metric_name="http_request_duration_seconds_bucket",
        status="available",
        confidence=0.95,
        promql_template=(
            'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket'
            '{service="test-service"}[5m])) by (le, service))'
        ),
        service_label="service",
        required_labels=["le"],
    )


def _make_error_rate_mapping() -> MetricMapping:
    return MetricMapping(
        semantic_type="error_rate",
        metric_name="http_request_total",
        status="available",
        confidence=0.90,
        promql_template=(
            'clamp_min(sum(rate(http_request_total{status=~"5..",service="test-service"}[5m]))'
            ' / sum(rate(http_request_total{service="test-service"}[5m])), 0)'
        ),
        service_label="service",
        required_labels=["status"],
    )


def _make_qps_mapping() -> MetricMapping:
    return MetricMapping(
        semantic_type="qps",
        metric_name="http_request_total",
        status="available",
        confidence=0.92,
        promql_template=(
            'sum(rate(http_request_total{service="test-service"}[5m])) by (service)'
        ),
        service_label="service",
        required_labels=[],
    )


def _make_cpu_mapping() -> MetricMapping:
    return MetricMapping(
        semantic_type="cpu_throttle",
        metric_name="container_cpu_cfs_throttled_seconds_total",
        status="available",
        confidence=0.88,
        promql_template=(
            'rate(container_cpu_cfs_throttled_seconds_total{service="test-service"}[5m])'
        ),
        service_label="service",
        required_labels=["container"],
    )


def _make_disk_mapping() -> MetricMapping:
    return MetricMapping(
        semantic_type="disk_avail",
        metric_name="node_filesystem_avail_bytes",
        status="available",
        confidence=0.85,
        promql_template='node_filesystem_avail_bytes{mountpoint=~"/|/data"}',
        service_label="service",
        required_labels=["mountpoint"],
    )


class TestRunbookTemplateContext:
    def test_context_builds_from_capability(self):
        capability = _make_capability("svc-a")
        ctx = RunbookTemplateContext(
            service_name="svc-a",
            incident_type="high_latency",
            title="Test Runbook",
            severity="P1",
            owner="test-owner",
            capability=capability,
        )

        assert ctx.service_name == "svc-a"
        assert ctx.incident_type == "high_latency"
        assert ctx.metrics_available is True
        assert ctx.logs_available is True
        assert ctx.traces_available is True
        assert ctx.k8s_accessible is True

    def test_context_with_no_capability_defaults_false(self):
        ctx = RunbookTemplateContext(
            service_name="svc-b",
            incident_type="generic",
            title="Fallback",
            severity="P2",
            owner="agent",
            capability=None,
        )

        assert ctx.metrics_available is False
        assert ctx.logs_available is False
        assert ctx.traces_available is False
        assert ctx.k8s_accessible is False
        assert ctx.capability_gaps == []

    def test_context_has_latency_metric_detection(self):
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="high_latency",
            title="T",
            severity="P2",
            owner="o",
            metric_mappings=[_make_latency_mapping()],
        )
        d = ctx.to_dict()
        assert d["has_latency_metric"] is True
        assert d["has_error_rate_metric"] is False

    def test_context_with_capability_gaps(self):
        capability = _make_capability(
            "svc",
            logs=False,
            traces=False,
            gaps=["No log collection configured"],
        )
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="high_error_rate",
            title="T",
            severity="P2",
            owner="o",
            capability=capability,
        )
        assert ctx.logs_available is False
        assert ctx.traces_available is False
        assert "No log collection configured" in ctx.capability_gaps

    def test_context_extra_merged(self):
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="x",
            title="T",
            severity="P2",
            owner="o",
            extra_context={"custom_field": "custom_value"},
        )
        d = ctx.to_dict()
        assert d["custom_field"] == "custom_value"


class TestRunbookTemplateEngine:
    def test_engine_loads_all_expected_templates(self):
        engine = RunbookTemplateEngine()
        templates = engine.list_templates()
        assert "generic_incident.md.j2" in templates
        assert "high_latency.md.j2" in templates
        assert "high_error_rate.md.j2" in templates
        assert "resource_saturation.md.j2" in templates
        assert "dependency_failure.md.j2" in templates

    def test_render_generic_template(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="my-service",
            incident_type="unknown_type",
            title="My Runbook",
            severity="P1",
            owner="sre-team",
            metric_mappings=[_make_latency_mapping(), _make_error_rate_mapping()],
        )
        result = engine.render(ctx)  # falls back to generic
        assert "---" in result
        assert "service: my-service" in result
        assert "severity: P1" in result
        assert "owner: sre-team" in result
        assert "# My Runbook" in result

    def test_render_generic_no_metrics(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="bare-service",
            incident_type="unknown",
            title="Bare Runbook",
            severity="P2",
            owner="agent",
        )
        result = engine.render(ctx)
        assert "# Bare Runbook" in result
        assert "### Metrics" not in result

    def test_template_auto_selection_high_latency(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="high_latency",
            title="Latency Runbook",
            severity="P2",
            owner="o",
            capability=_make_capability("svc"),
            metric_mappings=[_make_latency_mapping(), _make_qps_mapping()],
        )
        result = engine.render(ctx)
        assert "High latency is detected" in result
        assert "## Detection" in result
        assert "## Evidence To Collect" in result
        assert "### Latency Analysis" in result

    def test_high_latency_hides_trace_section_when_traces_unavailable(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="high_latency",
            title="T",
            severity="P2",
            owner="o",
            capability=_make_capability("svc", traces=False),
            metric_mappings=[_make_latency_mapping()],
        )
        result = engine.render(ctx)
        assert "### Trace Analysis" not in result

    def test_high_latency_shows_trace_section_when_traces_available(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="high_latency",
            title="T",
            severity="P2",
            owner="o",
            capability=_make_capability("svc", traces=True),
            metric_mappings=[_make_latency_mapping()],
        )
        result = engine.render(ctx)
        assert "### Trace Analysis" in result

    def test_render_high_error_rate_template(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="api-gateway",
            incident_type="high_error_rate",
            title="Error Rate Runbook",
            severity="P1",
            owner="platform",
            capability=_make_capability("api-gateway"),
            metric_mappings=[_make_error_rate_mapping(), _make_qps_mapping()],
        )
        result = engine.render(ctx)
        assert "High error rate is detected" in result
        assert "### Error Rate Analysis" in result

    def test_high_error_rate_hides_k8s_section_when_no_k8s(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="high_error_rate",
            title="T",
            severity="P2",
            owner="o",
            capability=_make_capability("svc", k8s=False),
            metric_mappings=[_make_error_rate_mapping()],
        )
        result = engine.render(ctx)
        assert "### Deployment Check" not in result

    def test_render_resource_saturation_template(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="worker-pool",
            incident_type="resource_saturation",
            title="Resource Saturation Runbook",
            severity="P1",
            owner="infra",
            capability=_make_capability("worker-pool"),
            metric_mappings=[_make_cpu_mapping(), _make_disk_mapping()],
        )
        result = engine.render(ctx)
        assert "Resource saturation is detected" in result
        assert "### Resource Metrics" in result
        assert "CPU Throttling Rate" in result

    def test_render_dependency_failure_template(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="order-service",
            incident_type="dependency_failure",
            title="Dependency Failure Runbook",
            severity="P1",
            owner="backend",
            capability=_make_capability("order-service"),
            metric_mappings=[_make_error_rate_mapping(), _make_latency_mapping()],
        )
        result = engine.render(ctx)
        assert "Dependency failure is detected" in result
        assert "### Dependency Metrics" in result

    def test_template_fallback_on_missing_template(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="high_latency",
            title="T",
            severity="P2",
            owner="o",
        )
        result = engine.render(ctx, template_name="nonexistent.md.j2")
        assert "# T" in result
        assert "## Detection" in result

    def test_template_does_not_invent_metrics(self):
        """Output must not reference fake metric names without metric mappings."""
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="high_latency",
            title="T",
            severity="P2",
            owner="o",
        )
        result = engine.render(ctx)
        assert "http_request_duration_seconds_bucket" not in result

    def test_render_for_service_finds_matching_capability(self):
        engine = RunbookTemplateEngine()
        discovery = DiscoveryResult(
            run_id="run-1",
            services=[],
            capability_matrix=[
                _make_capability("order-service", metrics=True, logs=False),
                _make_capability("payment-service", metrics=True, logs=True),
            ],
            metric_mappings=[_make_latency_mapping()],
            status="degraded",
        )
        result = engine.render_for_service(
            discovery=discovery,
            service_name="order-service",
            incident_type="high_latency",
        )
        assert "order-service" in result
        assert "### Latency Analysis" in result
        assert "### Log Analysis" not in result  # logs disabled for this service

    def test_render_for_service_no_capability_fallback(self):
        engine = RunbookTemplateEngine()
        discovery = DiscoveryResult(
            run_id="run-1",
            services=[],
            capability_matrix=[],
            metric_mappings=[],
            status="degraded",
        )
        result = engine.render_for_service(
            discovery=discovery,
            service_name="unknown-svc",
            incident_type="generic",
        )
        assert "unknown-svc" in result
        assert "## Detection" in result

    def test_all_incident_types_map_to_template(self):
        for incident_type in INCIDENT_TYPE_TO_TEMPLATE:
            assert isinstance(INCIDENT_TYPE_TO_TEMPLATE[incident_type], str)

    def test_front_matter_includes_all_required_fields(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="high_latency",
            title="Test Runbook",
            severity="P3",
            owner="test-team",
            metric_mappings=[_make_latency_mapping()],
        )
        result = engine.render(ctx)
        assert "service: svc" in result
        assert "incident_type: high_latency" in result
        assert "severity: P3" in result
        assert "owner: test-team" in result
        assert "generated_by: runbook_template_engine" in result

    def test_capability_gaps_rendered_when_present(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="resource_saturation",
            title="T",
            severity="P2",
            owner="o",
            capability=_make_capability(
                "svc",
                gaps=[
                    "No disk metrics available",
                    "Trace collection not configured",
                ],
            ),
        )
        result = engine.render(ctx)
        assert "### Known Capability Gaps" in result
        assert "No disk metrics available" in result
        assert "Trace collection not configured" in result

    def test_capability_gaps_not_rendered_when_empty(self):
        engine = RunbookTemplateEngine()
        ctx = RunbookTemplateContext(
            service_name="svc",
            incident_type="high_error_rate",
            title="T",
            severity="P2",
            owner="o",
            capability=_make_capability("svc", gaps=[]),
            metric_mappings=[_make_error_rate_mapping()],
        )
        result = engine.render(ctx)
        assert "### Known Capability Gaps" not in result

    def test_list_templates_returns_names(self):
        engine = RunbookTemplateEngine()
        names = engine.list_templates()
        assert "generic_incident.md.j2" in names
        assert all(not n.startswith("/") for n in names)

    def test_get_template_returns_jinja2_template(self):
        engine = RunbookTemplateEngine()
        tmpl = engine.get_template("generic_incident.md.j2")
        assert tmpl is not None
        rendered = tmpl.render({"service_name": "test", "title": "Test", "today": "2026-01-01"})
        assert "test" in rendered
