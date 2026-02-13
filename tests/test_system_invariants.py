from __future__ import annotations

from types import SimpleNamespace

import chess_review
from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION
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


def _payload_v2(
    *,
    your_color: str,
    result: str,
    total_moves: int,
    player_label_counts: dict[str, int],
) -> dict:
    total_plies = int(total_moves) * 2
    opponent_counts = {
        "good": int(total_moves),
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
            "player_total_plies": int(total_moves),
            "player_total_moves": int(total_moves),
            "player_label_counts": dict(player_label_counts),
            "label_counts_by_side": by_side,
            "label_counts": merged,
        },
        "key_positions": [],
    }


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
            payload=_payload_v2(
                your_color="white",
                result="1-0",
                total_moves=60,
                player_label_counts={"good": 56, "inaccuracy": 2, "mistake": 1, "blunder": 1, "brilliant": 0},
            ),
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
            _payload_v2(
                your_color="white",
                result="1-0",
                total_moves=40,
                player_label_counts={"good": 40, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
            ),
            _payload_v2(
                your_color="white",
                result="0-1",
                total_moves=1000,
                player_label_counts={"good": 940, "inaccuracy": 20, "mistake": 30, "blunder": 10, "brilliant": 0},
            ),
        ]
    )

    assert max(scores.values()) <= 90
    assert min(scores.values()) >= 0
