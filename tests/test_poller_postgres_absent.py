from __future__ import annotations

import sys
from types import SimpleNamespace

import chess_review
from src.db import ingest_check as ingest_check_module
from src.db import player_metrics as player_metrics_module
from src.db import runtime_updates as runtime_updates_module


def _base_args(tmp_path):
    return SimpleNamespace(
        out=tmp_path / "output",
        timezone="UTC",
        provider="ollama",
        ollama_model="llama3.1:8b",
        gpt_model="gpt-4o-mini",
        ollama_url="http://127.0.0.1:11434",
        timeout=5,
        max_tokens=100,
        update_index=False,
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_disable_notification=False,
        username="logan",
        player_summary_every_n=20,
    )


def _sample_game() -> chess_review.GameInfo:
    return chess_review.GameInfo(
        game_url="https://www.chess.com/game/live/123",
        pgn='[Event "Live Chess"]\n[WhiteElo "1200"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        end_time=1_706_000_000,
        time_control="600",
        rated=True,
        rules="chess",
        white_username="logan",
        black_username="opponent",
        white_rating=1200,
        black_rating=1190,
        result="1-0",
        your_color="white",
        opponent="opponent",
    )


def test_process_game_does_not_fail_without_postgres(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(chess_review, "call_ollama_generate", lambda **_kwargs: "# review")

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        output_path = chess_review.process_game(conn, _base_args(tmp_path), _sample_game())
    finally:
        conn.close()

    assert output_path is not None
    assert output_path.exists()
    assert (tmp_path / "output" / "player_stats.md").exists()


def test_process_game_does_not_fail_when_postgres_is_down(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/chess")
    monkeypatch.setattr(chess_review, "call_ollama_generate", lambda **_kwargs: "# review")

    def _raise_connect(_url):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(ingest_check_module, "_connect_db", _raise_connect)
    monkeypatch.setattr(player_metrics_module, "_connect_db", _raise_connect)
    monkeypatch.setattr(runtime_updates_module, "_connect_db", _raise_connect)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        output_path = chess_review.process_game(conn, _base_args(tmp_path), _sample_game())
    finally:
        conn.close()

    assert output_path is not None
    assert output_path.exists()
    assert (tmp_path / "output" / "player_stats.md").exists()


def test_poll_once_without_postgres_does_not_crash(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = True
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0


def test_player_summary_triggers_every_n_and_does_not_retrigger_after_restart(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.delenv("DATABASE_URL", raising=False)

    calls: list[str] = []

    def _fake_ollama_generate(**kwargs):
        user_msg = str(kwargs.get("user_msg", ""))
        calls.append(user_msg)
        if "Create a Markdown summary for this player's latest cadence window." in user_msg:
            return "# player summary"
        return "# game review"

    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)

    db_path = tmp_path / "state.sqlite"
    args = _base_args(tmp_path)
    args.player_summary_every_n = 2
    game1 = _sample_game()
    game2 = chess_review.GameInfo(
        game_url="https://www.chess.com/game/live/124",
        pgn='[Event "Live Chess"]\n[WhiteElo "1201"]\n[Result "0-1"]\n1. d4 d5 0-1\n',
        end_time=1_706_000_060,
        time_control="600",
        rated=True,
        rules="chess",
        white_username="logan",
        black_username="opponent",
        white_rating=1201,
        black_rating=1192,
        result="0-1",
        your_color="white",
        opponent="opponent",
    )

    conn = chess_review.init_db(db_path)
    try:
        assert chess_review.process_game(conn, args, game1) is not None
        assert not (tmp_path / "output" / "player_summary.md").exists()

        assert chess_review.process_game(conn, args, game2) is not None
        summary_path = tmp_path / "output" / "player_summary.md"
        assert summary_path.exists()
        assert summary_path.read_text(encoding="utf-8").strip() == "# player summary"
    finally:
        conn.close()

    calls_before_restart = len(calls)
    conn = chess_review.init_db(db_path)
    try:
        result = chess_review._maybe_generate_player_summary(
            conn,
            args,
            latest_game=game2,
            stats_path=tmp_path / "output" / "player_stats.md",
        )
    finally:
        conn.close()

    assert result is None
    assert len(calls) == calls_before_restart


def test_state_db_defaults_from_env_and_init_db_writes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CHESS_USERNAME", "logan")
    monkeypatch.setenv("CHESS_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("STATE_DB", "/data/state.sqlite")
    monkeypatch.setattr(sys, "argv", ["chess_review.py", "--once"])

    args = chess_review.parse_args()
    assert str(args.state_db) == "/data/state.sqlite"

    writable_state_db = tmp_path / "data" / "state.sqlite"
    monkeypatch.setenv("STATE_DB", str(writable_state_db))
    monkeypatch.setattr(sys, "argv", ["chess_review.py", "--once"])
    args = chess_review.parse_args()
    conn = chess_review.init_db(args.state_db)
    try:
        row = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    finally:
        conn.close()

    assert row is not None
    assert writable_state_db.exists()
