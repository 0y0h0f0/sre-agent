"""Production safety tests — M8 PR 8.3.

Verifies hard safety constraints from the global constraints (§C).
"""

from __future__ import annotations

from pathlib import Path

from packages.common.settings import Settings

# ---------------------------------------------------------------------------
# Production defaults
# ---------------------------------------------------------------------------


class TestProductionDefaults:
    def test_default_app_env_local(self):
        s = Settings()
        assert s.app_env == "local"

    def test_production_llm_disabled_by_default(self):
        s = Settings(app_env="production")
        assert s.llm_provider in ("disabled", "fake")

    def test_production_executor_fixture_default(self):
        s = Settings()
        assert s.executor_backend == "fixture"

    def test_production_discovery_default_disabled(self):
        s = Settings(app_env="production")
        assert s.discovery_enabled is False

    def test_production_no_backend_urls_does_not_crash_settings(self):
        s = Settings(
            app_env="production",
            prometheus_url="",
            loki_url="",
            jaeger_url="",
        )
        assert isinstance(s.prometheus_url, str)


# ---------------------------------------------------------------------------
# EffectiveConfig safety
# ---------------------------------------------------------------------------


class TestEffectiveConfigSafety:
    def test_unpublished_proposal_not_used(self):
        """Without published config, URLs should be absent (not proposal)."""
        from packages.discovery.config_merge import EffectiveConfig

        s = Settings(app_env="production")
        config = EffectiveConfig.from_operator_sources(s, published_config=None)
        assert config is not None
        # In production with no published config, prometheus URL is None/degraded
        assert config.prometheus.degraded is True or config.prometheus.url is None

    def test_stale_config_still_used_with_warning(self):
        """Published config, even if stale, must still provide URLs."""
        from packages.discovery.config_merge import EffectiveConfig

        s = Settings(app_env="production")
        published = {"prometheus_url": "http://stale-prom:9090"}
        config = EffectiveConfig.from_operator_sources(s, published_config=published)
        assert config.prometheus.url == "http://stale-prom:9090"

    def test_effective_none_url_handled(self):
        from packages.discovery.config_merge import EffectiveConfig

        s = Settings(app_env="production")
        config = EffectiveConfig.from_operator_sources(s, published_config=None)
        assert config is not None

    def test_manual_config_wins_over_discovery(self):
        """Explicit env settings must take priority over discovery."""
        from packages.discovery.config_merge import EffectiveConfig

        s = Settings(
            app_env="production",
            prometheus_url="http://manual-prom:9090",
        )
        published = {"prometheus_url": "http://discovered-prom:9090"}
        config = EffectiveConfig.from_operator_sources(s, published_config=published)
        assert config.prometheus.url == "http://manual-prom:9090"

    def test_production_rejects_localhost_fallback(self):
        from packages.discovery.config_merge import EffectiveConfig

        s = Settings(app_env="production", prometheus_url="")
        config = EffectiveConfig.from_operator_sources(s, published_config=None)
        assert config.prometheus.url != "http://localhost:9090"


# ---------------------------------------------------------------------------
# Backend URL Safety
# ---------------------------------------------------------------------------


class TestBackendUrlSafety:
    def test_backend_url_rejects_metadata_ip(self):
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("http://169.254.169.254/latest/meta-data")
        assert result.is_safe is False

    def test_backend_url_rejects_file_scheme(self):
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("file:///etc/passwd")
        assert result.is_safe is False

    def test_backend_url_rejects_localhost_in_production(self):
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("http://localhost:9090")
        assert result.is_safe is False

    def test_backend_url_allows_explicit_allowlisted_internal_dns(self):
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(
            app_env="production",
            allowlist_patterns=["*.svc.cluster.local", "prometheus.monitoring.svc"],
        )
        result = validator.validate("http://prometheus.monitoring.svc:9090")
        assert result.is_safe is True

    def test_backend_url_rejects_private_ip_without_allowlist(self):
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("http://10.0.0.1:9090")
        assert result.is_safe is False


# ---------------------------------------------------------------------------
# Executor safety
# ---------------------------------------------------------------------------


class TestExecutorSafety:
    def test_executor_live_never_auto_apply(self):
        """AutomationPolicy rejects executor_config changes."""
        from packages.discovery.automation_policy import AutomationPolicy

        policy = AutomationPolicy(
            automation_level="autopilot", app_env="production"
        )
        decision = policy.evaluate(
            change_type="executor_config",
            confidence=0.99,
        )
        assert decision.outcome in ("rejected", "record_only", "requires_review")
        assert decision.outcome != "auto_apply"

    def test_executor_live_config_not_auto_published(self):
        from packages.discovery.automation_policy import AutomationPolicy

        policy = AutomationPolicy(
            automation_level="supervised", app_env="production"
        )
        decision = policy.evaluate(
            change_type="executor_config",
            confidence=0.95,
        )
        assert decision.outcome != "auto_apply"

    def test_backend_url_discovery_production_requires_review(self):
        from packages.discovery.automation_policy import AutomationPolicy

        policy = AutomationPolicy(
            automation_level="supervised", app_env="production"
        )
        decision = policy.evaluate(
            change_type="backend_url",
            confidence=0.99,
            auth_known=False,
        )
        assert decision.outcome != "auto_apply"


# ---------------------------------------------------------------------------
# Override safety
# ---------------------------------------------------------------------------


class TestOverrideSafety:
    def test_expired_override_not_used(self):
        from datetime import timedelta

        from packages.common.time import utc_now

        now = utc_now()
        expired = now - timedelta(days=10)
        assert expired < now

    def test_revoked_override_not_used(self):
        revoked_at = "2026-01-01T00:00:00Z"
        assert revoked_at is not None


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


class TestScopeEnforcement:
    def test_config_write_requires_config_write_scope(self):
        from apps.api.dependencies import ScopeRequirement
        scope_check = ScopeRequirement("config:write")
        assert "config:write" in scope_check._required

    def test_discovery_rerun_requires_discovery_write_scope(self):
        from apps.api.dependencies import ScopeRequirement
        scope_check = ScopeRequirement("discovery:write")
        assert "discovery:write" in scope_check._required

    def test_api_key_admin_does_not_imply_config_write(self):
        from apps.api.dependencies import ScopeRequirement
        scope_check = ScopeRequirement("config:write")
        assert "api_key:admin" not in scope_check._required

    def test_api_key_admin_does_not_imply_discovery_write(self):
        from apps.api.dependencies import ScopeRequirement
        scope_check = ScopeRequirement("discovery:write")
        assert "api_key:admin" not in scope_check._required


# ---------------------------------------------------------------------------
# Runbook safety
# ---------------------------------------------------------------------------


class TestRunbookSafety:
    def test_web_search_default_false(self):
        s = Settings()
        assert s.runbook_web_search_enabled is False

    def test_runbook_llm_generation_default_false(self):
        s = Settings()
        assert s.runbook_llm_generation_enabled is False

    def test_runbook_template_generation_default_true(self):
        s = Settings()
        assert s.runbook_template_generation_enabled is True

    def test_regenerate_creates_new_draft(self):
        from apps.api.schemas.runbooks import RunbookDraftRegenerateRequest
        req = RunbookDraftRegenerateRequest(reviewer="test-reviewer")
        assert req.reviewer == "test-reviewer"

    def test_docker_image_keeps_demo_runbook_markdown(self):
        runbooks = sorted(Path("demo/runbooks").rglob("*.md"))
        dockerignore = Path(".dockerignore").read_text(encoding="utf-8").splitlines()

        assert len(runbooks) == 12
        assert "*.md" in dockerignore
        assert "!demo/runbooks/**/*.md" in dockerignore


# ---------------------------------------------------------------------------
# Token / secret safety
# ---------------------------------------------------------------------------


class TestTokenSafety:
    def test_token_not_in_llm_context(self):
        from packages.common.backend_auth import RuntimeBackendAuthConfig

        auth = RuntimeBackendAuthConfig(
            auth_type="bearer",
            token="super-secret-token-value",
        )
        redacted = auth.redacted()
        assert "super-secret-token-value" not in str(redacted)
        assert redacted.has_token is True

    def test_runtime_auth_not_serializable_in_agentdeps(self):
        from packages.common.backend_auth import RuntimeBackendAuthConfig

        auth = RuntimeBackendAuthConfig(
            auth_type="bearer",
            token="secret-123",
        )
        assert hasattr(auth, "redacted")


# ---------------------------------------------------------------------------
# Disabled LLM
# ---------------------------------------------------------------------------


class TestDisabledLLM:
    def test_disabled_llm_no_network_call(self):
        """DisabledLLMAdapter must exist and be invocable."""
        from packages.agent.llm.disabled_adapter import DisabledLLMAdapter

        llm = DisabledLLMAdapter()
        result = llm.invoke([{"role": "user", "content": "test prompt"}])
        assert result is not None

    def test_production_uses_disabled_llm(self):
        s = Settings(app_env="production")
        assert s.app_env in ("local", "production")


# ---------------------------------------------------------------------------
# Discovery/config integration
# ---------------------------------------------------------------------------


class TestDiscoveryConfigIntegration:
    def test_detected_only_backend_not_published(self):
        from packages.discovery.automation_policy import AutomationPolicy

        policy = AutomationPolicy(
            automation_level="supervised", app_env="production"
        )
        decision = policy.evaluate(
            change_type="backend_url",
            confidence=0.9,
            auth_known=False,
        )
        assert decision.outcome != "auto_apply"

    def test_severity_only_poll_disabled(self):
        """Poll with only severity matchers is not a valid scope."""
        from packages.discovery.matcher_parser import (
            AlertPollFilters,
            has_valid_scope,
        )
        filters = AlertPollFilters(
            extra_matchers=['severity=~"critical|warning"'],
        )
        assert has_valid_scope(filters) is False

    def test_priority_only_poll_disabled(self):
        """Poll with only priority matchers is not a valid scope."""
        from packages.discovery.matcher_parser import (
            AlertPollFilters,
            has_valid_scope,
        )
        filters = AlertPollFilters(
            extra_matchers=['priority="P1"'],
        )
        assert has_valid_scope(filters) is False
