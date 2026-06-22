"""Unit tests for packages/memory/."""

from __future__ import annotations

import json

from packages.memory.compressor import Compressor
from packages.memory.context_budget import ContextBudgeter
from packages.memory.schemas import (
    BuildContextInput,
    ContextBudget,
    MemoryFilters,
    MemoryItemCreate,
)
from packages.memory.token_counter import TokenCounter


class TestTokenCounter:
    def test_empty_text_returns_zero(self) -> None:
        assert TokenCounter().count_tokens("") == 0

    def test_normal_text(self) -> None:
        assert TokenCounter().count_tokens("hello world") == 2

    def test_batch(self) -> None:
        assert TokenCounter().count_tokens_batch(["a" * 8, "b" * 4]) == [2, 1]


class TestContextBudgeter:
    def test_allocates_defaults(self) -> None:
        budget = ContextBudgeter(32_000).allocate_budget()
        assert budget.total_limit == 32_000
        assert budget.evidence > 0

    def test_evidence_over_threshold(self) -> None:
        budgeter = ContextBudgeter(32_000)
        budget = budgeter.allocate_budget()
        assert budgeter.evidence_over_threshold(budget.evidence + 1, budget)
        assert not budgeter.evidence_over_threshold(1, budget)

    def test_check_budget(self) -> None:
        budgeter = ContextBudgeter()
        budget = budgeter.allocate_budget()
        overflow = budgeter.check_budget({"evidence": 99_999}, budget)
        assert overflow["evidence"] is True

    def test_is_over_budget(self) -> None:
        budgeter = ContextBudgeter()
        budget = budgeter.allocate_budget()
        assert not budgeter.is_over_budget({}, budget)
        assert budgeter.is_over_budget({"evidence": 99_999}, budget)


class TestCompressor:
    def test_compress_logs_keeps_top_samples(self) -> None:
        c = Compressor()
        items = [
            {
                "type": "log",
                "samples": [{"msg": "a"}, {"msg": "b"}, {"msg": "c"}, {"msg": "d"}, {"msg": "e"}],
            }
        ]
        result = c._compress_by_type_to_items("log", items)
        assert len(result[0]["samples"]) == 3
        assert result[0]["omitted_count"] == 2

    def test_compress_metrics_keeps_stats(self) -> None:
        c = Compressor()
        items = [{"type": "metric", "stats": {"min": 1, "max": 10, "avg": 5, "p95": 9}}]
        result = c._compress_by_type_to_items("metric", items)
        assert "stats" in result[0]

    def test_compress_traces_limits_spans(self) -> None:
        c = Compressor()
        items = [
            {
                "type": "trace",
                "slow_spans": [{"id": i} for i in range(10)],
                "error_spans": [{"id": i} for i in range(10)],
            }
        ]
        result = c._compress_by_type_to_items("trace", items)
        assert len(result[0]["slow_spans"]) <= 5

    def test_compress_nested_payload_preserves_evidence_ids(self) -> None:
        c = Compressor()

        log_result = c._compress_by_type_to_items(
            "log",
            [
                {
                    "type": "log",
                    "source": "loki",
                    "evidence_id": "evi_log",
                    "payload": {
                        "samples": [{"msg": str(i)} for i in range(5)],
                        "error_type_counts": {"timeout": 3},
                    },
                }
            ],
        )
        assert log_result[0]["evidence_id"] == "evi_log"
        assert len(log_result[0]["samples"]) == 3
        assert log_result[0]["error_counts"] == {"timeout": 3}

        metric_result = c._compress_by_type_to_items(
            "metric",
            [
                {
                    "type": "metric",
                    "source": "prometheus",
                    "evidence_id": "evi_metric",
                    "payload": {
                        "metric_type": "error_rate",
                        "service": "checkout",
                        "stats": {"min": 0, "max": 5, "avg": 2, "p95": 4},
                    },
                }
            ],
        )
        assert metric_result[0]["evidence_id"] == "evi_metric"
        assert metric_result[0]["metric_type"] == "error_rate"
        assert metric_result[0]["stats"]["p95"] == 4

        trace_result = c._compress_by_type_to_items(
            "trace",
            [
                {
                    "type": "trace",
                    "source": "otel",
                    "evidence_id": "evi_trace",
                    "payload": {
                        "slow_spans": [{"id": i} for i in range(10)],
                        "error_spans": [
                            {"service": "checkout", "downstream_service": "payments"}
                        ],
                    },
                }
            ],
        )
        assert trace_result[0]["evidence_id"] == "evi_trace"
        assert trace_result[0]["downstream_services"] == ["payments"]

    def test_no_compression_for_small_evidence(self) -> None:
        c = Compressor()
        evidence = [{"type": "log", "samples": [{"msg": "only one"}]}]
        budget = ContextBudget.with_defaults()
        plans = c.generate_compression_plan(evidence, budget)
        assert len(plans) == 0

    def test_compress_report_inputs_omits_raw_logs_and_keeps_traceability(self) -> None:
        c = Compressor()
        evidence = [
            {
                "type": "log",
                "source": "loki",
                "evidence_id": f"evi_log_{idx}",
                "summary": f"RAW_LOG_SUMMARY_{idx}_SHOULD_NOT_PROMPT",
                "payload": {
                    "message": f"RAW_LOG_LINE_{idx}_SHOULD_NOT_PROMPT",
                    "samples": [{"msg": f"RAW_SAMPLE_{idx}_SHOULD_NOT_PROMPT"}],
                    "line_count": 100 + idx,
                    "top_stack_signature": f"RAW_STACK_SIGNATURE_{idx}_SHOULD_NOT_PROMPT",
                    "top_error_type": f"RAW_ERROR_TYPE_{idx}_SHOULD_NOT_PROMPT",
                    "error_type_counts": {f"RAW_ERROR_KEY_{idx}_SHOULD_NOT_PROMPT": idx},
                },
            }
            for idx in range(14)
        ]
        evidence.append({
            "type": "runbook",
            "evidence_id": "evi_runbook",
            "payload": {"chunk_id": "chk_report", "source_path": "runbooks/high-5xx.md"},
            "summary": "Known high 5xx rollback path",
        })

        report_context, compression = c.compress_report_inputs(
            evidence=evidence,
            actions=[{"type": "rollback_release", "reason": "bad deploy"}],
            errors=[{"node": "verify", "error": "RAW_ERROR_DETAIL_SHOULD_NOT_PROMPT"}],
        )
        serialized = json.dumps(report_context, default=str)

        assert "RAW_LOG_LINE" not in serialized
        assert "RAW_LOG_SUMMARY" not in serialized
        assert "RAW_SAMPLE" not in serialized
        assert "RAW_STACK_SIGNATURE" not in serialized
        assert "RAW_ERROR_TYPE" not in serialized
        assert "RAW_ERROR_KEY" not in serialized
        assert "RAW_ERROR_DETAIL" not in serialized
        assert "evi_log_0" in report_context["retained_evidence_ids"]
        assert "evi_runbook" in report_context["omitted_evidence_ids"]
        assert "evi_runbook" in report_context["all_evidence_ids"]
        assert report_context["runbook_chunk_ids"] == ["chk_report"]
        assert compression.retained_evidence_ids
        assert "evi_runbook" in compression.omitted_evidence_ids


class TestMemoryStore:
    def test_put_and_get_by_scope(self, db_session) -> None:
        from packages.memory.memory_store import MemoryStore

        store = MemoryStore(db_session)
        store.put(
            MemoryItemCreate(
                scope="run", scope_key="run_123", memory_type="summary", content="test"
            )
        )
        db_session.commit()
        results = store.get_by_scope("run", "run_123")
        assert len(results) >= 1
        assert results[0].content == "test"

    def test_search_fallback(self, db_session) -> None:
        from packages.memory.memory_store import MemoryStore

        store = MemoryStore(db_session)
        store.put(
            MemoryItemCreate(
                scope="service",
                scope_key="checkout",
                memory_type="episodic",
                content="5xx after deploy",
                importance=0.9,
            )
        )
        db_session.commit()
        results = store.search("5xx", MemoryFilters(scope="service"), top_k=5)
        assert len(results) >= 1


class TestContextBuilder:
    def test_build_minimal(self) -> None:
        from packages.memory.context_builder import ContextBuilder

        builder = ContextBuilder()
        ctx_input = BuildContextInput(
            incident={"_system_prompt": "You are an SRE.", "service_name": "checkout"},
            evidence=[],
            runbook_chunks=[],
            memories=[],
        )
        result = builder.build(ctx_input)
        assert len(result.messages) > 0
        assert "static" in result.token_usage_estimate

    def test_build_with_evidence(self) -> None:
        from packages.memory.context_builder import ContextBuilder

        builder = ContextBuilder()
        ctx_input = BuildContextInput(
            incident={"_system_prompt": "You are an SRE."},
            evidence=[{"type": "log", "source": "loki", "summary": "test log"}],
            runbook_chunks=[{"chunk_id": "chk_1", "score": 0.9, "excerpt": "runbook text"}],
            memories=[{"memory_id": "mem_1", "importance": 0.8, "content": "past incident"}],
        )
        result = builder.build(ctx_input)
        assert result.token_usage_estimate["evidence"] > 0

    def test_build_keeps_deterministic_evidence_order(self) -> None:
        from packages.memory.context_builder import ContextBuilder

        builder = ContextBuilder()
        ctx_input = BuildContextInput(
            incident={"_system_prompt": "You are an SRE."},
            evidence=[
                {"type": "trace", "evidence_id": "evi_trace", "summary": "trace"},
                {"type": "log", "evidence_id": "evi_log", "summary": "log"},
            ],
            runbook_chunks=[],
            memories=[],
        )

        result = builder.build(ctx_input)
        content = result.messages[-1]["content"]

        assert content.index("evi_log") < content.index("evi_trace")

    def test_stable_prefix_hash_ignores_dynamic_user_context(self) -> None:
        from packages.memory.context_builder import ContextBuilder

        builder = ContextBuilder()
        first = builder.build(BuildContextInput(
            incident={
                "_system_prompt": "You are an SRE.",
                "service_name": "checkout",
                "alert_name": "High5xx",
            },
            output_schema="DiagnosisOutput",
            evidence=[{
                "type": "log",
                "evidence_id": "evi_1",
                "summary": "timeout on checkout",
            }],
            runbook_chunks=[{
                "chunk_id": "chk_1",
                "score": 0.9,
                "excerpt": "checkout runbook",
            }],
            memories=[{
                "memory_id": "mem_1",
                "content": "checkout incident memory",
                "importance": 0.9,
            }],
            cross_incident=[{
                "incident_id": "inc_1",
                "summary": "previous checkout outage",
            }],
        ))
        second = builder.build(BuildContextInput(
            incident={
                "_system_prompt": "You are an SRE.",
                "service_name": "payments",
                "alert_name": "Latency",
            },
            output_schema="DiagnosisOutput",
            evidence=[{
                "type": "log",
                "evidence_id": "evi_2",
                "summary": "different raw stack trace",
            }],
            runbook_chunks=[{
                "chunk_id": "chk_2",
                "score": 0.95,
                "excerpt": "payments runbook",
            }],
            memories=[{
                "memory_id": "mem_2",
                "content": "payments incident memory",
                "importance": 0.8,
            }],
            cross_incident=[{
                "incident_id": "inc_2",
                "summary": "previous payments outage",
            }],
        ))

        assert ContextBuilder.stable_prefix_hash(first.messages) == (
            ContextBuilder.stable_prefix_hash(second.messages)
        )
        assert "different raw stack trace" not in first.messages[0]["content"]
        assert "different raw stack trace" not in second.messages[0]["content"]

    def test_stable_prefix_hash_changes_for_static_schema_or_version(self) -> None:
        from packages.memory.context_builder import ContextBuilder

        builder = ContextBuilder()
        base = builder.build(BuildContextInput(
            incident={"_system_prompt": "You are an SRE."},
            output_schema="DiagnosisOutput",
        ))
        schema_changed = builder.build(BuildContextInput(
            incident={"_system_prompt": "You are an SRE."},
            output_schema="DiagnosisOutput:v2",
        ))
        prompt_changed = builder.build(BuildContextInput(
            incident={"_system_prompt": "You are a senior SRE."},
            output_schema="DiagnosisOutput",
        ))
        base_hash = ContextBuilder.stable_prefix_hash(base.messages)

        assert ContextBuilder.stable_prefix_hash(schema_changed.messages) != base_hash
        assert ContextBuilder.stable_prefix_hash(prompt_changed.messages) != base_hash
        assert ContextBuilder.stable_prefix_hash(
            base.messages,
            prompt_version="v2",
        ) != base_hash
        assert ContextBuilder.stable_prefix_hash(
            base.messages,
            schema_version="v2",
        ) != base_hash

    def test_segment_keys_are_versioned_and_safe(self) -> None:
        from packages.memory.context_builder import ContextBuilder

        builder = ContextBuilder()
        result = builder.build(BuildContextInput(
            incident={"_system_prompt": "You are an SRE."},
            output_schema="DiagnosisOutput",
            runbook_chunks=[{
                "chunk_id": "chk_1",
                "title": "Private runbook",
                "excerpt": "raw runbook text with api.prod.svc.cluster.local/path",
                "metadata": {"content_hash": "sha256:abc123"},
                "score": 0.9,
            }],
        ))
        keys = result.segment_cache_keys

        assert any(key.startswith("prompt_segment:static:diagnosis:v1:") for key in keys)
        assert any(key.startswith("prompt_segment:schema:diagnosis:v1:") for key in keys)
        assert any(key.startswith("prompt_segment:runbook:chk_1:v1:") for key in keys)
        joined = " ".join(keys)
        assert "DiagnosisOutput" not in joined
        assert "raw runbook text" not in joined
        assert "svc.cluster.local" not in joined
        assert "/path" not in joined

    def test_segment_keys_hash_untrusted_runbook_identifiers(self) -> None:
        from packages.memory.context_builder import ContextBuilder

        builder = ContextBuilder()
        result = builder.build(BuildContextInput(
            incident={"_system_prompt": "You are an SRE."},
            output_schema="DiagnosisOutput",
            runbook_chunks=[{
                "chunk_id": "api.prod.svc.cluster.local/path",
                "title": "Private runbook",
                "excerpt": "raw runbook text",
                "content_hash": "not a hash api.prod.svc.cluster.local/path",
                "score": 0.9,
            }],
        ))
        joined = " ".join(result.segment_cache_keys)

        assert "api.prod.svc.cluster.local" not in joined
        assert "/path" not in joined
        assert "not_a_hash" not in joined
        assert "raw runbook text" not in joined
