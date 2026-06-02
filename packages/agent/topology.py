"""Service topology and cascading-failure analysis (roadmap Phase 1.4).

Extends single-service diagnosis to cross-service dependency chains. Pure and
deterministic — builds a dependency graph from a config file or OTel trace
spans, identifies the root service of a propagation chain, and correlates
incidents that cluster in the same time window along the graph.

Edge direction: ``A -> B`` means "A depends on / calls B" (B is downstream).
A cascading failure surfaces as symptoms on upstream callers while the true
root sits at the downstream-most anomalous dependency.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from packages.common.time import ensure_utc

# Severity ranks: lower number = more severe (P1 is highest).
_SEVERITY_ORDER = ["P1", "P2", "P3", "P4"]


class ServiceTopology:
    """A directed service dependency graph."""

    def __init__(self, dependencies: Mapping[str, Iterable[str]] | None = None) -> None:
        self._deps: dict[str, set[str]] = {}
        for service, downstream in (dependencies or {}).items():
            self._deps.setdefault(service, set())
            for dep in downstream or ():
                self._deps[service].add(dep)
                self._deps.setdefault(dep, set())

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> ServiceTopology:
        """Build from a ``{"services": {svc: [downstream, ...]}}`` mapping."""
        raw = config.get("services", config)
        services = raw if isinstance(raw, Mapping) else {}
        return cls({str(svc): list(downs or []) for svc, downs in services.items()})

    @classmethod
    def from_file(cls, path: str | Path) -> ServiceTopology:
        """Load topology from a JSON file; empty graph if missing/invalid."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls({})
        return cls.from_config(data) if isinstance(data, Mapping) else cls({})

    @classmethod
    def from_trace_spans(cls, spans: Iterable[Mapping[str, Any]]) -> ServiceTopology:
        """Derive edges from ``service -> downstream_service`` span pairs."""
        deps: dict[str, list[str]] = {}
        for span in spans:
            service = span.get("service")
            if not isinstance(service, str) or not service:
                continue
            deps.setdefault(service, [])
            downstream = span.get("downstream_service")
            if isinstance(downstream, str) and downstream:
                deps[service].append(downstream)
        return cls(deps)

    @property
    def services(self) -> set[str]:
        return set(self._deps)

    def dependencies(self, service: str) -> set[str]:
        """Services that ``service`` calls (downstream)."""
        return set(self._deps.get(service, set()))

    def dependents(self, service: str) -> set[str]:
        """Services that call ``service`` (upstream callers)."""
        return {caller for caller, downs in self._deps.items() if service in downs}

    def is_adjacent(self, a: str, b: str) -> bool:
        return b in self.dependencies(a) or a in self.dependencies(b)


def analyze_propagation(
    topology: ServiceTopology, anomalous_services: Iterable[str]
) -> dict[str, Any]:
    """Identify root vs cascade services among an anomalous set.

    A root service is an anomalous service with no anomalous downstream
    dependency (the bottom of the failure chain). Cascade services are the
    upstream callers showing symptoms.
    """
    anomalous = {s for s in anomalous_services if s}
    if not anomalous:
        return _propagation_result(False, [], [], [])

    roots = sorted(s for s in anomalous if not (topology.dependencies(s) & anomalous))
    cascade = sorted(anomalous - set(roots))
    chains = [_chain_to_root(topology, s, anomalous, set(roots)) for s in cascade]
    # A genuine cascade needs at least one symptomatic caller above a root.
    is_cascade = len(anomalous) >= 2 and bool(cascade) and bool(roots)
    return _propagation_result(is_cascade, roots, cascade, chains)


def correlate_incidents(
    incidents: Sequence[Mapping[str, Any]],
    topology: ServiceTopology,
    window_seconds: float = 300.0,
) -> list[dict[str, Any]]:
    """Cluster incidents that co-occur in time and are topologically related.

    Each cluster spanning a dependency edge is flagged for escalation, with the
    propagation root and a suggested (more severe) severity.
    """
    items = [_normalize_incident(inc) for inc in incidents]
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        parent[find(i)] = find(j)

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _within_window(items[i], items[j], window_seconds) and _related(
                items[i]["service"], items[j]["service"], topology
            ):
                union(i, j)

    clusters: dict[int, list[dict[str, Any]]] = {}
    for idx, item in enumerate(items):
        clusters.setdefault(find(idx), []).append(item)

    results: list[dict[str, Any]] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        services = sorted({m["service"] for m in members})
        propagation = analyze_propagation(topology, services)
        max_severity = min((m["severity"] for m in members), key=_severity_rank)
        results.append(
            {
                "incident_ids": sorted(m["incident_id"] for m in members),
                "services": services,
                "root_services": propagation["root_services"],
                "is_cascade": propagation["is_cascade"],
                "escalate": True,
                "suggested_severity": _escalate(max_severity),
            }
        )
    return results


def analyze_cascade_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a cascade analysis from an incident's trace evidence.

    Self-contained: builds a topology from the incident's own error spans, so it
    works without external config. Returns ``is_cascade=False`` for the common
    single-service case, leaving existing behaviour unchanged.
    """
    incident_service = str(state.get("service_name", "") or "")
    error_spans: list[dict[str, Any]] = []
    for evidence in state.get("traces_evidence", []) or []:
        payload = evidence.get("payload") if isinstance(evidence, dict) else None
        if isinstance(payload, dict):
            error_spans.extend(
                sp for sp in payload.get("error_spans", []) if isinstance(sp, dict)
            )

    topology = ServiceTopology.from_trace_spans(error_spans)
    anomalous: set[str] = {incident_service} if incident_service else set()
    for span in error_spans:
        for key in ("service", "downstream_service"):
            value = span.get(key)
            if isinstance(value, str) and value:
                anomalous.add(value)

    return analyze_propagation(topology, anomalous)


# --------------------------------------------------------------------------- #
# Internal helpers                                                             #
# --------------------------------------------------------------------------- #
def _propagation_result(
    is_cascade: bool,
    roots: list[str],
    cascade: list[str],
    chains: list[list[str]],
) -> dict[str, Any]:
    return {
        "is_cascade": is_cascade,
        "root_services": roots,
        "cascade_services": cascade,
        "chains": chains,
    }


def _chain_to_root(
    topology: ServiceTopology,
    start: str,
    anomalous: set[str],
    roots: set[str],
) -> list[str]:
    """Walk downstream through anomalous services to a root (cycle-safe)."""
    path = [start]
    visited = {start}
    current = start
    while current not in roots:
        nxt = sorted(topology.dependencies(current) & (anomalous - visited))
        if not nxt:
            break
        current = nxt[0]
        visited.add(current)
        path.append(current)
    return path


def _normalize_incident(incident: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "incident_id": str(incident.get("incident_id", "")),
        "service": str(incident.get("service", "") or ""),
        "severity": str(incident.get("severity", "P4") or "P4"),
        "started_at": _coerce_time(incident.get("started_at")),
    }


def _coerce_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str) and value:
        try:
            return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _within_window(a: dict[str, Any], b: dict[str, Any], window_seconds: float) -> bool:
    ta, tb = a["started_at"], b["started_at"]
    if ta is None or tb is None:
        return True  # unknown timing — let topology decide relatedness
    return bool(abs((ta - tb).total_seconds()) <= window_seconds)


def _related(service_a: str, service_b: str, topology: ServiceTopology) -> bool:
    if not service_a or not service_b:
        return False
    return service_a == service_b or topology.is_adjacent(service_a, service_b)


def _severity_rank(severity: str) -> int:
    try:
        return _SEVERITY_ORDER.index(severity.upper())
    except ValueError:
        return len(_SEVERITY_ORDER)


def _escalate(severity: str) -> str:
    rank = _severity_rank(severity)
    return _SEVERITY_ORDER[max(0, rank - 1)] if rank < len(_SEVERITY_ORDER) else severity
