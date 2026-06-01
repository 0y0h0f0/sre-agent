from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import ToolCall
from packages.tools.base import ToolResult


class ToolCallRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        agent_run_id: str,
        node_name: str,
        tool_name: str,
        query: BaseModel | dict[str, Any],
        result: ToolResult,
        input_summary: str,
        tool_call_id: str | None = None,
    ) -> ToolCall:
        call = ToolCall(
            tool_call_id=tool_call_id or new_id("tool_"),
            agent_run_id=agent_run_id,
            node_name=node_name,
            tool_name=tool_name,
            input_json=_query_json(query),
            input_summary=input_summary,
            output_json=result.model_dump(mode="json"),
            output_summary=result.summary,
            status=result.status,
            error_message=result.error_message,
            duration_ms=result.duration_ms,
            cache_key=result.cache_key,
            cache_hit=result.cache_hit,
        )
        self.db.add(call)
        return call

    def list_for_run(self, agent_run_id: str) -> Sequence[ToolCall]:
        stmt = (
            select(ToolCall)
            .where(ToolCall.agent_run_id == agent_run_id)
            .order_by(ToolCall.created_at.asc(), ToolCall.id.asc())
        )
        return self.db.scalars(stmt).all()

    def list_for_node(self, agent_run_id: str, node_name: str) -> Sequence[ToolCall]:
        stmt = (
            select(ToolCall)
            .where(ToolCall.agent_run_id == agent_run_id, ToolCall.node_name == node_name)
            .order_by(ToolCall.created_at.asc(), ToolCall.id.asc())
        )
        return self.db.scalars(stmt).all()


def _query_json(query: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(query, BaseModel):
        return query.model_dump(mode="json")
    return dict(query)
