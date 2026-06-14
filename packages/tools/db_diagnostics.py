"""Database read-only diagnosis tool (roadmap Phase 2.3).

Connects to PostgreSQL to read live state: connection pool (``pg_stat_activity``),
locks (``pg_locks``), and slow queries (``pg_stat_statements`` top-N).

Hard boundary: every operation is read-only. The live backend uses a dedicated
connection, forces ``SET TRANSACTION READ ONLY`` plus a ``statement_timeout``,
runs only predefined SELECT statements, and re-checks each statement with
:func:`_assert_read_only` for defense in depth. No mutations are ever issued.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from packages.common.settings import Settings
from packages.tools.base import ToolResult, ToolStatus, compact_summary, elapsed_ms, start_timer

DbOperation = Literal["connection_pool", "locks", "slow_queries"]

# Predefined, read-only SELECT statements keyed by operation. User input never
# reaches the SQL text — only the operation name selects a fixed statement.
_QUERIES: dict[str, str] = {
    "connection_pool": (
        "SELECT state, count(*) AS connections FROM pg_stat_activity "
        "GROUP BY state ORDER BY connections DESC"
    ),
    "locks": ("SELECT mode, count(*) AS held FROM pg_locks GROUP BY mode ORDER BY held DESC"),
    "slow_queries": (
        "SELECT query, calls, mean_exec_time FROM pg_stat_statements "
        "ORDER BY mean_exec_time DESC LIMIT %(limit)s"
    ),
}


class DbDiagnosticsQuery(BaseModel):
    operation: DbOperation = "connection_pool"
    limit: int = Field(default=10, ge=1, le=100)

    @field_validator("operation")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class DbBackend(Protocol):
    name: str

    def fetch(self, query: DbDiagnosticsQuery) -> list[dict[str, Any]]:
        """Return rows for the requested read-only operation."""


class FixtureDbBackend:
    """Reads diagnostics from a fixture file (MVP default)."""

    name = "fixture"

    def __init__(self, fixture_path: str | Path = "demo/faults/db_diagnostics.json") -> None:
        self.fixture_path = Path(fixture_path)

    def fetch(self, query: DbDiagnosticsQuery) -> list[dict[str, Any]]:
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        rows = payload.get(query.operation, [])
        if not isinstance(rows, list):
            msg = f"db fixture for {query.operation} must be a list"
            raise ValueError(msg)
        return [row for row in rows if isinstance(row, dict)][: query.limit]


class LiveDbBackend:
    """Read-only PostgreSQL diagnostics via a dedicated connection."""

    name = "live"

    def __init__(
        self,
        *,
        dsn: str,
        statement_timeout_ms: int = 2000,
        connect_timeout_seconds: float = 2.0,
    ) -> None:
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms
        self.connect_timeout_seconds = connect_timeout_seconds

    def fetch(self, query: DbDiagnosticsQuery) -> list[dict[str, Any]]:
        sql = _QUERIES[query.operation]
        _assert_read_only(sql)
        try:
            import psycopg  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - optional dependency
            msg = "psycopg not installed; use db_diagnostics_backend=fixture"
            raise RuntimeError(msg) from exc

        with psycopg.connect(
            self.dsn, connect_timeout=int(max(1, self.connect_timeout_seconds))
        ) as conn:
            # read_only=True makes every transaction on this connection read-only,
            # so we never issue a separate (and order-sensitive) SET TRANSACTION
            # READ ONLY. statement_timeout is a literal int (no SQL params on SET).
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
                cur.execute(sql, {"limit": query.limit})
                columns = [desc[0] for desc in cur.description or []]
                return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]


def _assert_read_only(sql: str) -> None:
    """Reject any statement that is not a single read-only SELECT."""
    normalized = sql.strip().lower()
    if not normalized.startswith("select"):
        msg = "db diagnostics only permits SELECT statements"
        raise ValueError(msg)
    forbidden = ("insert", "update", "delete", "drop", "alter", "truncate", "create", "grant")
    if any(f" {word} " in f" {normalized} " for word in forbidden):
        msg = "db diagnostics statement contains a write keyword"
        raise ValueError(msg)


class DbDiagnosticsTool:
    name = "db_diagnostics"

    def __init__(
        self,
        *,
        backend: DbBackend | None = None,
        fixture_path: str | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        if backend is None:
            backend = FixtureDbBackend(
                fixture_path=fixture_path or "demo/faults/db_diagnostics.json"
            )
        self.backend = backend
        # Part of the BaseTool contract; the live backend owns the real timeout.
        self.timeout_seconds = timeout_seconds

    def run(self, query: BaseModel) -> ToolResult:
        db_query = DbDiagnosticsQuery.model_validate(query)
        started_at = start_timer()
        try:
            rows = self.backend.fetch(db_query)
            has_data = bool(rows)
            status: ToolStatus = "succeeded" if has_data else "degraded"
            data = {"operation": db_query.operation, "rows": rows}
            return ToolResult(
                status=status,
                data=data,
                summary=compact_summary(
                    {
                        "operation": db_query.operation,
                        "rows": len(rows),
                    }
                ),
                evidence=[
                    {
                        "type": "db",
                        "source": self.backend.name,
                        "title": f"db {db_query.operation}",
                        "payload": data,
                    }
                ]
                if has_data
                else [],
                duration_ms=elapsed_ms(started_at),
                error_message=None if has_data else "empty db diagnostics result",
            )
        except Exception as exc:
            return ToolResult(
                status="degraded",
                data={},
                summary=f"db diagnostics unavailable for {db_query.operation}",
                duration_ms=elapsed_ms(started_at),
                error_message=str(exc),
            )


def build_db_diagnostics_backend(settings: Settings) -> DbBackend:
    """Select the db diagnostics backend from settings (default: fixture)."""
    backend = settings.db_diagnostics_backend.strip().lower()
    if backend == "fixture":
        return FixtureDbBackend(fixture_path=settings.db_diagnostics_fixture_path)
    if backend == "live":
        if not settings.db_diagnostics_url:
            msg = "db_diagnostics_backend=live requires db_diagnostics_url"
            raise ValueError(msg)
        return LiveDbBackend(
            dsn=settings.db_diagnostics_url,
            statement_timeout_ms=settings.db_diagnostics_statement_timeout_ms,
            connect_timeout_seconds=settings.tool_timeout_seconds,
        )
    msg = f"unknown db_diagnostics_backend '{settings.db_diagnostics_backend}'"
    raise ValueError(msg)
