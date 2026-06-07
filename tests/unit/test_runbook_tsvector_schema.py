from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy.dialects import postgresql, sqlite

from packages.db.models import RunbookChunk


def _load_tsvector_migration():
    path = Path("migrations/versions/0003_runbook_tsvector.py")
    spec = importlib.util.spec_from_file_location("migration_0003_runbook_tsvector", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runbook_tsv_content_model_uses_tsvector_on_postgres() -> None:
    column_type = RunbookChunk.__table__.c.tsv_content.type

    assert column_type.compile(dialect=postgresql.dialect()).lower() == "tsvector"
    assert column_type.compile(dialect=sqlite.dialect()).lower() == "text"


def test_runbook_tsvector_migration_uses_postgres_tsvector() -> None:
    migration = _load_tsvector_migration()

    postgres_type = migration._tsvector_column_type("postgresql")
    sqlite_type = migration._tsvector_column_type("sqlite")

    assert postgres_type.compile(dialect=postgresql.dialect()).lower() == "tsvector"
    assert sqlite_type.compile(dialect=sqlite.dialect()).lower() == "text"
