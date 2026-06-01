from __future__ import annotations

from sqlalchemy.orm import Session

from apps.api.schemas.agent_runs import AgentRunDetailResponse, AgentRunNode, ToolCallSummary
from apps.api.schemas.common import AgentRunStatus
from packages.common.errors import NotFoundError
from packages.db.models import AgentRunNode as AgentRunNodeModel
from packages.db.models import ToolCall as ToolCallModel
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.tool_calls import ToolCallRepository


class AgentRunService:
    def __init__(self, db: Session) -> None:
        self.agent_runs = AgentRunRepository(db)
        self.tool_calls = ToolCallRepository(db)

    def get_detail(self, agent_run_id: str) -> AgentRunDetailResponse:
        run = self.agent_runs.get_by_public_id(agent_run_id)
        if run is None:
            raise NotFoundError("agent_run", agent_run_id)
        nodes = [self._node_schema(node) for node in self.agent_runs.list_nodes(agent_run_id)]
        tool_calls = [
            self._tool_call_schema(call) for call in self.tool_calls.list_for_run(agent_run_id)
        ]
        return AgentRunDetailResponse(
            agent_run_id=run.agent_run_id,
            incident_id=run.incident_id,
            status=AgentRunStatus(run.status),
            celery_task_id=run.celery_task_id,
            error_code=run.error_code,
            error_message=run.error_message,
            state=run.state,
            checkpoint_thread_id=run.checkpoint_thread_id,
            checkpoint_ns=run.checkpoint_ns,
            latest_checkpoint_id=run.latest_checkpoint_id,
            nodes=nodes,
            tool_calls=tool_calls,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    def _node_schema(self, node: AgentRunNodeModel) -> AgentRunNode:
        return AgentRunNode(
            name=node.name,
            status=node.status,
            started_at=node.started_at,
            finished_at=node.finished_at,
            duration_ms=node.duration_ms,
            input_summary=node.input_summary,
            output_summary=node.output_summary,
            tool_calls=[],
        )

    def _tool_call_schema(self, call: ToolCallModel) -> ToolCallSummary:
        return ToolCallSummary(
            tool_call_id=call.tool_call_id,
            node_name=call.node_name,
            tool_name=call.tool_name,
            status=call.status,
            input_summary=call.input_summary,
            output_summary=call.output_summary,
            duration_ms=call.duration_ms,
            cache_key=call.cache_key,
            cache_hit=call.cache_hit,
            error_message=call.error_message,
            created_at=call.created_at,
        )
