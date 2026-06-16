"""runbook_chunk_embeddings

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-15 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

try:
    from pgvector.sqlalchemy import Vector as PgVector
except Exception:  # pragma: no cover - optional in local SQLite test envs
    PgVector = None  # type: ignore[assignment]

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    vector_type = sa.JSON()
    if PgVector is not None:
        vector_type = vector_type.with_variant(PgVector(dim=512), "postgresql")
    op.create_table(
        "runbook_chunk_embeddings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("runbook_chunk_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("embedding_vector", vector_type, nullable=False),
        sa.Column(
            "vector_backend",
            sa.String(length=32),
            nullable=False,
            server_default="pgvector",
        ),
        sa.Column("text_hash", sa.String(length=128), nullable=False),
        sa.Column(
            "redaction_version",
            sa.String(length=32),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="available",
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "runbook_chunk_id",
            "provider",
            "model",
            "dimension",
            "text_hash",
            name="uq_chunk_embedding_provider",
        ),
    )
    op.create_index(
        op.f("ix_runbook_chunk_embeddings_runbook_chunk_id"),
        "runbook_chunk_embeddings",
        ["runbook_chunk_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_runbook_chunk_embeddings_status"),
        "runbook_chunk_embeddings",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_runbook_chunk_embeddings_status"),
        table_name="runbook_chunk_embeddings",
    )
    op.drop_index(
        op.f("ix_runbook_chunk_embeddings_runbook_chunk_id"),
        table_name="runbook_chunk_embeddings",
    )
    op.drop_table("runbook_chunk_embeddings")
