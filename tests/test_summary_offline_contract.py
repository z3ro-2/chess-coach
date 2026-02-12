from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import chess_review
from src.config.provider_config import set_provider
from src.engine_traits import compute_engine_trait_scores


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
    assert lines[8].strip() == "---"

    headings = [line.strip() for line in lines if line.startswith("## ")]
    assert headings == [
        "## Snapshot",
        "## Engine-Derived Traits",
        "## Primary Weaknesses",
        "## Training Priority",
    ]

    training_lines = [line for line in _section_lines(markdown, "## Training Priority") if line.startswith("- ")]
    assert len(training_lines) == 3


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
    # Deterministic expected scores:
    # tactical_awareness=70, material_discipline=64, conversion_ability=50,
    # defensive_resilience=50, blunder_frequency=96
    return [
        {
            "game_summary": {"your_color": "white", "result": "1-0", "total_moves": 20},
            "key_positions": [
                {"player": "White", "move_number": 4, "label": "good", "tactical_flag": "none", "material_change": 3},
                {"player": "White", "move_number": 5, "label": "blunder", "tactical_flag": "hanging_piece", "material_change": -2},
                {"player": "White", "move_number": 6, "label": "mistake", "tactical_flag": "tactical_miss", "material_change": -1},
            ],
        },
        {
            "game_summary": {"your_color": "white", "result": "1/2-1/2", "total_moves": 20},
            "key_positions": [
                {"player": "White", "move_number": 4, "label": "good", "tactical_flag": "none", "material_change": 4},
                {"player": "White", "move_number": 10, "label": "good", "tactical_flag": "none", "material_change": -4},
            ],
        },
        {
            "game_summary": {"your_color": "white", "result": "0-1", "total_moves": 10},
            "key_positions": [
                {"player": "White", "move_number": 3, "label": "blunder", "tactical_flag": "tactical_miss", "material_change": -5},
            ],
        },
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
def strict_template_output() -> str:
    return """---
date_utc: 2026-02-12
your_color: white
opponent: opponent
result: 1-0
win_pct: 50.0
loss_pct: 25.0
draw_pct: 25.0
---

## Snapshot
- Total games: 4
- Record: 2–1–1
- Win rate: 50.0%

## Engine-Derived Traits
- Tactical Awareness: 70
- Material Discipline: 64
- Conversion Ability: 50
- Defensive Resilience: 50
- Blunder Frequency: 96

## Primary Weaknesses
- Conversion Ability: Lowest deterministic engine-derived score in the rolling window (50/100).

## Training Priority
- Review each blunder and identify the missed tactical cue.
- Run daily tactical sets focused on hanging pieces and tactical misses.
- Add a conversion checklist when up material by +3 or more.
"""


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
        "tactical_awareness": 70,
        "material_discipline": 64,
        "conversion_ability": 50,
        "defensive_resilience": 50,
        "blunder_frequency": 96,
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
        "tactical_awareness": 82,
        "material_discipline": 79,
        "conversion_ability": 50,
        "defensive_resilience": 100,
        "blunder_frequency": 98,
    }


def test_llm_output_format_conforms_to_strict_template(
    monkeypatch,
    tmp_path,
    known_recent_meta,
    predictable_key_payloads,
    strict_template_output,
) -> None:
    set_provider("ollama")
    captured: dict[str, str] = {}

    def _fake_ollama_generate(**kwargs):
        captured["system_msg"] = str(kwargs.get("system_msg", ""))
        captured["user_msg"] = str(kwargs.get("user_msg", ""))
        return strict_template_output

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
        summary_context=summary_context,
    )

    assert out == strict_template_output
    assert "Do not compute, infer, or recompute any metric." in captured["user_msg"]
    assert "Do not do arithmetic, percentages, ranking, or score derivation." in captured["user_msg"]
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

