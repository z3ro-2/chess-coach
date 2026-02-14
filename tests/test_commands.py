from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import chess_review
from src.commands import run_command
import json


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
    assert "Engine: OK" in text
    assert "LLM Provider: ollama" in text
    assert "Postgres: not connected" in text
    assert "SQLite: connected" in text
    assert "Games since last summary:" in text
    assert "Pending games count:" in text


def test_status_includes_llm_info(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("LLM_TEMPERATURE", "0.05")
    monkeypatch.setenv("LLM_TOP_P", "0.7")

    out_dir = tmp_path / "output"
    md_dir = out_dir / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    (md_dir / "latest.md").write_text(
        "## Review\nok\n\n## LLM Diagnostics\n"
        '{"prompt_hash":"abc123","output_hash":"def456","retry_attempted":false}\n',
        encoding="utf-8",
    )

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _args(tmp_path)
        args.out = out_dir
        result = run_command("status", conn, args)
    finally:
        conn.close()

    text = str(result["text"])
    assert "Engine: OK" in text
    assert "LLM Provider: ollama" in text
    assert "Model: llama3.1:8b" in text
    assert "Prompt hash: abc123" in text
    assert "Fallback rate: " in text
    assert "Last review time: none" in text
    assert "Postgres: not connected" in text
    assert "SQLite: connected" in text


def test_status_includes_pending_games(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _args(tmp_path)
        args.out.mkdir(parents=True, exist_ok=True)
        conn.execute(
            """
            INSERT INTO engine_payloads (game_url, end_time, created_at, engine_depth, payload_json)
            VALUES (?, ?, strftime('%s','now'), ?, ?)
            """,
            ("https://www.chess.com/game/live/1000", 1_706_000_111, 15, '{"schema_version":2}'),
        )
        conn.commit()
        result = run_command("status", conn, args)
    finally:
        conn.close()

    text = str(result["text"])
    assert "Pending games count: 1" in text


def test_status_includes_queue_health_metrics(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "src.commands.get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "pending_total": 12,
            "eligible_now": 4,
            "excluded_by_cooldown": 3,
            "excluded_by_attempt_cap": 2,
            "excluded_by_engine_failed": 1,
            "excluded_by_success_notified": 2,
        },
    )
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        result = run_command("status", conn, _args(tmp_path))
    finally:
        conn.close()

    text = str(result["text"])
    assert "Queue Health:" in text
    assert "- pending_total: 12" in text
    assert "- eligible_now: 4" in text
    assert "- excluded_by_cooldown: 3" in text
    assert "- excluded_by_attempt_cap: 2" in text
    assert "- excluded_by_engine_failed: 1" in text
    assert "- excluded_by_success_notified: 2" in text


def test_status_queue_health_defaults_when_diagnostics_unavailable(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "src.commands.get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        result = run_command("status", conn, _args(tmp_path))
    finally:
        conn.close()

    text = str(result["text"])
    assert "Queue Health:" in text
    assert "- pending_total: 0" in text
    assert "- eligible_now: 0" in text
    assert "- excluded_by_cooldown: 0" in text
    assert "- excluded_by_attempt_cap: 0" in text
    assert "- excluded_by_engine_failed: 0" in text
    assert "- excluded_by_success_notified: 0" in text


def test_status_includes_last_review_time(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _args(tmp_path)
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
        result = run_command("status", conn, args)
    finally:
        conn.close()

    text = str(result["text"])
    assert "Last review time: 2024-01-23 08:55:00 UTC" in text


def test_tg_smoketest_dry_run_prints_actions_in_order(tmp_path, capsys) -> None:
    md_path = tmp_path / "review.md"
    md_path.write_text("# review", encoding="utf-8")
    args = SimpleNamespace(
        tg_smoketest=True,
        tg_smoketest_game_url="https://www.chess.com/game/live/123456",
        tg_smoketest_md=md_path,
        tg_smoketest_caption="smoke caption",
        dry_run=True,
        telegram_chat_id="42",
        telegram_disable_notification=False,
        telegram_bot_token="",
        timeout=5,
    )

    rc = chess_review.run_telegram_smoketest(args)
    assert rc == 0

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0].startswith("TG-SMOKETEST action=send_text payload=")
    assert lines[1].startswith("TG-SMOKETEST action=send_document payload=")

    first_payload = json.loads(lines[0].split("payload=", 1)[1])
    second_payload = json.loads(lines[1].split("payload=", 1)[1])

    assert first_payload["text"] == "https://www.chess.com/game/live/123456"
    assert first_payload["disable_web_page_preview"] is False
    assert "parse_mode" not in first_payload
    assert second_payload["file_path"] == str(md_path)


def test_parse_args_allows_tg_smoketest_without_username(monkeypatch, tmp_path) -> None:
    md_path = tmp_path / "review.md"
    md_path.write_text("# review", encoding="utf-8")
    monkeypatch.delenv("CHESS_USERNAME", raising=False)
    monkeypatch.delenv("CHESS_OUTPUT_DIR", raising=False)

    args = chess_review.parse_args(
        [
            "--tg-smoketest",
            "--game-url",
            "https://www.chess.com/game/live/1",
            "--md",
            str(md_path),
            "--dry-run",
        ]
    )

    assert args.tg_smoketest is True
    assert str(args.tg_smoketest_game_url) == "https://www.chess.com/game/live/1"
    assert Path(args.tg_smoketest_md) == md_path


def test_parse_args_allows_queue_without_username(monkeypatch) -> None:
    monkeypatch.delenv("CHESS_USERNAME", raising=False)
    monkeypatch.delenv("CHESS_OUTPUT_DIR", raising=False)
    args = chess_review.parse_args(["--queue"])
    assert args.queue is True


def test_queue_inspector_outputs_counts_and_top_rows(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "total_games_in_db": 20,
            "total_pending_success_notified_false": 7,
            "eligible_now": 3,
            "excluded_by_cooldown": 2,
            "excluded_by_attempt_cap": 1,
            "excluded_by_engine_failed": 1,
            "top_newest_pending": [
                {
                    "game_url": "https://www.chess.com/game/live/1",
                    "success_notified": False,
                    "engine_failed": False,
                    "attempt_count": 2,
                    "last_attempt_at": "2026-02-14T01:00:00Z",
                }
            ],
        },
    )

    rc = chess_review.run_queue_inspector(SimpleNamespace(queue=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Queue Inspector:" in out
    assert "- Total games in DB: 20" in out
    assert "- Total pending (success_notified = FALSE): 7" in out
    assert "- Total eligible: 3" in out
    assert "- Blocked by cooldown: 2" in out
    assert "- Blocked by attempt cap: 1" in out
    assert "- Blocked by engine_failed: 1" in out
    assert "game_url=https://www.chess.com/game/live/1" in out


def test_queue_inspector_outputs_none_when_no_rows(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        chess_review,
        "get_pending_games_for_processing_diagnostics",
        lambda **_kwargs: {
            "total_games_in_db": 0,
            "total_pending_success_notified_false": 0,
            "eligible_now": 0,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "top_newest_pending": [],
        },
    )

    rc = chess_review.run_queue_inspector(SimpleNamespace(queue=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "- Top 10 pending rows:" in out
    assert "  <none>" in out


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
            return json.dumps(
                {
                    "overall_profile": "Stable competitive profile.",
                    "strengths": ["Keeps pieces active.", "Maintains structure."],
                    "weaknesses": ["Conversion can be sharper."],
                    "improvement_priorities": [
                        "Drill conversion patterns.",
                        "Review tactical misses.",
                        "Play structured endgame practice.",
                    ],
                    "style_assessment": "Practical and active.",
                    "confidence": "MEDIUM",
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        return "# review"

    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)

    def _fake_trait_window_metrics(_conn, _args, *, window_size: int):
        assert window_size == 7
        return {
            "scores": trait_scores,
            "trait_window_games": 7,
            "trait_window_moves": 350,
            "confidence": "MEDIUM",
        }

    monkeypatch.setattr(chess_review, "_compute_trait_scores_and_window_metrics", _fake_trait_window_metrics)

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
        assert "Trait scores (v2 window 7/7 games):" in str(result["text"])
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


def test_summary_command_reports_trait_integrity_warning(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        chess_review,
        "call_ollama_generate",
        lambda **_kwargs: json.dumps(
            {
                "overall_profile": "Cautionary profile due to integrity warning.",
                "strengths": ["Keeps fighting in difficult positions."],
                "weaknesses": ["Data quality concerns limit reliability."],
                "improvement_priorities": ["Re-run clean trait window.", "Validate payload integrity.", "Resume training after clean window."],
                "style_assessment": "Uncertain due to integrity warning.",
                "confidence": "LOW",
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    )

    def _fake_trait_window_metrics(_conn, _args, *, window_size: int):
        assert window_size == 5
        return {
            "scores": {
                "tactical_awareness": 50,
                "material_discipline": 50,
                "conversion_ability": 50,
                "defensive_resilience": 50,
                "blunder_frequency": 50,
            },
            "trait_window_games": 5,
            "trait_window_moves": 180,
            "confidence": "LOW",
            "confidence_reason": "trait window integrity warning (non_good_rate_gt_0_75)",
            "integrity_warning": True,
            "integrity_warning_reasons": ["non_good_rate_gt_0_75"],
            "trait_diagnostics": {
                "window_integrity": {
                    "warning": True,
                    "reasons": ["non_good_rate_gt_0_75"],
                    "trait_update_refused": False,
                }
            },
        }

    monkeypatch.setattr(chess_review, "_compute_trait_scores_and_window_metrics", _fake_trait_window_metrics)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = _args(tmp_path)
        args.player_trait_window = 5
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
    finally:
        conn.close()

    text = str(result["text"])
    assert "Trait integrity warning: non_good_rate_gt_0_75" in text


def test_llm_config_command_uses_env_values(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.25")
    monkeypatch.setenv("LLM_TOP_P", "0.85")
    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        result = run_command("llm-config", conn, _args(tmp_path))
    finally:
        conn.close()
    parsed = json.loads(str(result["text"]))
    assert parsed == {
        "LLM_MAX_TOKENS": 2048,
        "LLM_TEMPERATURE": 0.25,
        "LLM_TOP_P": 0.85,
        "OLLAMA_MODEL": "llama3.2:3b",
        "OLLAMA_URL": "http://localhost:11434",
    }


def test_llm_config_command_shows_defaults_when_env_missing(monkeypatch, tmp_path) -> None:
    for key in ("OLLAMA_URL", "OLLAMA_MODEL", "LLM_TEMPERATURE", "LLM_TOP_P", "LLM_MAX_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        result = run_command("llm-config", conn, _args(tmp_path))
    finally:
        conn.close()
    parsed = json.loads(str(result["text"]))
    assert parsed == {
        "LLM_MAX_TOKENS": 600,
        "LLM_TEMPERATURE": 0.05,
        "LLM_TOP_P": 0.7,
        "OLLAMA_MODEL": "llama3.1:8b",
        "OLLAMA_URL": "http://127.0.0.1:11434",
    }
    assert isinstance(parsed["OLLAMA_URL"], str) and parsed["OLLAMA_URL"].startswith("http")
    assert isinstance(parsed["OLLAMA_MODEL"], str) and bool(parsed["OLLAMA_MODEL"].strip())
    assert isinstance(parsed["LLM_TEMPERATURE"], float)
    assert isinstance(parsed["LLM_TOP_P"], float)
    assert isinstance(parsed["LLM_MAX_TOKENS"], int)
