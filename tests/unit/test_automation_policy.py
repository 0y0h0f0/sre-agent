"""Tests for PR 0.3: AutomationPolicy."""

from __future__ import annotations

import pytest

from packages.discovery.automation_policy import AutomationPolicy


class TestAutomationLevelOff:
    def test_off_returns_record_only(self):
        """AUTOMATION_LEVEL=off makes everything record-only."""
        policy = AutomationPolicy(automation_level="off")
        result = policy.evaluate("service_label", confidence=0.95)
        assert result.outcome == "record_only"


class TestAutomationLevelPropose:
    def test_propose_returns_record_only(self):
        """AUTOMATION_LEVEL=propose records proposals only."""
        policy = AutomationPolicy(automation_level="propose")
        result = policy.evaluate("service_label", confidence=0.95)
        assert result.outcome == "record_only"


class TestSupervised:
    def test_high_confidence_cross_validated_auto_apply(self):
        """Supervised with high confidence and cross-validation auto-applies."""
        policy = AutomationPolicy(automation_level="supervised")
        result = policy.evaluate(
            "service_label",
            confidence=0.85,
            cross_validated=True,
            metadata_complete=True,
        )
        assert result.outcome == "auto_apply"

    def test_low_confidence_require_review(self):
        """Supervised with low confidence requires review."""
        policy = AutomationPolicy(automation_level="supervised")
        result = policy.evaluate("metric_mapping", confidence=0.60)
        assert result.outcome == "requires_review"

    def test_very_high_confidence_auto_apply_no_cross_validation(self):
        """Very high confidence (>= threshold + 0.10) auto-applies alone."""
        policy = AutomationPolicy(automation_level="supervised")
        result = policy.evaluate(
            "service_label",
            confidence=0.95,
            cross_validated=False,
            metadata_complete=True,
        )
        assert result.outcome == "auto_apply"


class TestAutopilot:
    def test_threshold_lower_than_supervised(self):
        """Autopilot has a lower confidence threshold (0.65 vs 0.80)."""
        policy = AutomationPolicy(automation_level="autopilot")
        result = policy.evaluate(
            "service_label",
            confidence=0.70,
            cross_validated=True,
            metadata_complete=True,
        )
        assert result.outcome == "auto_apply"


class TestExecutorLive:
    def test_executor_live_never_auto_apply(self):
        """EXECUTOR_BACKEND=live is never auto-applied."""
        policy = AutomationPolicy(automation_level="autopilot")
        result = policy.evaluate("executor_config", confidence=0.99)
        assert result.outcome == "rejected"


class TestApplyModeValidation:
    def test_apply_mode_more_aggressive_rejected(self):
        """apply_mode=supervised cannot exceed automation_level=propose."""
        with pytest.raises(ValueError, match="cannot exceed"):
            AutomationPolicy(
                automation_level="propose", apply_mode="supervised"
            )

    def test_apply_mode_more_conservative_allowed(self):
        """apply_mode=propose is more conservative, allowed."""
        policy = AutomationPolicy(
            automation_level="supervised", apply_mode="propose"
        )
        result = policy.evaluate("service_label", confidence=0.95)
        assert result.outcome == "record_only"

    def test_apply_mode_inherit_equals_automation_level(self):
        """apply_mode=inherit uses automation_level directly."""
        policy = AutomationPolicy(
            automation_level="autopilot", apply_mode="inherit"
        )
        result = policy.evaluate(
            "service_label",
            confidence=0.70,
            cross_validated=True,
            metadata_complete=True,
        )
        assert result.outcome == "auto_apply"


class TestBackendUrl:
    def test_backend_url_discovery_production_requires_review(self):
        """Production always requires review for backend URL."""
        policy = AutomationPolicy(app_env="production")
        result = policy.evaluate("backend_url", confidence=0.99)
        assert result.outcome == "requires_review"

    def test_backend_url_discovery_local_can_auto_apply(self):
        """Local env can auto-apply backend URL with sufficient confidence."""
        policy = AutomationPolicy(app_env="local")
        result = policy.evaluate(
            "backend_url", confidence=0.85, auth_known=True
        )
        assert result.outcome == "auto_apply"

    def test_backend_url_auth_unknown_requires_review(self):
        """Unknown auth forces review for backend URL even in local."""
        policy = AutomationPolicy(app_env="local")
        result = policy.evaluate(
            "backend_url", confidence=0.95, auth_known=False
        )
        assert result.outcome == "requires_review"


class TestServiceLabel:
    def test_cross_validated_auto_apply(self):
        """Cross-validated service label auto-applies at threshold."""
        policy = AutomationPolicy()
        result = policy.evaluate(
            "service_label",
            confidence=0.82,
            cross_validated=True,
            metadata_complete=True,
        )
        assert result.outcome == "auto_apply"

    def test_single_source_at_threshold_requires_review(self):
        """Single source at threshold requires review."""
        policy = AutomationPolicy()
        result = policy.evaluate(
            "service_label",
            confidence=0.82,
            cross_validated=False,
            metadata_complete=True,
        )
        assert result.outcome == "requires_review"


class TestMetricMapping:
    def test_all_checks_pass_auto_apply(self):
        """High confidence, cross-validated, metadata complete → auto-apply."""
        policy = AutomationPolicy()
        result = policy.evaluate(
            "metric_mapping",
            confidence=0.88,
            cross_validated=True,
            metadata_complete=True,
        )
        assert result.outcome == "auto_apply"

    def test_metadata_missing_not_auto_apply(self):
        """Missing metadata prevents auto-apply."""
        policy = AutomationPolicy()
        result = policy.evaluate(
            "metric_mapping",
            confidence=0.95,
            metadata_complete=False,
        )
        assert result.outcome == "requires_review"

    def test_no_confidence_requires_review(self):
        """No confidence score requires review."""
        policy = AutomationPolicy()
        result = policy.evaluate("metric_mapping", confidence=None)
        assert result.outcome == "requires_review"


class TestEvaluateAll:
    def test_evaluate_all_aggregates_conservatively(self):
        """Most conservative outcome wins in aggregate decision."""
        policy = AutomationPolicy(app_env="production")
        items: list[tuple[str, float | None]] = [
            ("service_label", 0.90),
            ("backend_url", 0.95),
        ]
        result = policy.evaluate_all(items)
        assert result.overall_outcome == "requires_review"
        assert len(result.items) == 2

    def test_evaluate_all_auto_apply_when_all_pass(self):
        """When all items pass, overall outcome is auto_apply."""
        policy = AutomationPolicy(automation_level="autopilot")
        items: list[tuple[str, float | None]] = [
            ("service_label", 0.90),
            ("metric_mapping", 0.85),
        ]
        result = policy.evaluate_all(
            items, cross_validated=True, metadata_complete=True
        )
        assert result.overall_outcome == "auto_apply"


class TestAuthConfig:
    def test_auth_config_always_requires_review(self):
        """Auth config changes always require review."""
        policy = AutomationPolicy(automation_level="autopilot")
        result = policy.evaluate(
            "auth_config", confidence=0.99, auth_known=True
        )
        assert result.outcome == "requires_review"


class TestOtherChangeType:
    def test_other_follows_standard_rules(self):
        """Other change types follow standard threshold rules."""
        policy = AutomationPolicy(automation_level="supervised")
        result = policy.evaluate(
            "other",
            confidence=0.95,
            cross_validated=True,
            metadata_complete=True,
        )
        assert result.outcome == "auto_apply"
