"""Unit tests for service topology and cascade analysis (roadmap Phase 1.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from packages.agent.topology import (
    ServiceTopology,
    analyze_cascade_from_state,
    analyze_propagation,
    correlate_incidents,
)


def _topo() -> ServiceTopology:
    # checkout -> payments -> postgres ; checkout -> postgres
    return ServiceTopology.from_config(
        {"services": {"checkout": ["payments", "postgres"], "payments": ["postgres"]}}
    )


# --------------------------------------------------------------------------- #
# Topology construction                                                        #
# --------------------------------------------------------------------------- #
class TestServiceTopology:
    def test_from_config_dependencies_and_dependents(self) -> None:
        topo = _topo()
        assert topo.dependencies("checkout") == {"payments", "postgres"}
        assert topo.dependents("postgres") == {"checkout", "payments"}
        assert "postgres" in topo.services

    def test_from_trace_spans(self) -> None:
        spans = [
            {"service": "checkout", "downstream_service": "payments"},
            {"service": "payments", "downstream_service": "postgres"},
        ]
        topo = ServiceTopology.from_trace_spans(spans)
        assert topo.dependencies("checkout") == {"payments"}
        assert topo.is_adjacent("payments", "postgres") is True

    def test_from_file_missing_returns_empty(self, tmp_path: Any) -> None:
        topo = ServiceTopology.from_file(tmp_path / "nope.json")
        assert topo.services == set()

    def test_from_file_reads_config(self, tmp_path: Any) -> None:
        path = tmp_path / "topo.json"
        path.write_text('{"services": {"a": ["b"]}}', encoding="utf-8")
        topo = ServiceTopology.from_file(path)
        assert topo.dependencies("a") == {"b"}


# --------------------------------------------------------------------------- #
# Propagation analysis                                                         #
# --------------------------------------------------------------------------- #
class TestAnalyzePropagation:
    def test_no_anomalies(self) -> None:
        result = analyze_propagation(_topo(), [])
        assert result["is_cascade"] is False
        assert result["root_services"] == []

    def test_single_service_is_not_cascade(self) -> None:
        result = analyze_propagation(_topo(), ["checkout"])
        assert result["is_cascade"] is False
        assert result["root_services"] == ["checkout"]
        assert result["cascade_services"] == []

    def test_root_is_downstream_most(self) -> None:
        result = analyze_propagation(_topo(), ["checkout", "payments", "postgres"])
        assert result["is_cascade"] is True
        assert result["root_services"] == ["postgres"]
        assert result["cascade_services"] == ["checkout", "payments"]

    def test_chain_walks_to_root(self) -> None:
        result = analyze_propagation(_topo(), ["checkout", "payments", "postgres"])
        chains = {c[0]: c for c in result["chains"]}
        assert chains["checkout"][-1] == "postgres"
        assert chains["payments"] == ["payments", "postgres"]

    def test_unrelated_services_are_not_cascade(self) -> None:
        # Two anomalous services with no dependency edge → both roots, no cascade.
        topo = ServiceTopology.from_config({"services": {"a": [], "b": []}})
        result = analyze_propagation(topo, ["a", "b"])
        assert result["is_cascade"] is False
        assert result["cascade_services"] == []


# --------------------------------------------------------------------------- #
# Incident correlation                                                         #
# --------------------------------------------------------------------------- #
class TestCorrelateIncidents:
    def _incidents(self) -> list[dict[str, Any]]:
        base = datetime(2026, 6, 1, 0, 3, tzinfo=UTC)
        return [
            {"incident_id": "inc_1", "service": "checkout", "severity": "P2",
             "started_at": base.isoformat()},
            {"incident_id": "inc_2", "service": "payments", "severity": "P3",
             "started_at": base.isoformat()},
        ]

    def test_clusters_related_incidents_in_window(self) -> None:
        clusters = correlate_incidents(self._incidents(), _topo(), window_seconds=300)
        assert len(clusters) == 1
        cluster = clusters[0]
        assert cluster["incident_ids"] == ["inc_1", "inc_2"]
        assert cluster["escalate"] is True
        # max severity P2 escalates to P1
        assert cluster["suggested_severity"] == "P1"
        assert cluster["root_services"] == ["payments"]

    def test_no_cluster_when_outside_window(self) -> None:
        incidents = self._incidents()
        incidents[1]["started_at"] = datetime(2026, 6, 1, 1, 0, tzinfo=UTC).isoformat()
        clusters = correlate_incidents(incidents, _topo(), window_seconds=300)
        assert clusters == []

    def test_no_cluster_when_unrelated_services(self) -> None:
        incidents = self._incidents()
        incidents[1]["service"] = "billing"  # not in topology
        clusters = correlate_incidents(incidents, _topo(), window_seconds=300)
        assert clusters == []

    def test_accepts_datetime_objects_and_clusters(self) -> None:
        base = datetime(2026, 6, 1, 0, 3, tzinfo=UTC)
        incidents = [
            {"incident_id": "inc_1", "service": "checkout", "severity": "P4",
             "started_at": base},
            {"incident_id": "inc_2", "service": "payments", "severity": "P4",
             "started_at": base},
        ]
        clusters = correlate_incidents(incidents, _topo(), window_seconds=300)
        assert len(clusters) == 1
        # P4 escalates one rank to P3
        assert clusters[0]["suggested_severity"] == "P3"

    def test_missing_timestamps_fall_back_to_topology(self) -> None:
        incidents = [
            {"incident_id": "inc_1", "service": "checkout", "severity": "P2"},
            {"incident_id": "inc_2", "service": "payments", "severity": "P2"},
        ]
        clusters = correlate_incidents(incidents, _topo(), window_seconds=1)
        assert len(clusters) == 1


class TestFromFileInvalid:
    def test_invalid_json_returns_empty(self, tmp_path: Any) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        assert ServiceTopology.from_file(path).services == set()

    def test_non_object_json_returns_empty(self, tmp_path: Any) -> None:
        path = tmp_path / "list.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert ServiceTopology.from_file(path).services == set()


# --------------------------------------------------------------------------- #
# State integration                                                            #
# --------------------------------------------------------------------------- #
def _ev_trace(error_spans: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "trace", "payload": {"error_spans": error_spans}}


class TestAnalyzeCascadeFromState:
    def test_single_service_no_cascade(self) -> None:
        state = {"service_name": "checkout", "traces_evidence": []}
        result = analyze_cascade_from_state(state)
        assert result["is_cascade"] is False

    def test_downstream_error_span_is_cascade(self) -> None:
        state = {
            "service_name": "checkout",
            "traces_evidence": [
                _ev_trace(
                    [{"service": "checkout", "downstream_service": "payments", "status": "error"}]
                )
            ],
        }
        result = analyze_cascade_from_state(state)
        assert result["is_cascade"] is True
        assert result["root_services"] == ["payments"]
        assert result["cascade_services"] == ["checkout"]
