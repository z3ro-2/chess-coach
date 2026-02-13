from __future__ import annotations

import sys
import json
from types import SimpleNamespace

import analysis_pipeline as pipeline_module
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
        enable_engine=True,
        stockfish_path="/fake/stockfish",
        engine_depth=12,
        update_index=False,
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_disable_notification=False,
        username="logan",
        player_summary_every_n=20,
        player_trait_window=20,
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
    monkeypatch.setattr(chess_review, "run_analysis_pipeline", lambda **_kwargs: "# review")

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
    monkeypatch.setattr(chess_review, "run_analysis_pipeline", lambda **_kwargs: "# review")

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


def test_process_game_engine_failure_skips_markdown_and_db_and_llm(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.delenv("DATABASE_URL", raising=False)

    llm_called = {"value": False}
    telegram_calls: list[str] = []

    monkeypatch.setattr(pipeline_module, "_run_stockfish_oracle", lambda **_kwargs: None)
    monkeypatch.setattr(
        chess_review,
        "_call_selected_llm_backend",
        lambda **_kwargs: llm_called.update(value=True) or "# should-not-run",
    )
    monkeypatch.setattr(
        chess_review,
        "send_telegram_message",
        lambda message, **_kwargs: telegram_calls.append(str(message)),
    )

    args = _base_args(tmp_path)
    args.telegram_bot_token = "token"
    args.telegram_chat_id = "42"

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        output_path = chess_review.process_game(conn, args, _sample_game())
        processed_count = int(conn.execute("SELECT COUNT(*) FROM processed_games").fetchone()[0])
    finally:
        conn.close()

    assert output_path is None
    assert llm_called["value"] is False
    assert processed_count == 0
    assert len(telegram_calls) == 1
    assert "Engine failure" in telegram_calls[0]
    assert not list((tmp_path / "output" / "md").glob("*.md"))


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
    monkeypatch.setattr(chess_review, "run_analysis_pipeline", lambda **_kwargs: "# game review")
    monkeypatch.setattr(
        chess_review,
        "_compute_trait_scores_and_window_metrics",
        lambda *_args, **_kwargs: {
            "scores": {
                "tactical_awareness": 90,
                "material_discipline": 85,
                "conversion_ability": 80,
                "defensive_resilience": 75,
                "blunder_frequency": 95,
            },
            "trait_window_games": 20,
            "trait_window_moves": 320,
            "confidence": "MEDIUM",
        },
    )

    calls: list[str] = []
    expected_timeout: dict[str, int | None] = {"value": None}

    def _fake_ollama_generate(**kwargs):
        if expected_timeout["value"] is not None:
            assert kwargs.get("timeout") == expected_timeout["value"]
        user_msg = str(kwargs.get("user_msg", ""))
        calls.append(user_msg)
        if "Format the deterministic player summary." in user_msg:
            return json.dumps(
                {
                    "overall_profile": "Steady profile with room for tactical cleanup.",
                    "strengths": ["Finds active plans.", "Maintains practical chances."],
                    "weaknesses": ["Tactical conversion inconsistencies."],
                    "improvement_priorities": [
                        "Daily tactical puzzle block.",
                        "Post-game blunder review checklist.",
                        "Conversion drills from winning positions.",
                    ],
                    "style_assessment": "Active style with occasional tactical drift.",
                    "confidence": "MEDIUM",
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        return "# summary fallback"

    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)

    db_path = tmp_path / "state.sqlite"
    args = _base_args(tmp_path)
    expected_timeout["value"] = args.timeout
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
        summary_text = summary_path.read_text(encoding="utf-8")
        assert "## Snapshot" in summary_text
        assert "## Training Priority" in summary_text
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
