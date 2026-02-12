from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import chess_review
from src.commands import run_command


def _args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        out=tmp_path / "output",
        username="logan",
        player_summary_every_n=1,
        player_trait_window=20,
        provider="ollama",
        ollama_url="http://127.0.0.1:11434",
        ollama_model="llama3.1:8b",
        gpt_model="gpt-4o-mini",
        timeout=5,
        max_tokens=200,
        telegram_bot_token="",
        telegram_chat_id="",
    )


def _sample_game() -> chess_review.GameInfo:
    return chess_review.GameInfo(
        game_url="https://www.chess.com/game/live/999",
        pgn='[Event "Live Chess"]\n[WhiteElo "1200"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        end_time=1_706_000_100,
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


def test_status_command_without_postgres(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        result = run_command("status", conn, _args(tmp_path))
    finally:
        conn.close()

    text = str(result["text"])
    assert "Postgres: disabled" in text
    assert "Games since last summary:" in text


def test_summary_command_updates_state_and_does_not_retrigger_after_restart(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    calls: list[str] = []
    expected_timeout: dict[str, int | None] = {"value": None}
    trait_scores = {
        "tactical_awareness": 91,
        "material_discipline": 88,
        "conversion_ability": 79,
        "defensive_resilience": 84,
        "blunder_frequency": 95,
    }

    def _fake_ollama_generate(**kwargs):
        if expected_timeout["value"] is not None:
            assert kwargs.get("timeout") == expected_timeout["value"]
        user_msg = str(kwargs.get("user_msg", ""))
        calls.append(user_msg)
        if "Format the deterministic player summary." in user_msg:
            return "# forced summary"
        return "# review"

    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)

    def _fake_trait_scores(_conn, _args, *, window_size: int):
        assert window_size == 7
        return trait_scores

    monkeypatch.setattr(chess_review, "_compute_trait_scores_for_window", _fake_trait_scores)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _args(tmp_path)
        expected_timeout["value"] = args.timeout
        args.player_trait_window = 7
        args.out.mkdir(parents=True, exist_ok=True)
        game = _sample_game()
        md_path = args.out / "md" / "g.md"
        pgn_path = args.out / "pgn" / "g.pgn"
        chess_review.write_text(md_path, "# review")
        chess_review.write_text(pgn_path, game.pgn)
        chess_review.mark_processed(
            conn=conn,
            game_url=game.game_url,
            end_time=game.end_time,
            md_path=md_path,
            pgn_path=pgn_path,
            provider=args.provider,
            model=args.ollama_model,
            content_hash="h",
        )
        chess_review._record_processed_game_meta(conn, game)
        result = run_command("summary", conn, args)
        assert Path(result["file"]).exists()
        assert "Trait scores (last 7 games):" in str(result["text"])
    finally:
        conn.close()

    calls_before_restart = len(calls)
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        retrigger = chess_review._maybe_generate_player_summary(
            conn,
            _args(tmp_path),
            latest_game=_sample_game(),
            stats_path=tmp_path / "output" / "player_stats.md",
        )
    finally:
        conn.close()

    assert retrigger is None
    assert len(calls) == calls_before_restart
