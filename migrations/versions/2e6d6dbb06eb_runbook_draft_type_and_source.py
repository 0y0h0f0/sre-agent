"""runbook_draft_type_and_source

Revision ID: 2e6d6dbb06eb
Revises: c3d4e5f6a7b8
Create Date: 2026-06-12 17:51:59.057628

Add draft_type, source, discovery_run_id, parent_draft_id columns to runbook_drafts.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2e6d6dbb06eb'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runbook_drafts",
        sa.Column(
            "draft_type",
            sa.String(32),
            nullable=False,
            server_default="incident_cluster",
        ),
    )
    op.add_column(
        "runbook_drafts",
        sa.Column(
            "source",
            sa.String(64),
            nullable=False,
            server_default="llm",
        ),
    )
    op.add_column(
        "runbook_drafts",
        sa.Column("discovery_run_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "runbook_drafts",
        sa.Column("parent_draft_id", sa.String(64), nullable=True),
    )
    op.create_index(
        op.f("ix_runbook_drafts_draft_type"),
        "runbook_drafts",
        ["draft_type"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_runbook_drafts_draft_type"), table_name="runbook_drafts")
    op.drop_column("runbook_drafts", "parent_draft_id")
    op.drop_column("runbook_drafts", "discovery_run_id")
    op.drop_column("runbook_drafts", "source")
    op.drop_column("runbook_drafts", "draft_type")
