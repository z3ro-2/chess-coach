from __future__ import annotations

import src.db.runtime_updates as runtime_updates


def test_should_notify_then_mark_engine_failure(monkeypatch) -> None:
    state = {"failure_notified": False, "update_calls": 0, "commits": 0}

    class _DummyConn:
        def commit(self) -> None:
            state["commits"] += 1

        def rollback(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(runtime_updates, "_table_columns", lambda _conn, table: {"id", "platform_user"} if table == "players" else {"id", "player_id", "game_url", "failure_notified"})
    monkeypatch.setattr(runtime_updates, "_resolve_or_create_player_id", lambda _conn, _username, _cols: 7)
    monkeypatch.setattr(runtime_updates, "_upsert_game", lambda _conn, **_kwargs: (99, False))

    def _fetchone_stub(_conn, query: str, _params=()):
        if "SELECT failure_notified" in query:
            return {"failure_notified": state["failure_notified"]}
        raise AssertionError(f"Unexpected query: {query}")

    def _execute_stub(_conn, query: str, _params=()):
        if "UPDATE games SET failure_notified = TRUE" in query:
            state["failure_notified"] = True
            state["update_calls"] += 1
            return None
        raise AssertionError(f"Unexpected execute query: {query}")

    monkeypatch.setattr(runtime_updates, "_fetchone", _fetchone_stub)
    monkeypatch.setattr(runtime_updates, "_execute", _execute_stub)

    game_payload = {
        "game_url": "https://www.chess.com/game/live/123",
        "pgn": "[Event \"Live Chess\"]",
        "end_time": 1_706_000_000,
        "player_color": "white",
    }

    first = runtime_updates.should_notify_engine_failure(
        player_username="logan",
        game_payload=game_payload,
    )
    marked = runtime_updates.mark_engine_failure_notified(
        player_username="logan",
        game_payload=game_payload,
    )
    second = runtime_updates.should_notify_engine_failure(
        player_username="logan",
        game_payload=game_payload,
    )

    assert first["available"] is True
    assert first["should_notify"] is True
    assert first["reason"] == "notify_pending"

    assert marked["available"] is True
    assert marked["updated"] is True

    assert second["available"] is True
    assert second["should_notify"] is False

    assert state["failure_notified"] is True
    assert state["update_calls"] == 1
    assert state["commits"] == 3
