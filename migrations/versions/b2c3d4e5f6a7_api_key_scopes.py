"""api_key_scopes

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-12 11:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add roles, scopes, is_bootstrap columns to api_keys."""
    op.add_column(
        "api_keys",
        sa.Column(
            "roles", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column(
            "scopes", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column(
            "is_bootstrap", sa.Boolean(), nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    """Remove roles, scopes, is_bootstrap from api_keys."""
    op.drop_column("api_keys", "is_bootstrap")
    op.drop_column("api_keys", "scopes")
    op.drop_column("api_keys", "roles")
