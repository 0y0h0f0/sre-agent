"""Unit tests for packages/memory/."""

from __future__ import annotations

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

    def test_no_compression_for_small_evidence(self) -> None:
        c = Compressor()
        evidence = [{"type": "log", "samples": [{"msg": "only one"}]}]
        budget = ContextBudget.with_defaults()
        plans = c.generate_compression_plan(evidence, budget)
        assert len(plans) == 0


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
