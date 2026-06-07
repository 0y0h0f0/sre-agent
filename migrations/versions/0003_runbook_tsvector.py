"""runbook tsvector for hybrid search

Revision ID: 0003_runbook_tsvector
Revises: 0002_phase3_alerts_email
Create Date: 2026-06-04 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_runbook_tsvector"
down_revision = "0002_phase3_alerts_email"
branch_labels = None
depends_on = None

_TSVECTOR_TRIGGER = """
CREATE OR REPLACE FUNCTION runbook_chunks_tsv_trigger_fn() RETURNS trigger AS $$
BEGIN
  NEW.tsv_content :=
    setweight(to_tsvector('english', COALESCE(NEW.title, '')), 'A') ||
    setweight(to_tsvector('english', COALESCE(NEW.content, '')), 'B');
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_runbook_chunks_tsv
  BEFORE INSERT OR UPDATE ON runbook_chunks
  FOR EACH ROW EXECUTE FUNCTION runbook_chunks_tsv_trigger_fn();
"""

_DOWNGRADE_TRIGGER = """
DROP TRIGGER IF EXISTS trg_runbook_chunks_tsv ON runbook_chunks;
DROP FUNCTION IF EXISTS runbook_chunks_tsv_trigger_fn();
"""


def _tsvector_column_type(dialect_name: str) -> sa.types.TypeEngine:
    if dialect_name == "sqlite":
        return sa.Text()
    return postgresql.TSVECTOR()


def upgrade() -> None:
    dialect_name = op.get_context().dialect.name
    op.add_column(
        "runbook_chunks",
        sa.Column("tsv_content", _tsvector_column_type(dialect_name), nullable=True),
    )
    if dialect_name != "sqlite":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.create_index(
            "ix_runbook_chunks_tsv",
            "runbook_chunks",
            [sa.text("tsv_content")],
            postgresql_using="gin",
        )
        op.execute(_TSVECTOR_TRIGGER)
        op.execute(
            sa.text(
                "UPDATE runbook_chunks SET tsv_content = "
                "setweight(to_tsvector('english', COALESCE(title, '')), 'A') || "
                "setweight(to_tsvector('english', COALESCE(content, '')), 'B')"
            )
        )


def downgrade() -> None:
    dialect_name = op.get_context().dialect.name
    if dialect_name != "sqlite":
        op.execute(_DOWNGRADE_TRIGGER)
        op.drop_index("ix_runbook_chunks_tsv", table_name="runbook_chunks")
    op.drop_column("runbook_chunks", "tsv_content")
