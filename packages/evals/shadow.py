"""Shadow mode runner — run parallel diagnosis without side effects.

Runs a shadow diagnosis with a different model/prompt version, writing
results only to eval tables. Never touches real incidents, agent_runs,
approvals, or actions.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import EvalRun
from packages.db.repositories.incidents import IncidentRepository


def run_shadow_diagnosis(
    db: Session,
    incident_id: str,
    shadow_model: str,
    shadow_prompt_version: str,
) -> EvalRun:
    """Run a shadow diagnosis and persist results to eval tables.

    This is a placeholder stub — full implementation requires wiring up
    the diagnosis graph with alternate model/prompt and writing results
    to eval_run/eval_case tables without side effects.
    """
    incident_repo = IncidentRepository(db)
    incident = incident_repo.get_by_public_id(incident_id)

    eval_run = EvalRun(
        eval_run_id=new_id("eval_"),
        status="shadow_started",
        suite="shadow",
        model_name=shadow_model,
        prompt_version=shadow_prompt_version,
        started_at=utc_now(),
    )
    db.add(eval_run)
    db.flush()

    if incident is None:
        eval_run.status = "shadow_failed"
        eval_run.metrics = {"error": "incident not found"}
        eval_run.finished_at = utc_now()
        db.commit()
        return eval_run

    # Placeholder: full shadow mode would clone the agent deps with the
    # shadow model/prompt, run the diagnosis graph, and compare outputs
    # without touching real incident/agent_run/approval/action tables.
    eval_run.status = "shadow_completed"
    eval_run.finished_at = utc_now()
    eval_run.metrics = {
        "incident_id": incident_id,
        "shadow_model": shadow_model,
        "shadow_prompt_version": shadow_prompt_version,
        "note": "shadow mode stub — full implementation pending",
    }
    db.commit()
    return eval_run
