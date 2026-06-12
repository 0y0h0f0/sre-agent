"""LangGraph StateGraph builder — wires nodes with dependency injection."""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, StateGraph

from packages.agent.nodes.build_context import build_context
from packages.agent.nodes.collect_all_evidence import collect_all_evidence
from packages.agent.nodes.collect_gap import MAX_DIAGNOSE_CYCLES, collect_gap
from packages.agent.nodes.compress_context import compress_context
from packages.agent.nodes.cross_incident import cross_incident
from packages.agent.nodes.diagnose import diagnose
from packages.agent.nodes.execute_action import execute_action
from packages.agent.nodes.generate_report import generate_report
from packages.agent.nodes.guardrail_check import guardrail_check
from packages.agent.nodes.human_approval import human_approval
from packages.agent.nodes.parse_alert import parse_alert
from packages.agent.nodes.persist_memory import persist_memory
from packages.agent.nodes.plan_actions import plan_actions
from packages.agent.nodes.rank_hypotheses import rank_hypotheses
from packages.agent.nodes.retrieve_memory import retrieve_memory
from packages.agent.nodes.retrieve_runbook import retrieve_runbook
from packages.agent.nodes.take_snapshot import take_snapshot
from packages.agent.nodes.verify import MAX_VERIFY_CYCLES, verify
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState


def build_graph(deps: AgentDeps, checkpointer: Any | None = None) -> Any:
    """Build and compile the incident diagnosis StateGraph.

    Each node function is called as ``node(state) -> state`` by LangGraph.
    ``deps`` is bound via :func:`functools.partial`.
    """
    graph = StateGraph(IncidentState)

    graph.add_node("parse_alert", partial(parse_alert, deps=deps))
    graph.add_node("collect_all_evidence", partial(collect_all_evidence, deps=deps))
    graph.add_node("collect_gap", partial(collect_gap, deps=deps))
    graph.add_node("retrieve_memory", partial(retrieve_memory, deps=deps))
    graph.add_node("cross_incident", partial(cross_incident, deps=deps))
    graph.add_node("retrieve_runbook", partial(retrieve_runbook, deps=deps))
    graph.add_node("build_context", partial(build_context, deps=deps))
    graph.add_node("diagnose", partial(diagnose, deps=deps))
    graph.add_node("rank_hypotheses", partial(rank_hypotheses, deps=deps))
    graph.add_node("plan_actions", partial(plan_actions, deps=deps))
    graph.add_node("guardrail_check", partial(guardrail_check, deps=deps))
    graph.add_node("human_approval", partial(human_approval, deps=deps))
    graph.add_node("compress_context", partial(compress_context, deps=deps))
    graph.add_node("take_snapshot", partial(take_snapshot, deps=deps))
    graph.add_node("execute_action", partial(execute_action, deps=deps))
    graph.add_node("verify", partial(verify, deps=deps))
    graph.add_node("generate_report", partial(generate_report, deps=deps))
    graph.add_node("persist_memory", partial(persist_memory, deps=deps))

    graph.set_entry_point("parse_alert")
    graph.add_edge("parse_alert", "collect_all_evidence")
    graph.add_edge("collect_all_evidence", "retrieve_memory")
    graph.add_edge("retrieve_memory", "cross_incident")
    graph.add_edge("cross_incident", "retrieve_runbook")
    graph.add_edge("retrieve_runbook", "build_context")
    graph.add_edge("build_context", "diagnose")
    graph.add_edge("diagnose", "compress_context")
    graph.add_conditional_edges(
        "compress_context",
        _route_after_diagnose,
        {"collect": "collect_gap", "rank": "rank_hypotheses"},
    )
    graph.add_edge("collect_gap", "build_context")
    graph.add_edge("rank_hypotheses", "plan_actions")
    graph.add_edge("plan_actions", "guardrail_check")

    graph.add_conditional_edges(
        "guardrail_check",
        _route_after_guardrail,
        {"execute": "take_snapshot", "approval": "human_approval", "report": "generate_report"},
    )
    graph.add_conditional_edges(
        "human_approval",
        _route_after_approval,
        {
            "execute": "take_snapshot",
            "replan": "plan_actions",
            "report": "generate_report",
        },
    )
    graph.add_edge("take_snapshot", "execute_action")
    graph.add_edge("execute_action", "verify")
    graph.add_conditional_edges(
        "verify",
        _route_after_verify,
        {"report": "generate_report", "replan": "plan_actions"},
    )
    graph.add_edge("generate_report", "persist_memory")
    graph.add_edge("persist_memory", END)

    return graph.compile(checkpointer=checkpointer)


def _route_after_guardrail(state: IncidentState) -> str:
    needs_approval = state.get("_needs_approval", False)
    all_l4 = state.get("_all_l4", False)
    if all_l4:
        return "report"
    if needs_approval:
        return "approval"
    return "execute"


def _route_after_diagnose(state: IncidentState) -> str:
    """Route after compress_context: collect gaps or proceed to ranking.

    When the LLM diagnosis reports missing_evidence and we have not exceeded
    the cycle limit, route back to collect_gap for targeted re-collection.
    Otherwise proceed to rank_hypotheses.
    """
    rationale = state.get("diagnosis_rationale", {})
    missing = rationale.get("missing_evidence", [])
    cycles = int(state.get("_collect_gap_cycles", 0))
    if missing and cycles < MAX_DIAGNOSE_CYCLES:
        return "collect"
    return "rank"


def _route_after_verify(state: IncidentState) -> str:
    """Route after verify: generate report or replan.

    - ``resolved`` / ``unknown`` / max cycles → generate report
    - ``improving`` / ``unchanged`` / ``degraded`` → replan (plan_actions
      receives degraded feedback with rollback guidance)
    """
    verdict = state.get("verify_result", "resolved")
    cycles = int(state.get("_verify_cycles", 0))
    if verdict in ("resolved", "skipped", "unknown", "error") or cycles >= MAX_VERIFY_CYCLES:
        return "report"
    return "replan"


def _route_after_approval(state: IncidentState) -> str:
    """Route based on human decision after interrupt."""
    from packages.agent.nodes.human_approval import MAX_REPLAN_CYCLES

    phase = state.get("phase", "")
    if phase == "approval_approved":
        return "execute"
    if phase == "approval_rejected":
        # Bound the reject -> replan cycle; give up to report after the cap so
        # repeated rejections cannot loop until the graph recursion limit.
        if int(state.get("_replan_count", 0)) >= MAX_REPLAN_CYCLES:
            return "report"
        return "replan"
    return "report"
