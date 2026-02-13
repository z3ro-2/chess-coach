from __future__ import annotations

import src.db.runtime_updates as runtime_updates


def test_should_skip_game_due_to_attempt_backoff_true(monkeypatch) -> None:
    class _DummyConn:
        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, table: {"id", "platform_user"} if table == "players" else {"id", "player_id", "game_url", "attempt_count", "last_attempt_at"},
    )
    monkeypatch.setattr(runtime_updates, "_resolve_or_create_player_id", lambda _conn, _username, _cols: 7)
    monkeypatch.setattr(runtime_updates, "_upsert_game", lambda _conn, **_kwargs: (99, False))
    monkeypatch.setattr(runtime_updates, "_fetchone", lambda *_args, **_kwargs: {"attempt_count": 8, "blocked": True})

    decision = runtime_updates.should_skip_game_due_to_attempt_backoff(
        player_username="logan",
        game_payload={"game_url": "https://www.chess.com/game/live/1"},
        max_attempts=5,
        window_hours=6,
        ignore_backoff=False,
    )

    assert decision["available"] is True
    assert decision["skip"] is True
    assert decision["reason"] == "attempt_backoff"
    assert decision["attempt_count"] == 8


def test_record_game_attempt_increments_and_stamps(monkeypatch) -> None:
    state = {"updates": 0, "commits": 0, "last_error": None}

    class _DummyConn:
        def commit(self) -> None:
            state["commits"] += 1

        def rollback(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, table: {"id", "platform_user"} if table == "players" else {"id", "player_id", "game_url", "attempt_count", "last_attempt_at", "last_error"},
    )
    monkeypatch.setattr(runtime_updates, "_resolve_or_create_player_id", lambda _conn, _username, _cols: 7)
    monkeypatch.setattr(runtime_updates, "_upsert_game", lambda _conn, **_kwargs: (99, False))

    def _execute_stub(_conn, query: str, params=()):
        if "UPDATE games" in query and "attempt_count" in query:
            state["updates"] += 1
            state["last_error"] = params[0]
            return None
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(runtime_updates, "_execute", _execute_stub)

    result = runtime_updates.record_game_attempt(
        player_username="logan",
        game_payload={"game_url": "https://www.chess.com/game/live/1"},
        last_error="boom",
    )

    assert result["available"] is True
    assert result["updated"] is True
    assert state["updates"] == 1
    assert state["commits"] == 1
    assert state["last_error"] == "boom"
