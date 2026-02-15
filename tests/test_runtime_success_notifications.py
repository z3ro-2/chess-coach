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
    state = {"success_notified": False, "review_notified": False, "analysis_complete": False, "last_error_cleared": False, "update_calls": 0, "commits": 0}

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
        lambda _conn, table: {"id", "platform_user"} if table == "players" else {"id", "player_id", "game_url", "success_notified", "review_notified", "analysis_complete", "last_error"},
    )
    monkeypatch.setattr(runtime_updates, "_resolve_or_create_player_id", lambda _conn, _username, _cols: 7)
    monkeypatch.setattr(runtime_updates, "_upsert_game", lambda _conn, **_kwargs: (99, False))

    def _execute_stub(_conn, query: str, _params=()):
        if "UPDATE games SET" in query and "success_notified = TRUE" in query and "review_notified = TRUE" in query:
            state["success_notified"] = True
            state["review_notified"] = True
            state["analysis_complete"] = "analysis_complete = TRUE" in query
            state["last_error_cleared"] = "last_error = NULL" in query
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
    assert state["analysis_complete"] is True
    assert state["last_error_cleared"] is True
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


def test_mark_analysis_complete_sets_flag_and_path(monkeypatch) -> None:
    state = {"analysis_complete": False, "md_path": None, "commits": 0}

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
        lambda _conn, table: {"id", "platform_user"} if table == "players" else {"id", "player_id", "game_url", "analysis_complete", "md_path"},
    )
    monkeypatch.setattr(runtime_updates, "_resolve_or_create_player_id", lambda _conn, _username, _cols: 7)
    monkeypatch.setattr(runtime_updates, "_upsert_game", lambda _conn, **_kwargs: (99, False))

    def _execute_stub(_conn, query: str, params=()):
        if "UPDATE games SET analysis_complete = TRUE, md_path = %s WHERE id = %s" not in query:
            raise AssertionError(f"Unexpected query: {query}")
        state["analysis_complete"] = True
        state["md_path"] = params[0]
        return None

    monkeypatch.setattr(runtime_updates, "_execute", _execute_stub)

    result = runtime_updates.mark_analysis_complete(
        player_username="logan",
        game_payload={"game_url": "https://www.chess.com/game/live/123"},
        md_path="/data/md/abc.md",
    )

    assert result["available"] is True
    assert result["updated"] is True
    assert state["analysis_complete"] is True
    assert state["md_path"] == "/data/md/abc.md"
    assert state["commits"] == 1


def test_mark_telegram_send_failed_persists_error(monkeypatch) -> None:
    state = {"tg_send_failed": False, "tg_last_error": None, "commits": 0}

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
        lambda _conn, table: {"id", "platform_user"} if table == "players" else {"id", "player_id", "game_url", "tg_send_failed", "tg_last_error"},
    )
    monkeypatch.setattr(runtime_updates, "_resolve_or_create_player_id", lambda _conn, _username, _cols: 7)
    monkeypatch.setattr(runtime_updates, "_upsert_game", lambda _conn, **_kwargs: (99, False))

    def _execute_stub(_conn, query: str, params=()):
        if "UPDATE games SET tg_send_failed = TRUE, tg_last_error = %s WHERE id = %s" not in query:
            raise AssertionError(f"Unexpected query: {query}")
        state["tg_send_failed"] = True
        state["tg_last_error"] = params[0]
        return None

    monkeypatch.setattr(runtime_updates, "_execute", _execute_stub)

    result = runtime_updates.mark_telegram_send_failed(
        player_username="logan",
        game_payload={"game_url": "https://www.chess.com/game/live/123"},
        error_message="Telegram API error 500: down",
    )

    assert result["available"] is True
    assert result["updated"] is True
    assert state["tg_send_failed"] is True
    assert "500" in str(state["tg_last_error"])
    assert state["commits"] == 1


def test_record_pgn_missing_not_found_marks_permanent_after_cap(monkeypatch) -> None:
    state = {"commits": 0, "params": None}

    class _DummyConn:
        def commit(self) -> None:
            state["commits"] += 1

        def rollback(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("PGN_MISSING_MAX_ATTEMPTS", "5")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, _table: {"id", "game_url", "pgn_missing", "pgn_missing_attempts", "pgn_missing_last_attempt_at"},
    )
    monkeypatch.setattr(runtime_updates, "_fetchone", lambda *_args, **_kwargs: {"id": 99, "attempts": 4})
    monkeypatch.setattr(
        runtime_updates,
        "_execute",
        lambda _conn, _query, params=(): state.__setitem__("params", tuple(params)),
    )

    out = runtime_updates.record_pgn_missing_not_found(game_url="https://www.chess.com/game/live/123")

    assert out["available"] is True
    assert out["updated"] is True
    assert out["attempts"] == 5
    assert out["pgn_missing"] is True
    assert state["params"] == (5, True, 99)
    assert state["commits"] == 1


def test_load_games_missing_pgn_applies_missing_backoff_and_cap_filters(monkeypatch) -> None:
    captured = {"query": "", "params": ()}

    class _DummyConn:
        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("PGN_MISSING_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("PGN_MISSING_RETRY_SECONDS", "600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, _table: {
            "id",
            "game_url",
            "pgn",
            "raw_pgn",
            "game_pgn",
            "played_at",
            "pgn_missing",
            "pgn_missing_attempts",
            "pgn_missing_last_attempt_at",
        },
    )

    def _fetchall_stub(_conn, query: str, params=()):
        captured["query"] = str(query)
        captured["params"] = tuple(params)
        return []

    monkeypatch.setattr(runtime_updates, "_fetchall", _fetchall_stub)

    rows = runtime_updates.load_games_missing_pgn(limit=10)

    assert rows == []
    assert "COALESCE(pgn_missing, FALSE) = FALSE" in captured["query"]
    assert "COALESCE(pgn_missing_attempts, 0) < %s" in captured["query"]
    assert "pgn_missing_last_attempt_at IS NULL OR pgn_missing_last_attempt_at < (NOW() - (%s * INTERVAL '1 second'))" in captured["query"]
    assert captured["params"] == (5, 600, 10)


def test_reset_game_processing_state_clears_stuck_fields(monkeypatch) -> None:
    captured = {"query": "", "params": (), "commits": 0}

    class _DummyConn:
        def commit(self) -> None:
            captured["commits"] += 1

        def rollback(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, _table: {
            "id",
            "game_url",
            "engine_failed",
            "failure_notified",
            "attempt_count",
            "last_attempt_at",
            "success_notified",
            "review_notified",
            "completed_at",
            "last_error",
            "analysis_complete",
            "md_path",
            "tg_send_failed",
            "tg_last_error",
            "pgn_missing",
            "pgn_missing_attempts",
            "pgn_missing_last_attempt_at",
        },
    )
    monkeypatch.setattr(runtime_updates, "_fetchone", lambda *_args, **_kwargs: {"id": 42})
    monkeypatch.setattr(
        runtime_updates,
        "_execute",
        lambda _conn, query, params=(): captured.update({"query": str(query), "params": tuple(params)}),
    )

    out = runtime_updates.reset_game_processing_state(game_url="https://www.chess.com/game/live/123")

    assert out["available"] is True
    assert out["updated"] is True
    assert out["reason"] == "reset"
    assert "engine_failed = %s" in captured["query"]
    assert "pgn_missing = %s" in captured["query"]
    assert captured["params"][-1] == 42
    assert captured["commits"] == 1
