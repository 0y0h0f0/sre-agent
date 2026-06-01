from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from apps.api.schemas.agent_runs import AgentRunDetailResponse
from apps.api.services.agent_run_service import AgentRunService

router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"])


@router.get("/{agent_run_id}", response_model=AgentRunDetailResponse)
def get_agent_run(
    agent_run_id: str,
    db: Session = Depends(get_db),
) -> AgentRunDetailResponse:
    return AgentRunService(db).get_detail(agent_run_id)
