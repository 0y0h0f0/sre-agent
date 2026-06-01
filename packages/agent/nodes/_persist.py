"""Shared helper to persist evidence items to the database."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from packages.db.repositories.evidence_items import EvidenceItemRepository


def persist_evidence(
    db: Session,
    incident_id: str,
    agent_run_id: str,
    evidence_list: list[dict[str, Any]],
) -> None:
    """Write each evidence dict as an EvidenceItem row."""
    repo = EvidenceItemRepository(db)
    for item in evidence_list:
        repo.create(
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            type=item.get("type", "unknown"),
            source=item.get("source", "unknown"),
            source_id=item.get("source_id"),
            title=item.get("title", str(item.get("summary", ""))[:200]),
            excerpt=str(item.get("summary", ""))[:500],
            payload=item,
        )
