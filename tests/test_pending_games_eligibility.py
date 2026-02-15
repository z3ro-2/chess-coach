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
    row_id = int(str(game_url).rstrip("/").split("/")[-1]) if str(game_url).rstrip("/").split("/")[-1].isdigit() else 0
    return {
        "id": row_id,
        "game_url": game_url,
        "pgn": "[Event \"Live Chess\"]\\n1. e4 e5 1-0",
        "played_at": played_at,
        "time_control": "600",
        "rules": "chess",
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
    assert out[0]["game_url"] == "https://www.chess.com/game/live/2"
    assert out[1]["game_url"] == "https://www.chess.com/game/live/1"


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


def test_get_pending_games_for_processing_respects_limit(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    rows = [
        _base_row(game_url=f"https://www.chess.com/game/live/{idx}", played_at=now - timedelta(minutes=idx))
        for idx in range(1, 7)
    ]

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(runtime_updates, "_table_columns", lambda _conn, _table: _required_game_columns())
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda _conn, _query, _params=(): list(rows))

    out = runtime_updates.get_pending_games_for_processing(limit=3)

    assert len(out) == 3


def test_get_pending_games_for_processing_orders_by_newest_played_at_then_created_at(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    same_played = now - timedelta(hours=1)
    first_created = _base_row(game_url="https://www.chess.com/game/live/created-first", played_at=same_played)
    first_created["created_at"] = now - timedelta(hours=2)
    second_created = _base_row(game_url="https://www.chess.com/game/live/created-second", played_at=same_played)
    second_created["created_at"] = now - timedelta(hours=1, minutes=30)
    newer_played = _base_row(game_url="https://www.chess.com/game/live/played-newest", played_at=now - timedelta(minutes=10))
    newer_played["created_at"] = now - timedelta(minutes=15)

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, _table: _required_game_columns() | {"created_at"},
    )
    monkeypatch.setattr(
        runtime_updates,
        "_fetchall",
        lambda _conn, _query, _params=(): [second_created, newer_played, first_created],
    )

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert [row["game_url"] for row in out] == [
        "https://www.chess.com/game/live/played-newest",
        "https://www.chess.com/game/live/created-second",
        "https://www.chess.com/game/live/created-first",
    ]


def test_get_pending_games_for_processing_prefers_newest_over_analysis_complete(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    analysis_complete = _base_row(game_url="https://www.chess.com/game/live/send-only", played_at=now - timedelta(minutes=30))
    analysis_complete["analysis_complete"] = True
    analysis_complete["md_path"] = "/data/md/send-only.md"
    regular = _base_row(game_url="https://www.chess.com/game/live/regular", played_at=now - timedelta(minutes=1))

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, _table: _required_game_columns() | {"analysis_complete", "md_path"},
    )
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda _conn, _query, _params=(): [regular, analysis_complete])

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert [row["game_url"] for row in out] == [
        "https://www.chess.com/game/live/regular",
        "https://www.chess.com/game/live/send-only",
    ]


def test_get_pending_games_for_processing_send_only_respects_tg_backoff(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    send_only_blocked = _base_row(game_url="https://www.chess.com/game/live/send-only-blocked", played_at=now - timedelta(minutes=5))
    send_only_blocked["analysis_complete"] = True
    send_only_blocked["md_path"] = "/data/md/send-only.md"
    send_only_blocked["tg_send_attempts"] = 1
    send_only_blocked["tg_last_send_at"] = now - timedelta(seconds=120)

    send_only_ready = _base_row(game_url="https://www.chess.com/game/live/send-only-ready", played_at=now - timedelta(minutes=6))
    send_only_ready["analysis_complete"] = True
    send_only_ready["md_path"] = "/data/md/send-only-ready.md"
    send_only_ready["tg_send_attempts"] = 1
    send_only_ready["tg_last_send_at"] = now - timedelta(seconds=601)

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("TG_RETRY_COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("TG_MAX_SEND_ATTEMPTS", "5")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, _table: _required_game_columns() | {"analysis_complete", "md_path", "tg_send_attempts", "tg_last_send_at"},
    )
    monkeypatch.setattr(
        runtime_updates,
        "_fetchall",
        lambda _conn, _query, _params=(): [send_only_blocked, send_only_ready],
    )

    diag = runtime_updates.get_pending_games_for_processing_diagnostics(limit=10)

    assert diag["eligible_now"] == 1
    assert diag["excluded_by_tg_send_backoff"] == 1
    assert [row["game_url"] for row in diag["eligible_rows"]] == ["https://www.chess.com/game/live/send-only-ready"]


def test_get_pending_games_for_processing_falls_back_to_created_at_when_played_at_missing(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    missing_played_newer_created = _base_row(
        game_url="https://www.chess.com/game/live/missing-played-newer-created",
        played_at=now - timedelta(minutes=5),
    )
    missing_played_newer_created["played_at"] = None
    missing_played_newer_created["created_at"] = now - timedelta(minutes=1)

    missing_played_older_created = _base_row(
        game_url="https://www.chess.com/game/live/missing-played-older-created",
        played_at=now - timedelta(minutes=4),
    )
    missing_played_older_created["played_at"] = None
    missing_played_older_created["created_at"] = now - timedelta(minutes=3)

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, _table: _required_game_columns() | {"created_at"},
    )
    monkeypatch.setattr(
        runtime_updates,
        "_fetchall",
        lambda _conn, _query, _params=(): [missing_played_older_created, missing_played_newer_created],
    )

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert [row["game_url"] for row in out] == [
        "https://www.chess.com/game/live/missing-played-newer-created",
        "https://www.chess.com/game/live/missing-played-older-created",
    ]


def test_get_pending_games_for_processing_tiebreak_uses_created_at_then_id(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    same_played = now - timedelta(minutes=10)
    same_created = now - timedelta(minutes=5)
    row_a = _base_row(game_url="https://www.chess.com/game/live/8101", played_at=same_played)
    row_a["created_at"] = same_created
    row_a["id"] = 8101
    row_b = _base_row(game_url="https://www.chess.com/game/live/8102", played_at=same_played)
    row_b["created_at"] = same_created
    row_b["id"] = 8102
    row_c = _base_row(game_url="https://www.chess.com/game/live/8103", played_at=same_played)
    row_c["created_at"] = now - timedelta(minutes=1)
    row_c["id"] = 8103

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, _table: _required_game_columns() | {"created_at"},
    )
    monkeypatch.setattr(runtime_updates, "_fetchall", lambda _conn, _query, _params=(): [row_a, row_b, row_c])

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert [row["game_url"] for row in out] == [
        "https://www.chess.com/game/live/8103",  # newer created_at wins
        "https://www.chess.com/game/live/8102",  # same created_at, higher id wins
        "https://www.chess.com/game/live/8101",
    ]


def test_get_pending_games_for_processing_excludes_unsupported_rules_and_missing_time_control(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    supported = _base_row(game_url="https://www.chess.com/game/live/supported", played_at=now - timedelta(minutes=3))
    supported["rules"] = "chess"
    supported["time_control"] = "600"

    unsupported_rules = _base_row(game_url="https://www.chess.com/game/live/unsupported-rules", played_at=now - timedelta(minutes=1))
    unsupported_rules["rules"] = "chess960"
    unsupported_rules["time_control"] = "600"

    missing_time_control = _base_row(game_url="https://www.chess.com/game/live/missing-tc", played_at=now - timedelta(minutes=2))
    missing_time_control["rules"] = "chess"
    missing_time_control["time_control"] = ""

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setenv("POLL_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("POLL_COOLDOWN_SECONDS", "600")
    monkeypatch.setattr(runtime_updates, "_connect_db", lambda _url: (_DummyConn(), lambda: None))
    monkeypatch.setattr(
        runtime_updates,
        "_table_columns",
        lambda _conn, _table: _required_game_columns() | {"rules", "time_control"},
    )
    monkeypatch.setattr(
        runtime_updates,
        "_fetchall",
        lambda _conn, _query, _params=(): [supported, unsupported_rules, missing_time_control],
    )

    out = runtime_updates.get_pending_games_for_processing(limit=10)

    assert [row["game_url"] for row in out] == ["https://www.chess.com/game/live/supported"]
