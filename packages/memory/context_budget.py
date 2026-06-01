"""Context budget management — allocation and overflow detection."""

from __future__ import annotations

from packages.memory.schemas import ContextBudget


class ContextBudgeter:
    """Allocates and checks token budgets for prompt assembly."""

    def __init__(self, total_limit: int = 32_000) -> None:
        self.total_limit = total_limit

    def allocate_budget(self) -> ContextBudget:
        return ContextBudget.with_defaults(self.total_limit)

    def check_budget(self, usage: dict[str, int], budget: ContextBudget) -> dict[str, bool]:
        limits: dict[str, int] = {
            "static": budget.static_prompt + budget.schema_tokens,
            "alert": budget.alert,
            "evidence": budget.evidence,
            "runbook": budget.runbook,
            "memory": budget.memory,
            "scratchpad": budget.scratchpad,
        }
        return {cat: usage.get(cat, 0) > limits.get(cat, 0) for cat in limits}

    def is_over_budget(self, usage: dict[str, int], budget: ContextBudget) -> bool:
        return any(self.check_budget(usage, budget).values())

    def evidence_over_threshold(
        self, evidence_tokens: int, budget: ContextBudget, threshold: float = 0.8
    ) -> bool:
        return evidence_tokens > int(budget.evidence * threshold)
