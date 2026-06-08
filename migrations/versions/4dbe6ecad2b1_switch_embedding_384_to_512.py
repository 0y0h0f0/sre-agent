"""switch_embedding_384_to_512

Revision ID: 4dbe6ecad2b1
Revises: 0009_phase7_evals
Create Date: 2026-06-07 21:48:30.907584
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "4dbe6ecad2b1"
down_revision = "0009_phase7_evals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Switch embedding columns from 384-dim to 512-dim (bge_zh compatible)."""
    op.execute(
        "ALTER TABLE runbook_chunks ALTER COLUMN embedding TYPE vector(512)"
    )
    op.execute(
        "ALTER TABLE memory_items ALTER COLUMN embedding TYPE vector(512)"
    )


def downgrade() -> None:
    """Revert embedding columns back to 384-dim."""
    op.execute(
        "ALTER TABLE runbook_chunks ALTER COLUMN embedding TYPE vector(384)"
    )
    op.execute(
        "ALTER TABLE memory_items ALTER COLUMN embedding TYPE vector(384)"
    )
