"""discovery_config_models

Revision ID: a1b2c3d4e5f6
Revises: 4dbe6ecad2b1
Create Date: 2026-06-12 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a1b2c3d4e5f6"
down_revision = "4dbe6ecad2b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create discovery/config models and extend audit_logs."""

    # --- AuditLog extensions ---
    op.alter_column(
        "audit_logs",
        "action",
        existing_type=sa.String(32),
        type_=sa.String(64),
        existing_nullable=False,
    )
    op.add_column(
        "audit_logs",
        sa.Column("source", sa.String(32), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("request_id", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_audit_logs_source_created",
        "audit_logs",
        ["source", "created_at"],
    )
    op.create_index(
        "ix_audit_logs_source",
        "audit_logs",
        ["source"],
    )

    # --- DiscoveryRun ---
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "discovery_run_id", sa.String(64), nullable=False
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="running",
        ),
        sa.Column(
            "trigger_type",
            sa.String(32),
            nullable=False,
            server_default="automatic",
        ),
        sa.Column(
            "triggered_by", sa.String(128), nullable=True
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "error_message", sa.Text(), nullable=True
        ),
        sa.Column(
            "summary",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_discovery_runs_run_id",
        "discovery_runs",
        ["discovery_run_id"],
        unique=True,
    )
    op.create_index(
        "ix_discovery_runs_status",
        "discovery_runs",
        ["status"],
    )

    # --- DiscoveryProposal ---
    op.create_table(
        "discovery_proposals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "proposal_id", sa.String(64), nullable=False
        ),
        sa.Column(
            "discovery_run_id",
            sa.String(64),
            sa.ForeignKey(
                "discovery_runs.discovery_run_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending_review",
        ),
        sa.Column(
            "config_diff",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "rejected_reason", sa.Text(), nullable=True
        ),
        sa.Column(
            "reviewed_by", sa.String(128), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_discovery_proposals_proposal_id",
        "discovery_proposals",
        ["proposal_id"],
        unique=True,
    )
    op.create_index(
        "ix_discovery_proposals_run_id",
        "discovery_proposals",
        ["discovery_run_id"],
    )
    op.create_index(
        "ix_discovery_proposals_status",
        "discovery_proposals",
        ["status"],
    )

    # --- EffectiveConfigVersion ---
    op.create_table(
        "effective_config_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "version_id", sa.String(64), nullable=False
        ),
        sa.Column(
            "proposal_id",
            sa.String(64),
            sa.ForeignKey(
                "discovery_proposals.proposal_id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column(
            "version_number", sa.Integer(), nullable=False
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="published",
        ),
        sa.Column(
            "config_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "published_by", sa.String(128), nullable=True
        ),
        sa.Column(
            "rolled_back_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "stale_after_days",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
        sa.Column(
            "stale_warning_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_effective_config_versions_version_id",
        "effective_config_versions",
        ["version_id"],
        unique=True,
    )
    op.create_index(
        "ix_effective_config_versions_proposal_id",
        "effective_config_versions",
        ["proposal_id"],
    )
    op.create_index(
        "ix_effective_config_versions_status",
        "effective_config_versions",
        ["status"],
    )

    # --- DiscoveryOverride ---
    op.create_table(
        "discovery_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "override_id", sa.String(64), nullable=False
        ),
        sa.Column(
            "backend_type", sa.String(32), nullable=False
        ),
        sa.Column(
            "override_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoke_reason", sa.Text(), nullable=True
        ),
        sa.Column(
            "created_by_key_id",
            sa.String(64),
            nullable=True,
        ),
        sa.Column(
            "created_by_scopes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
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
        "ix_discovery_overrides_override_id",
        "discovery_overrides",
        ["override_id"],
        unique=True,
    )
    op.create_index(
        "ix_discovery_overrides_backend_type",
        "discovery_overrides",
        ["backend_type"],
    )
    op.create_index(
        "ix_discovery_overrides_expires_at",
        "discovery_overrides",
        ["expires_at"],
    )


def downgrade() -> None:
    """Remove discovery/config tables and revert audit_logs changes."""

    op.drop_table("discovery_overrides")
    op.drop_table("effective_config_versions")
    op.drop_table("discovery_proposals")
    op.drop_table("discovery_runs")

    op.drop_index("ix_audit_logs_source", table_name="audit_logs")
    op.drop_index("ix_audit_logs_source_created", table_name="audit_logs")
    op.drop_column("audit_logs", "request_id")
    op.drop_column("audit_logs", "source")
    op.alter_column(
        "audit_logs",
        "action",
        existing_type=sa.String(64),
        type_=sa.String(32),
        existing_nullable=False,
    )
