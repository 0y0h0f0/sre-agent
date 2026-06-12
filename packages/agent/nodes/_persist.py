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
) -> list[dict[str, Any]]:
    """Write evidence rows and annotate state evidence with DB evidence_id values."""
    repo = EvidenceItemRepository(db)
    persisted: list[dict[str, Any]] = []
    for item in evidence_list:
        evidence = repo.create(
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            type=item.get("type", "unknown"),
            source=item.get("source", "unknown"),
            source_id=item.get("source_id"),
            title=item.get("title", str(item.get("summary", ""))[:200]),
            excerpt=str(item.get("summary", ""))[:500],
            payload=dict(item),
            confidence=item.get("confidence"),
        )
        item["evidence_id"] = evidence.evidence_id
        evidence.payload = dict(item)
        persisted.append(item)
    db.flush()
    return persisted


def persist_evidence_batch(
    db: Session,
    incident_id: str,
    agent_run_id: str,
    evidence_by_source: dict[str, list[dict[str, Any]]],
) -> None:
    """Bulk-persist evidence from all collectors in a single transaction.

    Each item in *evidence_by_source* is annotated with its DB-assigned
    ``evidence_id`` in-place so downstream nodes can reference it.
    """
    repo = EvidenceItemRepository(db)
    for _source_name, items in evidence_by_source.items():
        for item in items:
            evidence = repo.create(
                incident_id=incident_id,
                agent_run_id=agent_run_id,
                type=item.get("type", "unknown"),
                source=item.get("source", "unknown"),
                source_id=item.get("source_id"),
                title=item.get("title", str(item.get("summary", ""))[:200]),
                excerpt=str(item.get("summary", ""))[:500],
                payload=dict(item),
                confidence=item.get("confidence"),
            )
            item["evidence_id"] = evidence.evidence_id
            evidence.payload = dict(item)
    db.flush()
