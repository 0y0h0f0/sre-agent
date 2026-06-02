"""LangGraph StateGraph builder — wires nodes with dependency injection."""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, StateGraph

from packages.agent.nodes.build_context import build_context
from packages.agent.nodes.collect_db import collect_db
from packages.agent.nodes.collect_deployment import collect_deployment
from packages.agent.nodes.collect_k8s import collect_k8s
from packages.agent.nodes.collect_logs import collect_logs
from packages.agent.nodes.collect_metrics import collect_metrics
from packages.agent.nodes.collect_traces import collect_traces
from packages.agent.nodes.diagnose import diagnose
from packages.agent.nodes.execute_action import execute_action
from packages.agent.nodes.generate_report import generate_report
from packages.agent.nodes.guardrail_check import guardrail_check
from packages.agent.nodes.human_approval import human_approval
from packages.agent.nodes.parse_alert import parse_alert
from packages.agent.nodes.plan_actions import plan_actions
from packages.agent.nodes.rank_hypotheses import rank_hypotheses
from packages.agent.nodes.retrieve_memory import retrieve_memory
from packages.agent.nodes.retrieve_runbook import retrieve_runbook
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState


def build_graph(deps: AgentDeps, checkpointer: Any | None = None) -> Any:
    """Build and compile the incident diagnosis StateGraph.

    Each node function is called as ``node(state) -> state`` by LangGraph.
    ``deps`` is bound via :func:`functools.partial`.
    """
    graph = StateGraph(IncidentState)

    graph.add_node("parse_alert", partial(parse_alert, deps=deps))
    graph.add_node("collect_metrics", partial(collect_metrics, deps=deps))
    graph.add_node("collect_logs", partial(collect_logs, deps=deps))
    graph.add_node("collect_traces", partial(collect_traces, deps=deps))
    graph.add_node("collect_deployment", partial(collect_deployment, deps=deps))
    graph.add_node("collect_k8s", partial(collect_k8s, deps=deps))
    graph.add_node("collect_db", partial(collect_db, deps=deps))
    graph.add_node("retrieve_memory", partial(retrieve_memory, deps=deps))
    graph.add_node("retrieve_runbook", partial(retrieve_runbook, deps=deps))
    graph.add_node("build_context", partial(build_context, deps=deps))
    graph.add_node("diagnose", partial(diagnose, deps=deps))
    graph.add_node("rank_hypotheses", partial(rank_hypotheses, deps=deps))
    graph.add_node("plan_actions", partial(plan_actions, deps=deps))
    graph.add_node("guardrail_check", partial(guardrail_check, deps=deps))
    graph.add_node("human_approval", partial(human_approval, deps=deps))
    graph.add_node("execute_action", partial(execute_action, deps=deps))
    graph.add_node("generate_report", partial(generate_report, deps=deps))

    graph.set_entry_point("parse_alert")
    graph.add_edge("parse_alert", "collect_metrics")
    graph.add_edge("collect_metrics", "collect_logs")
    graph.add_edge("collect_logs", "collect_traces")
    graph.add_edge("collect_traces", "collect_deployment")
    graph.add_edge("collect_deployment", "collect_k8s")
    graph.add_edge("collect_k8s", "collect_db")
    graph.add_edge("collect_db", "retrieve_memory")
    graph.add_edge("retrieve_memory", "retrieve_runbook")
    graph.add_edge("retrieve_runbook", "build_context")
    graph.add_edge("build_context", "diagnose")
    graph.add_edge("diagnose", "rank_hypotheses")
    graph.add_edge("rank_hypotheses", "plan_actions")
    graph.add_edge("plan_actions", "guardrail_check")

    graph.add_conditional_edges(
        "guardrail_check",
        _route_after_guardrail,
        {"execute": "execute_action", "approval": "human_approval", "report": "generate_report"},
    )
    graph.add_conditional_edges(
        "human_approval",
        _route_after_approval,
        {
            "execute": "execute_action",
            "replan": "plan_actions",
            "report": "generate_report",
        },
    )
    graph.add_edge("execute_action", "generate_report")
    graph.add_edge("generate_report", END)

    return graph.compile(checkpointer=checkpointer)


def _route_after_guardrail(state: IncidentState) -> str:
    needs_approval = state.get("_needs_approval", False)
    all_l4 = state.get("_all_l4", False)
    if all_l4:
        return "report"
    if needs_approval:
        return "approval"
    return "execute"


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
