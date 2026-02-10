from datetime import datetime, timezone

import src.db.bootstrap as bootstrap_module


def test_ensure_bootstrap_runs_once_then_skips_with_parser_flow(monkeypatch) -> None:
    state = {"game_count": 0}

    class _DummyConn:
        def rollback(self) -> None:
            return None

        def commit(self) -> None:
            return None

    class _ParsedGame:
        def __init__(self) -> None:
            self.game_url = "https://www.chess.com/game/live/123"
            self.pgn = '[Event "Live Chess"]\n[WhiteElo "1200"]\n[Result "1-0"]\n'
            self.end_time = 1_706_000_000
            self.time_control = "600"
            self.rated = True
            self.rules = "chess"
            self.white_username = "logan"
            self.black_username = "opponent"
            self.white_rating = 1200
            self.black_rating = 1190
            self.result = "1-0"
            self.your_color = "white"

        @property
        def end_dt_utc(self) -> datetime:
            return datetime.fromtimestamp(self.end_time, tz=timezone.utc)

    fetch_calls: list[tuple[str, int]] = []
    parse_calls: list[str] = []

    def _connect_db_stub(_database_url: str):
        return _DummyConn(), (lambda: None)

    def _resolve_player_id_stub(_conn, *, username: str, player_columns: set[str]):
        assert username == "logan"
        assert "platform_user" in player_columns
        return 7

    def _count_games_stub(_conn, *, player_id: int):
        assert player_id == 7
        return state["game_count"]

    def _insert_games_stub(_conn, *, player_id: int, games: list[dict], game_columns: set[str]):
        assert player_id == 7
        assert "player_id" in game_columns
        inserted = len(games)
        state["game_count"] += inserted
        return inserted

    def _fetch_recent_games_stub(username: str, lookback_days: int):
        fetch_calls.append((username, lookback_days))
        return [{"url": "https://www.chess.com/game/live/123"}]

    def _parse_game_stub(raw: dict, username: str):
        parse_calls.append(raw.get("url", ""))
        assert username == "logan"
        return _ParsedGame()

    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    monkeypatch.setattr(bootstrap_module, "_connect_db", _connect_db_stub)
    monkeypatch.setattr(
        bootstrap_module,
        "_safe_table_columns",
        lambda _conn, table_name: {"platform_user"} if table_name == "players" else {"player_id", "game_url", "pgn", "end_time"},
    )
    monkeypatch.setattr(bootstrap_module, "_resolve_or_create_player_id", _resolve_player_id_stub)
    monkeypatch.setattr(bootstrap_module, "_count_games_for_player", _count_games_stub)
    monkeypatch.setattr(bootstrap_module, "_insert_games_for_player", _insert_games_stub)
    monkeypatch.setattr(bootstrap_module, "_seed_player_ratings_for_bootstrap", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bootstrap_module, "_seed_rating_history_if_available", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(bootstrap_module, "_seed_traits_if_available", lambda *_args, **_kwargs: 0)

    first = bootstrap_module.ensure_bootstrap(
        username="logan",
        bootstrap_games=25,
        fetch_recent_games_fn=_fetch_recent_games_stub,
        parse_game_fn=_parse_game_stub,
    )
    second = bootstrap_module.ensure_bootstrap(
        username="logan",
        bootstrap_games=25,
        fetch_recent_games_fn=_fetch_recent_games_stub,
        parse_game_fn=_parse_game_stub,
    )

    assert first["ran"] is True
    assert first["reason"] == "bootstrapped"
    assert first["inserted_games"] == 1
    assert first["requested_games"] == 25

    assert second["ran"] is False
    assert second["reason"] == "already_seeded"

    assert len(fetch_calls) == 1
    assert len(parse_calls) == 1
