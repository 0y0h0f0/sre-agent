"""Token counting utility — deterministic, LLM-free character heuristic."""

from __future__ import annotations

from typing import Any


class TokenCounter:
    """Estimate token counts from text using characters-per-token heuristic."""

    CHARS_PER_TOKEN: int = 4

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // self.CHARS_PER_TOKEN)

    def count_tokens_batch(self, texts: list[str]) -> list[int]:
        return [self.count_tokens(t) for t in texts]

    def count_dict_tokens(self, data: dict[str, Any]) -> int:
        import json

        return self.count_tokens(json.dumps(data, default=str))
