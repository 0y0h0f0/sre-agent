"""add incident_comments, evidence_annotations, audit_logs, approval_groups
tables, and email_token on approvals

Revision ID: 0007_phase6_collaboration
Revises: 0006_phase5_feedback
Create Date: 2026-06-04 02:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_phase6_collaboration"
down_revision = "0006_phase5_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Incident comments with threading
    op.create_table(
        "incident_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("comment_id", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column(
            "incident_id",
            sa.String(64),
            sa.ForeignKey("incidents.incident_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("author", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("parent_comment_id", sa.String(64), nullable=True, index=True),
        sa.Column(
            "mentioned_users",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Evidence annotations
    op.create_table(
        "evidence_annotations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("annotation_id", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column(
            "evidence_id",
            sa.String(64),
            sa.ForeignKey("evidence_items.evidence_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "incident_id",
            sa.String(64),
            sa.ForeignKey("incidents.incident_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("author", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Audit trail
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("audit_id", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column(
            "incident_id",
            sa.String(64),
            sa.ForeignKey("incidents.incident_id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False, index=True),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column(
            "details",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
            server_default=sa.text("now()"),
        ),
    )

    # Approval groups for team-based routing
    op.create_table(
        "approval_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("group_id", sa.String(64), unique=True, index=True, nullable=False),
        sa.Column("name", sa.String(128), unique=True, nullable=False),
        sa.Column("service_pattern", sa.String(255), nullable=False),
        sa.Column(
            "members",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
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
    )

    # Email approval token on existing approvals table
    op.add_column(
        "approvals",
        sa.Column("email_token", sa.String(64), nullable=True, unique=True),
    )
    op.add_column(
        "approvals",
        sa.Column("email_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approvals_email_token", "approvals", ["email_token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_approvals_email_token")
    op.drop_column("approvals", "email_token_expires_at")
    op.drop_column("approvals", "email_token")
    op.drop_table("approval_groups")
    op.drop_table("audit_logs")
    op.drop_table("evidence_annotations")
    op.drop_table("incident_comments")
