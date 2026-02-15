import src.db.schema as schema_module
from pathlib import Path


def test_ensure_postgres_core_schema_includes_failure_notified_column(monkeypatch) -> None:
    executed: list[str] = []

    class _DummyConn:
        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    def _connect_db_stub(_database_url: str):
        return _DummyConn(), (lambda: None)

    def _execute_stub(_conn, query: str, _params=()):
        executed.append(" ".join(query.split()))

    def _table_columns_stub(_conn, table_name: str):
        if table_name == "players":
            return {"id", "username"}
        if table_name == "games":
            return {
                "id",
                "player_id",
                "failure_notified",
                "review_notified",
                "success_notified",
                "analysis_complete",
                "md_path",
                "tg_send_failed",
                "tg_last_error",
                "tg_send_attempts",
                "tg_last_send_at",
                "pgn_missing",
                "pgn_missing_attempts",
                "pgn_missing_last_attempt_at",
                "pgn_missing_count",
                "pgn_missing_terminal",
                "engine_failed",
                "completed_at",
                "last_attempt_at",
                "attempt_count",
                "last_error",
            }
        return set()

    monkeypatch.setattr(schema_module, "_connect_db", _connect_db_stub)
    monkeypatch.setattr(schema_module, "_execute", _execute_stub)
    monkeypatch.setattr(schema_module, "_table_columns", _table_columns_stub)

    result = schema_module.ensure_postgres_core_schema(database_url="postgres://example")

    assert result["ready"] is True
    assert result["reason"] == "ready"
    assert "games" in result["tables_ready"]
    assert any(
        "CREATE TABLE IF NOT EXISTS games" in q
        and "failure_notified BOOLEAN NOT NULL DEFAULT FALSE" in q
        and "review_notified BOOLEAN NOT NULL DEFAULT FALSE" in q
        and "success_notified BOOLEAN NOT NULL DEFAULT FALSE" in q
        and "analysis_complete BOOLEAN NOT NULL DEFAULT FALSE" in q
        and "md_path TEXT" in q
        and "tg_send_failed BOOLEAN NOT NULL DEFAULT FALSE" in q
        and "tg_last_error TEXT" in q
        and "tg_send_attempts INTEGER NOT NULL DEFAULT 0" in q
        and "tg_last_send_at TIMESTAMPTZ" in q
        and "pgn_missing BOOLEAN NOT NULL DEFAULT FALSE" in q
        and "pgn_missing_attempts INTEGER NOT NULL DEFAULT 0" in q
        and "pgn_missing_last_attempt_at TIMESTAMPTZ" in q
        and "pgn_missing_count INTEGER NOT NULL DEFAULT 0" in q
        and "pgn_missing_terminal BOOLEAN NOT NULL DEFAULT FALSE" in q
        and "engine_failed BOOLEAN NOT NULL DEFAULT FALSE" in q
        and "completed_at TIMESTAMPTZ" in q
        and "last_attempt_at TIMESTAMPTZ" in q
        and "attempt_count INTEGER NOT NULL DEFAULT 0" in q
        and "last_error TEXT" in q
        for q in executed
    )
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS failure_notified BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS review_notified BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS success_notified BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS analysis_complete BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS md_path TEXT" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS tg_send_failed BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS tg_last_error TEXT" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS tg_send_attempts INTEGER NOT NULL DEFAULT 0" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS tg_last_send_at TIMESTAMPTZ" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS pgn_missing BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS pgn_missing_attempts INTEGER NOT NULL DEFAULT 0" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS pgn_missing_last_attempt_at TIMESTAMPTZ" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS pgn_missing_count INTEGER NOT NULL DEFAULT 0" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS pgn_missing_terminal BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS engine_failed BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS last_error TEXT" in q for q in executed)


def test_games_retry_completion_migration_script_contains_required_alters() -> None:
    text = Path("migrations/002_games_schema_retry_and_completion.sql").read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert "ALTER TABLE games ADD COLUMN IF NOT EXISTS pgn_missing_count INTEGER NOT NULL DEFAULT 0;" in normalized
    assert "ALTER TABLE games ADD COLUMN IF NOT EXISTS pgn_missing_terminal BOOLEAN NOT NULL DEFAULT FALSE;" in normalized
    assert "ALTER TABLE games ADD COLUMN IF NOT EXISTS analysis_complete BOOLEAN NOT NULL DEFAULT FALSE;" in normalized
    assert "ALTER TABLE games ADD COLUMN IF NOT EXISTS tg_send_attempts INTEGER NOT NULL DEFAULT 0;" in normalized
    assert "ALTER TABLE games ADD COLUMN IF NOT EXISTS tg_last_send_at TIMESTAMPTZ;" in normalized
    assert "ALTER TABLE games ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;" in normalized


def test_ensure_postgres_core_schema_idempotent_on_older_schema(monkeypatch) -> None:
    executed: list[str] = []

    class _DummyConn:
        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    def _connect_db_stub(_database_url: str):
        return _DummyConn(), (lambda: None)

    def _execute_stub(_conn, query: str, _params=()):
        executed.append(" ".join(query.split()))

    def _table_columns_stub(_conn, table_name: str):
        if table_name == "players":
            return {"id", "username"}
        if table_name == "games":
            # Simulate an older schema view that lacks newly added columns.
            return {"id", "player_id", "game_url", "pgn", "end_time", "created_at"}
        return set()

    monkeypatch.setattr(schema_module, "_connect_db", _connect_db_stub)
    monkeypatch.setattr(schema_module, "_execute", _execute_stub)
    monkeypatch.setattr(schema_module, "_table_columns", _table_columns_stub)

    first = schema_module.ensure_postgres_core_schema(database_url="postgres://example")
    second = schema_module.ensure_postgres_core_schema(database_url="postgres://example")

    assert first["ready"] is True
    assert second["ready"] is True
    # Repeated runs must remain safe/idempotent and keep issuing IF NOT EXISTS alters.
    assert sum("ALTER TABLE games ADD COLUMN IF NOT EXISTS analysis_complete BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed) >= 2
    assert sum("ALTER TABLE games ADD COLUMN IF NOT EXISTS tg_send_attempts INTEGER NOT NULL DEFAULT 0" in q for q in executed) >= 2
    assert sum("ALTER TABLE games ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ" in q for q in executed) >= 2
