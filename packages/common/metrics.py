"""Prometheus metrics registry for agent observability.

All custom application metrics are defined here and registered at import
time so ``generate_latest()`` in the health router picks them up automatically.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --- Counters ---

diagnosis_total = Counter(
    "agentp_diagnosis_total",
    "Total diagnosis runs completed",
    ["status", "model"],
)

nfa_total = Counter(
    "agentp_nfa_total",
    "Total NFA (Not Actionable Alert) marks",
    ["service"],
)

tool_call_total = Counter(
    "agentp_tool_call_total",
    "Total tool calls",
    ["tool_name", "status"],
)

tool_cache_hit_total = Counter(
    "agentp_tool_cache_hit_total",
    "Tool cache hits",
    ["tool_name"],
)

tool_cache_miss_total = Counter(
    "agentp_tool_cache_miss_total",
    "Tool cache misses",
    ["tool_name"],
)

llm_prompt_tokens_total = Counter(
    "agentp_llm_prompt_tokens_total",
    "Total LLM prompt tokens consumed",
    ["model", "provider"],
)

llm_completion_tokens_total = Counter(
    "agentp_llm_completion_tokens_total",
    "Total LLM completion tokens consumed",
    ["model", "provider"],
)

llm_cache_hit_total = Counter(
    "agentp_llm_cache_hit_total",
    "Provider-level LLM cache hits",
    ["provider"],
)

llm_cache_miss_total = Counter(
    "agentp_llm_cache_miss_total",
    "Provider-level LLM cache misses",
    ["provider"],
)

approval_total = Counter(
    "agentp_approval_total",
    "Total approval decisions",
    ["decision"],
)

# --- Histograms ---

diagnosis_duration_seconds = Histogram(
    "agentp_diagnosis_duration_seconds",
    "Diagnosis duration from alert enqueue to report generation",
    ["status"],
    buckets=[10, 30, 60, 120, 300, 600],
)

approval_response_time_seconds = Histogram(
    "agentp_approval_response_time_seconds",
    "Time from approval request to decision",
    ["decision"],
    buckets=[10, 60, 300, 600, 1800, 3600],
)

tool_call_duration_seconds = Histogram(
    "agentp_tool_call_duration_seconds",
    "Tool call duration",
    ["tool_name"],
    buckets=[0.1, 0.5, 1, 2, 5, 10],
)

llm_call_duration_seconds = Histogram(
    "agentp_llm_call_duration_seconds",
    "LLM API call duration",
    ["model", "provider"],
    buckets=[1, 5, 10, 30, 60],
)

# --- LLM error counter ---

llm_call_errors_total = Counter(
    "agentp_llm_call_errors_total",
    "Total LLM API call errors",
    ["model", "provider", "error_type"],
)

# --- Email send metrics ---

email_send_total = Counter(
    "agentp_email_send_total",
    "Total email send attempts",
    ["notification_type", "status"],
)

email_send_duration_seconds = Histogram(
    "agentp_email_send_duration_seconds",
    "Email send duration (SMTP round-trip)",
    ["notification_type"],
    buckets=[1, 5, 10, 30, 60],
)

# --- M9 feature flag conflict counter ---

m9_feature_flag_conflict_total = Counter(
    "agentp_m9_feature_flag_conflict_total",
    "M9 feature flag conflicts (sub-feature enabled but M9 global gate disabled)",
    ["feature"],
)

llm_incident_diff_total = Counter(
    "agentp_llm_incident_diff_total",
    "LLM incident diff analysis outcomes",
    ["status"],
)

grafana_webhook_ingest_total = Counter(
    "agentp_grafana_webhook_ingest_total",
    "Grafana webhook ingest attempts",
    ["status"],
)

grafana_webhook_ignored_total = Counter(
    "agentp_grafana_webhook_ignored_total",
    "Grafana webhook requests ignored (disabled)",
    ["reason"],
)

# --- M9 LLM Runbook Draft ---

llm_runbook_draft_total = Counter(
    "agentp_llm_runbook_draft_total",
    "LLM runbook draft generation outcomes",
    ["status"],
)

# --- M9 Web Search ---

web_search_requests_total = Counter(
    "agentp_web_search_requests_total",
    "Web search requests made for runbook enrichment",
    ["status", "reason"],
)

web_search_blocked_total = Counter(
    "agentp_web_search_blocked_total",
    "Web search requests blocked by safety rules",
    ["reason"],
)

# --- M9 Tempo Trace ---

tempo_trace_queries_total = Counter(
    "agentp_tempo_trace_queries_total",
    "Tempo trace queries",
    ["status", "mode"],
)

tempo_capability_detected = Gauge(
    "agentp_tempo_capability_detected",
    "Tempo capability detection result (1=supported, 0=unsupported)",
    ["capability"],
)

# --- M9 Semantic Search ---

semantic_search_queries_total = Counter(
    "agentp_semantic_search_queries_total",
    "Semantic runbook search queries",
    ["mode", "status"],
)

embedding_jobs_total = Counter(
    "agentp_embedding_jobs_total",
    "Embedding job outcomes",
    ["provider", "status"],
)

# --- M9 Secret Redaction ---

m9_secret_redaction_failures_total = Counter(
    "agentp_m9_secret_redaction_failures_total",
    "M9 secret redaction failures (blocks external call)",
    ["component"],
)

# --- Gauges ---

active_diagnoses = Gauge(
    "agentp_active_diagnoses",
    "Currently in-flight diagnosis runs",
)

m9_feature_enabled = Gauge(
    "agentp_m9_feature_enabled",
    "M9 feature enabled state (1=enabled, 0=disabled)",
    ["feature"],
)


def _sanitize_label(value: str) -> str:
    """Sanitize a Prometheus label value to match ``[a-zA-Z_][a-zA-Z0-9_]*``."""
    import re

    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", value)
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized


class AgentMetricsCollector:
    """Convenience wrapper around Prometheus metrics for domain recording."""

    @staticmethod
    def record_diagnosis_completed(
        *,
        status: str,
        duration_seconds: float,
        model: str,
        provider: str = "unknown",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        m = _sanitize_label(model)
        p = _sanitize_label(provider)
        diagnosis_total.labels(status=status, model=m).inc()
        diagnosis_duration_seconds.labels(status=status).observe(duration_seconds)
        if prompt_tokens:
            llm_prompt_tokens_total.labels(model=m, provider=p).inc(prompt_tokens)
        if completion_tokens:
            llm_completion_tokens_total.labels(model=m, provider=p).inc(completion_tokens)

    @staticmethod
    def record_approval_decision(
        *, decision: str, response_time_seconds: float
    ) -> None:
        approval_total.labels(decision=decision).inc()
        approval_response_time_seconds.labels(decision=decision).observe(
            response_time_seconds
        )

    @staticmethod
    def record_tool_call(
        *, tool_name: str, status: str, duration_seconds: float, cache_hit: bool
    ) -> None:
        t = _sanitize_label(tool_name)
        tool_call_total.labels(tool_name=t, status=status).inc()
        tool_call_duration_seconds.labels(tool_name=t).observe(duration_seconds)
        if cache_hit:
            tool_cache_hit_total.labels(tool_name=t).inc()
        else:
            tool_cache_miss_total.labels(tool_name=t).inc()

    @staticmethod
    def record_llm_usage(
        *,
        model: str,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
        duration_seconds: float,
        cache_hit: bool,
    ) -> None:
        m = _sanitize_label(model)
        p = _sanitize_label(provider)
        llm_prompt_tokens_total.labels(model=m, provider=p).inc(prompt_tokens)
        llm_completion_tokens_total.labels(model=m, provider=p).inc(completion_tokens)
        llm_call_duration_seconds.labels(model=m, provider=p).observe(duration_seconds)
        if cache_hit:
            llm_cache_hit_total.labels(provider=p).inc()
        else:
            llm_cache_miss_total.labels(provider=p).inc()

    @staticmethod
    def record_nfa(*, service: str) -> None:
        nfa_total.labels(service=_sanitize_label(service)).inc()

    @staticmethod
    def inc_active_diagnoses() -> None:
        active_diagnoses.inc()

    @staticmethod
    def dec_active_diagnoses() -> None:
        active_diagnoses.dec()

    @staticmethod
    def record_llm_error(
        *, model: str, provider: str, error_type: str
    ) -> None:
        llm_call_errors_total.labels(
            model=_sanitize_label(model),
            provider=_sanitize_label(provider),
            error_type=error_type,
        ).inc()

    @staticmethod
    def record_email_send(
        *, notification_type: str, status: str, duration_seconds: float
    ) -> None:
        email_send_total.labels(
            notification_type=notification_type, status=status
        ).inc()
        email_send_duration_seconds.labels(
            notification_type=notification_type
        ).observe(duration_seconds)

    # --- M9-specific recording methods ---

    @staticmethod
    def record_m9_feature_enabled(*, feature: str, enabled: bool) -> None:
        """Set the M9 feature enabled gauge for a specific feature."""
        m9_feature_enabled.labels(feature=feature).set(1 if enabled else 0)

    @staticmethod
    def record_llm_runbook_draft(*, status: str) -> None:
        """Record an LLM runbook draft generation outcome."""
        llm_runbook_draft_total.labels(status=status).inc()

    @staticmethod
    def record_web_search_request(*, status: str, reason: str = "") -> None:
        """Record a web search request outcome."""
        web_search_requests_total.labels(status=status, reason=reason).inc()

    @staticmethod
    def record_web_search_blocked(*, reason: str) -> None:
        """Record a web search request blocked by safety rules."""
        web_search_blocked_total.labels(reason=reason).inc()

    @staticmethod
    def record_tempo_trace_query(*, status: str, mode: str) -> None:
        """Record a Tempo trace query outcome."""
        tempo_trace_queries_total.labels(status=status, mode=mode).inc()

    @staticmethod
    def record_tempo_capability(*, capability: str, supported: bool) -> None:
        """Set Tempo capability detection gauge."""
        tempo_capability_detected.labels(capability=capability).set(
            1 if supported else 0
        )

    @staticmethod
    def record_semantic_search_query(*, mode: str, status: str) -> None:
        """Record a semantic search query outcome."""
        semantic_search_queries_total.labels(mode=mode, status=status).inc()

    @staticmethod
    def record_embedding_job(*, provider: str, status: str) -> None:
        """Record an embedding job outcome."""
        embedding_jobs_total.labels(provider=provider, status=status).inc()

    @staticmethod
    def record_secret_redaction_failure(*, component: str) -> None:
        """Record a secret redaction failure that blocks an external call."""
        m9_secret_redaction_failures_total.labels(component=component).inc()
