"""Pydantic schemas for memory, token budgets, and context compression.

These models are used by the memory package and must not depend on LLM providers.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ContextBudget(BaseModel):
    """Token budget allocation across prompt segments.

    The field defaults are the raw schema defaults used by BuildContextInput.
    ``with_defaults()`` below provides the percentage allocation path used by
    ContextBudgeter. Keep both semantics in mind when changing budgets.
    """

    total_limit: int = 32_000
    reserved_for_completion: int = 8_000
    static_prompt: int = 6_000
    schema_tokens: int = 2_000
    alert: int = 3_200
    evidence: int = 9_600
    runbook: int = 6_400
    memory: int = 3_200
    cross_incident: int = 3_200
    scratchpad: int = 1_600

    @property
    def prompt_limit(self) -> int:
        """Return the prompt-side budget after reserving completion tokens."""
        return self.total_limit - self.reserved_for_completion

    @classmethod
    def with_defaults(cls, total_limit: int = 32_000) -> ContextBudget:
        """Allocate budget by percentage.

        Per spec: static+schema=25%, evidence=30%, runbook=20%,
        memory=10%, alert=10%, scratchpad=5%.  Total = 100%.
        """
        default_reserved = cls.model_fields["reserved_for_completion"].default
        prompt = total_limit - default_reserved
        schema = int(prompt * 0.0625)
        return cls(
            total_limit=total_limit,
            reserved_for_completion=default_reserved,
            static_prompt=int(prompt * 0.25) - schema,
            schema_tokens=schema,
            alert=int(prompt * 0.10),
            evidence=int(prompt * 0.30),
            runbook=int(prompt * 0.20),
            memory=int(prompt * 0.10),
            cross_incident=int(prompt * 0.05),
            scratchpad=0,
        )


class CompressedContext(BaseModel):
    """Result of compressing one category of evidence.

    retained/omitted evidence IDs are part of the audit contract: downstream
    reports can say which facts were preserved and which were summarized away.
    """

    summary: str = ""
    retained_evidence_ids: list[str] = Field(default_factory=list)
    omitted_evidence_ids: list[str] = Field(default_factory=list)
    before_tokens: int = 0
    after_tokens: int = 0
    compression_ratio: float = 0.0
    risk_notes: list[str] = Field(default_factory=list)


class BuildContextInput(BaseModel):
    """Input for ContextBuilder.build().

    All fields are already sanitized/collected by agent nodes. This schema does
    not fetch tools, query memory, or call an LLM.
    """

    incident: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    runbook_chunks: list[dict[str, Any]] = Field(default_factory=list)
    memories: list[dict[str, Any]] = Field(default_factory=list)
    cross_incident: list[dict[str, Any]] = Field(default_factory=list)
    output_schema: str = ""
    budget: ContextBudget = Field(default_factory=ContextBudget)


class BuiltContext(BaseModel):
    """Output from ContextBuilder.build().

    ``segment_cache_keys`` are app prompt-segment metadata, not provider cache
    hit/miss counters.
    """

    messages: list[dict[str, Any]] = Field(default_factory=list)
    token_usage_estimate: dict[str, int] = Field(default_factory=dict)
    segment_cache_keys: list[str] = Field(default_factory=list)
    compressed_context: list[CompressedContext] = Field(default_factory=list)


class MemoryItemCreate(BaseModel):
    """Schema for creating a memory item.

    ``content_json`` may carry structured incident/action metadata while
    ``content`` remains available for lexical fallback search.
    """

    scope: str
    scope_key: str
    memory_type: str = "semantic"
    content: str = ""
    content_json: dict[str, Any] | None = None
    embedding: list[float] | None = None
    importance: float = 0.5
    expires_at: str | None = None
    source_ref: str | None = None


class MemoryFilters(BaseModel):
    """Filters for memory search across L0-L3 scopes."""

    scope: str | None = None
    scope_key: str | None = None
    memory_type: str | None = None
    min_importance: float | None = None
    service: str | None = None
