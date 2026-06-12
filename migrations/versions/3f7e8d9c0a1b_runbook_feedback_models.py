"""runbook_feedback_models

Revision ID: 3f7e8d9c0a1b
Revises: 2e6d6dbb06eb
Create Date: 2026-06-12 16:00:00.000000

M7: RunbookFeedbackSummary + AmendmentDraft tables for deterministic
runbook feedback analysis (no LLM / web_search).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "3f7e8d9c0a1b"
down_revision = "2e6d6dbb06eb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create runbook feedback summary and amendment draft tables."""

    op.create_table(
        "runbook_feedback_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("summary_id", sa.String(64), nullable=False),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("fault_type", sa.String(128), nullable=False),
        sa.Column("incident_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "incident_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("total_actions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("successful_actions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_actions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_actions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_actions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "top_action_types",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "missing_fault_types",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "missing_diagnostic_steps",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "recurring_evidence_patterns",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "generated_by",
            sa.String(128),
            nullable=False,
            server_default="runbook_feedback",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_runbook_feedback_summaries_summary_id",
        "runbook_feedback_summaries",
        ["summary_id"],
        unique=True,
    )
    op.create_index(
        "ix_runbook_feedback_summaries_service_fault",
        "runbook_feedback_summaries",
        ["service", "fault_type"],
    )
    op.create_index(
        "ix_runbook_feedback_summaries_cooldown",
        "runbook_feedback_summaries",
        ["cooldown_until"],
    )

    op.create_table(
        "amendment_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("amendment_id", sa.String(64), nullable=False),
        sa.Column("summary_id", sa.String(64), nullable=False),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("fault_type", sa.String(128), nullable=False),
        sa.Column("target_draft_id", sa.String(64), nullable=True),
        sa.Column(
            "amendment_type",
            sa.String(32),
            nullable=False,
            server_default="addition",
        ),
        sa.Column("section_path", sa.String(255), nullable=True),
        sa.Column("original_content", sa.Text(), nullable=True),
        sa.Column("proposed_content", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "evidence_incident_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "evidence_action_stats",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending_review",
        ),
        sa.Column("reviewer", sa.String(128), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["summary_id"],
            ["runbook_feedback_summaries.summary_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_amendment_drafts_amendment_id",
        "amendment_drafts",
        ["amendment_id"],
        unique=True,
    )
    op.create_index(
        "ix_amendment_drafts_summary_id",
        "amendment_drafts",
        ["summary_id"],
    )
    op.create_index(
        "ix_amendment_drafts_service",
        "amendment_drafts",
        ["service"],
    )
    op.create_index(
        "ix_amendment_drafts_status",
        "amendment_drafts",
        ["status"],
    )


def downgrade() -> None:
    """Remove runbook feedback summary and amendment draft tables."""
    op.drop_index("ix_amendment_drafts_status", table_name="amendment_drafts")
    op.drop_index("ix_amendment_drafts_service", table_name="amendment_drafts")
    op.drop_index("ix_amendment_drafts_summary_id", table_name="amendment_drafts")
    op.drop_index("ix_amendment_drafts_amendment_id", table_name="amendment_drafts")
    op.drop_table("amendment_drafts")

    op.drop_index(
        "ix_runbook_feedback_summaries_cooldown",
        table_name="runbook_feedback_summaries",
    )
    op.drop_index(
        "ix_runbook_feedback_summaries_service_fault",
        table_name="runbook_feedback_summaries",
    )
    op.drop_index(
        "ix_runbook_feedback_summaries_summary_id",
        table_name="runbook_feedback_summaries",
    )
    op.drop_table("runbook_feedback_summaries")
