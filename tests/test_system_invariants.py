from __future__ import annotations

from types import SimpleNamespace

import chess_review
from src.engine_traits import compute_engine_trait_scores


def _backfill_args(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        username="logan",
        provider="ollama",
        backfill=1,
        enable_engine=True,
        state_db=tmp_path / "state.sqlite",
        out=tmp_path / "output",
    )


def test_backfill_mode_never_calls_llm_polling_telegram_or_markdown(monkeypatch, tmp_path, capsys) -> None:
    args = _backfill_args(tmp_path)
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
        "call_ollama_generate",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Backfill must never call LLM.")),
    )
    monkeypatch.setattr(
        chess_review,
        "call_openai_chat",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Backfill must never call LLM.")),
    )
    monkeypatch.setattr(
        chess_review,
        "poll_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Backfill must never start polling.")),
    )
    monkeypatch.setattr(
        chess_review,
        "write_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Backfill must never write markdown.")),
    )
    monkeypatch.setattr(
        chess_review.threading,
        "Thread",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Backfill must never start Telegram thread.")),
    )

    code = chess_review.main()
    out = capsys.readouterr().out

    assert code == 0
    assert "Backfill Summary:" in out


def test_trait_window_recompute_uses_only_stored_payloads(monkeypatch, tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        chess_review._store_engine_payload(
            conn,
            game_url="https://www.chess.com/game/live/999",
            end_time=1_706_000_999,
            engine_depth=15,
            payload={
                "game_summary": {
                    "your_color": "white",
                    "result": "1-0",
                    "total_moves": 60,
                    "total_plies": 120,
                    "label_counts": {"good": 56, "inaccuracy": 2, "mistake": 1, "blunder": 1, "brilliant": 0},
                },
                "key_positions": [],
            },
        )
        monkeypatch.setattr(
            chess_review,
            "_analyze_game_with_stockfish",
            lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Summary trait recompute must not run Stockfish.")),
        )
        scores = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=1)
    finally:
        conn.close()

    assert isinstance(scores, dict)
    assert "tactical_awareness" in scores


def test_trait_scores_respect_global_caps_and_non_negative_bounds() -> None:
    scores = compute_engine_trait_scores(
        [
            {
                "game_summary": {
                    "your_color": "white",
                    "result": "1-0",
                    "total_moves": 40,
                    "total_plies": 80,
                    "label_counts": {"good": 40, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
                },
                "key_positions": [],
            },
            {
                "game_summary": {
                    "your_color": "white",
                    "result": "0-1",
                    "total_moves": 1000,
                    "total_plies": 2000,
                    "label_counts": {"good": 940, "inaccuracy": 20, "mistake": 30, "blunder": 10, "brilliant": 0},
                },
                "key_positions": [],
            },
        ]
    )

    assert max(scores.values()) <= 90
    assert min(scores.values()) >= 0
