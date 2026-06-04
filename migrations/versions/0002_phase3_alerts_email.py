"""phase3 alerts and email notifications

Revision ID: 0002_phase3_alerts_email
Revises: c26ca1452607
Create Date: 2026-06-03 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision = "0002_phase3_alerts_email"
down_revision = "c26ca1452607"
branch_labels = None
depends_on = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    json_type = _json_type()
    dialect_name = op.get_context().dialect.name
    raw_payload_default = (
        sa.text("'{}'::jsonb") if dialect_name == "postgresql" else sa.text("'{}'")
    )
    op.add_column(
        "incidents",
        sa.Column(
            "raw_payload",
            json_type,
            nullable=False,
            server_default=raw_payload_default,
        ),
    )
    if dialect_name != "sqlite":
        op.alter_column("incidents", "raw_payload", server_default=None)

    op.create_table(
        "email_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email_log_id", sa.String(length=64), nullable=False),
        sa.Column("notification_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("recipients", json_type, nullable=False),
        sa.Column("recipient_count", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("related_incident_id", sa.String(length=64), nullable=True),
        sa.Column("related_agent_run_id", sa.String(length=64), nullable=True),
        sa.Column("related_approval_id", sa.String(length=64), nullable=True),
        sa.Column("related_report_id", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_email_log_email_log_id"), "email_log", ["email_log_id"], unique=True)
    op.create_index(op.f("ix_email_log_notification_type"), "email_log", ["notification_type"])
    op.create_index(op.f("ix_email_log_status"), "email_log", ["status"])
    op.create_index("ix_email_log_created_at", "email_log", [sa.literal_column("created_at DESC")])
    op.create_index(op.f("ix_email_log_related_incident_id"), "email_log", ["related_incident_id"])
    op.create_index(
        op.f("ix_email_log_related_agent_run_id"), "email_log", ["related_agent_run_id"]
    )
    op.create_index(op.f("ix_email_log_related_approval_id"), "email_log", ["related_approval_id"])
    op.create_index(op.f("ix_email_log_related_report_id"), "email_log", ["related_report_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_email_log_related_report_id"), table_name="email_log")
    op.drop_index(op.f("ix_email_log_related_approval_id"), table_name="email_log")
    op.drop_index(op.f("ix_email_log_related_agent_run_id"), table_name="email_log")
    op.drop_index(op.f("ix_email_log_related_incident_id"), table_name="email_log")
    op.drop_index("ix_email_log_created_at", table_name="email_log")
    op.drop_index(op.f("ix_email_log_status"), table_name="email_log")
    op.drop_index(op.f("ix_email_log_notification_type"), table_name="email_log")
    op.drop_index(op.f("ix_email_log_email_log_id"), table_name="email_log")
    op.drop_table("email_log")
    op.drop_column("incidents", "raw_payload")
