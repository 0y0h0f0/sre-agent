"""ContextBuilder — assembles LLM prompt messages from evidence within budget."""

from __future__ import annotations

import json
from typing import Any

from packages.memory.compressor import Compressor
from packages.memory.context_budget import ContextBudgeter
from packages.memory.schemas import (
    BuildContextInput,
    BuiltContext,
    CompressedContext,
)
from packages.memory.token_counter import TokenCounter


class ContextBuilder:
    """Assembles the prompt context for a diagnosis LLM call.

    Does NOT call an LLM. Produces a BuiltContext with assembled messages
    and token estimates.
    """

    def __init__(
        self,
        budgeter: ContextBudgeter | None = None,
        compressor: Compressor | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.budgeter = budgeter or ContextBudgeter()
        self.compressor = compressor or Compressor()
        self.token_counter = token_counter or TokenCounter()

    def build(self, input: BuildContextInput) -> BuiltContext:
        """Assemble budgeted messages and report token/cache metadata."""
        budget = input.budget
        if budget.total_limit <= 0:
            # A non-positive limit is treated as "use system defaults" for
            # callers that only want default allocation behavior.
            budget = self.budgeter.allocate_budget()

        usage: dict[str, int] = {}
        compressed_list: list[CompressedContext] = []

        # Stabilize evidence ordering for cache determinism and test snapshots.
        # This also keeps evidence IDs near the facts they support after
        # compression and report generation.
        evidence = sorted(
            input.evidence,
            key=lambda e: (e.get("type", ""), e.get("timestamp", ""), e.get("evidence_id", "")),
        )
        system_text = input.incident.get("_system_prompt", "")
        schema_text = input.output_schema
        usage["static"] = self._count(system_text) + self._count(schema_text)

        # Alert
        alert_text = json.dumps(input.incident, default=str)
        usage["alert"] = self._count(alert_text)

        # Evidence is the only segment that currently emits compression events.
        # Runbooks/memory are budget-capped below, but not summarized here.
        evidence_tokens = self._count(json.dumps(evidence, default=str))
        if self.budgeter.evidence_over_threshold(evidence_tokens, budget):
            evidence, comp_ctx = self.compressor.compress_evidence(evidence, budget)
            compressed_list.append(comp_ctx)
        usage["evidence"] = self._count(json.dumps(evidence, default=str))

        # Runbook chunks are sorted by score and capped by budget. They are not
        # re-ranked here; retrieval/reranking already happened in packages.rag.
        runbook_chunks = self._sort_runbooks(input.runbook_chunks)
        runbook_tokens = 0
        capped_runbooks: list[dict[str, Any]] = []
        for chunk in runbook_chunks:
            ct = self._count(json.dumps(chunk, default=str))
            if runbook_tokens + ct > budget.runbook:
                break
            capped_runbooks.append(chunk)
            runbook_tokens += ct
        usage["runbook"] = runbook_tokens

        # Memory — sort by relevance (score) first, then importance as
        # tie-breaker. The store may return lexical/vector results depending on
        # backend availability, so this keeps final prompt order predictable.
        memories = sorted(
            input.memories,
            key=lambda m: m.get("score", m.get("relevance", m.get("importance", 0))),
            reverse=True,
        )
        mem_tokens = 0
        capped_memories: list[dict[str, Any]] = []
        for mem in memories:
            mt = self._count(json.dumps(mem, default=str))
            if mem_tokens + mt > budget.memory:
                break
            capped_memories.append(mem)
            mem_tokens += mt
        usage["memory"] = mem_tokens

        # Cross-incident context is budget-capped separately from memory so one
        # very similar incident cannot crowd out service/procedural memory.
        cross_incident_tokens = 0
        capped_cross_incident: list[dict[str, Any]] = []
        if input.cross_incident:
            for ci in input.cross_incident:
                ct = self._count(json.dumps(ci, default=str))
                if cross_incident_tokens + ct > budget.cross_incident:
                    break
                capped_cross_incident.append(ci)
                cross_incident_tokens += ct
        usage["cross_incident"] = cross_incident_tokens
        usage["scratchpad"] = 0

        messages = self._build_messages(
            system_text=system_text,
            schema_text=schema_text,
            alert_text=alert_text,
            evidence=evidence,
            runbook_chunks=capped_runbooks,
            memories=capped_memories,
            cross_incident=capped_cross_incident,
        )

        return BuiltContext(
            messages=messages,
            token_usage_estimate=usage,
            segment_cache_keys=self._segment_keys(input),
            compressed_context=compressed_list,
        )

    def _count(self, text: str) -> int:
        """Use the deterministic token estimator for prompt budgeting."""
        return self.token_counter.count_tokens(text)

    @staticmethod
    def _sort_runbooks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sort runbook chunks by score, then chunk ID for stable ties."""
        return sorted(
            chunks, key=lambda c: (c.get("score", 0), c.get("chunk_id", "")), reverse=True
        )

    @staticmethod
    def _build_messages(
        *,
        system_text: str,
        schema_text: str,
        alert_text: str,
        evidence: list[dict[str, Any]],
        runbook_chunks: list[dict[str, Any]],
        memories: list[dict[str, Any]],
        cross_incident: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the final chat-style message list.

        The system message contains stable prompt/schema material. Dynamic
        incident evidence stays in the user message to maximize provider prefix
        cache opportunities without confusing it with app/tool cache metrics.
        """
        messages: list[dict[str, Any]] = []

        # Stable system prompt (enables provider prefix caching)
        if system_text or schema_text:
            sys_content = "\n\n".join(p for p in [system_text, schema_text] if p)
            messages.append({"role": "system", "content": sys_content})

        # Dynamic content in user message
        parts: list[str] = []
        parts.append(f"# Alert\n{alert_text}")
        parts.append(
            f"# Evidence\n{json.dumps(evidence, default=str)}"
            if evidence
            else "# Evidence\nNo evidence collected."
        )
        parts.append(
            f"# Runbook\n{json.dumps(runbook_chunks, default=str)}"
            if runbook_chunks
            else "# Runbook\nNo relevant runbook entries."
        )
        parts.append(
            f"# Memory\n{json.dumps(memories, default=str)}"
            if memories
            else "# Memory\nNo relevant memory items."
        )
        if cross_incident:
            parts.append(
                f"# Related Incidents\n{json.dumps(cross_incident, default=str)}"
            )
        messages.append({"role": "user", "content": "\n\n".join(parts)})
        return messages

    @staticmethod
    def _segment_keys(input: BuildContextInput) -> list[str]:
        """Return stable app-level prompt segment cache keys.

        These keys are metadata for the application segment cache. They are not
        provider prompt-cache hit counters.
        """
        keys: list[str] = []
        if input.output_schema:
            keys.append("prompt_segment:schema:diagnosis:v1")
        for chunk in input.runbook_chunks:
            cid = chunk.get("chunk_id", "")
            if cid:
                keys.append(f"prompt_segment:runbook:{cid}:v1")
        return keys
