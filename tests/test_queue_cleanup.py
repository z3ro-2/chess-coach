from __future__ import annotations

from datetime import datetime, timedelta, timezone

import src.db.runtime_updates as runtime_updates


class _DummyConn:
    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _required_columns() -> set[str]:
    return {
        "id",
        "player_id",
        "game_url",
        "pgn",
        "played_at",
        "success_notified",
        "engine_failed",
        "attempt_count",
        "last_attempt_at",
        "completed_at",
    }


def _row(
    *,
    game_id: int,
    game_url: str,
    played_at: datetime,
    success_notified: bool = False,
    engine_failed: bool = False,
    attempt_count: int = 0,
    last_attempt_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict[str, object]:
    pgn = '[Event "Live Chess"]\n1. e4 e5 1-0\n'
    return {
        "id": game_id,
        "game_url": game_url,
        "pgn": pgn,
        "raw_pgn": pgn,
        "game_pgn": pgn,
        "end_time": 1_706_000_000 + game_id,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
        "played_at": played_at,
        "success_notified": success_notified,
        "engine_failed": engine_failed,
        "attempt_count": attempt_count,
        "last_attempt_at": last_attempt_at,
        "completed_at": completed_at,
    }


def test_cleanup_marks_completed_and_pending_excludes_them(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        _row(
            game_id=1,
            game_url="https://www.chess.com/game/live/success-done",
            played_at=now - timedelta(hours=2),
            success_notified=True,
            last_attempt_at=now - timedelta(hours=2),
        ),
        _row(
            game_id=2,
            game_url="https://www.chess.com/game/live/failed-maxed",
            played_at=now - timedelta(hours=3),
            engine_failed=True,
            attempt_count=5,
            last_attempt_at=now - timedelta(hours=2),
        ),
        _row(
            game_id=3,
            game_url="https://www.chess.com/game/live/pending",
            played_at=now - timedelta(hours=1),
            attempt_count=1,
            last_attempt_at=now - timedelta(hours=2),
        ),
    ]

    def _fetchall(_conn, query: str, _params=()):
        if "SELECT id, game_url, success_notified, engine_failed, attempt_count, last_attempt_at, completed_at" in query:
            return [dict(r) for r in rows if r.get("completed_at") is None]
        if "SELECT" in query and "FROM games" in query and "played_at" in query:
            return [dict(r) for r in rows]
        return []

    def _execute(_conn, query: str, params=()):
        if "UPDATE games SET completed_at = NOW()" in query:
            game_id = int(params[0])
            for row in rows:
                if int(row["id"]) == game_id and row.get("completed_at") is None:
                    row["completed_at"] = now

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, table: {"id", "platform_user"} if table == "players" else _required_columns(),
    )
    monkeypatch.setattr(runtime_updates, "_find_player_row", lambda *_args, **_kwargs: {"id": 1})
    monkeypatch.setattr(runtime_updates, "_fetchall", _fetchall)
    monkeypatch.setattr(runtime_updates, "_execute", _execute)

    cleanup = runtime_updates.cleanup_completed_games(player_username="logan")
    pending = runtime_updates.get_pending_games_for_processing(limit=10)

    assert cleanup["available"] is True
    assert int(cleanup["marked_count"]) == 2
    assert [str(r["game_url"]) for r in pending] == ["https://www.chess.com/game/live/pending"]


def test_cleanup_respects_poll_cooldown_seconds(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        _row(
            game_id=10,
            game_url="https://www.chess.com/game/live/recent-success",
            played_at=now - timedelta(hours=1),
            success_notified=True,
            last_attempt_at=now - timedelta(seconds=30),
        )
    ]

    def _fetchall(_conn, query: str, _params=()):
        if "SELECT id, game_url, success_notified, engine_failed, attempt_count, last_attempt_at, completed_at" in query:
            return [dict(r) for r in rows if r.get("completed_at") is None]
        return []

    def _execute(_conn, query: str, params=()):
        if "UPDATE games SET completed_at = NOW()" in query:
            raise AssertionError("cleanup should not mark during cooldown")

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, table: {"id", "platform_user"} if table == "players" else _required_columns(),
    )
    monkeypatch.setattr(runtime_updates, "_find_player_row", lambda *_args, **_kwargs: {"id": 1})
    monkeypatch.setattr(runtime_updates, "_fetchall", _fetchall)
    monkeypatch.setattr(runtime_updates, "_execute", _execute)

    cleanup = runtime_updates.cleanup_completed_games(player_username="logan")

    assert cleanup["available"] is True
    assert int(cleanup["marked_count"]) == 0
