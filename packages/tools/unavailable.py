"""UnavailableTool — placeholder for tools whose backend is not configured.

Used by ``_build_deps`` when a backend URL is ``None``. Returns a
``ToolResult(status="degraded")`` with a clear message so the diagnosis
workflow can continue without crashing.
"""

from __future__ import annotations

from pydantic import BaseModel

from packages.tools.base import BaseTool, ToolResult


class UnavailableTool(BaseTool):
    """A tool that always returns ``degraded`` because its backend is unavailable.

    Used as a safe placeholder when a backend URL is not configured,
    avoiding ``None`` being passed to real tool constructors that
    expect a valid ``base_url``.
    """

    def __init__(self, name: str, *, reason: str = "Backend not configured") -> None:
        self._name = name
        self._reason = reason

    @property
    def name(self) -> str:
        return self._name

    @property
    def timeout_seconds(self) -> float:
        return 1.0

    def run(self, query: BaseModel) -> ToolResult:
        """Always returns degraded with the stored reason."""
        return ToolResult(
            status="degraded",
            data={},
            summary=f"[{self._name}] {self._reason}",
            duration_ms=0,
            error_message=self._reason,
        )
