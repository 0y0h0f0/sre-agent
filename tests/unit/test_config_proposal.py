"""Tests for M3 PR 3.5: ConfigProposalGenerator."""

from __future__ import annotations

from packages.discovery.automation_policy import AutomationPolicy
from packages.discovery.config_proposal import ConfigProposalGenerator
from packages.discovery.models import BackendEndpoint, DiscoveryResult, MetricMapping


class TestConfigProposal:
    def test_proposal_not_published_by_default(self):
        """Proposals are not auto-published by default (requires review)."""
        result = DiscoveryResult(
            run_id="dr_test",
            backend_endpoints=[
                BackendEndpoint(
                    backend_type="prometheus",
                    url="http://prom.local:9090",
                    source="k8s_service",
                    status="ready",
                    confidence=0.85,
                ),
            ],
            status="succeeded",
        )
        generator = ConfigProposalGenerator(
            policy=AutomationPolicy(app_env="production"),
        )
        proposal = generator.generate(result, {})

        assert proposal.has_changes
        assert not proposal.ready_to_publish
        assert proposal.overall_decision == "requires_review"

    def test_proposal_diff_empty_when_no_changes(self):
        """No diff items when nothing changed."""
        result = DiscoveryResult(run_id="dr_test", status="succeeded")
        generator = ConfigProposalGenerator()
        proposal = generator.generate(result, {
            "prometheus_url": "http://prom.local:9090",
        })

        assert not proposal.has_changes

    def test_backend_url_diff_production_requires_review(self):
        """In production, backend URL proposals always require review."""
        result = DiscoveryResult(
            run_id="dr_test",
            backend_endpoints=[
                BackendEndpoint(
                    backend_type="prometheus",
                    url="http://prom.monitoring.svc:9090",
                    source="k8s_service",
                    status="ready",
                    confidence=0.99,
                    auth_required_unknown=False,
                ),
            ],
            status="succeeded",
        )
        generator = ConfigProposalGenerator(
            policy=AutomationPolicy(app_env="production"),
        )
        proposal = generator.generate(result, {})

        assert proposal.has_changes
        assert not proposal.ready_to_publish
        url_item = proposal.items[0]
        assert url_item.decision == "requires_review"

    def test_backend_url_diff_local_can_auto_apply(self):
        """In local env, high-confidence backend URL can auto-apply."""
        result = DiscoveryResult(
            run_id="dr_test",
            backend_endpoints=[
                BackendEndpoint(
                    backend_type="prometheus",
                    url="http://prom.local:9090",
                    source="k8s_service",
                    status="ready",
                    confidence=0.95,
                    auth_required_unknown=False,
                ),
            ],
            status="succeeded",
        )
        generator = ConfigProposalGenerator(
            policy=AutomationPolicy(app_env="local"),
        )
        proposal = generator.generate(result, {})

        assert proposal.has_changes
        assert proposal.ready_to_publish

    def test_service_label_high_confidence_can_auto_apply(self):
        """High-confidence metric mapping can auto-apply."""
        result = DiscoveryResult(
            run_id="dr_test",
            metric_mappings=[
                MetricMapping(
                    semantic_type="latency",
                    metric_name="http_request_duration_seconds_bucket",
                    status="available",
                    confidence=0.95,
                    promql_template="histogram_quantile(...)",
                ),
            ],
            status="succeeded",
        )
        generator = ConfigProposalGenerator(
            policy=AutomationPolicy(
                automation_level="supervised",
                app_env="local",
            ),
        )
        proposal = generator.generate(result, {})

        assert proposal.has_changes
        assert proposal.ready_to_publish

    def test_metric_mapping_high_confidence_can_auto_apply(self):
        """Metric mapping with high confidence can auto-apply."""
        result = DiscoveryResult(
            run_id="dr_test",
            metric_mappings=[
                MetricMapping(
                    semantic_type="error_rate",
                    metric_name="http_request_total",
                    status="available",
                    confidence=0.95,
                    promql_template="sum(rate(...))",
                ),
            ],
            status="succeeded",
        )
        # Cross-validated + high confidence → auto_apply
        policy = AutomationPolicy(
            automation_level="autopilot",
            app_env="local",
        )
        generator = ConfigProposalGenerator(policy=policy)
        proposal = generator.generate(result, {})

        assert proposal.ready_to_publish

    def test_proposal_low_confidence_requires_review(self):
        """Low-confidence proposals require review."""
        result = DiscoveryResult(
            run_id="dr_test",
            metric_mappings=[
                MetricMapping(
                    semantic_type="latency",
                    metric_name="unknown_latency_metric",
                    status="degraded",
                    confidence=0.5,
                ),
            ],
            status="degraded",
        )
        generator = ConfigProposalGenerator()
        proposal = generator.generate(result, {})

        # Degraded metrics are not proposed.
        assert not proposal.has_changes
