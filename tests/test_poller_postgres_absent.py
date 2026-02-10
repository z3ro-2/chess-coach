from __future__ import annotations

from types import SimpleNamespace

import chess_review


def test_process_game_does_not_fail_without_postgres(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    monkeypatch.setattr(chess_review, "call_ollama_generate", lambda **_kwargs: "# review")

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        game = chess_review.GameInfo(
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
        args = SimpleNamespace(
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

        output_path = chess_review.process_game(conn, args, game)
    finally:
        conn.close()

    assert output_path is not None
    assert output_path.exists()

