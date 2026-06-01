"""Rank hypotheses by evidence strength, diversity, and correlation."""

from __future__ import annotations

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now


def rank_hypotheses(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        hypotheses = state.get("hypotheses", [])
        if not hypotheses:
            deps.node_tracer(
                node_id=node_id,
                agent_run_id=state["agent_run_id"],
                name="rank_hypotheses",
                status="succeeded",
                started_at=started_at,
                finished_at=utc_now(),
                input_summary="no hypotheses",
                output_summary="skipped",
            )
            return {**state, "phase": "hypotheses_ranked"}

        deployment_evidence = state.get("deployment_evidence", [])
        runbook_ctx = state.get("runbook_context", [])
        memory_ctx = state.get("memory_context", [])

        ranked = []
        for h in hypotheses:
            eids = h.get("supporting_evidence_ids", [])
            evidence_count = len(eids)
            source_diversity = len(
                {eid.split("_")[0] if "_" in eid else eid[:4] for eid in eids if eid}
            )

            stmt_lower = h.get("statement", "").lower()
            dep_corr = (
                0.8
                if deployment_evidence
                and any(kw in stmt_lower for kw in ("deploy", "release", "rollback"))
                else 0.0
            )

            rb_score = 0.0
            kw3 = stmt_lower.split()[:3]
            for chunk in runbook_ctx:
                if any(kw in chunk.get("excerpt", "").lower() for kw in kw3):
                    rb_score = max(rb_score, chunk.get("score", 0))

            mem_score = 0.0
            for mem in memory_ctx:
                if any(kw in mem.get("content", "").lower() for kw in kw3):
                    mem_score = max(mem_score, mem.get("importance", 0))

            score = (
                min(evidence_count / 5.0, 1.0) * 0.35
                + (source_diversity / 4.0) * 0.20
                + dep_corr * 0.20
                + rb_score * 0.15
                + mem_score * 0.10
            )
            ranked.append(
                {
                    **h,
                    "evidence_count": evidence_count,
                    "source_diversity": source_diversity,
                    "deployment_correlation": dep_corr,
                    "runbook_match_score": rb_score,
                    "memory_similarity_score": mem_score,
                    "score": round(score, 4),
                }
            )

        ranked.sort(key=lambda h: h.get("score", 0), reverse=True)
        for i, h in enumerate(ranked):
            h["rank"] = i + 1

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="rank_hypotheses",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"candidates={len(hypotheses)}",
            output_summary=f"top={ranked[0].get('statement', '')[:80]}" if ranked else "empty",
        )
        return {**state, "hypotheses": ranked, "phase": "hypotheses_ranked"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="rank_hypotheses",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "rank_hypotheses", "error": str(exc)})
        return state
