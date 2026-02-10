from __future__ import annotations

from types import SimpleNamespace

import chess_review
from src.db import ingest_check as ingest_check_module
from src.db import player_metrics as player_metrics_module


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


def test_process_game_does_not_fail_when_postgres_is_down(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:5432/chess")
    monkeypatch.setattr(chess_review, "call_ollama_generate", lambda **_kwargs: "# review")

    def _raise_connect(_url):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(ingest_check_module, "_connect_db", _raise_connect)
    monkeypatch.setattr(player_metrics_module, "_connect_db", _raise_connect)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        output_path = chess_review.process_game(conn, _base_args(tmp_path), _sample_game())
    finally:
        conn.close()

    assert output_path is not None
    assert output_path.exists()


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
