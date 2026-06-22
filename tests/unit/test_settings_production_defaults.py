"""Tests for PR 0.1: Settings environment defaults and production safety defaults."""

from __future__ import annotations

from packages.common.settings import Settings


def test_default_app_env_local():
    """When APP_ENV is not set, default is 'local'."""
    settings = Settings()
    assert settings.app_env == "local"


def test_production_llm_default_disabled(monkeypatch):
    """When APP_ENV=production and LLM_PROVIDER is not explicitly set, default is 'disabled'."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "disabled"


def test_production_executor_default_fixture():
    """Executor backend stays 'fixture' even in production."""
    settings = Settings(app_env="production")
    assert settings.executor_backend == "fixture"


def test_local_can_use_localhost_defaults():
    """In local mode, all localhost defaults are preserved."""
    settings = Settings()
    assert settings.prometheus_url == "http://localhost:9090"
    assert settings.loki_url == "http://localhost:3100"
    assert settings.alertmanager_url == "http://localhost:9093"
    assert settings.jaeger_url == "http://localhost:16686"


def test_local_llm_default_fake():
    """In local mode, LLM provider defaults to 'fake'."""
    settings = Settings()
    assert settings.llm_provider == "fake"


def test_llm_profile_settings_default_to_inherit():
    """Profile settings are inert unless explicitly configured."""
    settings = Settings()
    assert settings.llm_fast_json_model == ""
    assert settings.llm_fast_json_max_tokens == 0
    assert settings.llm_diagnose_reasoning_model == ""
    assert settings.llm_diagnose_reasoning_max_tokens == 0
    assert settings.llm_report_model == ""
    assert settings.llm_report_max_tokens == 0
    assert settings.llm_deterministic_report_enabled is False
    assert settings.llm_node_model_overrides == ""
    assert settings.llm_node_max_tokens == ""
    assert settings.llm_multi_perspective_parallel_enabled is False
    assert settings.llm_provider == "fake"
    assert settings.runbook_web_search_enabled is False


def test_automation_level_default_supervised():
    """AUTOMATION_LEVEL defaults to 'supervised'."""
    settings = Settings()
    assert settings.automation_level == "supervised"


def test_discovery_apply_mode_inherit():
    """DISCOVERY_APPLY_MODE defaults to 'inherit'."""
    settings = Settings()
    assert settings.discovery_apply_mode == "inherit"


def test_production_discovery_default_disabled():
    """When APP_ENV=production without explicit DISCOVERY_ENABLED, default is False."""
    settings = Settings(app_env="production")
    assert settings.discovery_enabled is False


def test_local_discovery_default_enabled():
    """In local mode, discovery is enabled by default."""
    settings = Settings()
    assert settings.discovery_enabled is True


def test_discovery_manual_rerun_enabled_default_true():
    """DISCOVERY_MANUAL_RERUN_ENABLED defaults to True."""
    settings = Settings()
    assert settings.discovery_manual_rerun_enabled is True


def test_runbook_llm_default_false():
    """RUNBOOK_LLM_GENERATION_ENABLED defaults to False."""
    settings = Settings()
    assert settings.runbook_llm_generation_enabled is False


def test_runbook_web_search_default_false():
    """RUNBOOK_WEB_SEARCH_ENABLED defaults to False."""
    settings = Settings()
    assert settings.runbook_web_search_enabled is False


def test_alert_source_default_webhook():
    """ALERT_SOURCE defaults to 'webhook'."""
    settings = Settings()
    assert settings.alert_source == "webhook"


def test_backward_compat_existing_fields():
    """Existing settings fields retain their original defaults."""
    settings = Settings()
    assert settings.database_url == "postgresql+psycopg://sre:sre@localhost:5432/sre"
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.tool_timeout_seconds == 2.0
    assert settings.trace_backend == "fixture"
    assert settings.k8s_backend == "fixture"
    assert settings.token_budget_total == 32_000
    assert settings.celery_task_always_eager is False


def test_app_env_production_can_be_set_explicitly():
    """APP_ENV can be set to 'production' via constructor."""
    settings = Settings(app_env="production")
    assert settings.app_env == "production"


def test_app_env_via_env_var(monkeypatch):
    """APP_ENV can be set via environment variable."""
    monkeypatch.setenv("APP_ENV", "production")
    settings = Settings()
    assert settings.app_env == "production"


def test_production_explicit_llm_provider_respected():
    """When LLM_PROVIDER is explicitly set to 'fake' in production, it is respected."""
    settings = Settings(app_env="production", llm_provider="fake")
    assert settings.llm_provider == "fake"


def test_production_explicit_discovery_enabled_respected():
    """When DISCOVERY_ENABLED is explicitly set to True in production, it is respected."""
    settings = Settings(app_env="production", discovery_enabled=True)
    assert settings.discovery_enabled is True


def test_production_explicit_llm_provider_via_kwargs():
    """Explicit kwarg llm_provider takes precedence over production default."""
    settings = Settings(app_env="production", llm_provider="openai")
    assert settings.llm_provider == "openai"


def test_alert_poll_settings_defaults():
    """All ALERT_POLL_* settings have correct defaults."""
    settings = Settings()
    assert settings.alertmanager_url == "http://localhost:9093"
    assert settings.alertmanager_read_token is None
    assert settings.alert_poll_interval_seconds == 30
    assert settings.alert_poll_lock_ttl_seconds == 60
    assert settings.alert_poll_timeout_seconds == 20
    assert settings.alert_poll_resolved_grace_period_seconds == 120
    assert settings.alert_poll_resolved_missing_rounds == 3
    assert settings.alert_poll_receiver_filter == ""
    assert settings.alert_poll_filter_matchers == ""
    assert settings.alert_poll_namespace_allowlist == ""
    assert settings.alert_poll_service_allowlist == ""
    assert settings.alert_poll_max_alerts_per_round == 200
    assert settings.alert_poll_max_new_incidents_per_round == 20
    assert settings.alert_poll_max_incidents_per_service_per_minute == 5


def test_all_new_fields_accessible_via_env(monkeypatch):
    """All new settings fields can be set via environment variables."""
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("AUTOMATION_LEVEL", "off")
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    monkeypatch.setenv("DISCOVERY_MANUAL_RERUN_ENABLED", "false")
    monkeypatch.setenv("DISCOVERY_APPLY_MODE", "propose")
    monkeypatch.setenv("RUNBOOK_TEMPLATE_GENERATION_ENABLED", "false")
    monkeypatch.setenv("RUNBOOK_LLM_GENERATION_ENABLED", "true")
    monkeypatch.setenv("RUNBOOK_WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("RUNBOOK_WEB_SEARCH_CACHE_ENABLED", "false")
    monkeypatch.setenv("ALERT_SOURCE", "both")
    monkeypatch.setenv("ALERTMANAGER_URL", "http://am:9093")
    monkeypatch.setenv("ALERT_POLL_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("ALERT_POLL_LOCK_TTL_SECONDS", "120")
    monkeypatch.setenv("ALERT_POLL_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("ALERT_POLL_RESOLVED_GRACE_PERIOD_SECONDS", "300")
    monkeypatch.setenv("ALERT_POLL_RESOLVED_MISSING_ROUNDS", "5")
    monkeypatch.setenv("ALERT_POLL_RECEIVER_FILTER", "sre|platform")
    monkeypatch.setenv("ALERT_POLL_FILTER_MATCHERS", "severity=~critical")
    monkeypatch.setenv("ALERT_POLL_NAMESPACE_ALLOWLIST", "prod,staging")
    monkeypatch.setenv("ALERT_POLL_SERVICE_ALLOWLIST", "api,worker")
    monkeypatch.setenv("ALERT_POLL_MAX_ALERTS_PER_ROUND", "500")
    monkeypatch.setenv("ALERT_POLL_MAX_NEW_INCIDENTS_PER_ROUND", "50")
    monkeypatch.setenv("ALERT_POLL_MAX_INCIDENTS_PER_SERVICE_PER_MINUTE", "10")
    monkeypatch.setenv("BACKEND_URL_ALLOWLIST", "*.svc,*.cluster.local")
    monkeypatch.setenv("LLM_FAST_JSON_MODEL", "qwen-fast")
    monkeypatch.setenv("LLM_FAST_JSON_MAX_TOKENS", "128")
    monkeypatch.setenv("LLM_DIAGNOSE_REASONING_MODEL", "qwen-reasoning")
    monkeypatch.setenv("LLM_DIAGNOSE_REASONING_MAX_TOKENS", "1536")
    monkeypatch.setenv("LLM_REPORT_MODEL", "qwen-report")
    monkeypatch.setenv("LLM_REPORT_MAX_TOKENS", "1024")
    monkeypatch.setenv("LLM_DETERMINISTIC_REPORT_ENABLED", "true")
    monkeypatch.setenv("LLM_NODE_MODEL_OVERRIDES", "generate_report=qwen-report-hot")
    monkeypatch.setenv("LLM_NODE_MAX_TOKENS", "generate_report=768")
    monkeypatch.setenv("LLM_MULTI_PERSPECTIVE_PARALLEL_ENABLED", "true")

    settings = Settings()
    assert settings.app_env == "local"
    assert settings.automation_level == "off"
    assert settings.discovery_enabled is False
    assert settings.discovery_manual_rerun_enabled is False
    assert settings.discovery_apply_mode == "propose"
    assert settings.runbook_template_generation_enabled is False
    assert settings.runbook_llm_generation_enabled is True
    assert settings.runbook_web_search_enabled is True
    assert settings.runbook_web_search_cache_enabled is False
    assert settings.alert_source == "both"
    assert settings.alertmanager_url == "http://am:9093"
    assert settings.alert_poll_interval_seconds == 60
    assert settings.alert_poll_lock_ttl_seconds == 120
    assert settings.alert_poll_timeout_seconds == 30
    assert settings.alert_poll_resolved_grace_period_seconds == 300
    assert settings.alert_poll_resolved_missing_rounds == 5
    assert settings.alert_poll_receiver_filter == "sre|platform"
    assert settings.alert_poll_filter_matchers == "severity=~critical"
    assert settings.alert_poll_namespace_allowlist == "prod,staging"
    assert settings.alert_poll_service_allowlist == "api,worker"
    assert settings.alert_poll_max_alerts_per_round == 500
    assert settings.alert_poll_max_new_incidents_per_round == 50
    assert settings.alert_poll_max_incidents_per_service_per_minute == 10
    assert settings.backend_url_allowlist == "*.svc,*.cluster.local"
    assert settings.llm_fast_json_model == "qwen-fast"
    assert settings.llm_fast_json_max_tokens == 128
    assert settings.llm_diagnose_reasoning_model == "qwen-reasoning"
    assert settings.llm_diagnose_reasoning_max_tokens == 1536
    assert settings.llm_report_model == "qwen-report"
    assert settings.llm_report_max_tokens == 1024
    assert settings.llm_deterministic_report_enabled is True
    assert settings.llm_node_model_overrides == "generate_report=qwen-report-hot"
    assert settings.llm_node_max_tokens == "generate_report=768"
    assert settings.llm_multi_perspective_parallel_enabled is True


def test_production_llm_profile_settings_do_not_enable_external_paths():
    settings = Settings(
        app_env="production",
        llm_fast_json_model="qwen-fast",
        llm_report_model="qwen-report",
        llm_node_max_tokens="report=768",
    )
    assert settings.llm_provider == "disabled"
    assert settings.llm_external_provider_allowed is False
    assert settings.runbook_web_search_enabled is False


def test_runbook_template_generation_enabled_default():
    """RUNBOOK_TEMPLATE_GENERATION_ENABLED defaults to True."""
    settings = Settings()
    assert settings.runbook_template_generation_enabled is True


def test_backend_url_allowlist_default_empty():
    """BACKEND_URL_ALLOWLIST defaults to empty string."""
    settings = Settings()
    assert settings.backend_url_allowlist == ""


def test_production_does_not_change_executor_backend():
    """Production does not change executor_backend — it stays 'fixture'."""
    settings = Settings(app_env="production")
    assert settings.executor_backend == "fixture"


def test_get_settings_returns_settings():
    """get_settings() returns a Settings instance."""
    from packages.common.settings import get_settings
    s = get_settings()
    assert isinstance(s, Settings)
    assert s.app_env == "local"
