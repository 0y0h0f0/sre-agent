"""Unit tests for Phase 2 multi-perspective LLM sub-agent diagnosis."""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock

from packages.agent.nodes.diagnose import (
    _load_topology,
    _multi_perspective_enabled,
    _multi_perspective_parallel_enabled,
    _run_specialist,
    _serialize_partial_output,
    diagnose,
)
from packages.agent.schemas import (
    AgentDeps,
    CompactDiagnosisOutput,
    DiagnosisOutput,
    compact_diagnosis_from_output,
)
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


def _perspective_from_prompt(prompt: str) -> str:
    for perspective in ("metrics", "logs", "traces", "synthesizer"):
        if f"[perspective:{perspective}]" in prompt:
            return perspective
    return "diagnose"


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
        if output_schema in (DiagnosisOutput, CompactDiagnosisOutput):
            output = DiagnosisOutput(
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
            if output_schema is CompactDiagnosisOutput:
                return compact_diagnosis_from_output(output)
            return output
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


class _ParallelLLM:
    def __init__(
        self,
        *,
        delays: dict[str, float] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.delays = delays or {}
        self.fail = fail or set()
        self.calls: list[dict[str, Any]] = []
        self.thread_ids: dict[str, int] = {}
        self.lock = threading.Lock()
        self.last_metadata: dict[str, Any] = {}

    def generate_json_with_metadata(
        self,
        prompt: str,
        output_schema: type[Any],
        *,
        thinking: bool = False,
        **kwargs: Any,
    ) -> tuple[Any, dict[str, Any]]:
        perspective = _perspective_from_prompt(prompt)
        time.sleep(self.delays.get(perspective, 0.0))
        if perspective in self.fail:
            raise RuntimeError(f"{perspective} failed")
        output = self._output(perspective, output_schema)
        metadata = self._metadata(perspective)
        with self.lock:
            self.calls.append({
                "prompt": prompt,
                "perspective": perspective,
                "thinking": thinking,
                "metadata": metadata,
            })
            self.thread_ids[perspective] = threading.get_ident()
        return output, dict(metadata)

    def generate_json(
        self,
        prompt: str,
        output_schema: type[Any],
        *,
        thinking: bool = False,
        **kwargs: Any,
    ) -> Any:
        perspective = _perspective_from_prompt(prompt)
        output = self._output(perspective, output_schema)
        metadata = self._metadata(perspective)
        with self.lock:
            self.calls.append({
                "prompt": prompt,
                "perspective": perspective,
                "thinking": thinking,
                "metadata": metadata,
            })
            self.last_metadata = metadata
        return output

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        self.last_metadata = self._metadata("repair")
        return '{"h":[],"rc":{"s":"repaired","c":0.5},"e":[],"r":[],"m":[]}'

    @staticmethod
    def _metadata(perspective: str) -> dict[str, Any]:
        return {
            "provider": "parallel-test",
            "model": f"{perspective}-model",
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "finish_reason": "stop",
        }

    @staticmethod
    def _output(perspective: str, output_schema: type[Any]) -> Any:
        output = DiagnosisOutput(
            hypotheses=[{
                "id": f"h_{perspective}",
                "statement": f"{perspective} hypothesis",
                "supporting_evidence_ids": [f"evi_{perspective}"],
                "confidence": 0.8,
                "rank_explanation": f"{perspective} evidence",
            }],
            root_cause={
                "summary": f"{perspective} root cause",
                "confidence": 0.8,
                "evidence_ids": [f"evi_{perspective}"],
            },
            evidence_ids=[f"evi_{perspective}"],
            missing_evidence=[],
        )
        if output_schema is CompactDiagnosisOutput:
            return compact_diagnosis_from_output(output)
        return output


class TestMultiPerspectiveEnabled:
    def test_disabled_by_default(self) -> None:
        deps = _deps(_SpyLLM(), _settings())
        assert _multi_perspective_enabled(deps) is False

    def test_enabled_when_flag_is_true(self) -> None:
        deps = _deps(_SpyLLM(), _settings(llm_multi_perspective_enabled=True))
        assert _multi_perspective_enabled(deps) is True

    def test_parallel_requires_call_local_metadata(self) -> None:
        deps = _deps(
            _SpyLLM(),
            _settings(
                llm_multi_perspective_enabled=True,
                llm_multi_perspective_parallel_enabled=True,
            ),
        )

        assert _multi_perspective_parallel_enabled(deps) is False


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
        # 3 specialists + 1 synthesizer; no synthetic top-level LLM call.
        expected = 4
        assert len(diagnose_calls) == expected, (
            f"Expected {expected} diagnose calls (3 specialists + synthesizer), "
            f"got {diagnose_calls}"
        )
        assert {call["node"] for call in diagnose_calls} == {
            "diagnose_metrics",
            "diagnose_logs",
            "diagnose_traces",
            "diagnose_synthesize",
        }

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
        diagnose(_state(severity="P0"), deps)
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


class TestDiagnoseMultiPerspectiveParallel:
    def test_parallel_specialists_are_near_max_delay(self) -> None:
        llm = _ParallelLLM(delays={"metrics": 0.08, "logs": 0.08, "traces": 0.08})
        deps = _deps(
            llm,
            _settings(
                llm_multi_perspective_enabled=True,
                llm_multi_perspective_parallel_enabled=True,
                llm_timeout_seconds=1.0,
            ),
        )

        started = time.perf_counter()
        result = diagnose(_state(), deps)
        elapsed = time.perf_counter() - started

        assert result["phase"] == "diagnosed"
        assert elapsed < 0.18
        assert {"metrics", "logs", "traces"}.issubset(llm.thread_ids)

    def test_parallel_specialist_failure_keeps_other_results(self) -> None:
        llm = _ParallelLLM(fail={"logs"})
        deps = _deps(
            llm,
            _settings(
                llm_multi_perspective_enabled=True,
                llm_multi_perspective_parallel_enabled=True,
            ),
        )

        result = diagnose(_state(), deps)
        calls = result["llm_calls"]
        by_node = {call["node"]: call for call in calls}

        assert result["phase"] == "diagnosed"
        assert by_node["diagnose_metrics"]["model"] == "metrics-model"
        assert by_node["diagnose_traces"]["model"] == "traces-model"
        assert "diagnose_logs" not in by_node
        assert by_node["diagnose_synthesize"]["model"] == "synthesizer-model"

    def test_parallel_specialist_timeout_does_not_pollute_metadata(self) -> None:
        llm = _ParallelLLM(delays={"logs": 0.25})
        deps = _deps(
            llm,
            _settings(
                llm_multi_perspective_enabled=True,
                llm_multi_perspective_parallel_enabled=True,
                llm_timeout_seconds=0.05,
            ),
        )

        started = time.perf_counter()
        result = diagnose(_state(), deps)
        elapsed = time.perf_counter() - started
        time.sleep(0.3)
        by_node = {call["node"]: call for call in result["llm_calls"]}

        assert result["phase"] == "diagnosed"
        assert elapsed < 0.2
        assert "diagnose_logs" not in by_node
        assert by_node["diagnose_metrics"]["model"] == "metrics-model"
        assert by_node["diagnose_traces"]["model"] == "traces-model"
        assert by_node["diagnose_synthesize"]["model"] == "synthesizer-model"
        assert llm.last_metadata["model"] == "synthesizer-model"

    def test_parallel_metadata_does_not_cross_contaminate(self) -> None:
        llm = _ParallelLLM(delays={"metrics": 0.02, "logs": 0.01, "traces": 0.03})
        deps = _deps(
            llm,
            _settings(
                llm_multi_perspective_enabled=True,
                llm_multi_perspective_parallel_enabled=True,
            ),
        )

        result = diagnose(_state(), deps)
        by_node = {
            call["node"]: call["model"]
            for call in result["llm_calls"]
            if call["node"].startswith("diagnose_")
        }

        assert by_node["diagnose_metrics"] == "metrics-model"
        assert by_node["diagnose_logs"] == "logs-model"
        assert by_node["diagnose_traces"] == "traces-model"
        assert by_node["diagnose_synthesize"] == "synthesizer-model"

    def test_parallel_specialists_do_not_touch_db_session(self) -> None:
        class RaisingDB:
            def __getattribute__(self, name: str) -> object:
                raise AssertionError(f"DB session accessed from diagnose: {name}")

        llm = _ParallelLLM()
        deps = _deps(
            llm,
            _settings(
                llm_multi_perspective_enabled=True,
                llm_multi_perspective_parallel_enabled=True,
            ),
        )
        deps.db = RaisingDB()  # type: ignore[assignment]

        result = diagnose(_state(), deps)

        assert result["phase"] == "diagnosed"

    def test_parallel_switch_off_preserves_sequential_path(self) -> None:
        llm = _ParallelLLM()
        deps = _deps(
            llm,
            _settings(
                llm_multi_perspective_enabled=True,
                llm_multi_perspective_parallel_enabled=False,
            ),
        )

        result = diagnose(_state(), deps)

        assert result["phase"] == "diagnosed"
        assert set(llm.thread_ids) == {"metrics", "logs", "traces"}
        assert set(llm.thread_ids.values()) == {threading.get_ident()}


class TestLoadTopology:
    def test_returns_empty_when_no_path(self) -> None:
        deps = _deps(_SpyLLM(), _settings(service_topology_path=""))
        result = _load_topology(_state(), deps)
        assert result == []

    def test_returns_empty_when_path_not_found(self) -> None:
        deps = _deps(_SpyLLM(), _settings(service_topology_path="/nonexistent/topo.json"))
        result = _load_topology(_state(), deps)
        assert result == []
