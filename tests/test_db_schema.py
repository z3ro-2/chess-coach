import src.db.schema as schema_module


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
            return {"id", "player_id", "failure_notified", "review_notified", "engine_failed"}
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
        and "engine_failed BOOLEAN NOT NULL DEFAULT FALSE" in q
        for q in executed
    )
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS failure_notified BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS review_notified BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
    assert any("ALTER TABLE games ADD COLUMN IF NOT EXISTS engine_failed BOOLEAN NOT NULL DEFAULT FALSE" in q for q in executed)
