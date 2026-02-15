from __future__ import annotations

import logging
import sys
import json
import os
import time
from datetime import datetime, timezone
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


def test_polling_debug_logs_skip_reasons_when_enabled(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.setenv("DEBUG_POLLING", "1")
    now_epoch = int(time.time())
    raw_games = [
        {"url": "https://www.chess.com/game/live/1", "pgn": "", "end_time": now_epoch, "white": {"username": "logan"}, "black": {"username": "opponent"}},
        {
            "url": "https://www.chess.com/game/live/2",
            "pgn": '[Event "Live Chess"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
            "end_time": now_epoch,
            "rules": "chess960",
            "time_control": "600",
            "white": {"username": "logan"},
            "black": {"username": "opponent"},
        },
        {
            "url": "https://www.chess.com/game/live/3",
            "pgn": '[Event "Live Chess"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
            "end_time": 1,
            "rules": "chess",
            "time_control": "600",
            "white": {"username": "logan"},
            "black": {"username": "opponent"},
        },
        {
            "url": "https://www.chess.com/game/live/4",
            "pgn": '[Event "Live Chess"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
            "end_time": now_epoch,
            "rules": "chess",
            "time_control": "600",
            "white": {"username": "logan"},
            "black": {"username": "opponent"},
        },
        {
            "url": "https://www.chess.com/game/live/5",
            "pgn": '[Event "Live Chess"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
            "end_time": now_epoch,
            "rules": "chess",
            "time_control": "600",
            "white": {"username": "logan"},
            "black": {"username": "opponent"},
        },
    ]
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: raw_games)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = True
        args.retries = 1

        processed_game = chess_review.parse_game(raw_games[3], "logan")
        assert processed_game is not None
        md_path = tmp_path / "output" / "md" / "already.md"
        pgn_path = tmp_path / "output" / "pgn" / "already.pgn"
        chess_review.write_text(md_path, "# review")
        chess_review.write_text(pgn_path, processed_game.pgn)
        chess_review.mark_processed(
            conn=conn,
            game_url=processed_game.game_url,
            end_time=processed_game.end_time,
            md_path=md_path,
            pgn_path=pgn_path,
            provider=args.provider,
            model=args.ollama_model,
            content_hash="h",
        )

        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 1
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "[POLL-DEBUG] total games fetched from chess.com: 5" in text
    assert "reason=missing PGN" in text
    assert "reason=rules_filter mismatch" in text
    assert "reason=outside lookback window" in text
    assert "reason=already processed" in text
    assert "source=state_db" in text
    assert "Game selected for processing: https://www.chess.com/game/live/5" in text


def test_polling_debug_logs_suppressed_when_flag_disabled(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.delenv("DEBUG_POLLING", raising=False)
    monkeypatch.setattr(
        chess_review,
        "fetch_recent_games",
        lambda *_args, **_kwargs: [
            {"url": "https://www.chess.com/game/live/1", "pgn": "", "end_time": int(time.time()), "white": {"username": "logan"}, "black": {"username": "opponent"}}
        ],
    )

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = True
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    assert "[POLL-DEBUG]" not in "\n".join(record.getMessage() for record in caplog.records)


def test_retry_failures_flag_processes_seeded_failed_game_once(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"process": 0}

    row = {
        "game_url": "https://www.chess.com/game/live/9001",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": int(time.time()),
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }

    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [row])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [{"game_url": row["game_url"], "success_notified": False, "engine_failed": False, "attempt_count": 0, "last_attempt_at": ""}],
        },
    )
    monkeypatch.setattr(chess_review, "load_retry_failure_game_payloads", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "fetch_recent_games",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("retry-failures should not call fetch_recent_games")),
    )
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})

    def _process_stub(_conn, _args, game):
        calls["process"] += 1
        return tmp_path / "output" / "md" / f"{game.game_url.split('/')[-1]}.md"

    monkeypatch.setattr(chess_review, "process_game", _process_stub)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = True
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 1
    assert calls["process"] == 1


def test_poll_once_runs_queue_cleanup_job(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    cleanup_calls = {"count": 0}
    monkeypatch.setattr(
        chess_review,
        "cleanup_completed_games",
        lambda **_kwargs: cleanup_calls.__setitem__("count", cleanup_calls["count"] + 1)
        or {"available": True, "reason": "ok", "marked_count": 0},
    )
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 0,
            "eligible_now": 0,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.dry_run = False
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    assert cleanup_calls["count"] == 1


def test_retry_failures_processes_pending_eligible_even_when_legacy_backoff_says_skip(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"process": 0}

    row = {
        "game_url": "https://www.chess.com/game/live/9002",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": int(time.time()),
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }

    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [row])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [{"game_url": row["game_url"], "success_notified": False, "engine_failed": False, "attempt_count": 0, "last_attempt_at": ""}],
        },
    )
    monkeypatch.setattr(chess_review, "load_retry_failure_game_payloads", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "should_skip_game_due_to_attempt_backoff",
        lambda **_kwargs: {"available": True, "reason": "attempt_backoff", "skip": True, "attempt_count": 7},
    )
    monkeypatch.setattr(chess_review, "record_game_attempt", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})

    def _process_stub(_conn, _args, _game):
        calls["process"] += 1
        return tmp_path / "output" / "md" / "x.md"

    monkeypatch.setattr(chess_review, "process_game", _process_stub)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = True
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        args.attempt_backoff_max_attempts = 3
        args.attempt_backoff_window_hours = 24
        args.ignore_attempt_backoff = False
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 1
    assert calls["process"] == 1


def test_retry_failures_dedupes_overlapping_sources(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"process": 0}

    row_a = {
        "game_url": "https://www.chess.com/game/live/9010",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": int(time.time()) - 10,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }
    row_b = {
        **row_a,
        "game_url": "https://www.chess.com/game/live/9011",
        "end_time": int(time.time()),
    }

    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [row_a])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [{"game_url": row_a["game_url"], "success_notified": False, "engine_failed": False, "attempt_count": 0, "last_attempt_at": ""}],
        },
    )
    monkeypatch.setattr(chess_review, "load_retry_failure_game_payloads", lambda **_kwargs: [row_a, row_b])
    monkeypatch.setattr(
        chess_review,
        "fetch_recent_games",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("retry-failures should not call fetch_recent_games")),
    )
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})

    def _process_stub(_conn, _args, _game):
        calls["process"] += 1
        return tmp_path / "output" / "md" / "ok.md"

    monkeypatch.setattr(chess_review, "process_game", _process_stub)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = True
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 2
    assert calls["process"] == 2


def test_poll_once_uses_pending_helper_in_non_retry_mode(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"process": 0}
    now_epoch = int(time.time())
    row = {
        "game_url": "https://www.chess.com/game/live/9123",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": now_epoch,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }

    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [row])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [{"game_url": row["game_url"], "success_notified": False, "engine_failed": False, "attempt_count": 0, "last_attempt_at": ""}],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "record_game_attempt", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "should_skip_game_due_to_attempt_backoff", lambda **_kwargs: {"available": False, "skip": False})

    def _process_stub(_conn, _args, game):
        calls["process"] += 1
        return tmp_path / "output" / "md" / f"{game.game_url.split('/')[-1]}.md"

    monkeypatch.setattr(chess_review, "process_game", _process_stub)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 1
    assert calls["process"] == 1


def test_poll_once_processes_pending_diagnostics_rows_without_revalidating_eligibility(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"process": 0}
    row = {
        "game_url": "https://www.chess.com/game/live/9551",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": int(time.time()),
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [row])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [row],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "record_game_attempt", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "should_skip_game_due_to_attempt_backoff", lambda **_kwargs: {"available": False, "skip": False})
    monkeypatch.setattr(chess_review, "is_game_eligible_for_processing", lambda *_args, **_kwargs: False)

    def _process_stub(_conn, _args, _game):
        calls["process"] += 1
        return tmp_path / "output" / "md" / "x.md"

    monkeypatch.setattr(chess_review, "process_game", _process_stub)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 1
    assert calls["process"] == 1
    assert "Poll cycle skips eligibility_filter=0" in "\n".join(r.getMessage() for r in caplog.records)


def test_poll_once_processes_eligible_rows_from_diagnostics_even_without_new_inserts(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"process": 0}
    row = {
        "game_url": "https://www.chess.com/game/live/9552",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": int(time.time()),
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [row],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "record_game_attempt", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "should_skip_game_due_to_attempt_backoff", lambda **_kwargs: {"available": False, "skip": False})
    monkeypatch.setattr(chess_review, "is_game_eligible_for_processing", lambda *_args, **_kwargs: True)

    def _process_stub(_conn, _args, _game):
        calls["process"] += 1
        return tmp_path / "output" / "md" / "x.md"

    monkeypatch.setattr(chess_review, "process_game", _process_stub)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 1
    assert calls["process"] == 1


def test_poll_once_processed_row_updates_attempt_and_success_and_writes_md(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    row = {
        "game_url": "https://www.chess.com/game/live/9666",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": int(time.time()),
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }
    calls = {"attempt": 0, "success_flags": 0}
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [row])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [row],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chess_review, "run_analysis_pipeline", lambda **_kwargs: "# review")
    monkeypatch.setattr(
        chess_review,
        "record_game_attempt",
        lambda **_kwargs: calls.__setitem__("attempt", calls["attempt"] + 1)
        or {"available": True, "reason": "attempt_logged", "updated": True},
    )
    monkeypatch.setattr(
        chess_review,
        "mark_review_success_flags",
        lambda **_kwargs: calls.__setitem__("success_flags", calls["success_flags"] + 1)
        or {"available": True, "reason": "success_marked", "updated": True},
    )
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 1
    assert calls["attempt"] == 1
    assert calls["success_flags"] == 1
    assert len(list((tmp_path / "output" / "md").glob("*.md"))) == 1


def test_poll_once_engine_failure_marks_failed_and_next_cycle_skips(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    state = {"engine_failed": False, "process_calls": 0}
    row = {
        "game_url": "https://www.chess.com/game/live/9777",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": int(time.time()),
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }

    def _diag(**_kwargs):
        if state["engine_failed"]:
            return {
                "pending_total": 1,
                "eligible_now": 0,
                "excluded_by_cooldown": 0,
                "excluded_by_attempt_cap": 0,
                "excluded_by_engine_failed": 1,
                "excluded_by_success_notified": 0,
                "top_newest_pending": [],
                "eligible_rows": [],
            }
        return {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [row],
        }

    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing_diagnostics", _diag)
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [] if state["engine_failed"] else [row])
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: state.__setitem__("engine_failed", True) or {"available": True, "updated": True})
    monkeypatch.setattr(
        chess_review,
        "process_game",
        lambda *_args, **_kwargs: state.__setitem__("process_calls", state["process_calls"] + 1)
        or (_ for _ in ()).throw(RuntimeError("engine failure")),
    )
    monkeypatch.setattr(chess_review, "_sleep", lambda *_args, **_kwargs: None)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        first = chess_review.poll_once(conn, args)
        second = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert first == 0
    assert second == 0
    assert state["engine_failed"] is True
    assert state["process_calls"] == 1


def test_poll_once_logs_cycle_queue_metrics(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 8,
            "eligible_now": 2,
            "excluded_by_cooldown": 3,
            "excluded_by_attempt_cap": 1,
            "excluded_by_engine_failed": 1,
            "excluded_by_success_notified": 1,
            "top_newest_pending": [],
            "eligible_rows": [],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Queue cycle pending_total=8 eligible_now=2 skipped_by_success_notified=1 skipped_by_engine_failed=1 skipped_by_attempt_cap=1 skipped_by_cooldown=3" in text


def test_poll_exec_audit_logs_candidates_and_attempt_results(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    row = {
        "game_url": "https://www.chess.com/game/live/8123",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": int(time.time()),
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }
    monkeypatch.setenv("ENABLE_POLL_EXEC_AUDIT", "1")
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [row])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [row],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "process_game", lambda *_args, **_kwargs: tmp_path / "output" / "md" / "ok.md")

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 1
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "[POLL-EXEC] candidates_count=1 candidates=https://www.chess.com/game/live/8123" in text
    assert "[POLL-EXEC] candidate url=https://www.chess.com/game/live/8123 source=pending" in text
    assert "[POLL-EXEC] attempt_result url=https://www.chess.com/game/live/8123 result=success" in text


def test_poll_exec_audit_logs_pending_row_parse_failures(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    bad_row = {
        "game_url": "https://www.chess.com/game/live/bad-parse",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": 0,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }
    monkeypatch.setenv("ENABLE_POLL_EXEC_AUDIT", "1")
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [bad_row])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [bad_row],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "[POLL-EXEC] skip url=https://www.chess.com/game/live/bad-parse reason=pending_row_parse_failed" in text
    assert "Eligible rows detected (eligible_now=1) but no executable candidates were produced this cycle." in text


def test_poll_once_respects_poll_batch_size_for_pending_queries(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    captured: dict[str, int] = {}

    monkeypatch.setenv("POLL_BATCH_SIZE", "3")
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})

    def _diag(**kwargs):
        captured["diag_limit"] = int(kwargs.get("limit", -1))
        return {
            "pending_total": 0,
            "eligible_now": 0,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [],
        }

    def _pending(**kwargs):
        captured["pending_limit"] = int(kwargs.get("limit", -1))
        return []

    monkeypatch.setattr(chess_review, "get_pending_games_for_processing_diagnostics", _diag)
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", _pending)
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.dry_run = False
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    assert captured["diag_limit"] == 3
    assert captured["pending_limit"] == 3


def test_poll_once_processes_newest_eligible_first_and_respects_batch_size(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    processed_urls: list[str] = []
    now_epoch = int(time.time())
    older = {
        "game_url": "https://www.chess.com/game/live/7001",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": now_epoch - 30,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
        "played_at": "2026-02-10T00:00:00+00:00",
        "created_at": "2026-02-10T00:05:00+00:00",
    }
    middle = {
        **older,
        "game_url": "https://www.chess.com/game/live/7002",
        "end_time": now_epoch - 20,
        "played_at": "2026-02-11T00:00:00+00:00",
        "created_at": "2026-02-11T00:05:00+00:00",
    }
    newest = {
        **older,
        "game_url": "https://www.chess.com/game/live/7003",
        "end_time": now_epoch - 10,
        "played_at": "2026-02-12T00:00:00+00:00",
        "created_at": "2026-02-12T00:05:00+00:00",
    }

    monkeypatch.setenv("POLL_BATCH_SIZE", "2")
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 3,
            "eligible_now": 3,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [older, middle, newest],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "record_game_attempt", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "should_skip_game_due_to_attempt_backoff", lambda **_kwargs: {"available": False, "skip": False})

    def _process_stub(_conn, _args, game):
        processed_urls.append(game.game_url)
        return tmp_path / "output" / "md" / f"{game.game_url.split('/')[-1]}.md"

    monkeypatch.setattr(chess_review, "process_game", _process_stub)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 2
    assert processed_urls == [
        "https://www.chess.com/game/live/7003",
        "https://www.chess.com/game/live/7002",
    ]


def test_poll_once_guarantees_progress_when_eligible_exists(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"process": 0}
    row = {
        "game_url": "https://www.chess.com/game/live/7101",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": int(time.time()),
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [row])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [row],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(chess_review, "should_skip_game_due_to_attempt_backoff", lambda **_kwargs: {"available": True, "skip": True, "attempt_count": 99})
    monkeypatch.setattr(chess_review, "is_game_eligible_for_processing", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})

    def _process_stub(_conn, _args, _game):
        calls["process"] += 1
        return tmp_path / "output" / "md" / "force.md"

    monkeypatch.setattr(chess_review, "process_game", _process_stub)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.dry_run = False
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 1
    assert calls["process"] == 1


def test_poll_once_no_eligible_means_no_processing(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 0,
            "eligible_now": 0,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chess_review, "process_game", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not process")))

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.dry_run = False
        args.retries = 1
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0


def test_main_process_on_startup_enabled_runs_one_startup_poll(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"poll_once": 0}

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            return None

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            return None

    def _poll_once(_conn, _args):
        calls["poll_once"] += 1
        return 1

    args = _base_args(tmp_path)
    args.once = False
    args.queue = False
    args.tg_smoketest = False
    args.command = None
    args.state_db = tmp_path / "state.sqlite"
    args.poll_seconds = 300
    args.backfill = 0
    args.no_bootstrap = True
    args.bootstrap_games = None
    args.rebuild_payloads = False

    monkeypatch.setenv("PROCESS_ON_STARTUP", "1")
    monkeypatch.setattr(chess_review, "parse_args", lambda: args)
    monkeypatch.setattr(chess_review.threading, "Thread", _DummyThread)
    monkeypatch.setattr(chess_review, "_sync_runtime_provider", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chess_review, "ensure_postgres_core_schema", lambda: {"ready": False, "reason": "no_database_url", "tables_ready": []})
    monkeypatch.setattr(chess_review, "poll_once", _poll_once)
    monkeypatch.setattr(chess_review, "_sleep", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    code = chess_review.main()

    assert code == 0
    assert calls["poll_once"] == 1


def test_main_once_processes_pending_via_poll_once(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"run_once": 0, "thread_init": 0, "thread_start": 0}

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            calls["thread_init"] += 1
            return None

        def start(self) -> None:
            calls["thread_start"] += 1
            return None

        def join(self, timeout=None) -> None:
            return None

    def _run_once(_conn, _args):
        calls["run_once"] += 1
        return 1

    args = _base_args(tmp_path)
    args.once = True
    args.queue = False
    args.tg_smoketest = False
    args.command = None
    args.state_db = tmp_path / "state.sqlite"
    args.poll_seconds = 300
    args.backfill = 0
    args.no_bootstrap = True
    args.bootstrap_games = None
    args.rebuild_payloads = False

    monkeypatch.setattr(chess_review, "parse_args", lambda: args)
    monkeypatch.setattr(chess_review.threading, "Thread", _DummyThread)
    monkeypatch.setattr(chess_review, "_sync_runtime_provider", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chess_review, "ensure_postgres_core_schema", lambda: {"ready": False, "reason": "no_database_url", "tables_ready": []})
    monkeypatch.setattr(chess_review, "run_once_single_shot", _run_once)

    caplog.set_level(logging.INFO, logger="chess_review")
    code = chess_review.main()

    assert code == 0
    assert calls["run_once"] == 1
    assert calls["thread_init"] == 0
    assert calls["thread_start"] == 0
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Single-shot --once run started" in text
    assert "Single-shot --once run complete: processed=1" in text
    assert "Telegram command loop started" not in text


def test_main_once_logs_no_eligible_work_and_exits(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"run_once": 0, "thread_start": 0}

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            return None

        def start(self) -> None:
            calls["thread_start"] += 1
            return None

        def join(self, timeout=None) -> None:
            return None

    args = _base_args(tmp_path)
    args.once = True
    args.queue = False
    args.tg_smoketest = False
    args.command = None
    args.state_db = tmp_path / "state.sqlite"
    args.poll_seconds = 300
    args.backfill = 0
    args.no_bootstrap = True
    args.bootstrap_games = None
    args.rebuild_payloads = False

    monkeypatch.setattr(chess_review, "parse_args", lambda: args)
    monkeypatch.setattr(chess_review.threading, "Thread", _DummyThread)
    monkeypatch.setattr(chess_review, "_sync_runtime_provider", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chess_review, "ensure_postgres_core_schema", lambda: {"ready": False, "reason": "no_database_url", "tables_ready": []})
    monkeypatch.setattr(
        chess_review,
        "run_once_single_shot",
        lambda *_args, **_kwargs: calls.__setitem__("run_once", calls["run_once"] + 1) or 0,
    )

    caplog.set_level(logging.INFO, logger="chess_review")
    code = chess_review.main()

    assert code == 0
    assert calls["run_once"] == 1
    assert calls["thread_start"] == 0
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Single-shot --once run complete: no eligible work" in text
    assert "Telegram command loop started" not in text


def test_main_reset_game_runs_reset_and_exits_without_polling(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"reset": 0, "thread_start": 0, "poll_once": 0}

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            return None

        def start(self) -> None:
            calls["thread_start"] += 1
            return None

        def join(self, timeout=None) -> None:
            return None

    args = _base_args(tmp_path)
    args.once = False
    args.queue = False
    args.tg_smoketest = False
    args.command = None
    args.reset_game = "https://www.chess.com/game/live/98765"
    args.state_db = tmp_path / "state.sqlite"
    args.backfill = 0

    monkeypatch.setattr(chess_review, "parse_args", lambda: args)
    monkeypatch.setattr(chess_review.threading, "Thread", _DummyThread)
    monkeypatch.setattr(
        chess_review,
        "reset_game_processing_state",
        lambda **_kwargs: calls.__setitem__("reset", calls["reset"] + 1)
        or {"available": True, "reason": "reset", "updated": True, "changed_fields": ["engine_failed", "success_notified"]},
    )
    monkeypatch.setattr(chess_review, "poll_once", lambda *_args, **_kwargs: calls.__setitem__("poll_once", calls["poll_once"] + 1) or 0)

    caplog.set_level(logging.INFO, logger="chess_review")
    code = chess_review.main()

    assert code == 0
    assert calls["reset"] == 1
    assert calls["thread_start"] == 0
    assert calls["poll_once"] == 0
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Game reset applied game_url=https://www.chess.com/game/live/98765" in text
    assert "Telegram command loop started" not in text


def test_main_smoke_runs_checks_and_exits_without_polling(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"smoke": 0, "thread_start": 0, "poll_once": 0}

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            return None

        def start(self) -> None:
            calls["thread_start"] += 1
            return None

        def join(self, timeout=None) -> None:
            return None

    args = _base_args(tmp_path)
    args.once = False
    args.queue = False
    args.smoke = True
    args.smoke_send_telegram = False
    args.tg_smoketest = False
    args.command = None
    args.reset_game = ""
    args.backfill = 0

    monkeypatch.setattr(chess_review, "parse_args", lambda: args)
    monkeypatch.setattr(chess_review.threading, "Thread", _DummyThread)
    monkeypatch.setattr(chess_review, "run_smoke_checks", lambda _args: calls.__setitem__("smoke", calls["smoke"] + 1) or 0)
    monkeypatch.setattr(chess_review, "poll_once", lambda *_args, **_kwargs: calls.__setitem__("poll_once", calls["poll_once"] + 1) or 0)

    code = chess_review.main()

    assert code == 0
    assert calls["smoke"] == 1
    assert calls["thread_start"] == 0
    assert calls["poll_once"] == 0


def test_main_process_on_startup_disabled_runs_no_immediate_poll(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"poll_once": 0}

    class _DummyThread:
        def __init__(self, *args, **kwargs):
            return None

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            return None

    args = _base_args(tmp_path)
    args.once = False
    args.queue = False
    args.tg_smoketest = False
    args.command = None
    args.state_db = tmp_path / "state.sqlite"
    args.poll_seconds = 300
    args.backfill = 0
    args.no_bootstrap = True
    args.bootstrap_games = None
    args.rebuild_payloads = False

    monkeypatch.setenv("PROCESS_ON_STARTUP", "0")
    monkeypatch.setattr(chess_review, "parse_args", lambda: args)
    monkeypatch.setattr(chess_review.threading, "Thread", _DummyThread)
    monkeypatch.setattr(chess_review, "_sync_runtime_provider", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chess_review, "ensure_postgres_core_schema", lambda: {"ready": False, "reason": "no_database_url", "tables_ready": []})
    monkeypatch.setattr(chess_review, "poll_once", lambda *_args, **_kwargs: calls.__setitem__("poll_once", calls["poll_once"] + 1) or 0)
    monkeypatch.setattr(chess_review, "_sleep", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    code = chess_review.main()

    assert code == 0
    assert calls["poll_once"] == 0


def test_run_once_single_shot_forces_batch_size_one_and_restores_env(monkeypatch, tmp_path) -> None:
    ingest_check_module.close_ingest_db_check()
    calls = {"poll_once": 0, "batch_size_seen": None, "pending_only": None}
    monkeypatch.setenv("POLL_BATCH_SIZE", "7")

    def _poll_once(_conn, _args, *, pending_only=False):
        calls["poll_once"] += 1
        calls["batch_size_seen"] = os.environ.get("POLL_BATCH_SIZE")
        calls["pending_only"] = bool(pending_only)
        return 1

    monkeypatch.setattr(chess_review, "poll_once", _poll_once)
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        processed = chess_review.run_once_single_shot(conn, args)
    finally:
        conn.close()

    assert processed == 1
    assert calls["poll_once"] == 1
    assert calls["batch_size_seen"] == "1"
    assert calls["pending_only"] is True
    assert os.environ.get("POLL_BATCH_SIZE") == "7"


def test_poll_once_logs_pending_diagnostics(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.setenv("DEBUG_POLLING", "1")
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 9,
            "eligible_now": 2,
            "excluded_by_cooldown": 3,
            "excluded_by_attempt_cap": 1,
            "excluded_by_engine_failed": 2,
            "excluded_by_success_notified": 1,
            "top_newest_pending": [
                {"game_url": "https://www.chess.com/game/live/1", "success_notified": False, "engine_failed": False, "attempt_count": 1, "last_attempt_at": "2026-02-14T00:00:00Z"}
            ],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "pending diagnostics pending_total=9 eligible_now=2 excluded_by_cooldown=3 excluded_by_attempt_cap=1 excluded_by_engine_failed=2 excluded_by_success_notified=1" in text
    assert "pending newest url=https://www.chess.com/game/live/1" in text


def test_queue_debug_logging_enabled_includes_counts_and_samples(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.setenv("ENABLE_QUEUE_DEBUG", "1")
    monkeypatch.delenv("DEBUG_POLLING", raising=False)
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 12,
            "eligible_now": 4,
            "excluded_by_success_notified": 3,
            "excluded_by_engine_failed": 2,
            "excluded_by_attempt_cap": 2,
            "excluded_by_cooldown": 1,
            "sample_excluded_by_success_notified": ["https://www.chess.com/game/live/s1"],
            "sample_excluded_by_engine_failed": ["https://www.chess.com/game/live/e1"],
            "sample_excluded_by_attempt_cap": ["https://www.chess.com/game/live/a1"],
            "sample_excluded_by_cooldown": ["https://www.chess.com/game/live/c1"],
            "top_newest_pending": [],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "[QUEUE-DEBUG] pending_total=12 eligible_now=4 excluded_by_success_notified=3 excluded_by_engine_failed=2 excluded_by_attempt_cap=2 excluded_by_cooldown=1" in text
    assert "[QUEUE-DEBUG] sample_excluded_by_success_notified=https://www.chess.com/game/live/s1" in text
    assert "[QUEUE-DEBUG] sample_excluded_by_engine_failed=https://www.chess.com/game/live/e1" in text
    assert "[QUEUE-DEBUG] sample_excluded_by_attempt_cap=https://www.chess.com/game/live/a1" in text
    assert "[QUEUE-DEBUG] sample_excluded_by_cooldown=https://www.chess.com/game/live/c1" in text


def test_queue_debug_logging_disabled_suppresses_queue_logs(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.delenv("ENABLE_QUEUE_DEBUG", raising=False)
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 12,
            "eligible_now": 4,
            "excluded_by_success_notified": 3,
            "excluded_by_engine_failed": 2,
            "excluded_by_attempt_cap": 2,
            "excluded_by_cooldown": 1,
            "sample_excluded_by_success_notified": ["https://www.chess.com/game/live/s1"],
            "sample_excluded_by_engine_failed": ["https://www.chess.com/game/live/e1"],
            "sample_excluded_by_attempt_cap": ["https://www.chess.com/game/live/a1"],
            "sample_excluded_by_cooldown": ["https://www.chess.com/game/live/c1"],
            "top_newest_pending": [],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "[QUEUE-DEBUG]" not in text


def test_queue_debug_logs_structured_exclusion_reasons(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    monkeypatch.setenv("ENABLE_QUEUE_DEBUG", "1")
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 5,
            "eligible_now": 0,
            "excluded_by_cooldown": 1,
            "excluded_by_attempt_cap": 1,
            "excluded_by_engine_failed": 1,
            "excluded_by_success_notified": 1,
            "excluded_by_pgn_missing_terminal": 1,
            "sample_excluded_by_success_notified": ["https://www.chess.com/game/live/succ"],
            "sample_excluded_by_engine_failed": ["https://www.chess.com/game/live/eng"],
            "sample_excluded_by_pgn_missing_terminal": ["https://www.chess.com/game/live/pgnterm"],
            "sample_excluded_by_attempt_cap": ["https://www.chess.com/game/live/attcap"],
            "sample_excluded_by_cooldown": ["https://www.chess.com/game/live/cool"],
            "top_newest_pending": [],
            "eligible_rows": [],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "[QUEUE-DEBUG] exclude url=https://www.chess.com/game/live/succ reason=success_notified" in text
    assert "[QUEUE-DEBUG] exclude url=https://www.chess.com/game/live/eng reason=engine_failed" in text
    assert "[QUEUE-DEBUG] exclude url=https://www.chess.com/game/live/pgnterm reason=pgn_missing_terminal" in text
    assert "[QUEUE-DEBUG] exclude url=https://www.chess.com/game/live/attcap reason=attempt_cap" in text
    assert "[QUEUE-DEBUG] exclude url=https://www.chess.com/game/live/cool reason=cooldown" in text


def test_queue_debug_logs_structured_per_game_eligibility_reasons_when_enabled(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    now_epoch = int(time.time())
    row = {
        "url": "https://www.chess.com/game/live/6001",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": now_epoch,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "white": {"username": "logan", "rating": 1200},
        "black": {"username": "opponent", "rating": 1190},
    }
    monkeypatch.setenv("ENABLE_QUEUE_DEBUG", "1")
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 0,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "should_skip_game_due_to_attempt_backoff", lambda **_kwargs: {"available": False, "skip": False})
    monkeypatch.setattr(chess_review, "is_game_eligible_for_processing", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        chess_review,
        "eligibility_rejection_reasons",
        lambda *_args, **_kwargs: {
            "success_notified": False,
            "engine_failed": False,
            "cooldown_active": False,
            "attempt_cap": True,
            "missing_pgn": False,
        },
    )
    monkeypatch.setattr(chess_review, "process_game", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not process")))

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "[QUEUE-DEBUG] game_skip url=https://www.chess.com/game/live/6001" in text
    assert "attempt_cap=True" in text
    assert "missing_pgn=False" in text


def test_queue_debug_structured_skip_logs_suppressed_when_disabled(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    row = {
        "game_url": "https://www.chess.com/game/live/6002",
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": int(time.time()),
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
        "attempt_count": 6,
        "last_attempt_at": datetime.now(timezone.utc),
    }
    monkeypatch.delenv("ENABLE_QUEUE_DEBUG", raising=False)
    monkeypatch.setattr(chess_review, "cleanup_completed_games", lambda **_kwargs: {"available": True, "reason": "ok", "marked_count": 0})
    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [row])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 1,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
            "eligible_rows": [row],
        },
    )
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "should_skip_game_due_to_attempt_backoff", lambda **_kwargs: {"available": False, "skip": False})
    monkeypatch.setattr(chess_review, "process_game", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not process")))

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "game_skip url=https://www.chess.com/game/live/6002" not in text


def test_poll_once_repairs_missing_pgn_then_processes_game(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    now_epoch = int(time.time())
    target_url = "https://www.chess.com/game/live/7777"
    raw_row = {
        "url": target_url,
        "pgn": '[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        "end_time": now_epoch,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "white": {"username": "logan", "rating": 1200},
        "black": {"username": "opponent", "rating": 1190},
    }
    pending_row = {
        "game_url": target_url,
        "pgn": raw_row["pgn"],
        "end_time": now_epoch,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "result": "1-0",
        "white_username": "logan",
        "black_username": "opponent",
        "white_rating": 1200,
        "black_rating": 1190,
        "player_color": "white",
    }
    calls = {"pending": 0, "process": 0, "updated": 0}

    def _pending_rows(**_kwargs):
        calls["pending"] += 1
        if calls["pending"] == 1:
            return []
        return [pending_row]

    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", _pending_rows)
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 0,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
        },
    )
    monkeypatch.setattr(chess_review, "load_games_missing_pgn", lambda **_kwargs: [{"game_url": target_url}])
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [raw_row])
    monkeypatch.setattr(
        chess_review,
        "update_game_pgn_for_url",
        lambda **_kwargs: calls.__setitem__("updated", calls["updated"] + 1) or True,
    )
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "clear_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "mark_engine_failed", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "record_game_attempt", lambda **_kwargs: {"available": False})
    monkeypatch.setattr(chess_review, "should_skip_game_due_to_attempt_backoff", lambda **_kwargs: {"available": False, "skip": False})

    def _process_stub(_conn, _args, _game):
        calls["process"] += 1
        return tmp_path / "output" / "md" / "repaired.md"

    monkeypatch.setattr(chess_review, "process_game", _process_stub)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 1
    assert calls["updated"] == 1
    assert calls["process"] == 1
    assert "pgn_fetch_missing_retry game_url=https://www.chess.com/game/live/7777 status=success" in "\n".join(
        rec.getMessage() for rec in caplog.records
    )


def test_poll_once_missing_pgn_not_found_records_attempt_and_marks_permanent_missing(monkeypatch, tmp_path, caplog) -> None:
    ingest_check_module.close_ingest_db_check()
    target_url = "https://www.chess.com/game/live/1470001"
    calls = {"record_missing": 0}

    monkeypatch.setattr(chess_review, "get_pending_games_for_processing", lambda **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 1,
            "eligible_now": 0,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "top_newest_pending": [],
        },
    )
    monkeypatch.setattr(chess_review, "load_games_missing_pgn", lambda **_kwargs: [{"game_url": target_url}])
    monkeypatch.setattr(chess_review, "fetch_recent_games", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        chess_review,
        "record_pgn_missing_not_found",
        lambda **_kwargs: calls.__setitem__("record_missing", calls["record_missing"] + 1)
        or {"available": True, "updated": True, "attempts": 3, "pgn_missing_terminal": True},
    )
    monkeypatch.setattr(chess_review, "process_game", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not process")))

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _base_args(tmp_path)
        args.retry_failures = False
        args.lookback_days = 10
        args.rules_filter = "chess"
        args.dry_run = False
        args.retries = 1
        caplog.set_level(logging.INFO, logger="chess_review")
        created = chess_review.poll_once(conn, args)
    finally:
        conn.close()

    assert created == 0
    assert calls["record_missing"] == 1
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "pgn_fetch_missing_retry game_url=https://www.chess.com/game/live/1470001 status=not_found attempts=3 pgn_missing_terminal=True" in text
