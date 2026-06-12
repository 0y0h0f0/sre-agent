"""Unit tests for Phase 2 multi-perspective LLM sub-agent diagnosis."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from packages.agent.nodes.diagnose import (
    _load_topology,
    _multi_perspective_enabled,
    _run_specialist,
    _serialize_partial_output,
    diagnose,
)
from packages.agent.schemas import AgentDeps, DiagnosisOutput
from packages.agent.state import IncidentState
from packages.common.settings import Settings
from packages.memory.context_builder import ContextBuilder
from packages.tools.cache import RequestLocalToolCache


def _settings(**overrides: Any) -> Settings:
    kwargs: dict[str, Any] = dict(
        database_url="sqlite://",
        redis_url="memory://",
        celery_broker_url="memory://",
        celery_result_backend="memory://",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _state(**overrides: Any) -> IncidentState:
    state: IncidentState = {
        "incident_id": "inc_test",
        "agent_run_id": "run_test",
        "alert_payload": {},
        "service_name": "checkout",
        "alert_name": "DatabaseConnectionExhaustion",
        "severity": "P1",
        "time_window": {},
        "metrics_evidence": [{"type": "metric", "evidence_id": "evi_m1", "summary": "cpu high"}],
        "logs_evidence": [{"type": "log", "evidence_id": "evi_l1", "summary": "timeout errors"}],
        "traces_evidence": [{"type": "trace", "evidence_id": "evi_t1", "summary": "slow spans"}],
        "deployment_evidence": [],
        "k8s_evidence": [],
        "db_evidence": [],
        "runbook_context": [],
        "memory_context": [],
        "cross_incident_context": [],
        "hypotheses": [],
        "root_cause": {},
        "recommended_actions": [],
        "approval_status": {},
        "execution_results": [],
        "incident_report": {},
        "token_budget": {},
        "compression_events": [],
        "errors": [],
        "phase": "initial",
        "_built_messages": [],
    }
    state.update(overrides)  # type: ignore[typeddict-unknown-key]
    return state  # type: ignore[return-value]


def _deps(llm: Any, settings: Settings | None = None) -> AgentDeps:
    s = settings or _settings()
    return AgentDeps(
        db=MagicMock(),
        settings=s,
        tool_cache=RequestLocalToolCache(),
        metrics_tool=MagicMock(),
        logs_tool=MagicMock(),
        trace_tool=MagicMock(),
        git_change_tool=MagicMock(),
        runbook_search_tool=MagicMock(),
        memory_store=MagicMock(),
        context_builder=ContextBuilder(),
        llm=llm,
        node_tracer=lambda **kw: None,
        tool_call_recorder=lambda **kw: None,
    )


class _SpyLLM:
    """Records calls and returns canned DiagnosisOutput."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.last_metadata: dict[str, Any] = {
            "provider": "test",
            "model": "test-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "finish_reason": "stop",
        }

    def generate_json(
        self, prompt: str, output_schema: type[Any], *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        self.calls.append({"prompt": prompt, "thinking": thinking, "schema": output_schema})
        # Update last_metadata so record_llm_call captures it
        self.last_metadata = {
            "provider": "test",
            "model": "test-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "finish_reason": "stop",
        }
        if output_schema is DiagnosisOutput:
            return DiagnosisOutput(
                hypotheses=[{
                    "id": "h1",
                    "statement": f"Test hypothesis from prompt {len(self.calls)}",
                    "supporting_evidence_ids": [],
                    "confidence": 0.8,
                    "rank_explanation": "test",
                }],
                root_cause={
                    "summary": f"Root cause #{len(self.calls)}",
                    "confidence": 0.8, "evidence_ids": [],
                },
                evidence_ids=[],
                missing_evidence=[],
            )
        return output_schema()

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        return '{"hypotheses":[],"root_cause":{},"evidence_ids":[],"missing_evidence":[]}'


class _FailingLLM:
    """Raises on every call."""

    last_metadata: dict[str, Any] = {}

    def generate_json(
        self, prompt: str, output_schema: type[Any], *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        raise RuntimeError("LLM unavailable")

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        raise RuntimeError("LLM unavailable")


class TestMultiPerspectiveEnabled:
    def test_disabled_by_default(self) -> None:
        deps = _deps(_SpyLLM(), _settings())
        assert _multi_perspective_enabled(deps) is False

    def test_enabled_when_flag_is_true(self) -> None:
        deps = _deps(_SpyLLM(), _settings(llm_multi_perspective_enabled=True))
        assert _multi_perspective_enabled(deps) is True


class TestRunSpecialist:
    def test_specialist_produces_diagnosis_output(self) -> None:
        llm = _SpyLLM()
        deps = _deps(llm)
        state = _state()
        output = _run_specialist(
            state, deps, "metrics",
            state.get("metrics_evidence", []),
            "You are a metrics specialist.",
        )
        assert isinstance(output, DiagnosisOutput)
        assert len(output.hypotheses) >= 1
        assert llm.calls[-1]["thinking"] is False

    def test_specialist_failure_returns_empty_output(self) -> None:
        llm = _FailingLLM()
        deps = _deps(llm)
        state = _state()
        output = _run_specialist(
            state, deps, "metrics",
            state.get("metrics_evidence", []),
            "You are a metrics specialist.",
        )
        assert isinstance(output, DiagnosisOutput)
        assert len(output.hypotheses) == 0

    def test_perspective_tag_in_prompt(self) -> None:
        llm = _SpyLLM()
        deps = _deps(llm)
        _run_specialist(_state(), deps, "logs", [], "Log specialist.")
        assert "[perspective:logs]" in llm.calls[-1]["prompt"]


class TestSerializePartialOutput:
    def test_empty_returns_placeholder(self) -> None:
        empty = DiagnosisOutput()
        result = _serialize_partial_output(empty)
        assert "specialist returned no results" in result

    def test_with_hypotheses_includes_all_fields(self) -> None:
        output = DiagnosisOutput(
            hypotheses=[{
                "id": "h1", "statement": "Test", "supporting_evidence_ids": ["evi_1"],
                "confidence": 0.9, "rank_explanation": "best match",
            }],
            root_cause={"summary": "RC", "confidence": 0.9, "evidence_ids": ["evi_1"]},
            evidence_ids=["evi_1"],
            missing_evidence=["more data"],
        )
        result = _serialize_partial_output(output)
        assert "Test" in result
        assert "evi_1" in result


class TestDiagnoseMultiPerspective:
    def test_full_multi_perspective_flow(self) -> None:
        llm = _SpyLLM()
        deps = _deps(llm, _settings(llm_multi_perspective_enabled=True))
        result = diagnose(_state(), deps)

        assert result["phase"] == "diagnosed"
        assert len(result["hypotheses"]) >= 1
        assert result["root_cause"]["summary"]
        diagnose_calls = [
            c for c in result["llm_calls"]
            if c["node"].startswith("diagnose_") or c["node"] == "diagnose"
        ]
        # 4 specialists + 1 top-level = 5 diagnose entries
        expected = 5
        assert len(diagnose_calls) == expected, (
            f"Expected {expected} diagnose calls (3 specialists + synthesizer + top-level), "
            f"got {diagnose_calls}"
        )

    def test_single_call_when_disabled(self) -> None:
        llm = _SpyLLM()
        deps = _deps(llm, _settings(llm_multi_perspective_enabled=False))
        result = diagnose(_state(), deps)

        assert result["phase"] == "diagnosed"
        diagnose_calls = [
            c for c in result["llm_calls"]
            if c["node"].startswith("diagnose_")
        ]
        assert len(diagnose_calls) == 0

    def test_full_llm_failure_falls_back_to_rules_diagnosis(self) -> None:
        """When ALL LLM calls fail, _rules_diagnosis provides the final output."""
        llm = _FailingLLM()
        deps = _deps(llm, _settings(llm_multi_perspective_enabled=True))
        result = diagnose(_state(), deps)

        assert result["phase"] == "diagnosed"
        assert len(result["hypotheses"]) >= 1  # from rules fallback

    def test_synthesizer_gets_deep_reasoning(self) -> None:
        llm = _SpyLLM()
        deps = _deps(
            llm, _settings(llm_multi_perspective_enabled=True, llm_reasoning_enabled=True)
        )
        diagnose(_state(), deps)
        synth_calls = [c for c in llm.calls if "[perspective:synthesizer]" in c["prompt"]]
        assert len(synth_calls) == 1
        assert synth_calls[0]["thinking"] is True

    def test_specialists_use_standard_reasoning(self) -> None:
        llm = _SpyLLM()
        deps = _deps(
            llm, _settings(llm_multi_perspective_enabled=True, llm_reasoning_enabled=True)
        )
        diagnose(_state(), deps)
        specialist_calls = [
            c for c in llm.calls
            if any(f"[perspective:{p}]" in c["prompt"] for p in ("metrics", "logs", "traces"))
        ]
        for call in specialist_calls:
            assert call["thinking"] is False, f"Specialist should use standard reasoning: {call}"


class TestLoadTopology:
    def test_returns_empty_when_no_path(self) -> None:
        deps = _deps(_SpyLLM(), _settings(service_topology_path=""))
        result = _load_topology(_state(), deps)
        assert result == []

    def test_returns_empty_when_path_not_found(self) -> None:
        deps = _deps(_SpyLLM(), _settings(service_topology_path="/nonexistent/topo.json"))
        result = _load_topology(_state(), deps)
        assert result == []
