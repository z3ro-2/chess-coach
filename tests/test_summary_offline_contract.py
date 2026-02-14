from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import analysis_pipeline as pipeline_module
import chess_review
from src.config.provider_config import set_provider
from src.engine_traits import compute_engine_trait_scores
from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION


def _section_lines(markdown: str, heading: str) -> list[str]:
    lines = markdown.splitlines()
    collecting = False
    collected: list[str] = []
    for line in lines:
        if line.strip() == heading:
            collecting = True
            continue
        if collecting and line.startswith("## "):
            break
        if collecting:
            collected.append(line)
    return collected


def _assert_strict_summary_format(markdown: str) -> None:
    lines = markdown.splitlines()

    # YAML front matter with strict fields and ordering.
    assert lines[0].strip() == "---"
    assert lines[1].startswith("date_utc:")
    assert lines[2].startswith("your_color:")
    assert lines[3].startswith("opponent:")
    assert lines[4].startswith("result:")
    assert lines[5].startswith("win_pct:")
    assert lines[6].startswith("loss_pct:")
    assert lines[7].startswith("draw_pct:")
    assert lines[8].startswith("trait_window_games:")
    assert lines[9].startswith("trait_window_moves:")
    assert lines[10].startswith("confidence:")
    assert lines[11].startswith("trait_diagnostics:")
    assert lines[12].strip() == "---"

    headings = [line.strip() for line in lines if line.startswith("## ")]
    assert headings == [
        "## Snapshot",
        "## Engine-Derived Traits",
        "## Primary Weaknesses",
        "## Training Priority",
    ]

    training_lines = [line for line in _section_lines(markdown, "## Training Priority") if line.startswith("- ")]
    assert len(training_lines) == 3


def _payload_v2(
    *,
    your_color: str,
    result: str,
    total_moves: int,
    player_label_counts: dict[str, int],
    key_positions: list[dict],
) -> dict:
    total_plies = int(total_moves) * 2
    player_total_plies = int(total_moves)
    if sum(int(v) for v in player_label_counts.values()) != player_total_plies:
        raise AssertionError("player_label_counts must sum to total_moves for fixture payloads")
    opponent_counts = {
        "good": player_total_plies,
        "inaccuracy": 0,
        "mistake": 0,
        "blunder": 0,
        "brilliant": 0,
    }
    by_side = (
        {"white": dict(player_label_counts), "black": opponent_counts}
        if your_color == "white"
        else {"white": opponent_counts, "black": dict(player_label_counts)}
    )
    merged = {k: int(by_side["white"][k]) + int(by_side["black"][k]) for k in player_label_counts}
    return {
        "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
        "game_summary": {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "your_color": your_color,
            "result": result,
            "total_moves": int(total_moves),
            "total_plies": int(total_plies),
            "white_plies": int(total_moves),
            "black_plies": int(total_moves),
            "unlabeled_white_plies": 0,
            "unlabeled_black_plies": 0,
            "label_counts_total": dict(merged),
            "label_counts_white": dict(by_side["white"]),
            "label_counts_black": dict(by_side["black"]),
            "player_total_plies": int(player_total_plies),
            "player_total_moves": int(player_total_plies),
            "player_label_counts": dict(player_label_counts),
            "label_counts_by_side": by_side,
            "label_counts": merged,
        },
        "key_positions": key_positions,
    }


@pytest.fixture
def known_recent_meta() -> list[tuple[int, str, str, int | None, str]]:
    # 2 wins, 1 loss, 1 draw for white => 50.0 / 25.0 / 25.0
    return [
        (1_706_000_100, "1-0", "white", 1200, "https://www.chess.com/game/live/1"),
        (1_706_000_200, "0-1", "white", 1201, "https://www.chess.com/game/live/2"),
        (1_706_000_300, "1-0", "white", 1202, "https://www.chess.com/game/live/3"),
        (1_706_000_400, "1/2-1/2", "white", 1203, "https://www.chess.com/game/live/4"),
    ]


@pytest.fixture
def predictable_key_payloads() -> list[dict]:
    # Deterministic expected scores with label_counts + total_plies primary signals.
    return [
        _payload_v2(
            your_color="white",
            result="1-0",
            total_moves=20,
            player_label_counts={"good": 14, "inaccuracy": 3, "mistake": 2, "blunder": 1, "brilliant": 0},
            key_positions=[
                {"player": "White", "move_number": 4, "label": "good", "tactical_flag": "none", "material_change": 3},
                {"player": "White", "move_number": 5, "label": "blunder", "tactical_flag": "hanging_piece", "material_change": -2},
                {"player": "White", "move_number": 6, "label": "mistake", "tactical_flag": "tactical_miss", "material_change": -1},
            ],
        ),
        _payload_v2(
            your_color="white",
            result="1/2-1/2",
            total_moves=20,
            player_label_counts={"good": 17, "inaccuracy": 2, "mistake": 1, "blunder": 0, "brilliant": 0},
            key_positions=[
                {"player": "White", "move_number": 4, "label": "good", "tactical_flag": "none", "material_change": 4},
                {"player": "White", "move_number": 10, "label": "good", "tactical_flag": "none", "material_change": -4},
            ],
        ),
        _payload_v2(
            your_color="white",
            result="0-1",
            total_moves=10,
            player_label_counts={"good": 6, "inaccuracy": 1, "mistake": 2, "blunder": 1, "brilliant": 0},
            key_positions=[
                {"player": "White", "move_number": 3, "label": "blunder", "tactical_flag": "tactical_miss", "material_change": -5},
            ],
        ),
    ]


@pytest.fixture
def summary_args(tmp_path: Path) -> SimpleNamespace:
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
        telegram_bot_token="token",
        telegram_chat_id="42",
        telegram_disable_notification=False,
        username="logan",
        player_summary_every_n=20,
        player_trait_window=20,
    )


@pytest.fixture
def sample_game() -> chess_review.GameInfo:
    return chess_review.GameInfo(
        game_url="https://www.chess.com/game/live/123",
        pgn='[Event "Live Chess"]\n[White "logan"]\n[Black "opponent"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
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


@pytest.fixture
def strict_summary_json_output() -> str:
    return """{"overall_profile":"Disciplined but conversion-limited profile.","strengths":["Tactical awareness remains competitive.","Blunder frequency trend is stable."],"weaknesses":["Conversion Ability: Lowest deterministic engine-derived score in the rolling window (50/100)."],"improvement_priorities":["Review each blunder and identify the missed tactical cue.","Run daily tactical sets focused on hanging pieces and tactical misses.","Add a conversion checklist when up material by +3 or more."],"style_assessment":"Practical style with strong tactical intent but uneven technique.","confidence":"MEDIUM"}"""


def test_player_summary_math_correctness_with_fixture(known_recent_meta) -> None:
    summary = chess_review._build_performance_summary(known_recent_meta)
    assert summary == {
        "total_games": 4,
        "wins": 2,
        "losses": 1,
        "draws": 1,
        "win_pct": 50.0,
        "loss_pct": 25.0,
        "draw_pct": 25.0,
    }


def test_trait_scoring_logic_correctness_with_fixture(predictable_key_payloads) -> None:
    scores = compute_engine_trait_scores(predictable_key_payloads)
    assert scores == {
        "tactical_awareness": 60,
        "material_discipline": 74,
        "conversion_ability": 90,
        "defensive_resilience": 83,
        "blunder_frequency": 89,
    }


def test_rolling_window_summary_results_use_expected_window(monkeypatch, tmp_path, predictable_key_payloads) -> None:
    conn: sqlite3.Connection = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        seen: dict[str, int] = {}

        def _fake_loader(_conn, _args, *, window_size: int):
            seen["window_size"] = window_size
            # emulate rolling-window slicing
            return predictable_key_payloads[:window_size]

        monkeypatch.setattr(chess_review, "_load_engine_payloads_for_trait_window", _fake_loader)
        scores = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=2)
    finally:
        conn.close()

    assert seen["window_size"] == 2
    assert scores == {
        "tactical_awareness": 72,
        "material_discipline": 80,
        "conversion_ability": 80,
        "defensive_resilience": 80,
        "blunder_frequency": 80,
    }


def test_llm_output_format_conforms_to_strict_template(
    monkeypatch,
    tmp_path,
    known_recent_meta,
    predictable_key_payloads,
    strict_summary_json_output,
) -> None:
    set_provider("ollama")
    captured: dict[str, str] = {}

    def _fake_ollama_generate(**kwargs):
        captured["system_msg"] = str(kwargs.get("system_msg", ""))
        captured["user_msg"] = str(kwargs.get("user_msg", ""))
        return strict_summary_json_output

    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)
    trait_scores = compute_engine_trait_scores(predictable_key_payloads)
    summary_context = {
        "date_utc": "2026-02-12",
        "your_color": "white",
        "opponent": "opponent",
        "result": "1-0",
    }
    args = SimpleNamespace(
        username="logan",
        gpt_model="gpt-4o-mini",
        ollama_model="llama3.1:8b",
        ollama_url="http://127.0.0.1:11434",
        timeout=5,
        max_tokens=200,
    )
    stats_path = tmp_path / "player_stats.md"
    stats_path.write_text("# stats", encoding="utf-8")

    out = chess_review._generate_player_summary_markdown(
        args,
        processed_count=4,
        cadence=4,
        stats_path=stats_path,
        recent_meta=known_recent_meta,
        trait_scores=trait_scores,
        trait_window_size=20,
        trait_window_moves=420,
        trait_confidence="MEDIUM",
        summary_context=summary_context,
    )

    assert out.startswith("---\n")
    assert "Do not compute, infer, or recompute any metric." in captured["user_msg"]
    assert "Do not do arithmetic, percentages, ranking, or score derivation." in captured["user_msg"]
    assert "trait_window_games" in captured["user_msg"]
    assert "trait_window_moves" in captured["user_msg"]
    assert "Return raw JSON only." in captured["user_msg"]
    assert "overall_profile" in captured["user_msg"]
    assert "trait_diagnostics" in captured["user_msg"]
    _assert_strict_summary_format(out)


def test_engine_failure_aborts_pipeline_and_sends_telegram(
    monkeypatch,
    tmp_path,
    summary_args,
    sample_game,
) -> None:
    telegram_calls: list[str] = []
    llm_called = {"value": False}

    monkeypatch.setattr(
        chess_review,
        "run_analysis_pipeline",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("Stockfish engine failed or produced no output.")),
    )
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

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        out = chess_review.process_game(conn, summary_args, sample_game)
        processed_count = int(conn.execute("SELECT COUNT(*) FROM processed_games").fetchone()[0])
    finally:
        conn.close()

    assert out is None
    assert processed_count == 0
    assert llm_called["value"] is False
    assert len(telegram_calls) == 1
    assert "Engine failure" in telegram_calls[0]


def test_success_path_emits_structured_json_log(monkeypatch, tmp_path, summary_args, sample_game, caplog) -> None:
    monkeypatch.setattr(
        chess_review,
        "run_analysis_pipeline",
        lambda **_kwargs: (
            "# Review\n\n## LLM Diagnostics\n"
            '{"model_name":"llama","prompt_hash":"p1","output_hash":"o1","format_violation":false,"retry_attempted":false}\n'
        ),
    )
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "consume_success_notification_once", lambda **_kwargs: {"available": True, "reason": "notify_pending", "should_notify": True})
    monkeypatch.setattr(chess_review, "mark_review_success_flags", lambda **_kwargs: {"available": True, "reason": "marked_notified", "updated": True})
    monkeypatch.setattr(chess_review, "send_telegram_message", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chess_review, "send_telegram_document", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chess_review, "sync_game_record_and_traits", lambda **_kwargs: {"available": False, "reason": "no_database_url"})
    monkeypatch.setattr(chess_review, "record_player_rating_for_game", lambda **_kwargs: False)
    monkeypatch.setattr(chess_review, "_write_player_stats_markdown", lambda *_args, **_kwargs: tmp_path / "output" / "player_stats.md")
    monkeypatch.setattr(chess_review, "_maybe_generate_player_summary", lambda *_args, **_kwargs: None)

    caplog.set_level("INFO")
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        out = chess_review.process_game(conn, summary_args, sample_game)
    finally:
        conn.close()

    assert out is not None
    json_logs = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if msg.startswith("{") and msg.endswith("}"):
            try:
                payload = json.loads(msg)
            except Exception:
                continue
            if isinstance(payload, dict) and payload.get("engine_success") is True:
                json_logs.append(payload)
    assert json_logs, "expected structured success JSON log"
    payload = json_logs[-1]
    assert payload["engine_success"] is True
    assert payload["llm_used"] is True
    assert payload["format_violation"] is False
    assert payload["retry_attempted"] is False
    assert payload["fallback_used"] is False
    assert payload["telegram_notified"] is True
    assert isinstance(payload["game_id"], str) and payload["game_id"]


def test_review_notified_gating_first_success_sends_second_skips(
    monkeypatch,
    tmp_path,
    summary_args,
    sample_game,
) -> None:
    telegram_text_calls: list[dict] = []
    telegram_doc_calls: list[dict] = []
    mark_calls: list[dict] = []
    notify_states = iter(
        [
            {"available": True, "reason": "notify_pending", "should_notify": True},
            {"available": True, "reason": "already_notified", "should_notify": False},
        ]
    )

    monkeypatch.setattr(chess_review, "run_analysis_pipeline", lambda **_kwargs: "# game review")
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "mark_processed", lambda **_kwargs: None)
    monkeypatch.setattr(chess_review, "_record_processed_game_meta", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chess_review, "sync_game_record_and_traits", lambda **_kwargs: {"available": False, "reason": "no_database_url"})
    monkeypatch.setattr(chess_review, "record_player_rating_for_game", lambda **_kwargs: False)
    monkeypatch.setattr(chess_review, "consume_success_notification_once", lambda **_kwargs: next(notify_states))
    monkeypatch.setattr(
        chess_review,
        "mark_review_success_flags",
        lambda **kwargs: mark_calls.append(dict(kwargs)) or {"available": True, "reason": "marked_notified", "updated": True},
    )
    monkeypatch.setattr(
        chess_review,
        "send_telegram_message",
        lambda message, **kwargs: telegram_text_calls.append({"message": str(message), "kwargs": dict(kwargs)}),
    )
    monkeypatch.setattr(
        chess_review,
        "send_telegram_document",
        lambda **kwargs: telegram_doc_calls.append(dict(kwargs)),
    )

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        first = chess_review.process_game(conn, summary_args, sample_game)
        second = chess_review.process_game(conn, summary_args, sample_game)
    finally:
        conn.close()

    assert first is not None
    assert second is not None
    assert len(telegram_text_calls) == 1
    assert telegram_text_calls[0]["message"] == sample_game.game_url
    assert telegram_text_calls[0]["kwargs"]["disable_web_page_preview"] is False
    assert len(telegram_doc_calls) == 1
    assert str(telegram_doc_calls[0]["file_path"]).endswith(".md")
    assert "Chess review generated" in str(telegram_doc_calls[0]["caption"])
    assert len(mark_calls) == 1


def test_success_notification_sends_document_with_md_path(
    monkeypatch,
    tmp_path,
    summary_args,
    sample_game,
) -> None:
    text_calls: list[dict] = []
    doc_calls: list[dict] = []

    monkeypatch.setattr(chess_review, "run_analysis_pipeline", lambda **_kwargs: "# game review")
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "mark_processed", lambda **_kwargs: None)
    monkeypatch.setattr(chess_review, "_record_processed_game_meta", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chess_review, "sync_game_record_and_traits", lambda **_kwargs: {"available": False, "reason": "no_database_url"})
    monkeypatch.setattr(chess_review, "record_player_rating_for_game", lambda **_kwargs: False)
    monkeypatch.setattr(chess_review, "consume_success_notification_once", lambda **_kwargs: {"available": True, "reason": "notify_pending", "should_notify": True})
    monkeypatch.setattr(chess_review, "mark_review_success_flags", lambda **_kwargs: {"available": True, "reason": "marked_notified", "updated": True})
    monkeypatch.setattr(
        chess_review,
        "send_telegram_message",
        lambda message, **kwargs: text_calls.append({"message": str(message), "kwargs": dict(kwargs)}),
    )
    monkeypatch.setattr(chess_review, "send_telegram_document", lambda **kwargs: doc_calls.append(dict(kwargs)))
    monkeypatch.setattr(chess_review, "_write_player_stats_markdown", lambda *_args, **_kwargs: tmp_path / "output" / "player_stats.md")
    monkeypatch.setattr(chess_review, "_maybe_generate_player_summary", lambda *_args, **_kwargs: None)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        out = chess_review.process_game(conn, summary_args, sample_game)
    finally:
        conn.close()

    assert out is not None
    assert len(text_calls) == 1
    assert text_calls[0]["message"] == sample_game.game_url
    assert text_calls[0]["kwargs"]["disable_web_page_preview"] is False
    assert len(doc_calls) == 1
    assert doc_calls[0]["file_path"] == out


def test_process_game_records_attempt_before_processing_and_marks_success_flags(
    monkeypatch,
    tmp_path,
    summary_args,
    sample_game,
) -> None:
    attempt_calls: list[dict] = []
    success_flag_calls: list[dict] = []

    monkeypatch.setattr(chess_review, "run_analysis_pipeline", lambda **_kwargs: "# game review")
    monkeypatch.setattr(chess_review, "is_processed", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(chess_review, "mark_processed", lambda **_kwargs: None)
    monkeypatch.setattr(chess_review, "_record_processed_game_meta", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chess_review, "sync_game_record_and_traits", lambda **_kwargs: {"available": False, "reason": "no_database_url"})
    monkeypatch.setattr(chess_review, "record_player_rating_for_game", lambda **_kwargs: False)
    monkeypatch.setattr(chess_review, "_write_player_stats_markdown", lambda *_args, **_kwargs: tmp_path / "output" / "player_stats.md")
    monkeypatch.setattr(chess_review, "_maybe_generate_player_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chess_review, "consume_success_notification_once", lambda **_kwargs: {"available": True, "reason": "already_consumed", "should_notify": False})
    monkeypatch.setattr(
        chess_review,
        "record_game_attempt",
        lambda **kwargs: attempt_calls.append(dict(kwargs)) or {"available": True, "reason": "updated", "updated": True},
    )
    monkeypatch.setattr(
        chess_review,
        "mark_review_success_flags",
        lambda **kwargs: success_flag_calls.append(dict(kwargs)) or {"available": True, "reason": "marked_success", "updated": True},
    )

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        out = chess_review.process_game(conn, summary_args, sample_game)
    finally:
        conn.close()

    assert out is not None
    assert len(attempt_calls) == 1
    assert attempt_calls[0]["last_error"] is None
    assert len(success_flag_calls) == 1


def test_process_game_engine_failure_marks_engine_failed_without_success_flags(
    monkeypatch,
    tmp_path,
    summary_args,
    sample_game,
) -> None:
    attempt_calls: list[dict] = []
    engine_failed_calls: list[dict] = []
    success_flag_calls: list[dict] = []

    monkeypatch.setattr(
        chess_review,
        "run_analysis_pipeline",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("Stockfish engine failed or produced no output.")),
    )
    monkeypatch.setattr(chess_review, "should_notify_engine_failure", lambda **_kwargs: {"available": True, "reason": "already_notified", "should_notify": False})
    monkeypatch.setattr(
        chess_review,
        "record_game_attempt",
        lambda **kwargs: attempt_calls.append(dict(kwargs)) or {"available": True, "reason": "updated", "updated": True},
    )
    monkeypatch.setattr(
        chess_review,
        "mark_engine_failed",
        lambda **kwargs: engine_failed_calls.append(dict(kwargs)) or {"available": True, "reason": "marked_engine_failed", "updated": True},
    )
    monkeypatch.setattr(
        chess_review,
        "mark_review_success_flags",
        lambda **kwargs: success_flag_calls.append(dict(kwargs)) or {"available": True, "reason": "marked_success", "updated": True},
    )

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        out = chess_review.process_game(conn, summary_args, sample_game)
    finally:
        conn.close()

    assert out is None
    assert len(attempt_calls) == 1
    assert len(engine_failed_calls) == 1
    assert success_flag_calls == []


def test_failure_notified_flag_set_after_success(
    monkeypatch,
    tmp_path,
    summary_args,
    sample_game,
) -> None:
    telegram_calls: list[str] = []
    marks: list[dict] = []

    monkeypatch.setattr(
        chess_review,
        "run_analysis_pipeline",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("Stockfish engine failed or produced no output.")),
    )
    monkeypatch.setattr(
        chess_review,
        "should_notify_engine_failure",
        lambda **_kwargs: {"available": True, "reason": "notify_pending", "should_notify": True},
    )
    monkeypatch.setattr(
        chess_review,
        "mark_engine_failure_notified",
        lambda **kwargs: marks.append(dict(kwargs)) or {"available": True, "reason": "marked_notified", "updated": True},
    )
    monkeypatch.setattr(
        chess_review,
        "send_telegram_message",
        lambda message, **_kwargs: telegram_calls.append(str(message)),
    )

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        out = chess_review.process_game(conn, summary_args, sample_game)
    finally:
        conn.close()

    assert out is None
    assert len(telegram_calls) == 1
    assert len(marks) == 1


def test_failure_not_notified_on_send_error(
    monkeypatch,
    tmp_path,
    summary_args,
    sample_game,
) -> None:
    marks: list[dict] = []

    monkeypatch.setattr(
        chess_review,
        "run_analysis_pipeline",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("Stockfish engine failed or produced no output.")),
    )
    monkeypatch.setattr(
        chess_review,
        "should_notify_engine_failure",
        lambda **_kwargs: {"available": True, "reason": "notify_pending", "should_notify": True},
    )
    monkeypatch.setattr(
        chess_review,
        "mark_engine_failure_notified",
        lambda **kwargs: marks.append(dict(kwargs)) or {"available": True, "reason": "marked_notified", "updated": True},
    )
    monkeypatch.setattr(
        chess_review,
        "send_telegram_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(chess_review.TelegramError("Telegram API error 400: bad request")),
    )

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        out = chess_review.process_game(conn, summary_args, sample_game)
    finally:
        conn.close()

    assert out is None
    assert marks == []


def test_telegram_send_retry_on_server_error(
    monkeypatch,
    tmp_path,
    summary_args,
    sample_game,
) -> None:
    calls = {"count": 0}
    marks: list[dict] = []

    monkeypatch.setattr(
        chess_review,
        "run_analysis_pipeline",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("Stockfish engine failed or produced no output.")),
    )
    monkeypatch.setattr(
        chess_review,
        "should_notify_engine_failure",
        lambda **_kwargs: {"available": True, "reason": "notify_pending", "should_notify": True},
    )
    monkeypatch.setattr(
        chess_review,
        "mark_engine_failure_notified",
        lambda **kwargs: marks.append(dict(kwargs)) or {"available": True, "reason": "marked_notified", "updated": True},
    )

    def _send_with_one_500(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise chess_review.TelegramError("Telegram API error 500: server error")
        return None

    monkeypatch.setattr(chess_review, "send_telegram_message", _send_with_one_500)
    monkeypatch.setattr(chess_review.time, "sleep", lambda _seconds: None)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        out = chess_review.process_game(conn, summary_args, sample_game)
    finally:
        conn.close()

    assert out is None
    assert calls["count"] == 2
    assert len(marks) == 1


def test_telegram_retry_strips_diagnostics(monkeypatch, tmp_path, summary_args) -> None:
    sent_messages: list[str] = []

    def _send_stub(message, **_kwargs):
        sent_messages.append(str(message))
        if len(sent_messages) == 1:
            raise chess_review.TelegramError("Telegram API error 400: bad request")
        return None

    monkeypatch.setattr(chess_review, "send_telegram_message", _send_stub)
    monkeypatch.setattr(chess_review, "TELEGRAM_FAILED_LOG_DIR", tmp_path / "logs")

    ok = chess_review._send_engine_failure_telegram_with_retry(
        message="# Engine failure\n- Detail: x\n- Reason: invalid <json>",
        args=summary_args,
        context="game_123",
    )

    assert ok is True
    assert len(sent_messages) == 2
    assert "- Reason:" in sent_messages[0]
    assert "- Reason:" not in sent_messages[1]


def test_telegram_persistent_failure_logs_to_file(monkeypatch, tmp_path, summary_args) -> None:
    monkeypatch.setattr(
        chess_review,
        "send_telegram_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(chess_review.TelegramError("Telegram API error 400: bad request")),
    )
    monkeypatch.setattr(chess_review, "TELEGRAM_FAILED_LOG_DIR", tmp_path / "logs")

    ok = chess_review._send_engine_failure_telegram_with_retry(
        message="# Engine failure\n- Detail: x\n- Reason: invalid <json>",
        args=summary_args,
        context="game(123)",
    )

    dump_path = tmp_path / "logs" / "tg_failed_game_123.txt"
    assert ok is False
    assert dump_path.exists()
    content = dump_path.read_text(encoding="utf-8")
    assert "<b>Engine failure</b>" in content
    assert "&lt;json&gt;" in content


def test_engine_payload_invalid_aborts_process_and_sends_telegram(
    monkeypatch,
    tmp_path,
    summary_args,
    sample_game,
) -> None:
    telegram_calls: list[str] = []
    llm_called = {"value": False}

    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "game_summary": {
                "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
                "engine_depth": 12,
                "result": "1-0",
                "total_plies": 4,
                "total_moves": 2,
                # Missing per-side v2 fields by construction.
                "label_counts": {"good": 3, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
            },
            "key_positions": [
                {"move_number": 1, "player": "White", "label": "good", "tactical_flag": "none", "material_change": 0, "played_san": "e4", "best_san": "e4"},
                {"move_number": 1, "player": "Black", "label": "good", "tactical_flag": "none", "material_change": 0, "played_san": "e5", "best_san": "e5"},
                {"move_number": 2, "player": "White", "label": "inaccuracy", "tactical_flag": "none", "material_change": 0, "played_san": "Nf3", "best_san": "Nc3"},
                {"move_number": 2, "player": "Black", "label": "good", "tactical_flag": "none", "material_change": 0, "played_san": "Nc6", "best_san": "Nc6"},
            ],
        },
    )
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

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        out = chess_review.process_game(conn, summary_args, sample_game)
        processed_count = int(conn.execute("SELECT COUNT(*) FROM processed_games").fetchone()[0])
    finally:
        conn.close()

    md_dir = summary_args.out / "md"
    md_files = list(md_dir.glob("*.md")) if md_dir.exists() else []
    assert out is None
    assert processed_count == 0
    assert llm_called["value"] is False
    assert len(telegram_calls) == 1
    assert "Engine failure" in telegram_calls[0]
    assert "Engine payload invalid:" in telegram_calls[0]
    assert md_files == []
