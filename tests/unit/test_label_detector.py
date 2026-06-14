"""Tests for M2 PR 2.2: Service Label Detector."""
from __future__ import annotations

from packages.discovery.label_detector import detect_k8s_service_label


class TestDetectK8sServiceLabel:
    def test_highest_coverage_wins(self):
        labels = [
            {"app": "checkout", "service": "checkout"},
            {"app": "payments"},
        ]
        result = detect_k8s_service_label(labels)
        assert result.service_label_key == "app"
        assert result.coverage == 1.0

    def test_metrics_can_differ_from_k8s(self):
        """Metrics label can be different from K8s label."""
        labels = [{"app.kubernetes.io/name": "checkout"}]
        result = detect_k8s_service_label(
            labels, metrics_service_label="app", metrics_label_coverage=0.9,
        )
        # K8s found "app.kubernetes.io/name", metrics found "app" — they differ.
        assert result.service_label_key is not None

    def test_low_coverage_requires_review(self):
        labels = [{"other": "x"}]
        result = detect_k8s_service_label(labels)
        assert result.service_label_key is None
        assert result.requires_review

    def test_alternatives_recorded(self):
        labels = [
            {"app": "svc1", "service": "svc1", "name": "svc1"},
            {"app": "svc2", "service": "svc2"},
        ]
        result = detect_k8s_service_label(labels)
        assert len(result.alternatives) > 0

    def test_cross_validation_increases_confidence(self):
        labels = [
            {"app": "svc1", "service": "svc1"},
            {"app": "svc2", "service": "svc2"},
        ]
        result = detect_k8s_service_label(
            labels, metrics_service_label="app", metrics_label_coverage=0.9,
        )
        assert result.confidence >= 0.9
        assert not result.requires_review

    def test_empty_labels(self):
        result = detect_k8s_service_label([])
        assert result.service_label_key is None
        assert result.requires_review

    def test_extended_candidate_keys(self):
        labels = [{"k8s-app": "dns"}, {"k8s-app": "metrics"}]
        result = detect_k8s_service_label(labels)
        assert result.service_label_key == "k8s-app"
        assert result.coverage == 1.0
