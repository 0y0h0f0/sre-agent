"""Runtime settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
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
    # Trace backend: fixture | jaeger | tempo (Phase 2.1). Default keeps tests
    # deterministic and local dev offline.
    trace_backend: str = "fixture"
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
    llm_reasoning_nodes: str = "diagnose"
    token_budget_total: int = Field(default=32_000, gt=0)

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

    # --- Phase 7: Ops & Engineering ---
    # 7.1 Auth
    api_key_auth_enabled: bool = True
    api_key_open_paths: str = "/healthz,/readyz,/metrics,/docs,/openapi.json"
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
