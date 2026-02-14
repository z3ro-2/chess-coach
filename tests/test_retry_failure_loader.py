from __future__ import annotations

from datetime import datetime, timedelta, timezone

import src.db.runtime_updates as runtime_updates


class _DummyConn:
    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _player_columns() -> set[str]:
    return {"id", "platform_user"}


def _game_columns() -> set[str]:
    return {
        "id",
        "player_id",
        "game_url",
        "pgn",
        "success_notified",
        "engine_failed",
        "attempt_count",
        "last_attempt_at",
    }


def _row(
    *,
    game_url: str,
    success_notified: bool = False,
    engine_failed: bool = False,
    attempt_count: int = 0,
    last_attempt_at: datetime | None = None,
    pgn: str = '[Event "Live Chess"]\n1. e4 e5 1-0\n',
) -> dict[str, object]:
    return {
        "game_url": game_url,
        "pgn": pgn,
        "raw_pgn": pgn,
        "game_pgn": pgn,
        "end_time": 1_706_000_000,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
        "success_notified": success_notified,
        "engine_failed": engine_failed,
        "attempt_count": attempt_count,
        "last_attempt_at": last_attempt_at,
    }


def test_load_retry_failure_game_payloads_filters_by_flags_attempt_cap_and_cooldown(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        _row(game_url="https://www.chess.com/game/live/success", success_notified=True, engine_failed=True),
        _row(game_url="https://www.chess.com/game/live/engine-allowed", engine_failed=True, attempt_count=9, last_attempt_at=now - timedelta(hours=1)),
        _row(game_url="https://www.chess.com/game/live/attempt-allowed", engine_failed=False, attempt_count=4, last_attempt_at=now - timedelta(hours=1)),
        _row(game_url="https://www.chess.com/game/live/attempt-blocked", engine_failed=False, attempt_count=5, last_attempt_at=now - timedelta(hours=1)),
        _row(game_url="https://www.chess.com/game/live/cooldown-blocked", engine_failed=True, attempt_count=1, last_attempt_at=now - timedelta(minutes=5)),
        _row(game_url="https://www.chess.com/game/live/no-pgn", engine_failed=True, pgn=""),
    ]

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, table: _player_columns() if table == "players" else _game_columns(),
    )
    monkeypatch.setattr(runtime_updates, "_find_player_row", lambda *_args, **_kwargs: {"id": 1})
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda *_args, **_kwargs: list(rows))

    out = runtime_updates.load_retry_failure_game_payloads(player_username="logan", limit=20)

    assert [str(row["game_url"]) for row in out] == [
        "https://www.chess.com/game/live/engine-allowed",
        "https://www.chess.com/game/live/attempt-allowed",
    ]


def test_load_retry_failure_game_payloads_respects_poll_env_values(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        _row(game_url="https://www.chess.com/game/live/max-blocked", engine_failed=False, attempt_count=3, last_attempt_at=now - timedelta(minutes=10)),
        _row(game_url="https://www.chess.com/game/live/recent-blocked", engine_failed=False, attempt_count=1, last_attempt_at=now - timedelta(seconds=30)),
        _row(game_url="https://www.chess.com/game/live/eligible", engine_failed=False, attempt_count=2, last_attempt_at=now - timedelta(minutes=2)),
    ]

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "60")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, table: _player_columns() if table == "players" else _game_columns(),
    )
    monkeypatch.setattr(runtime_updates, "_find_player_row", lambda *_args, **_kwargs: {"id": 1})
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda *_args, **_kwargs: list(rows))

    out = runtime_updates.load_retry_failure_game_payloads(player_username="logan", limit=20)

    assert [str(row["game_url"]) for row in out] == ["https://www.chess.com/game/live/eligible"]
