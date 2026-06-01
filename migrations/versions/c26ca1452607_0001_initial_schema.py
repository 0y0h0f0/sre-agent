"""0001_initial_schema

Revision ID: c26ca1452607
Revises: 
Create Date: 2026-06-01 20:02:07.047719
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

try:
    from pgvector.sqlalchemy import Vector as PgVector
except ImportError:  # pragma: no cover
    PgVector = None  # type: ignore[assignment]

# revision identifiers, used by Alembic.
revision = 'c26ca1452607'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension on PostgreSQL.
    if op.get_context().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('eval_cases',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('eval_case_id', sa.String(length=64), nullable=False),
    sa.Column('incident_type', sa.String(length=128), nullable=False),
    sa.Column('fixture_path', sa.String(length=512), nullable=False),
    sa.Column('expected_root_cause', sa.Text(), nullable=False),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_eval_cases_eval_case_id'), 'eval_cases', ['eval_case_id'], unique=True)
    op.create_table('eval_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('eval_run_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('metrics', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_eval_runs_eval_run_id'), 'eval_runs', ['eval_run_id'], unique=True)
    op.create_table('incidents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('incident_id', sa.String(length=64), nullable=False),
    sa.Column('fingerprint', sa.String(length=255), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('service', sa.String(length=128), nullable=False),
    sa.Column('severity', sa.String(length=8), nullable=False),
    sa.Column('alert_name', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('labels', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('annotations', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('root_cause_summary', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_incidents_incident_id'), 'incidents', ['incident_id'], unique=True)
    op.create_index('ix_incidents_service_created_at', 'incidents', ['service', sa.literal_column('created_at DESC')], unique=False)
    op.create_index(op.f('ix_incidents_status'), 'incidents', ['status'], unique=False)
    op.create_index('ix_incidents_status_severity', 'incidents', ['status', 'severity'], unique=False)
    op.create_index('uq_incidents_open_fingerprint', 'incidents', ['fingerprint'], unique=True, sqlite_where=sa.text("status NOT IN ('resolved', 'failed', 'mitigated')"), postgresql_where=sa.text("status NOT IN ('resolved', 'failed', 'mitigated')"))
    op.create_table('memory_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('memory_id', sa.String(length=64), nullable=False),
    sa.Column('scope', sa.String(length=32), nullable=False),
    sa.Column('scope_key', sa.String(length=255), nullable=False),
    sa.Column('memory_type', sa.String(length=32), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('embedding', sa.JSON().with_variant(PgVector(dim=384), 'postgresql'), nullable=True),
    sa.Column('importance', sa.Float(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source_ref', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_memory_items_memory_id'), 'memory_items', ['memory_id'], unique=True)
    op.create_index(op.f('ix_memory_items_scope_key'), 'memory_items', ['scope_key'], unique=False)
    op.create_table('runbook_chunks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chunk_id', sa.String(length=64), nullable=False),
    sa.Column('document_id', sa.String(length=128), nullable=False),
    sa.Column('source_path', sa.String(length=512), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=128), nullable=False),
    sa.Column('embedding', sa.JSON().with_variant(PgVector(dim=384), 'postgresql'), nullable=False),
    sa.Column('embedding_model', sa.String(length=128), nullable=False),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('content_hash')
    )
    op.create_index(op.f('ix_runbook_chunks_chunk_id'), 'runbook_chunks', ['chunk_id'], unique=True)
    op.create_index(op.f('ix_runbook_chunks_document_id'), 'runbook_chunks', ['document_id'], unique=False)
    op.create_table('agent_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('incident_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('celery_task_id', sa.String(length=255), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('model_name', sa.String(length=128), nullable=False),
    sa.Column('prompt_version', sa.String(length=64), nullable=False),
    sa.Column('state', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('checkpoint_thread_id', sa.String(length=64), nullable=True),
    sa.Column('checkpoint_ns', sa.String(length=64), nullable=False),
    sa.Column('latest_checkpoint_id', sa.String(length=255), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('total_prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('total_completion_tokens', sa.Integer(), nullable=False),
    sa.Column('provider_cache_hit_count', sa.Integer(), nullable=False),
    sa.Column('provider_cache_miss_count', sa.Integer(), nullable=False),
    sa.Column('app_cache_hit_count', sa.Integer(), nullable=False),
    sa.Column('app_cache_miss_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.incident_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_agent_runs_agent_run_id'), 'agent_runs', ['agent_run_id'], unique=True)
    op.create_index(op.f('ix_agent_runs_incident_id'), 'agent_runs', ['incident_id'], unique=False)
    op.create_index(op.f('ix_agent_runs_status'), 'agent_runs', ['status'], unique=False)
    op.create_table('actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('action_id', sa.String(length=64), nullable=False),
    sa.Column('incident_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('type', sa.String(length=128), nullable=False),
    sa.Column('risk_level', sa.String(length=8), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('executor', sa.String(length=64), nullable=False),
    sa.Column('target', sa.String(length=255), nullable=True),
    sa.Column('params', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('rollback_plan', sa.Text(), nullable=True),
    sa.Column('execution_result', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.agent_run_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.incident_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_actions_action_id'), 'actions', ['action_id'], unique=True)
    op.create_index(op.f('ix_actions_agent_run_id'), 'actions', ['agent_run_id'], unique=False)
    op.create_index(op.f('ix_actions_incident_id'), 'actions', ['incident_id'], unique=False)
    op.create_table('agent_run_nodes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('input_summary', sa.Text(), nullable=True),
    sa.Column('output_summary', sa.Text(), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.agent_run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_agent_run_nodes_agent_run_id'), 'agent_run_nodes', ['agent_run_id'], unique=False)
    op.create_index(op.f('ix_agent_run_nodes_node_id'), 'agent_run_nodes', ['node_id'], unique=True)
    op.create_table('evidence_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('evidence_id', sa.String(length=64), nullable=False),
    sa.Column('incident_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('type', sa.String(length=32), nullable=False),
    sa.Column('source', sa.String(length=128), nullable=False),
    sa.Column('source_id', sa.String(length=255), nullable=True),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('excerpt', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.agent_run_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.incident_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_evidence_items_agent_run_id'), 'evidence_items', ['agent_run_id'], unique=False)
    op.create_index(op.f('ix_evidence_items_evidence_id'), 'evidence_items', ['evidence_id'], unique=True)
    op.create_index(op.f('ix_evidence_items_incident_id'), 'evidence_items', ['incident_id'], unique=False)
    op.create_table('incident_reports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('report_id', sa.String(length=64), nullable=False),
    sa.Column('incident_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('root_cause', sa.Text(), nullable=False),
    sa.Column('impact', sa.Text(), nullable=False),
    sa.Column('timeline', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('actions', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('follow_ups', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('body_markdown', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.agent_run_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.incident_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('incident_id', 'version', name='uq_report_incident_version')
    )
    op.create_index(op.f('ix_incident_reports_agent_run_id'), 'incident_reports', ['agent_run_id'], unique=False)
    op.create_index(op.f('ix_incident_reports_incident_id'), 'incident_reports', ['incident_id'], unique=False)
    op.create_index(op.f('ix_incident_reports_report_id'), 'incident_reports', ['report_id'], unique=True)
    op.create_table('memory_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('node_name', sa.String(length=128), nullable=False),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('before_tokens', sa.Integer(), nullable=False),
    sa.Column('after_tokens', sa.Integer(), nullable=False),
    sa.Column('compression_ratio', sa.Float(), nullable=True),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.agent_run_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_memory_events_agent_run_id'), 'memory_events', ['agent_run_id'], unique=False)
    op.create_index(op.f('ix_memory_events_event_id'), 'memory_events', ['event_id'], unique=True)
    op.create_table('tool_calls',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tool_call_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('node_name', sa.String(length=128), nullable=False),
    sa.Column('tool_name', sa.String(length=128), nullable=False),
    sa.Column('input_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('input_summary', sa.Text(), nullable=False),
    sa.Column('output_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('output_summary', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('cache_key', sa.String(length=255), nullable=True),
    sa.Column('cache_hit', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.agent_run_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tool_calls_agent_run_id'), 'tool_calls', ['agent_run_id'], unique=False)
    op.create_index(op.f('ix_tool_calls_tool_call_id'), 'tool_calls', ['tool_call_id'], unique=True)
    op.create_table('approvals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('approval_id', sa.String(length=64), nullable=False),
    sa.Column('action_id', sa.String(length=64), nullable=False),
    sa.Column('incident_id', sa.String(length=64), nullable=False),
    sa.Column('agent_run_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('approver', sa.String(length=128), nullable=True),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('risk_ack', sa.Boolean(), nullable=False),
    sa.Column('confirm_action_type', sa.String(length=128), nullable=True),
    sa.Column('confirm_target', sa.String(length=255), nullable=True),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resume_token', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['action_id'], ['actions.action_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['agent_run_id'], ['agent_runs.agent_run_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.incident_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_approvals_action_id'), 'approvals', ['action_id'], unique=False)
    op.create_index(op.f('ix_approvals_agent_run_id'), 'approvals', ['agent_run_id'], unique=False)
    op.create_index(op.f('ix_approvals_approval_id'), 'approvals', ['approval_id'], unique=True)
    op.create_index(op.f('ix_approvals_incident_id'), 'approvals', ['incident_id'], unique=False)
    op.create_index(op.f('ix_approvals_status'), 'approvals', ['status'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f('ix_approvals_status'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_incident_id'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_approval_id'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_agent_run_id'), table_name='approvals')
    op.drop_index(op.f('ix_approvals_action_id'), table_name='approvals')
    op.drop_table('approvals')
    op.drop_index(op.f('ix_tool_calls_tool_call_id'), table_name='tool_calls')
    op.drop_index(op.f('ix_tool_calls_agent_run_id'), table_name='tool_calls')
    op.drop_table('tool_calls')
    op.drop_index(op.f('ix_memory_events_event_id'), table_name='memory_events')
    op.drop_index(op.f('ix_memory_events_agent_run_id'), table_name='memory_events')
    op.drop_table('memory_events')
    op.drop_index(op.f('ix_incident_reports_report_id'), table_name='incident_reports')
    op.drop_index(op.f('ix_incident_reports_incident_id'), table_name='incident_reports')
    op.drop_index(op.f('ix_incident_reports_agent_run_id'), table_name='incident_reports')
    op.drop_table('incident_reports')
    op.drop_index(op.f('ix_evidence_items_incident_id'), table_name='evidence_items')
    op.drop_index(op.f('ix_evidence_items_evidence_id'), table_name='evidence_items')
    op.drop_index(op.f('ix_evidence_items_agent_run_id'), table_name='evidence_items')
    op.drop_table('evidence_items')
    op.drop_index(op.f('ix_agent_run_nodes_node_id'), table_name='agent_run_nodes')
    op.drop_index(op.f('ix_agent_run_nodes_agent_run_id'), table_name='agent_run_nodes')
    op.drop_table('agent_run_nodes')
    op.drop_index(op.f('ix_actions_incident_id'), table_name='actions')
    op.drop_index(op.f('ix_actions_agent_run_id'), table_name='actions')
    op.drop_index(op.f('ix_actions_action_id'), table_name='actions')
    op.drop_table('actions')
    op.drop_index(op.f('ix_agent_runs_status'), table_name='agent_runs')
    op.drop_index(op.f('ix_agent_runs_incident_id'), table_name='agent_runs')
    op.drop_index(op.f('ix_agent_runs_agent_run_id'), table_name='agent_runs')
    op.drop_table('agent_runs')
    op.drop_index(op.f('ix_runbook_chunks_document_id'), table_name='runbook_chunks')
    op.drop_index(op.f('ix_runbook_chunks_chunk_id'), table_name='runbook_chunks')
    op.drop_table('runbook_chunks')
    op.drop_index(op.f('ix_memory_items_scope_key'), table_name='memory_items')
    op.drop_index(op.f('ix_memory_items_memory_id'), table_name='memory_items')
    op.drop_table('memory_items')
    op.drop_index('uq_incidents_open_fingerprint', table_name='incidents', sqlite_where=sa.text("status NOT IN ('resolved', 'failed', 'mitigated')"), postgresql_where=sa.text("status NOT IN ('resolved', 'failed', 'mitigated')"))
    op.drop_index('ix_incidents_status_severity', table_name='incidents')
    op.drop_index(op.f('ix_incidents_status'), table_name='incidents')
    op.drop_index('ix_incidents_service_created_at', table_name='incidents')
    op.drop_index(op.f('ix_incidents_incident_id'), table_name='incidents')
    op.drop_table('incidents')
    op.drop_index(op.f('ix_eval_runs_eval_run_id'), table_name='eval_runs')
    op.drop_table('eval_runs')
    op.drop_index(op.f('ix_eval_cases_eval_case_id'), table_name='eval_cases')
    op.drop_table('eval_cases')
    # ### end Alembic commands ###
