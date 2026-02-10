from __future__ import annotations

from datetime import datetime, timezone

import src.db.player_metrics as player_metrics


def test_insert_player_rating_row_is_idempotent(monkeypatch) -> None:
    inserted_urls: set[str] = set()

    def _fake_execute(_conn, query: str, params=()) -> None:
        normalized = " ".join(query.split()).lower()
        if normalized.startswith("insert into player_ratings"):
            inserted_urls.add(str(params[1]))

    def _fake_fetchone(_conn, query: str, params=()):
        normalized = " ".join(query.split()).lower()
        if "from player_ratings" in normalized and params and str(params[0]) in inserted_urls:
            return {"exists": 1}
        return None

    monkeypatch.setattr(player_metrics, "_execute", _fake_execute)
    monkeypatch.setattr(player_metrics, "_fetchone", _fake_fetchone)

    conn = object()
    kwargs = {
        "player_username": "logan",
        "game_url": "https://www.chess.com/game/live/123",
        "end_time": 1_706_000_000,
        "rating": 1200,
        "time_control": "600",
        "rated": True,
    }

    first = player_metrics.insert_player_rating_row(conn, **kwargs)
    second = player_metrics.insert_player_rating_row(conn, **kwargs)

    assert first is True
    assert second is False
    assert inserted_urls == {"https://www.chess.com/game/live/123"}


def test_record_player_rating_without_database_url_returns_false(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    inserted = player_metrics.record_player_rating_for_game(
        player_username="logan",
        game_url="https://www.chess.com/game/live/123",
        end_time=datetime.now(timezone.utc),
        player_color="white",
        pgn='[WhiteElo "1200"]',
        time_control="600",
        rated=True,
    )

    assert inserted is False


def test_record_player_rating_postgres_unreachable_returns_false(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/chess")

    def _raise_connect(_url: str):
        raise RuntimeError("down")

    monkeypatch.setattr(player_metrics, "_connect_db", _raise_connect)

    inserted = player_metrics.record_player_rating_for_game(
        player_username="logan",
        game_url="https://www.chess.com/game/live/123",
        end_time=datetime.now(timezone.utc),
        player_color="white",
        pgn='[WhiteElo "1200"]',
        time_control="600",
        rated=True,
    )

    assert inserted is False
