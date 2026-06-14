"""m9_amendment_draft_lifecycle

Revision ID: d4e5f6a7b8c9
Revises: 3f7e8d9c0a1b
Create Date: 2026-06-14 00:00:00.000000

M9 PR 9.3: allow LLM incident diff amendments to exist without an M7
feedback summary, and track approve/apply lifecycle metadata separately.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "3f7e8d9c0a1b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "amendment_drafts",
        "summary_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.add_column(
        "amendment_drafts",
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=False,
            server_default="runbook_feedback",
        ),
    )
    op.add_column(
        "amendment_drafts",
        sa.Column("related_incident_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "amendment_drafts",
        sa.Column("runbook_version_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "amendment_drafts",
        sa.Column(
            "confidence",
            sa.String(length=16),
            nullable=False,
            server_default="high",
        ),
    )
    op.add_column(
        "amendment_drafts",
        sa.Column(
            "proposal_kind",
            sa.String(length=32),
            nullable=False,
            server_default="proposed_patch",
        ),
    )
    op.add_column(
        "amendment_drafts",
        sa.Column("approved_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "amendment_drafts",
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "amendment_drafts",
        sa.Column("applied_to_draft_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "amendment_drafts",
        sa.Column(
            "applied_to_runbook_version_id",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "amendment_drafts",
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "amendment_drafts",
        sa.Column("superseded_by_amendment_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_amendment_drafts_related_incident_id",
        "amendment_drafts",
        ["related_incident_id"],
    )
    op.create_index(
        "ix_amendment_drafts_runbook_version_id",
        "amendment_drafts",
        ["runbook_version_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_amendment_drafts_runbook_version_id",
        table_name="amendment_drafts",
    )
    op.drop_index(
        "ix_amendment_drafts_related_incident_id",
        table_name="amendment_drafts",
    )
    op.drop_column("amendment_drafts", "superseded_by_amendment_id")
    op.drop_column("amendment_drafts", "applied_at")
    op.drop_column("amendment_drafts", "applied_to_runbook_version_id")
    op.drop_column("amendment_drafts", "applied_to_draft_id")
    op.drop_column("amendment_drafts", "approved_at")
    op.drop_column("amendment_drafts", "approved_by")
    op.drop_column("amendment_drafts", "proposal_kind")
    op.drop_column("amendment_drafts", "confidence")
    op.drop_column("amendment_drafts", "runbook_version_id")
    op.drop_column("amendment_drafts", "related_incident_id")
    op.drop_column("amendment_drafts", "source")
    op.alter_column(
        "amendment_drafts",
        "summary_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
