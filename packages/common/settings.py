"""Runtime settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
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
    tool_timeout_seconds: float = Field(default=2.0, gt=0)
    embedding_provider: str = "fake"
    llm_provider: str = "fake"
    llm_model: str = "fake-diagnosis-model"
    token_budget_total: int = Field(default=32_000, gt=0)
    token_budget_prompt: int = Field(default=12_000, gt=0)
    token_cache_enabled: bool = True
    celery_task_always_eager: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
