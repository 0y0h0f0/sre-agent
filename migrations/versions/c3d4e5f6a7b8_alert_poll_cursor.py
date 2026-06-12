"""alert_poll_cursor

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-12 14:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create alert_poll_cursors table for Alertmanager poll dedup."""
    op.create_table(
        "alert_poll_cursors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filter_hash", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("incident_id", sa.String(64), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "missing_rounds",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "filter_hash", "fingerprint",
            name="uq_poll_cursor_filter_fp",
        ),
    )
    op.create_index(
        "ix_alert_poll_cursors_filter_hash",
        "alert_poll_cursors",
        ["filter_hash"],
    )
    op.create_index(
        "ix_alert_poll_cursors_fingerprint",
        "alert_poll_cursors",
        ["fingerprint"],
    )


def downgrade() -> None:
    """Remove alert_poll_cursors table."""
    op.drop_index("ix_alert_poll_cursors_fingerprint", table_name="alert_poll_cursors")
    op.drop_index("ix_alert_poll_cursors_filter_hash", table_name="alert_poll_cursors")
    op.drop_table("alert_poll_cursors")
