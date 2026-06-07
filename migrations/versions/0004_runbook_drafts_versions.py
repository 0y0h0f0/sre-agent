"""runbook drafts and versions tables

Revision ID: 0004_runbook_drafts_versions
Revises: 0003_runbook_tsvector
Create Date: 2026-06-04 00:01:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision = "0004_runbook_drafts_versions"
down_revision = "0003_runbook_tsvector"
branch_labels = None
depends_on = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    json_type = _json_type()

    op.create_table(
        "runbook_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("draft_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=255), nullable=False),
        sa.Column("incident_ids", json_type, nullable=False),
        sa.Column("service", sa.String(length=128), nullable=False),
        sa.Column("incident_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("front_matter", json_type, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("source_chunk_ids", json_type, nullable=True),
        sa.Column("llm_model", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_runbook_drafts_draft_id"), "runbook_drafts", ["draft_id"], unique=True)
    op.create_index(op.f("ix_runbook_drafts_fingerprint"), "runbook_drafts", ["fingerprint"])
    op.create_index(op.f("ix_runbook_drafts_status"), "runbook_drafts", ["status"])

    op.create_table(
        "runbook_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("source_path", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("change_reason", sa.String(length=64), nullable=False),
        sa.Column("related_incident_id", sa.String(length=64), nullable=True),
        sa.Column("related_draft_id", sa.String(length=64), nullable=True),
        sa.Column("diff_from_previous", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_runbook_versions_version_id"), "runbook_versions", ["version_id"], unique=True
    )
    op.create_index(
        op.f("ix_runbook_versions_document_id"), "runbook_versions", ["document_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_runbook_versions_document_id"), table_name="runbook_versions")
    op.drop_index(op.f("ix_runbook_versions_version_id"), table_name="runbook_versions")
    op.drop_table("runbook_versions")
    op.drop_index(op.f("ix_runbook_drafts_status"), table_name="runbook_drafts")
    op.drop_index(op.f("ix_runbook_drafts_fingerprint"), table_name="runbook_drafts")
    op.drop_index(op.f("ix_runbook_drafts_draft_id"), table_name="runbook_drafts")
    op.drop_table("runbook_drafts")
