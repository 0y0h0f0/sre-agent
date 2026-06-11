"""Extract runbook templates from historical incident clusters."""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import BaseModel

from packages.db.repositories.incidents import IncidentRepository


class TemplateCandidate(BaseModel):
    fingerprint: str
    incident_count: int
    common_root_causes: list[str]
    common_actions: list[str]
    common_evidence_types: list[str]
    service: str
    incident_type: str
    severity_distribution: dict[str, int]


class TemplateExtractor:
    """Analyze resolved incidents grouped by fingerprint to extract common patterns.

    These patterns are used by RunbookGenerator to produce runbook drafts.
    """

    def __init__(self, incident_repo: IncidentRepository) -> None:
        self.incident_repo = incident_repo

    def extract_candidates(
        self,
        *,
        min_incident_count: int = 3,
        fingerprint: str | None = None,
    ) -> list[TemplateCandidate]:
        incidents = self.incident_repo.list_all()
        groups: dict[str, list[dict[str, Any]]] = {}

        for incident in incidents:
            fp = incident.fingerprint
            if fingerprint and fp != fingerprint:
                continue
            if incident.status not in ("resolved", "mitigated"):
                continue
            groups.setdefault(fp, []).append(
                {
                    "service": incident.service,
                    "alert_name": incident.alert_name,
                    "severity": incident.severity,
                    "annotations": dict(incident.annotations),
                }
            )

        candidates: list[TemplateCandidate] = []
        for fp, items in groups.items():
            if len(items) < min_incident_count:
                continue

            services = [item["service"] for item in items]
            service = Counter(services).most_common(1)[0][0]
            severities = Counter(item["severity"] for item in items)

            incident_type = ""
            for item in items:
                ann = item.get("annotations", {})
                itype = ann.get("incident_type", ann.get("alert_type", ""))
                if itype:
                    incident_type = itype
                    break
            if not incident_type:
                alert_names = [item["alert_name"] for item in items]
                incident_type = Counter(alert_names).most_common(1)[0][0]

            root_causes = []
            actions = []
            evidence_types = []
            for item in items:
                ann = item.get("annotations", {})
                rc = ann.get("root_cause", ann.get("root_cause_summary", ""))
                if rc:
                    root_causes.append(rc)
                act = ann.get("actions", ann.get("mitigation", ""))
                if act:
                    actions.append(str(act))
                ev = ann.get("evidence_types", ann.get("evidence", ""))
                if ev:
                    evidence_types.append(str(ev))

            candidates.append(
                TemplateCandidate(
                    fingerprint=fp,
                    incident_count=len(items),
                    common_root_causes=_deduplicate(root_causes),
                    common_actions=_deduplicate(actions),
                    common_evidence_types=_deduplicate(evidence_types),
                    service=service,
                    incident_type=incident_type,
                    severity_distribution=dict(severities),
                )
            )

        return candidates


def _deduplicate(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = item.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(item.strip())
    return result[:10]
