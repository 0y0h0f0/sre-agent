"""PR 9.7 — Grafana alert parser tests."""

from __future__ import annotations

from packages.common.settings import Settings


def _make_firing(**overrides):
    payload = {
        "receiver": "sre-agent",
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {"alertname": "HighErrorRate", "service": "checkout", "severity": "critical"},
            "annotations": {
                "summary": "5xx error rate > 5%",
                "dashboardURL": "https://grafana.local/d/dashboard",
                "panelURL": "https://grafana.local/d/panel",
                "ruleUID": "rule-abc123",
                "generatorURL": "https://grafana.local/alerting/grafana/rule-abc123",
            },
            "startsAt": "2026-06-01T12:00:00Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "fingerprint": "fp123",
        }],
        "commonLabels": {"namespace": "prod", "job": "checkout"},
        "commonAnnotations": {"runbook_url": "https://docs.example.com/runbook"},
        "groupKey": "{}:{}",
        "groupLabels": {"alertname": "HighErrorRate"},
    }
    payload.update(overrides)
    return payload


class TestGrafanaAlertParsing:
    def test_grafana_unified_alert_firing_parsed(self):
        from apps.api.schemas.alerts import grafana_to_alert
        result = grafana_to_alert(_make_firing())
        assert result["alert_name"] == "HighErrorRate"
        assert result["service"] == "checkout"
        assert result["severity"] in ("critical", "P1", "P2", "P3", "P4")

    def test_grafana_unified_alert_resolved_parsed(self):
        from apps.api.schemas.alerts import grafana_to_alert
        p = _make_firing()
        p["status"] = "resolved"
        p["alerts"][0]["status"] = "resolved"
        p["alerts"][0]["endsAt"] = "2026-06-01T13:00:00Z"
        result = grafana_to_alert(p)
        assert result["ends_at"] is not None

    def test_grafana_raw_labels_preserved(self):
        from apps.api.schemas.alerts import grafana_to_alert
        result = grafana_to_alert(_make_firing())
        assert "alertname" in result["labels"]
        assert result["labels"]["service"] == "checkout"

    def test_grafana_fingerprint_stable(self):
        from apps.api.schemas.alerts import grafana_to_alert
        r1 = grafana_to_alert(_make_firing())
        r2 = grafana_to_alert(_make_firing())
        assert r1["fingerprint"] == r2["fingerprint"]

    def test_grafana_fingerprint_excludes_dashboard_url(self):
        from apps.api.schemas.alerts import grafana_to_alert
        p1 = _make_firing()
        p2 = _make_firing()
        # Change dashboardURL — fingerprint should remain the same
        p2["alerts"][0]["annotations"]["dashboardURL"] = "https://different.local/diff"
        assert grafana_to_alert(p1)["fingerprint"] == grafana_to_alert(p2)["fingerprint"]

    def test_grafana_fingerprint_excludes_panel_url(self):
        from apps.api.schemas.alerts import grafana_to_alert
        p1 = _make_firing()
        p2 = _make_firing()
        p2["alerts"][0]["annotations"]["panelURL"] = "https://diff.local/p"
        assert grafana_to_alert(p1)["fingerprint"] == grafana_to_alert(p2)["fingerprint"]

    def test_grafana_fingerprint_excludes_rule_uid(self):
        from apps.api.schemas.alerts import grafana_to_alert
        p1 = _make_firing()
        p2 = _make_firing()
        p2["alerts"][0]["labels"]["ruleUID"] = "different-rule"
        assert grafana_to_alert(p1)["fingerprint"] == grafana_to_alert(p2)["fingerprint"]

    def test_grafana_fingerprint_excludes_generator_url(self):
        from apps.api.schemas.alerts import grafana_to_alert
        p1 = _make_firing()
        p2 = _make_firing()
        p2["alerts"][0]["annotations"]["generatorURL"] = "https://diff.local/gen"
        assert grafana_to_alert(p1)["fingerprint"] == grafana_to_alert(p2)["fingerprint"]


class TestGrafanaIngestDefaultDisabled:
    def test_grafana_ingest_default_disabled(self):
        settings = Settings()
        assert settings.grafana_alert_ingest_enabled is False


class TestGrafanaSettings:
    def test_grafana_webhook_max_bytes_default(self):
        settings = Settings()
        assert settings.grafana_webhook_max_bytes > 0

    def test_grafana_webhook_secret_ref_default_empty(self):
        settings = Settings()
        assert settings.grafana_webhook_secret_ref == ""
