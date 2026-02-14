from __future__ import annotations

import src.db.runtime_updates as runtime_updates


def test_consume_success_notification_once(monkeypatch) -> None:
    state = {"success_notified": False, "update_calls": 0, "commits": 0}

    class _DummyCursor:
        def __init__(self) -> None:
            self.rowcount = 0

        def execute(self, query: str, _params=()) -> None:
            if "UPDATE games SET success_notified = TRUE" not in query:
                raise AssertionError(f"Unexpected query: {query}")
            if state["success_notified"]:
                self.rowcount = 0
            else:
                state["success_notified"] = True
                state["update_calls"] += 1
                self.rowcount = 1

        def close(self) -> None:
            return None

    class _DummyConn:
        def commit(self) -> None:
            state["commits"] += 1

        def rollback(self) -> None:
            return None

        def cursor(self) -> _DummyCursor:
            return _DummyCursor()

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, table: {"id", "platform_user"} if table == "players" else {"id", "player_id", "game_url", "success_notified"},
    )
    monkeypatch.setattr(runtime_updates, "_resolve_or_create_player_id", lambda _conn, _username, _cols: 7)
    monkeypatch.setattr(runtime_updates, "_upsert_game", lambda _conn, **_kwargs: (99, False))

    game_payload = {
        "game_url": "https://www.chess.com/game/live/123",
        "pgn": '[Event "Live Chess"]',
        "end_time": 1_706_000_000,
        "player_color": "white",
    }

    first = runtime_updates.consume_success_notification_once(
        player_username="logan",
        game_payload=game_payload,
    )
    second = runtime_updates.consume_success_notification_once(
        player_username="logan",
        game_payload=game_payload,
    )

    assert first["available"] is True
    assert first["should_notify"] is True
    assert first["consumed"] is True
    assert first["reason"] == "consumed"

    assert second["available"] is True
    assert second["should_notify"] is False
    assert second["consumed"] is False
    assert second["reason"] == "already_consumed"

    assert state["success_notified"] is True
    assert state["update_calls"] == 1
    assert state["commits"] == 2


def test_mark_review_success_flags_sets_both_success_and_review_notified(monkeypatch) -> None:
    state = {"success_notified": False, "review_notified": False, "update_calls": 0, "commits": 0}

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
        lambda _conn, table: {"id", "platform_user"} if table == "players" else {"id", "player_id", "game_url", "success_notified", "review_notified"},
    )
    monkeypatch.setattr(runtime_updates, "_resolve_or_create_player_id", lambda _conn, _username, _cols: 7)
    monkeypatch.setattr(runtime_updates, "_upsert_game", lambda _conn, **_kwargs: (99, False))

    def _execute_stub(_conn, query: str, _params=()):
        if "UPDATE games SET success_notified = TRUE, review_notified = TRUE" in query:
            state["success_notified"] = True
            state["review_notified"] = True
            state["update_calls"] += 1
            return None
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(runtime_updates, "_execute", _execute_stub)

    result = runtime_updates.mark_review_success_flags(
        player_username="logan",
        game_payload={"game_url": "https://www.chess.com/game/live/123"},
    )

    assert result["available"] is True
    assert result["updated"] is True
    assert state["success_notified"] is True
    assert state["review_notified"] is True
    assert state["update_calls"] == 1
    assert state["commits"] == 1


def test_should_notify_review_success_uses_success_notified_flag(monkeypatch) -> None:
    state = {"success_notified": False}

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
        lambda _conn, table: {"id", "platform_user"} if table == "players" else {"id", "player_id", "game_url", "success_notified"},
    )
    monkeypatch.setattr(runtime_updates, "_resolve_or_create_player_id", lambda _conn, _username, _cols: 7)
    monkeypatch.setattr(runtime_updates, "_upsert_game", lambda _conn, **_kwargs: (99, False))
    monkeypatch.setattr(runtime_updates, "_fetchone", lambda *_args, **_kwargs: {"success_notified": state["success_notified"]})

    first = runtime_updates.should_notify_review_success(
        player_username="logan",
        game_payload={"game_url": "https://www.chess.com/game/live/123"},
    )
    state["success_notified"] = True
    second = runtime_updates.should_notify_review_success(
        player_username="logan",
        game_payload={"game_url": "https://www.chess.com/game/live/123"},
    )

    assert first["available"] is True
    assert first["should_notify"] is True
    assert second["available"] is True
    assert second["should_notify"] is False
