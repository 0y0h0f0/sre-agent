from __future__ import annotations

from packages.db.session import create_db_engine, get_session


def test_create_db_engine_with_sqlite() -> None:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    try:
        assert engine is not None
        assert engine.dialect.name == "sqlite"
    finally:
        engine.dispose()


def test_get_session_yields_and_closes() -> None:
    gen = get_session()
    session = next(gen)
    try:
        assert session is not None
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
