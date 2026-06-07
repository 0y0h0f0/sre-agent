"""extend eval_runs and eval_cases for Phase 7 eval framework

Revision ID: 0009_phase7_evals
Revises: 0008_phase7_api_keys
Create Date: 2026-06-04 05:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_phase7_evals"
down_revision = "0008_phase7_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "eval_runs",
        sa.Column("suite", sa.String(32), nullable=False, server_default="custom"),
    )
    op.add_column(
        "eval_runs",
        sa.Column(
            "model_name",
            sa.String(128),
            nullable=False,
            server_default="fake-diagnosis-model",
        ),
    )
    op.add_column(
        "eval_runs",
        sa.Column("prompt_version", sa.String(64), nullable=False, server_default="v1"),
    )
    op.add_column(
        "eval_runs",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "eval_runs",
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "eval_runs",
        sa.Column("git_commit", sa.String(64), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "eval_cases",
        sa.Column(
            "eval_run_id",
            sa.String(64),
            sa.ForeignKey("eval_runs.eval_run_id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
    )
    op.add_column(
        "eval_cases",
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "eval_cases",
        sa.Column("actual_root_cause", sa.Text(), nullable=True),
    )
    op.add_column(
        "eval_cases",
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "eval_cases",
        sa.Column("error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("eval_cases", "error")
    op.drop_column("eval_cases", "duration_ms")
    op.drop_column("eval_cases", "actual_root_cause")
    op.drop_column("eval_cases", "status")
    op.drop_column("eval_cases", "eval_run_id")
    op.drop_column("eval_runs", "git_commit")
    op.drop_column("eval_runs", "finished_at")
    op.drop_column("eval_runs", "started_at")
    op.drop_column("eval_runs", "prompt_version")
    op.drop_column("eval_runs", "model_name")
    op.drop_column("eval_runs", "suite")
