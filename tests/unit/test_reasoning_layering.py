"""Unit tests for reasoning-depth layering (roadmap Phase 1.2).

Covers the config-driven deep-reasoning selection, LLM-call auditing, and the
diagnose node's structured rationale. All offline and deterministic.
"""

from __future__ import annotations

import math
from typing import Any

from packages.agent.llm.reasoning import (
    capture_metadata,
    deep_reasoning_nodes,
    diagnosis_reasoning_trigger,
    format_call_metadata,
    llm_profile_call_options,
    record_llm_call,
    should_use_deep_reasoning,
    should_use_diagnosis_reasoning,
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

    def test_diagnosis_reasoning_requires_complexity_trigger(self) -> None:
        settings = _settings(llm_reasoning_enabled=True)
        assert should_use_diagnosis_reasoning(settings, "diagnose", {"severity": "P2"}) is False

    def test_diagnosis_reasoning_enabled_for_top_severity(self) -> None:
        settings = _settings(llm_reasoning_enabled=True)
        state = {"severity": "P0"}
        assert diagnosis_reasoning_trigger(state) == "top_severity"
        assert should_use_diagnosis_reasoning(settings, "diagnose", state) is True

    def test_diagnosis_reasoning_enabled_for_conflicting_evidence(self) -> None:
        settings = _settings(llm_reasoning_enabled=True)
        cross_validation = {"status": "conflicting", "needs_human_review": True}
        assert (
            diagnosis_reasoning_trigger(
                {"severity": "P2"}, cross_validation=cross_validation
            )
            == "evidence_conflict"
        )
        assert (
            should_use_diagnosis_reasoning(
                settings,
                "diagnose",
                {"severity": "P2"},
                cross_validation=cross_validation,
            )
            is True
        )

    def test_diagnosis_reasoning_enabled_for_cascade_or_missing_evidence(self) -> None:
        settings = _settings(llm_reasoning_enabled=True)
        assert (
            diagnosis_reasoning_trigger(
                {"severity": "P2"}, cascade_analysis={"is_cascade": True}
            )
            == "cascade_suspicion"
        )
        assert (
            should_use_diagnosis_reasoning(
                settings,
                "diagnose",
                {"severity": "P2"},
                cascade_analysis={"is_cascade": True},
            )
            is True
        )
        assert (
            diagnosis_reasoning_trigger(
                {"diagnosis_rationale": {"missing_evidence": ["pool config"]}}
            )
            == "missing_evidence"
        )

    def test_explicit_reasoning_node_override_forces_diagnosis_reasoning(self) -> None:
        settings = _settings(llm_reasoning_enabled=True, llm_reasoning_nodes="diagnose")
        assert should_use_diagnosis_reasoning(settings, "diagnose", {"severity": "P2"}) is True

    def test_formatted_default_reasoning_nodes_do_not_force_reasoning(self) -> None:
        settings = _settings(
            llm_reasoning_enabled=True,
            llm_reasoning_nodes="diagnose, diagnose_synthesize",
        )
        assert should_use_diagnosis_reasoning(settings, "diagnose", {"severity": "P2"}) is False

    def test_profile_call_options_only_emit_configured_differences(self) -> None:
        assert llm_profile_call_options(_settings(), "fast_json") == {}
        options = llm_profile_call_options(
            _settings(
                llm_model="qwen-base",
                llm_max_tokens=512,
                llm_fast_json_model="qwen-fast",
                llm_fast_json_max_tokens=96,
            ),
            "fast_json",
        )
        assert options == {"model": "qwen-fast", "max_tokens": 96}

    def test_profile_call_options_support_node_alias_overrides(self) -> None:
        options = llm_profile_call_options(
            _settings(
                llm_model="qwen-base",
                llm_max_tokens=512,
                llm_report_model="qwen-report",
                llm_report_max_tokens=900,
                llm_node_model_overrides="generate_report=qwen-report-hot",
                llm_node_max_tokens="generate_report=700",
            ),
            "report",
            aliases=("generate_report",),
        )

        assert options == {"model": "qwen-report-hot", "max_tokens": 700}


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

    def test_format_call_metadata_uses_safe_allowlist(self) -> None:
        meta = {
            "provider": {"raw": "provider"},
            "model": ["raw-model"],
            "usage": ["secret"],
            "redaction_count": {"raw": "count"},
            "raw_prompt": "secret prompt",
            "reasoning_summary": "raw reasoning",
        }

        assert format_call_metadata(meta) == ""

    def test_format_call_metadata_drops_malformed_usage_without_leaking(self) -> None:
        meta = {
            "provider": "openai",
            "model": "gpt-5.4",
            "usage": {
                "prompt_tokens": {"raw": "secret prompt tokens"},
                "completion_tokens": 3,
            },
            "redaction_count": 1,
        }

        out = format_call_metadata(meta)

        assert out == "llm=openai/gpt-5.4 tok=0/3 redact=1"
        assert "secret prompt tokens" not in out

    def test_capture_metadata_tolerates_plain_llm(self) -> None:
        assert capture_metadata(object()) == {}

    def test_record_llm_call_appends(self) -> None:
        state: dict[str, Any] = {}
        record_llm_call(state, "diagnose", {"provider": "fake", "model": "m"})
        record_llm_call(state, "plan_actions", {"provider": "fake", "model": "m"})
        record_llm_call(state, "noop", {})  # empty meta is ignored
        assert [c["node"] for c in state["llm_calls"]] == ["diagnose", "plan_actions"]

    def test_record_llm_call_keeps_only_allowed_metadata(self) -> None:
        state: dict[str, Any] = {}

        record_llm_call(
            state,
            "diagnose",
            {
                "provider": "openai",
                "model": "gpt-5.4",
                "finish_reason": "stop",
                "service_tier": "default",
                "provider_cache_status": "hit",
                "duration_ms": 123,
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "cached_prompt_tokens": 80,
                    "reasoning_tokens": 7,
                    "raw_prompt_tokens": 999,
                },
                "redaction_applied": True,
                "redaction_count": 2,
                "redaction_types": ["bearer_token", "password", "bearer_token"],
                "raw_prompt": "secret prompt",
                "raw_completion": "secret completion",
                "response_body": {"secret": "payload"},
                "reasoning_summary": "raw reasoning",
                "chain_of_thought": "raw chain",
            },
        )

        call = state["llm_calls"][0]
        assert call == {
            "node": "diagnose",
            "provider": "openai",
            "model": "gpt-5.4",
            "finish_reason": "stop",
            "service_tier": "default",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cached_prompt_tokens": 80,
                "reasoning_tokens": 7,
            },
            "provider_cache_status": "hit",
            "cache_hit": True,
            "duration_ms": 123,
            "redaction_applied": True,
            "redaction_count": 2,
            "redaction_types": ["bearer_token", "password"],
        }

    def test_record_llm_call_drops_malformed_and_unknown_metadata(self) -> None:
        state: dict[str, Any] = {}

        record_llm_call(
            state,
            "diagnose",
            {
                "provider": {"raw": "provider"},
                "model": ["raw-model"],
                "finish_reason": {"raw": "finish"},
                "service_tier": {"raw": "tier"},
                "provider_cache_status": ["hit"],
                "duration_ms": "123",
                "usage": {
                    "prompt_tokens": "100",
                    "completion_tokens": object(),
                    "cached_prompt_tokens": True,
                    "reasoning_tokens": {"raw": "reasoning"},
                },
                "redaction_applied": "yes",
                "redaction_count": "2",
                "redaction_types": ["bearer_token", {"raw": "type"}, ""],
                "prompt": "secret prompt",
                "completion": "secret completion",
                "query": "secret query",
                "reasoning_content": "raw reasoning",
                "thinking": "raw thinking",
            },
        )

        assert state["llm_calls"] == [
            {
                "node": "diagnose",
                "redaction_types": ["bearer_token"],
            }
        ]

    def test_record_llm_call_does_not_fold_unknown_cache_status_to_legacy_miss(
        self,
    ) -> None:
        state: dict[str, Any] = {}
        record_llm_call(
            state,
            "diagnose",
            {
                "provider": "openai",
                "model": "gpt-5.4",
                "provider_cache_status": "unknown",
                "cache_hit": False,
            },
        )

        assert state["llm_calls"] == [
            {
                "node": "diagnose",
                "provider": "openai",
                "model": "gpt-5.4",
                "provider_cache_status": "unknown",
            }
        ]

    def test_record_llm_call_does_not_fold_unknown_cache_status_to_legacy_hit(
        self,
    ) -> None:
        state: dict[str, Any] = {}
        record_llm_call(
            state,
            "diagnose",
            {
                "provider": "openai",
                "model": "gpt-5.4",
                "provider_cache_status": "unknown",
                "cache_hit": True,
            },
        )

        assert state["llm_calls"] == [
            {
                "node": "diagnose",
                "provider": "openai",
                "model": "gpt-5.4",
                "provider_cache_status": "unknown",
            }
        ]

    def test_record_llm_call_preserves_legacy_cache_hit_boolean(self) -> None:
        state: dict[str, Any] = {}
        record_llm_call(
            state,
            "diagnose",
            {
                "provider": "vllm",
                "model": "legacy",
                "cache_hit": True,
            },
        )

        assert state["llm_calls"] == [
            {
                "node": "diagnose",
                "provider": "vllm",
                "model": "legacy",
                "cache_hit": True,
            }
        ]

    def test_record_llm_call_drops_non_finite_numeric_metadata(self) -> None:
        state: dict[str, Any] = {}
        record_llm_call(
            state,
            "diagnose",
            {
                "provider": "openai",
                "model": "gpt-5.4",
                "duration_ms": math.inf,
                "redaction_count": math.nan,
                "usage": {
                    "prompt_tokens": math.inf,
                    "completion_tokens": math.nan,
                },
            },
        )

        assert state["llm_calls"] == [
            {
                "node": "diagnose",
                "provider": "openai",
                "model": "gpt-5.4",
            }
        ]

    def test_record_llm_call_drops_negative_numeric_metadata(self) -> None:
        state: dict[str, Any] = {}
        record_llm_call(
            state,
            "diagnose",
            {
                "provider": "openai",
                "model": "gpt-5.4",
                "duration_ms": -1,
                "redaction_count": -2,
                "usage": {
                    "prompt_tokens": -5,
                    "completion_tokens": -3,
                },
            },
        )

        assert state["llm_calls"] == [
            {
                "node": "diagnose",
                "provider": "openai",
                "model": "gpt-5.4",
            }
        ]

    def test_record_llm_call_ignores_empty_safe_metadata(self) -> None:
        state: dict[str, Any] = {}
        record_llm_call(
            state,
            "diagnose",
            {
                "raw_prompt": "secret prompt",
                "raw_completion": "secret completion",
                "reasoning_summary": "raw reasoning",
            },
        )
        assert "llm_calls" not in state


# --------------------------------------------------------------------------- #
# diagnose node — deep reasoning + auditable rationale                          #
# --------------------------------------------------------------------------- #
class _SpyLLM:
    """Records the thinking flag and exposes adapter-style metadata."""

    def __init__(self) -> None:
        self.thinking_seen: bool | None = None
        self.kwargs_seen: dict[str, Any] = {}
        self.last_metadata = {
            "provider": "vllm",
            "model": "qwen-7b",
            "usage": {"prompt_tokens": 42, "completion_tokens": 7},
        }

    def generate_json(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        self.thinking_seen = thinking
        self.kwargs_seen = dict(kwargs)
        self.last_metadata = {
            "provider": "vllm",
            "model": str(kwargs.get("model", "qwen-7b")),
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
    def test_requests_deep_reasoning_for_top_severity_when_enabled(self) -> None:
        llm = _SpyLLM()
        deps = _deps(llm, _settings(llm_reasoning_enabled=True))
        state = _state()
        state["severity"] = "P0"
        diagnose(state, deps)
        assert llm.thinking_seen is True

    def test_uses_diagnose_reasoning_profile_only_when_reasoning_triggers(self) -> None:
        llm = _SpyLLM()
        deps = _deps(
            llm,
            _settings(
                llm_reasoning_enabled=True,
                llm_diagnose_reasoning_model="qwen-reasoning",
                llm_diagnose_reasoning_max_tokens=1536,
            ),
        )
        state = _state()
        state["severity"] = "P0"
        diagnose(state, deps)
        assert llm.thinking_seen is True
        assert llm.kwargs_seen == {"model": "qwen-reasoning", "max_tokens": 1536}

    def test_does_not_request_deep_reasoning_without_trigger(self) -> None:
        llm = _SpyLLM()
        deps = _deps(llm, _settings(llm_reasoning_enabled=True))
        diagnose(_state(), deps)
        assert llm.thinking_seen is False
        assert llm.kwargs_seen == {}

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
