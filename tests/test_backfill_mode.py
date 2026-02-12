from __future__ import annotations

import json
from types import SimpleNamespace

import chess_review
import pytest


def _raw_game(*, game_id: int, end_time: int, result: str = "1-0") -> dict:
    return {
        "url": f"https://www.chess.com/game/live/{game_id}",
        "pgn": (
            f'[Event "Live Chess"]\n'
            f'[White "logan"]\n'
            f'[Black "opponent"]\n'
            f'[Result "{result}"]\n'
            f"1. e4 e5 {result}\n"
        ),
        "end_time": end_time,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "white": {"username": "logan", "rating": 1200},
        "black": {"username": "opponent", "rating": 1190},
    }


def _args(tmp_path, *, backfill: int) -> SimpleNamespace:
    return SimpleNamespace(
        username="logan",
        backfill=backfill,
        lookback_days=10,
        rules_filter="chess",
        stockfish_path="/fake/stockfish",
        engine_depth=12,
        player_trait_window=20,
        enable_engine=True,
        state_db=tmp_path / "state.sqlite",
        out=tmp_path / "output",
    )


def _parsed_games(raw_games: list[dict]) -> list[chess_review.GameInfo]:
    parsed: list[chess_review.GameInfo] = []
    for raw in raw_games:
        game = chess_review.parse_game(raw, "logan")
        assert game is not None
        parsed.append(game)
    return parsed


def test_backfill_runs_without_llm_calls(monkeypatch, tmp_path) -> None:
    args = _args(tmp_path, backfill=2)
    raw_games = [
        _raw_game(game_id=1, end_time=1_706_000_100),
        _raw_game(game_id=2, end_time=1_706_000_200),
    ]
    monkeypatch.setattr(
        chess_review,
        "_fetch_backfill_candidates",
        lambda **_kwargs: (_parsed_games(raw_games), len(raw_games)),
    )
    monkeypatch.setattr(
        chess_review,
        "_analyze_game_with_stockfish",
        lambda **_kwargs: {
            "game_summary": {"your_color": "white", "result": "1-0", "total_moves": 20},
            "key_positions": [],
        },
    )
    monkeypatch.setattr(
        chess_review,
        "call_ollama_generate",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("LLM should not be called in backfill mode.")),
    )
    monkeypatch.setattr(
        chess_review,
        "call_openai_chat",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("LLM should not be called in backfill mode.")),
    )
    monkeypatch.setattr(
        chess_review,
        "write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Markdown writes must not run in backfill mode.")),
    )

    conn = chess_review.init_db(args.state_db)
    try:
        result = chess_review.run_backfill(conn, args)
        processed_count = int(conn.execute("SELECT COUNT(*) FROM processed_games").fetchone()[0])
        meta_count = int(conn.execute("SELECT COUNT(*) FROM processed_game_meta").fetchone()[0])
    finally:
        conn.close()

    assert result["games_processed"] == 2
    assert result["engine_analyses"] == 2
    assert processed_count == 2
    assert meta_count == 2


def test_backfill_invokes_engine_once_per_game_and_stores_payloads(monkeypatch, tmp_path) -> None:
    args = _args(tmp_path, backfill=3)
    raw_games = [
        _raw_game(game_id=1, end_time=1_706_000_100),
        _raw_game(game_id=2, end_time=1_706_000_200, result="0-1"),
        _raw_game(game_id=3, end_time=1_706_000_300, result="1/2-1/2"),
    ]
    monkeypatch.setattr(
        chess_review,
        "_fetch_backfill_candidates",
        lambda **_kwargs: (_parsed_games(raw_games), len(raw_games)),
    )

    calls = {"count": 0}

    def _fake_analyze(**kwargs):
        calls["count"] += 1
        game = kwargs["game"]
        return {
            "game_summary": {"your_color": game.your_color, "result": game.result, "total_moves": 20},
            "key_positions": [{"player": "White", "label": "good", "tactical_flag": "none", "material_change": 0}],
        }

    monkeypatch.setattr(chess_review, "_analyze_game_with_stockfish", _fake_analyze)

    conn = chess_review.init_db(args.state_db)
    try:
        result = chess_review.run_backfill(conn, args)
        rows = conn.execute(
            "SELECT game_url, payload_json FROM engine_payloads ORDER BY end_time DESC"
        ).fetchall()
    finally:
        conn.close()

    assert result["games_processed"] == 3
    assert result["engine_analyses"] == 3
    assert calls["count"] == 3
    assert len(rows) == 3
    assert rows[0][0] == "https://www.chess.com/game/live/3"
    loaded = json.loads(str(rows[0][1]))
    assert loaded["game_summary"]["result"] == "1/2-1/2"
    assert isinstance(loaded["key_positions"], list)


def test_backfill_respects_n_when_fewer_games_exist(monkeypatch, tmp_path) -> None:
    args = _args(tmp_path, backfill=5)
    raw_games = [
        _raw_game(game_id=10, end_time=1_706_000_100),
        _raw_game(game_id=11, end_time=1_706_000_200),
    ]
    monkeypatch.setattr(
        chess_review,
        "_fetch_backfill_candidates",
        lambda **_kwargs: (_parsed_games(raw_games), len(raw_games)),
    )
    monkeypatch.setattr(
        chess_review,
        "_analyze_game_with_stockfish",
        lambda **_kwargs: {
            "game_summary": {"your_color": "white", "result": "1-0", "total_moves": 20},
            "key_positions": [],
        },
    )

    conn = chess_review.init_db(args.state_db)
    try:
        result = chess_review.run_backfill(conn, args)
    finally:
        conn.close()

    assert result["games_processed"] == 2
    assert result["engine_analyses"] == 2


def test_run_backfill_raises_when_limit_exceeds_two_hundred(monkeypatch, tmp_path) -> None:
    args = _args(tmp_path, backfill=201)
    monkeypatch.setattr(
        chess_review,
        "_fetch_backfill_candidates",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Fetch should not run when limit guard fails.")),
    )

    conn = chess_review.init_db(args.state_db)
    try:
        with pytest.raises(ValueError, match="Backfill limit exceeded: max 200 games at once."):
            chess_review.run_backfill(conn, args)
    finally:
        conn.close()


def test_select_backfill_games_stops_scanning_after_n_plus_buffer(monkeypatch, tmp_path) -> None:
    args = _args(tmp_path, backfill=50)
    monkeypatch.setattr(
        chess_review,
        "_month_range_for_lookback",
        lambda _now, _days: [(2025, 1), (2025, 2), (2025, 3), (2025, 4)],
    )

    calls: list[str] = []

    def _month_batch(month_tag: int, base_end: int) -> list[dict]:
        return [_raw_game(game_id=(month_tag * 1000) + i, end_time=base_end + i) for i in range(40)]

    monthly = {
        "2025/04": _month_batch(4, 1_706_040_000),
        "2025/03": _month_batch(3, 1_706_030_000),
        "2025/02": _month_batch(2, 1_706_020_000),
        "2025/01": _month_batch(1, 1_706_010_000),
    }

    def _fake_http_get_json(url: str, timeout: int = 0):
        _ = timeout
        calls.append(url)
        for key, games in monthly.items():
            if key in url:
                return {"games": games}
        return {"games": []}

    monkeypatch.setattr(chess_review, "_http_get_json", _fake_http_get_json)

    first_selected, first_considered = chess_review._select_backfill_games(args)
    second_selected, second_considered = chess_review._select_backfill_games(args)

    assert len(calls) == 4  # two calls per run, stops after Apr+Mar each run
    assert first_considered == 80  # target is N+25 => 75, month-granular stop gives 80
    assert second_considered == 80
    assert len(first_selected) == 50
    assert len(second_selected) == 50
    assert [g.game_url for g in first_selected] == [g.game_url for g in second_selected]
    assert first_selected[0].game_url.endswith("/4039")


def test_run_backfill_reports_sane_considered_count_with_large_archives(monkeypatch, tmp_path) -> None:
    args = _args(tmp_path, backfill=5)
    monkeypatch.setattr(
        chess_review,
        "_month_range_for_lookback",
        lambda _now, _days: [(2025, 1), (2025, 2), (2025, 3), (2025, 4), (2025, 5), (2025, 6)],
    )

    def _month_batch(month_tag: int, base_end: int) -> list[dict]:
        return [_raw_game(game_id=(month_tag * 1000) + i, end_time=base_end + i) for i in range(20)]

    monthly = {
        "2025/06": _month_batch(6, 1_706_060_000),
        "2025/05": _month_batch(5, 1_706_050_000),
        "2025/04": _month_batch(4, 1_706_040_000),
        "2025/03": _month_batch(3, 1_706_030_000),
        "2025/02": _month_batch(2, 1_706_020_000),
        "2025/01": _month_batch(1, 1_706_010_000),
    }

    calls: list[str] = []

    def _fake_http_get_json(url: str, timeout: int = 0):
        _ = timeout
        calls.append(url)
        for key, games in monthly.items():
            if key in url:
                return {"games": games}
        return {"games": []}

    monkeypatch.setattr(chess_review, "_http_get_json", _fake_http_get_json)
    monkeypatch.setattr(
        chess_review,
        "_analyze_game_with_stockfish",
        lambda **kwargs: {
            "game_summary": {"your_color": kwargs["game"].your_color, "result": kwargs["game"].result, "total_moves": 20},
            "key_positions": [],
        },
    )

    conn = chess_review.init_db(args.state_db)
    try:
        result = chess_review.run_backfill(conn, args)
    finally:
        conn.close()

    assert result["total_games_requested"] == 5
    assert result["games_fetched_from_chess_com"] == 40  # N+25 target => 30, month stop => 40
    assert result["games_analyzed_with_stockfish"] == 5
    assert len(calls) == 2


def test_main_backfill_skips_polling_and_telegram(monkeypatch, tmp_path, capsys) -> None:
    args = _args(tmp_path, backfill=1)

    monkeypatch.setattr(chess_review, "parse_args", lambda: args)
    monkeypatch.setattr(
        chess_review,
        "run_backfill",
        lambda *_args, **_kwargs: {
            "total_games_requested": 1,
            "games_fetched_from_chess_com": 1,
            "games_analyzed_with_stockfish": 1,
            "games_processed": 1,
            "engine_analyses": 1,
            "trait_scores": {
                "tactical_awareness": 90,
                "material_discipline": 85,
                "conversion_ability": 80,
                "defensive_resilience": 75,
                "blunder_frequency": 95,
            },
        },
    )
    monkeypatch.setattr(
        chess_review,
        "poll_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Polling must not run in backfill mode.")),
    )
    monkeypatch.setattr(
        chess_review,
        "_telegram_command_loop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Telegram loop must not run in backfill mode.")),
    )
    monkeypatch.setattr(
        chess_review.threading,
        "Thread",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Telegram thread must not initialize in backfill mode.")),
    )

    code = chess_review.main()
    out = capsys.readouterr().out

    assert code == 0
    assert "Backfill Summary:" in out
    assert "- total games requested: 1" in out
    assert "- games fetched from chess.com: 1" in out
    assert "- games analyzed with Stockfish: 1" in out


def test_main_backfill_returns_failure_status_when_backfill_errors(monkeypatch, tmp_path, caplog) -> None:
    args = _args(tmp_path, backfill=2)
    monkeypatch.setattr(chess_review, "parse_args", lambda: args)
    monkeypatch.setattr(
        chess_review,
        "run_backfill",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("Stockfish engine failed during backfill")),
    )
    monkeypatch.setattr(
        chess_review,
        "poll_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Polling must not run in failed backfill mode.")),
    )
    monkeypatch.setattr(
        chess_review.threading,
        "Thread",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Telegram thread must not initialize in backfill mode.")),
    )

    with caplog.at_level("ERROR"):
        code = chess_review.main()

    assert code == 1
    assert "Backfill failed:" in caplog.text
    assert "Stockfish engine failed during backfill" in caplog.text
