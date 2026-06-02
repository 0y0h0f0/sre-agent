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
    embedding_provider: str = "fake"
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
    token_budget_prompt: int = Field(default=12_000, gt=0)
    token_cache_enabled: bool = True
    celery_task_always_eager: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
