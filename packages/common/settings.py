"""Runtime settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://sre:sre@localhost:5432/sre"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    otel_collector_url: str = "http://localhost:4318"
    trace_fixture_path: str = "demo/faults/traces.json"
    git_changes_fixture_path: str = "demo/faults/git_changes.json"
    # Service dependency graph for cascading-failure analysis (roadmap Phase 1.4).
    service_topology_path: str = "demo/topology.json"
    tool_timeout_seconds: float = Field(default=2.0, gt=0)

    # --- Tool-layer productionization (roadmap Phase 2) ---
    # Label key used to select a service in PromQL/LogQL. Not every environment
    # names it "service"; keep it configurable (Phase 2.1).
    metrics_service_label: str = "service"
    logs_service_label: str = "service"
    # Query-safety caps. Large windows are sharded so a single request never
    # asks Prometheus/Loki for an unbounded series (Phase 2.1).
    metrics_step_seconds: int = Field(default=30, gt=0)
    metrics_max_window_seconds: int = Field(default=3600, gt=0)
    metrics_max_shards: int = Field(default=6, ge=1)
    # Trace backend: disabled | fixture | jaeger | tempo. Default keeps tests
    # deterministic and local dev offline.
    # M9 adds "disabled" and "tempo"; M8 supported "fixture" and "jaeger".
    trace_backend: str = "fixture"
    # M9: TRACE_ENABLED gates the entire trace tool. When false, TraceTool is
    # degraded regardless of trace_backend value.
    # Default True preserves backward-compatible local/dev behavior.
    trace_enabled: bool = True
    jaeger_url: str = "http://localhost:16686"
    tempo_url: str = "http://localhost:3200"
    # Deployment backend: fixture | github | argocd (Phase 2.1).
    deployment_backend: str = "fixture"
    github_api_url: str = "https://api.github.com"
    github_repo: str | None = None
    github_token: SecretStr | None = None
    argocd_url: str = "http://localhost:8080"
    argocd_token: SecretStr | None = None
    # Kubernetes read-only diagnosis: fixture | live (Phase 2.2). MVP scope is
    # read-only; "live" requires an explicitly configured cluster and never
    # performs production writes. Writes only ever emit dry-run suggestions.
    k8s_backend: str = "fixture"
    k8s_fixture_path: str = "demo/faults/k8s.json"
    k8s_namespace: str = "default"
    # Executor backend: fixture | live (Phase 2.5). "live" performs real K8s
    # mutations (restart, scale, rollback) and requires EXECUTOR_BACKEND=live
    # plus the full guardrail -> approval -> second-confirmation chain.
    executor_backend: str = "fixture"
    executor_timeout_seconds: float = Field(default=30.0, gt=0)
    executor_k8s_namespace: str = "default"
    # Database read-only diagnosis: fixture | live (Phase 2.3). "live" must use a
    # read-only account; the tool also forces SET TRANSACTION READ ONLY and a
    # statement timeout, and rejects any non-SELECT statement.
    db_diagnostics_backend: str = "fixture"
    db_diagnostics_fixture_path: str = "demo/faults/db_diagnostics.json"
    db_diagnostics_url: str | None = None
    db_diagnostics_statement_timeout_ms: int = Field(default=2000, gt=0)
    embedding_provider: str = "fake"
    # 4.4 Multi-language embedding
    embedding_bge_zh_url: str = "http://localhost:8083"
    embedding_text2vec_url: str = "http://localhost:8084"

    # 4.1 Hybrid search
    runbook_hybrid_search_enabled: bool = True
    runbook_hybrid_alpha_keyword: float = Field(default=0.65, gt=0, le=1)
    runbook_hybrid_alpha_nl: float = Field(default=0.35, gt=0, le=1)

    # 4.2 Reranker
    reranker_provider: str = "fake"
    reranker_cohere_api_key: SecretStr | None = None
    reranker_cohere_model: str = "rerank-english-v3.0"
    reranker_jina_base_url: str = "http://localhost:8081/v1"
    reranker_jina_model: str = "jina-reranker-v2-base-multilingual"
    reranker_bge_base_url: str = "http://localhost:8082"
    reranker_bge_model: str = "BAAI/bge-reranker-v2-m3"

    # LLM provider abstraction (roadmap Phase 1.1).
    # Provider selects the adapter: fake | vllm | openai | deepseek | anthropic.
    llm_provider: str = "fake"
    llm_model: str = "fake-diagnosis-model"
    llm_base_url: str = "http://localhost:8001/v1"
    # SecretStr keeps the key out of repr()/str()/tracebacks; unwrap with
    # .get_secret_value() at the point of use (see llm/factory.py).
    llm_api_key: SecretStr | None = None
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_tokens: int = Field(default=512, gt=0)
    llm_temperature: float = Field(default=0.1, ge=0)
    llm_reasoning_enabled: bool = False
    llm_reasoning_effort: str = "medium"
    # Per-node reasoning-depth layering (roadmap Phase 1.2). Comma-separated node
    # names that use deep reasoning when llm_reasoning_enabled is true.
    llm_reasoning_nodes: str = "diagnose,diagnose_synthesize"
    # Phase 2: when True, the diagnose node runs 3 specialist sub-agents
    # (metrics / logs / traces) plus a synthesizer instead of a single
    # monolithic LLM call.  Default off preserves existing behaviour.
    llm_multi_perspective_enabled: bool = False
    token_budget_total: int = Field(default=32_000, gt=0)

    # --- M0: Production Safety Foundation ---
    # Environment profile: local (default) keeps demo/CI compatible;
    # production enables safety defaults (LLM disabled, executor fixture).
    app_env: str = "local"

    # Automation level controls whether proposals are auto-applied.
    # off=record only, propose=record+propose, supervised=auto-apply
    # high-confidence proposals, autopilot=lower threshold.
    automation_level: str = "supervised"

    # Discovery enables automatic backend (Prometheus/Loki/Jaeger) detection.
    # local default true; production default false (set by model_validator).
    discovery_enabled: bool = True
    # Allow operator-initiated manual discovery rerun via API.
    discovery_manual_rerun_enabled: bool = True
    # How aggressively discovery proposals are applied.
    # inherit=use automation_level, propose=always propose, supervised=review required.
    discovery_apply_mode: str = "inherit"

    # Runbook template generation engine.
    runbook_template_generation_enabled: bool = True
    # LLM-based runbook draft generation (Phase 0-8 default off).
    runbook_llm_generation_enabled: bool = False
    # Web search for runbook enrichment (Phase 0-8 default off).
    runbook_web_search_enabled: bool = False

    # --- M7: Deterministic Runbook Feedback ---
    # Minimum incidents of the same (service, fault_type) before feedback triggers.
    runbook_amendment_min_incidents: int = Field(default=5, gt=0)
    # Cooldown in days between successive amendment drafts for the same
    # (service, fault_type) pair.
    runbook_amendment_cooldown_days: int = Field(default=7, gt=0)

    # Alert ingestion source.
    # webhook=POST /api/alerts only, poll=Alertmanager poll only,
    # both=webhook + poll, none=no ingestion.
    alert_source: str = "webhook"

    # --- Alertmanager Poll Configuration ---
    alertmanager_url: str = "http://localhost:9093"
    alertmanager_read_token: SecretStr | None = None
    alert_poll_interval_seconds: int = Field(default=30, gt=0)
    # Redis lock TTL must be >= poll_timeout + processing_budget + safety_margin.
    alert_poll_lock_ttl_seconds: int = Field(default=60, gt=0)
    alert_poll_timeout_seconds: int = Field(default=20, gt=0)
    # Active fingerprint must be missing longer than this to infer resolved.
    alert_poll_resolved_grace_period_seconds: int = Field(default=120, gt=0)
    # Number of consecutive missing rounds before resolved inference.
    alert_poll_resolved_missing_rounds: int = Field(default=3, gt=0)
    # Receiver filter (pipe-separated receiver names).
    alert_poll_receiver_filter: str = ""
    # Comma-separated Alertmanager matcher expressions
    # (e.g. 'severity=~"critical|warning",namespace=~"prod"').
    alert_poll_filter_matchers: str = ""
    # Comma-separated namespace allowlist for poll scope.
    alert_poll_namespace_allowlist: str = ""
    # Comma-separated service allowlist for poll scope.
    alert_poll_service_allowlist: str = ""
    alert_poll_max_alerts_per_round: int = Field(default=200, gt=0)
    alert_poll_max_new_incidents_per_round: int = Field(default=20, gt=0)
    alert_poll_max_incidents_per_service_per_minute: int = Field(default=5, gt=0)

    # --- Backend URL Safety ---
    # Comma-separated host patterns allowed for internal cluster DNS
    # (e.g. '*.svc,*.svc.cluster.local,prometheus.monitoring.svc').
    backend_url_allowlist: str = ""

    # --- M9: Controlled Enhancements (all default-off in production) ---
    # Global feature gate. When false, forces all M9 sub-capabilities off
    # regardless of their individual settings.
    m9_extensions_enabled: bool = False

    # --- M9: Web Search Safety Settings (PR 9.4) ---
    # Web search provider: disabled | fake | exa
    runbook_web_search_provider: str = "disabled"
    runbook_web_search_timeout_seconds: int = Field(default=10, gt=0)
    runbook_web_search_max_results: int = Field(default=5, gt=0, le=20)
    runbook_web_search_require_https: bool = True
    # Comma-separated allowed domain patterns (e.g. '*.docs.example.com,wikipedia.org').
    # In production, this MUST be non-empty for web search to be enabled.
    runbook_web_search_allowed_domains: str = ""
    # Comma-separated blocked domain patterns (overrides allowed).
    runbook_web_search_blocked_domains: str = ""
    runbook_web_search_max_content_bytes: int = Field(default=1_048_576, gt=0)
    runbook_web_search_cache_ttl_seconds: int = Field(default=86400, gt=0)
    runbook_web_search_max_redirects: int = Field(default=3, ge=0, le=10)

    # --- M9: Grafana Webhook Settings (PR 9.7) ---
    grafana_webhook_secret_ref: str = ""
    grafana_webhook_max_bytes: int = Field(default=256_000, gt=0)

    # M9 sub-feature gates — each default-off.
    # LLM-based runbook draft generation (PR 9.2).
    # (runbook_llm_generation_enabled already declared above, preserved for M8 compat)
    # LLM incident vs runbook diff analysis (PR 9.3).
    llm_incident_diff_enabled: bool = False
    min_incident_diff_evidence_refs: int = Field(default=5, ge=1)
    # M9 Tempo endpoint auto-discovery (PR 9.6). Production never auto-publishes.
    tempo_discovery_enabled: bool = False
    # M9 Grafana unified alerting webhook ingest (PR 9.7).
    grafana_alert_ingest_enabled: bool = False
    # M9 Semantic (vector) runbook search (PR 9.8).
    semantic_runbook_search_enabled: bool = False
    # M9 External embedding provider (PR 9.9). Requires SEMANTIC_RUNBOOK_SEARCH_ENABLED
    # and EMBEDDING_PROVIDER=external.
    external_embedding_provider_enabled: bool = False
    # Double opt-in for external cloud LLM (PR 9.2, 9.3). When false, only
    # locally-hosted LLM providers may be used even if M9 + LLM features are on.
    llm_external_provider_allowed: bool = False

    # Rollback state for total M9 revert. Pre-populate before enabling M9 so the
    # environment can be restored to its pre-M9 trace configuration.
    pre_m9_trace_backend: str = ""
    pre_m9_trace_enabled: str = ""

    @model_validator(mode='before')
    @classmethod
    def _apply_production_safety_defaults(cls, data: object) -> object:
        """Apply production-safe defaults when APP_ENV=production.

        Only overrides fields that were NOT explicitly set by the user,
        so explicit env vars / .env entries always take precedence.
        """
        if not isinstance(data, dict):
            return data
        app_env = data.get('app_env', 'local')
        if app_env != 'production':
            return data
        if 'llm_provider' not in data:
            data['llm_provider'] = 'disabled'
        if 'discovery_enabled' not in data:
            data['discovery_enabled'] = False
        return data

    @model_validator(mode='after')
    def _validate_m9_trace_backend(self) -> Settings:
        """Validate M9 TRACE_BACKEND enum values.

        TRACE_BACKEND must be one of: disabled, fixture, jaeger, tempo.
        Conflict detection (M9 disabled + tempo, fixture in production)
        is handled by feature_flags.resolve_m9_feature_flags().
        """
        valid_backends = frozenset({'disabled', 'fixture', 'jaeger', 'tempo'})
        if self.trace_backend not in valid_backends:
            raise ValueError(
                f"TRACE_BACKEND must be one of {sorted(valid_backends)}, "
                f"got '{self.trace_backend}'"
            )
        return self

    # --- Email notifications (roadmap Phase 3) ---
    smtp_host: str = ""
    smtp_port: int = Field(default=587, gt=0)
    smtp_tls_mode: str = "auto"
    smtp_timeout_seconds: float = Field(default=30.0, gt=0)
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "sre-agent@example.local"
    sre_email_list: str = ""
    web_base_url: str = "http://localhost:5173"
    notification_timezone: str = "UTC"
    token_budget_prompt: int = Field(default=12_000, gt=0)
    token_cache_enabled: bool = True
    celery_task_always_eager: bool = False

    # --- Phase 5: Memory & Continuous Learning ---
    nfa_auto_suppress_threshold: int = Field(default=3, gt=0)
    nfa_reset_days: int = Field(default=30, gt=0)
    cross_incident_similarity_threshold: float = Field(default=0.7, gt=0, le=1)
    cross_incident_max_results: int = Field(default=5, ge=1)

    # --- Phase 6: Collaboration & Approval Enhancement ---
    approval_auto_approve_minutes: int = Field(default=0, ge=0)
    approval_auto_approve_max_risk: str = "L2"
    rate_limit_max_requests: int = Field(default=10, ge=1)
    rate_limit_window_seconds: int = Field(default=60, ge=1)

    # --- Phase 7: Ops & Engineering ---
    # 7.1 Auth
    api_key_auth_enabled: bool = True
    api_key_open_paths: str = (
        "/healthz,/readyz,/metrics,/docs,/openapi.json,/api/approvals/by-token"
    )
    api_key_default_expiry_days: int = Field(default=90, gt=0)
    api_key_initial_seed: SecretStr | None = None
    # 7.2 Observability
    celery_metrics_port: int = Field(default=9800, gt=0)
    prometheus_metrics_enabled: bool = True
    # 7.3 Evals
    shadow_mode_enabled: bool = False
    # 7.4 HA
    db_pool_size: int = Field(default=5, gt=0)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_recycle_seconds: int = Field(default=3600, gt=0)
    db_connect_timeout_seconds: int = Field(default=5, gt=0)
    redis_socket_connect_timeout: float = Field(default=1.0, gt=0)
    redis_socket_timeout: float = Field(default=2.0, gt=0)
    redis_retry_on_timeout: bool = True
    cors_allow_origins: str = "http://localhost:5173"
    # How long a RUNNING agent run can be stuck before it is considered orphaned
    # (previous worker killed by SIGKILL) and re-executed. Default 5 minutes.
    task_orphan_timeout_seconds: int = Field(default=300, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
