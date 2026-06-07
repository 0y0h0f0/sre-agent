"""add false_positive_patterns, incident_correlations, feedback_items tables

Revision ID: 0006_phase5_feedback
Revises: 0005_runbook_language
Create Date: 2026-06-04 00:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_phase5_feedback"
down_revision = "0005_runbook_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "false_positive_patterns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pattern_id", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column("fingerprint", sa.String(255), unique=True, index=True, nullable=False),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("alert_name", sa.String(255), nullable=False),
        sa.Column("nfa_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("first_nfa_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_nfa_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("suppressed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppressed_by", sa.String(128), nullable=True),
        sa.Column("restored_by", sa.String(128), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_fpp_status", "false_positive_patterns", ["status"])

    op.create_table(
        "incident_correlations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("correlation_id", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column("incident_id_a", sa.String(64), nullable=False, index=True),
        sa.Column("incident_id_b", sa.String(64), nullable=False, index=True),
        sa.Column("correlation_type", sa.String(32), nullable=False),
        sa.Column("similarity_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_correlations_incident_a", "incident_correlations", ["incident_id_a"])
    op.create_index("ix_correlations_incident_b", "incident_correlations", ["incident_id_b"])

    op.create_table(
        "feedback_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("feedback_id", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column("incident_id", sa.String(64), nullable=False, index=True),
        sa.Column("agent_run_id", sa.String(64), nullable=True),
        sa.Column("feedback_type", sa.String(32), nullable=False, index=True),
        sa.Column("original_value", sa.JSON(), nullable=True),
        sa.Column("corrected_value", sa.JSON(), nullable=True),
        sa.Column("delta", sa.JSON(), nullable=True),
        sa.Column("submitted_by", sa.String(128), nullable=False, server_default="sre"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("feedback_items")
    op.drop_table("incident_correlations")
    op.drop_table("false_positive_patterns")
