"""Repository for discovery_proposals table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import DiscoveryProposal


class DiscoveryProposalRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        discovery_run_id: str,
        status: str = "pending_review",
        config_diff: dict[str, Any] | None = None,
        confidence: float | None = None,
    ) -> DiscoveryProposal:
        proposal = DiscoveryProposal(
            proposal_id=new_id("dp_"),
            discovery_run_id=discovery_run_id,
            status=status,
            config_diff=config_diff or {},
            confidence=confidence,
        )
        self.db.add(proposal)
        return proposal

    def get_by_id(self, proposal_id: str) -> DiscoveryProposal | None:
        stmt = select(DiscoveryProposal).where(
            DiscoveryProposal.proposal_id == proposal_id
        )
        return self.db.scalars(stmt).first()

    def list_for_run(self, discovery_run_id: str) -> Sequence[DiscoveryProposal]:
        stmt = (
            select(DiscoveryProposal)
            .where(DiscoveryProposal.discovery_run_id == discovery_run_id)
            .order_by(DiscoveryProposal.created_at.desc())
        )
        return self.db.scalars(stmt).all()

    def list_pending_review(self, limit: int = 50) -> Sequence[DiscoveryProposal]:
        stmt = (
            select(DiscoveryProposal)
            .where(DiscoveryProposal.status == "pending_review")
            .order_by(DiscoveryProposal.created_at.asc())
            .limit(limit)
        )
        return self.db.scalars(stmt).all()
