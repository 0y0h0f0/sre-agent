"""Unit tests for reasoning-depth layering (roadmap Phase 1.2).

Covers the config-driven deep-reasoning selection, LLM-call auditing, and the
diagnose node's structured rationale. All offline and deterministic.
"""

from __future__ import annotations

from typing import Any

from packages.agent.llm.reasoning import (
    capture_metadata,
    deep_reasoning_nodes,
    format_call_metadata,
    record_llm_call,
    should_use_deep_reasoning,
)
from packages.agent.nodes.diagnose import diagnose
from packages.agent.schemas import AgentDeps, DiagnosisOutput
from packages.common.settings import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Config-driven selection                                                      #
# --------------------------------------------------------------------------- #
class TestDeepReasoningSelection:
    def test_disabled_by_master_switch(self) -> None:
        settings = _settings(llm_reasoning_enabled=False, llm_reasoning_nodes="diagnose")
        assert should_use_deep_reasoning(settings, "diagnose") is False

    def test_enabled_for_listed_node(self) -> None:
        settings = _settings(llm_reasoning_enabled=True, llm_reasoning_nodes="diagnose")
        assert should_use_deep_reasoning(settings, "diagnose") is True

    def test_disabled_for_unlisted_node(self) -> None:
        settings = _settings(llm_reasoning_enabled=True, llm_reasoning_nodes="diagnose")
        assert should_use_deep_reasoning(settings, "plan_actions") is False

    def test_multiple_nodes_configurable(self) -> None:
        settings = _settings(
            llm_reasoning_enabled=True, llm_reasoning_nodes="diagnose, rank_hypotheses"
        )
        assert should_use_deep_reasoning(settings, "rank_hypotheses") is True
        assert deep_reasoning_nodes(settings) == frozenset({"diagnose", "rank_hypotheses"})

    def test_empty_config_falls_back_to_default(self) -> None:
        settings = _settings(llm_reasoning_enabled=True, llm_reasoning_nodes="")
        assert deep_reasoning_nodes(settings) == frozenset({"diagnose", "diagnose_synthesize"})


# --------------------------------------------------------------------------- #
# Metadata helpers                                                             #
# --------------------------------------------------------------------------- #
class TestMetadataHelpers:
    def test_format_empty(self) -> None:
        assert format_call_metadata({}) == ""
        assert format_call_metadata(None) == ""

    def test_format_full(self) -> None:
        meta = {
            "provider": "vllm",
            "model": "qwen-7b",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
        out = format_call_metadata(meta)
        assert "llm=vllm/qwen-7b" in out
        assert "tok=100/20" in out

    def test_capture_metadata_tolerates_plain_llm(self) -> None:
        assert capture_metadata(object()) == {}

    def test_record_llm_call_appends(self) -> None:
        state: dict[str, Any] = {}
        record_llm_call(state, "diagnose", {"provider": "fake", "model": "m"})
        record_llm_call(state, "plan_actions", {"provider": "fake", "model": "m"})
        record_llm_call(state, "noop", {})  # empty meta is ignored
        assert [c["node"] for c in state["llm_calls"]] == ["diagnose", "plan_actions"]


# --------------------------------------------------------------------------- #
# diagnose node — deep reasoning + auditable rationale                          #
# --------------------------------------------------------------------------- #
class _SpyLLM:
    """Records the thinking flag and exposes adapter-style metadata."""

    def __init__(self) -> None:
        self.thinking_seen: bool | None = None
        self.last_metadata = {
            "provider": "vllm",
            "model": "qwen-7b",
            "usage": {"prompt_tokens": 42, "completion_tokens": 7},
        }

    def generate_json(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        self.thinking_seen = thinking
        self.last_metadata = {
            "provider": "vllm",
            "model": "qwen-7b",
            "usage": {"prompt_tokens": 42, "completion_tokens": 7},
        }
        return DiagnosisOutput(
            hypotheses=[
                {
                    "id": "h1",
                    "statement": "pool saturated",
                    "supporting_evidence_ids": ["evd_1", "evd_2"],
                    "confidence": 0.9,
                    "rank_explanation": "db connections near max",
                }
            ],
            root_cause={
                "summary": "pool exhausted",
                "confidence": 0.9,
                "evidence_ids": ["evd_1"],
            },
            evidence_ids=["evd_1"],
            missing_evidence=["pool config"],
        )

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        return "{}"


class _BrokenLLM:
    def __init__(self) -> None:
        self.last_metadata = {
            "provider": "vllm",
            "model": "stale",
            "usage": {"prompt_tokens": 99, "completion_tokens": 99},
        }

    def generate_json(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        raise ValueError("bad json")

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        raise ValueError("still bad")


def _deps(llm: Any, settings: Settings) -> AgentDeps:
    traces: list[dict[str, Any]] = []
    return AgentDeps(
        db=object(),  # type: ignore[arg-type]
        settings=settings,
        tool_cache=object(),  # type: ignore[arg-type]
        metrics_tool=object(),  # type: ignore[arg-type]
        logs_tool=object(),  # type: ignore[arg-type]
        trace_tool=object(),  # type: ignore[arg-type]
        git_change_tool=object(),  # type: ignore[arg-type]
        runbook_search_tool=object(),  # type: ignore[arg-type]
        memory_store=object(),  # type: ignore[arg-type]
        context_builder=object(),  # type: ignore[arg-type]
        llm=llm,
        node_tracer=lambda **kw: traces.append(kw),
        tool_call_recorder=lambda **kw: None,
    )


def _state() -> dict[str, Any]:
    return {
        "incident_id": "inc_1",
        "agent_run_id": "run_1",
        "alert_name": "DatabaseConnectionExhaustion",
        "metrics_evidence": [],
        "logs_evidence": [],
        "runbook_context": [],
        "memory_context": [],
    }


class TestDiagnoseReasoning:
    def test_requests_deep_reasoning_when_enabled(self) -> None:
        llm = _SpyLLM()
        deps = _deps(llm, _settings(llm_reasoning_enabled=True, llm_reasoning_nodes="diagnose"))
        diagnose(_state(), deps)
        assert llm.thinking_seen is True

    def test_standard_reasoning_when_disabled(self) -> None:
        llm = _SpyLLM()
        deps = _deps(llm, _settings(llm_reasoning_enabled=False))
        diagnose(_state(), deps)
        assert llm.thinking_seen is False

    def test_produces_auditable_rationale_with_evidence_ids(self) -> None:
        deps = _deps(_SpyLLM(), _settings(llm_reasoning_enabled=True))
        result = diagnose(_state(), deps)
        rationale = result["diagnosis_rationale"]
        assert rationale["root_cause"] == "pool exhausted"
        assert rationale["evidence_ids"] == ["evd_1"]
        assert rationale["hypothesis_ranking"][0]["evidence_ids"] == ["evd_1", "evd_2"]
        assert rationale["hypothesis_ranking"][0]["why"] == "db connections near max"
        assert rationale["missing_evidence"] == ["pool config"]

    def test_records_llm_call_metadata(self) -> None:
        deps = _deps(_SpyLLM(), _settings(llm_reasoning_enabled=True))
        result = diagnose(_state(), deps)
        calls = result["llm_calls"]
        assert calls[0]["node"] == "diagnose"
        assert calls[0]["provider"] == "vllm"
        assert calls[0]["usage"]["prompt_tokens"] == 42

    def test_does_not_persist_raw_chain_of_thought(self) -> None:
        deps = _deps(_SpyLLM(), _settings(llm_reasoning_enabled=True))
        result = diagnose(_state(), deps)
        # Only structured rationale + audited metadata — no raw CoT keys.
        assert "chain_of_thought" not in result["diagnosis_rationale"]
        assert "thinking" not in result["llm_calls"][0]

    def test_labels_confidence_provenance_when_unadjusted(self) -> None:
        # No evidence -> no cross-validation adjustment -> source is the model.
        deps = _deps(_SpyLLM(), _settings(llm_reasoning_enabled=True))
        result = diagnose(_state(), deps)
        root_cause = result["root_cause"]
        assert root_cause["confidence_source"] == "model"
        assert root_cause["confidence"] == 0.9
        assert "model_confidence" not in root_cause

    def test_labels_confidence_provenance_when_cross_validated(self) -> None:
        # Conflicting evidence (metrics anomaly vs healthy logs) lowers confidence
        # and records the original model confidence as provenance.
        deps = _deps(_SpyLLM(), _settings(llm_reasoning_enabled=True))
        state = _state()
        state["metrics_evidence"] = [{"payload": {"stats": {"change_ratio": 0.5}}}]
        state["logs_evidence"] = [{"payload": {"error_type_counts": {}, "error_count": 0}}]
        result = diagnose(state, deps)
        root_cause = result["root_cause"]
        assert root_cause["confidence_source"] == "cross_validated"
        assert root_cause["model_confidence"] == 0.9
        assert root_cause["confidence"] < 0.9
        assert result["needs_human_review"] is True

    def test_rules_fallback_uses_state_evidence_ids_without_stale_metadata(self) -> None:
        deps = _deps(_BrokenLLM(), _settings(llm_reasoning_enabled=True))
        state = _state()
        state["metrics_evidence"] = [
            {"type": "metric", "evidence_id": "evi_metric", "summary": "pool saturated"}
        ]

        result = diagnose(state, deps)

        assert result["diagnosis_rationale"]["evidence_ids"] == ["evi_metric"]
        assert result["root_cause"]["evidence_ids"] == ["evi_metric"]
        assert result["llm_calls"] == []


# --------------------------------------------------------------------------- #
# Phase 2: multi-perspective reasoning selection                              #
# --------------------------------------------------------------------------- #


class TestMultiPerspectiveReasoningSelection:
    def test_synthesizer_is_deep_by_default(self) -> None:
        settings = _settings(llm_reasoning_enabled=True)
        assert should_use_deep_reasoning(settings, "diagnose_synthesize") is True

    def test_specialist_uses_standard_reasoning(self) -> None:
        settings = _settings(llm_reasoning_enabled=True)
        assert should_use_deep_reasoning(settings, "diagnose_metrics") is False
        assert should_use_deep_reasoning(settings, "diagnose_logs") is False
        assert should_use_deep_reasoning(settings, "diagnose_traces") is False

    def test_synthesizer_configurable(self) -> None:
        settings = _settings(
            llm_reasoning_enabled=True,
            llm_reasoning_nodes="diagnose_synthesize,plan_actions",
        )
        assert should_use_deep_reasoning(settings, "diagnose_synthesize") is True
        assert should_use_deep_reasoning(settings, "diagnose") is False
