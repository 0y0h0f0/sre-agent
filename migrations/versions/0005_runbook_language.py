"""add language column to runbook_chunks

Revision ID: 0005_runbook_language
Revises: 0004_runbook_drafts_versions
Create Date: 2026-06-04 00:02:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_runbook_language"
down_revision = "0004_runbook_drafts_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runbook_chunks",
        sa.Column("language", sa.String(length=8), nullable=False, server_default="en"),
    )


def downgrade() -> None:
    op.drop_column("runbook_chunks", "language")
