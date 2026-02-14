from __future__ import annotations

from datetime import datetime, timedelta, timezone

import src.db.runtime_updates as runtime_updates


class _DummyConn:
    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _required_game_columns() -> set[str]:
    return {
        "id",
        "game_url",
        "pgn",
        "played_at",
        "success_notified",
        "engine_failed",
        "attempt_count",
        "last_attempt_at",
    }


def _base_row(*, game_url: str, played_at: datetime) -> dict[str, object]:
    return {
        "game_url": game_url,
        "pgn": "[Event \"Live Chess\"]\\n1. e4 e5 1-0",
        "played_at": played_at,
        "success_notified": False,
        "engine_failed": False,
        "attempt_count": 0,
        "last_attempt_at": None,
    }


def test_get_pending_games_for_processing_returns_eligible_rows(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        _base_row(game_url="https://www.chess.com/game/live/2", played_at=now - timedelta(minutes=5)),
        _base_row(game_url="https://www.chess.com/game/live/1", played_at=now - timedelta(minutes=10)),
    ]

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "3600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(runtime_updates, "_table_columns", lambda _conn, _table: _required_game_columns())
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda _conn, _query, _params=(): list(rows))

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert len(out) == 2
    assert out[0]["game_url"] == "https://www.chess.com/game/live/1"
    assert out[1]["game_url"] == "https://www.chess.com/game/live/2"


def test_get_pending_games_for_processing_excludes_rows_blocked_by_cooldown(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    blocked = _base_row(game_url="https://www.chess.com/game/live/blocked", played_at=now - timedelta(minutes=20))
    blocked["last_attempt_at"] = now - timedelta(minutes=5)
    allowed = _base_row(game_url="https://www.chess.com/game/live/allowed", played_at=now - timedelta(minutes=10))
    allowed["last_attempt_at"] = now - timedelta(hours=2)

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "3600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(runtime_updates, "_table_columns", lambda _conn, _table: _required_game_columns())
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda _conn, _query, _params=(): [blocked, allowed])

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert [row["game_url"] for row in out] == ["https://www.chess.com/game/live/allowed"]


def test_get_pending_games_for_processing_excludes_rows_blocked_by_attempt_cap(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    blocked = _base_row(game_url="https://www.chess.com/game/live/blocked", played_at=now - timedelta(minutes=20))
    blocked["attempt_count"] = 5
    allowed = _base_row(game_url="https://www.chess.com/game/live/allowed", played_at=now - timedelta(minutes=10))
    allowed["attempt_count"] = 4

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "3600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(runtime_updates, "_table_columns", lambda _conn, _table: _required_game_columns())
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda _conn, _query, _params=(): [blocked, allowed])

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert [row["game_url"] for row in out] == ["https://www.chess.com/game/live/allowed"]


def test_get_pending_games_for_processing_excludes_success_notified_rows(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    blocked = _base_row(game_url="https://www.chess.com/game/live/blocked", played_at=now - timedelta(minutes=20))
    blocked["success_notified"] = True
    allowed = _base_row(game_url="https://www.chess.com/game/live/allowed", played_at=now - timedelta(minutes=10))

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "3600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(runtime_updates, "_table_columns", lambda _conn, _table: _required_game_columns())
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda _conn, _query, _params=(): [blocked, allowed])

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert [row["game_url"] for row in out] == ["https://www.chess.com/game/live/allowed"]


def test_get_pending_games_for_processing_excludes_engine_failed_rows(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    blocked = _base_row(game_url="https://www.chess.com/game/live/blocked", played_at=now - timedelta(minutes=20))
    blocked["engine_failed"] = True
    allowed = _base_row(game_url="https://www.chess.com/game/live/allowed", played_at=now - timedelta(minutes=10))

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "3600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(runtime_updates, "_table_columns", lambda _conn, _table: _required_game_columns())
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda _conn, _query, _params=(): [blocked, allowed])

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert [row["game_url"] for row in out] == ["https://www.chess.com/game/live/allowed"]


def test_get_pending_games_for_processing_diagnostics_counts_and_newest(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    rows = []
    success_notified = _base_row(game_url="https://www.chess.com/game/live/success", played_at=now - timedelta(minutes=1))
    success_notified["success_notified"] = True
    rows.append(success_notified)
    engine_failed = _base_row(game_url="https://www.chess.com/game/live/engine", played_at=now - timedelta(minutes=2))
    engine_failed["engine_failed"] = True
    rows.append(engine_failed)
    attempt_cap = _base_row(game_url="https://www.chess.com/game/live/attempt", played_at=now - timedelta(minutes=3))
    attempt_cap["attempt_count"] = 5
    rows.append(attempt_cap)
    cooldown = _base_row(game_url="https://www.chess.com/game/live/cooldown", played_at=now - timedelta(minutes=4))
    cooldown["last_attempt_at"] = now - timedelta(minutes=10)
    rows.append(cooldown)
    eligible = _base_row(game_url="https://www.chess.com/game/live/eligible", played_at=now - timedelta(minutes=5))
    rows.append(eligible)

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "3600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(runtime_updates, "_table_columns", lambda _conn, _table: _required_game_columns())
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda _conn, _query, _params=(): list(rows))

    diag = runtime_updates.get_pending_games_for_processing_diagnostics(limit=10)

    assert diag["total_games_in_db"] == 5
    assert diag["total_pending_success_notified_false"] == 4
    assert diag["pending_total"] == 4
    assert diag["eligible_now"] == 1
    assert diag["excluded_by_success_notified"] == 1
    assert diag["excluded_by_engine_failed"] == 1
    assert diag["excluded_by_attempt_cap"] == 1
    assert diag["excluded_by_cooldown"] == 1
    newest = list(diag["top_newest_pending"])
    assert newest
    assert newest[0]["game_url"] == "https://www.chess.com/game/live/engine"


def test_poll_env_config_changes_eligibility(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    blocked_by_short_cooldown = _base_row(
        game_url="https://www.chess.com/game/live/recent-attempt",
        played_at=now - timedelta(minutes=10),
    )
    blocked_by_short_cooldown["last_attempt_at"] = now - timedelta(minutes=4)
    allowed_by_higher_attempt_cap = _base_row(
        game_url="https://www.chess.com/game/live/attempt-three",
        played_at=now - timedelta(minutes=20),
    )
    allowed_by_higher_attempt_cap["attempt_count"] = 3

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "300")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(runtime_updates, "_table_columns", lambda _conn, _table: _required_game_columns())
    monkeypatch.setattr(
        runtime_updates,
        "_fetchall",
        lambda _conn, _query, _params=(): [blocked_by_short_cooldown, allowed_by_higher_attempt_cap],
    )

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert [row["game_url"] for row in out] == ["https://www.chess.com/game/live/attempt-three"]
