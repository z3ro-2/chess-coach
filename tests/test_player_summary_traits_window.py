from __future__ import annotations

from types import SimpleNamespace

import chess_review


def _payload(*, your_color: str, result: str, total_moves: int, key_positions: list[dict]) -> dict:
    return {
        "game_summary": {
            "your_color": your_color,
            "result": result,
            "total_moves": total_moves,
        },
        "key_positions": key_positions,
    }


def _insert_payload(conn, *, game_id: int, end_time: int, payload: dict) -> None:
    chess_review._store_engine_payload(
        conn,
        game_url=f"https://www.chess.com/game/live/{game_id}",
        end_time=end_time,
        engine_depth=15,
        payload=payload,
    )


def test_load_recent_game_reviews_for_traits_includes_backfill_and_respects_limit(tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        _insert_payload(
            conn,
            game_id=1,
            end_time=1_706_000_100,
            payload=_payload(your_color="white", result="1-0", total_moves=10, key_positions=[]),
        )
        _insert_payload(
            conn,
            game_id=2,
            end_time=1_706_000_200,
            payload=_payload(your_color="white", result="1-0", total_moves=20, key_positions=[]),
        )
        _insert_payload(
            conn,
            game_id=3,
            end_time=1_706_000_300,
            payload=_payload(your_color="white", result="1-0", total_moves=30, key_positions=[]),
        )
        payloads = chess_review._load_recent_game_reviews_for_traits(conn, 2)
    finally:
        conn.close()

    assert [int(p["game_summary"]["total_moves"]) for p in payloads] == [30, 20]


def test_compute_trait_scores_for_window_includes_backfill_payloads(monkeypatch, tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    monkeypatch.setattr(
        chess_review,
        "_analyze_game_with_stockfish",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Trait recompute must use stored payloads only.")),
    )
    try:
        _insert_payload(
            conn,
            game_id=101,
            end_time=1_706_000_100,
            payload={
                "game_summary": {"your_color": "white", "result": "1-0", "total_moves": 20},
                "key_positions": [
                    {"player": "White", "move_number": 4, "label": "good", "tactical_flag": "none", "material_change": 3},
                    {"player": "White", "move_number": 5, "label": "blunder", "tactical_flag": "hanging_piece", "material_change": -2},
                ],
            },
        )
        _insert_payload(
            conn,
            game_id=102,
            end_time=1_706_000_200,
            payload={
                "game_summary": {"your_color": "white", "result": "1/2-1/2", "total_moves": 20},
                "key_positions": [
                    {"player": "White", "move_number": 4, "label": "good", "tactical_flag": "none", "material_change": 4},
                    {"player": "White", "move_number": 10, "label": "good", "tactical_flag": "none", "material_change": -4},
                ],
            },
        )
        _insert_payload(
            conn,
            game_id=103,
            end_time=1_706_000_300,
            payload={
                "game_summary": {"your_color": "white", "result": "0-1", "total_moves": 20},
                "key_positions": [
                    {"player": "White", "move_number": 3, "label": "good", "tactical_flag": "none", "material_change": 3},
                    {"player": "White", "move_number": 6, "label": "blunder", "tactical_flag": "tactical_miss", "material_change": -3},
                    {"player": "White", "move_number": 8, "label": "mistake", "tactical_flag": "hanging_piece", "material_change": -1},
                ],
            },
        )
        scores = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=3)
    finally:
        conn.close()

    assert scores == {
        "tactical_awareness": 68,
        "material_discipline": 70,
        "conversion_ability": 33,
        "defensive_resilience": 50,
        "blunder_frequency": 97,
    }


def test_compute_trait_scores_for_window_respects_limit_n(tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        _insert_payload(
            conn,
            game_id=1,
            end_time=1_706_000_100,
            payload={
                "game_summary": {"your_color": "white", "result": "0-1", "total_moves": 20},
                "key_positions": [
                    {"player": "White", "move_number": 2, "label": "good", "tactical_flag": "none", "material_change": 3},
                    {"player": "White", "move_number": 4, "label": "blunder", "tactical_flag": "hanging_piece", "material_change": -5},
                    {"player": "White", "move_number": 5, "label": "blunder", "tactical_flag": "tactical_miss", "material_change": -3},
                ],
            },
        )
        _insert_payload(
            conn,
            game_id=2,
            end_time=1_706_000_200,
            payload={
                "game_summary": {"your_color": "white", "result": "1-0", "total_moves": 20},
                "key_positions": [{"player": "White", "move_number": 3, "label": "good", "tactical_flag": "none", "material_change": 3}],
            },
        )
        _insert_payload(
            conn,
            game_id=3,
            end_time=1_706_000_300,
            payload={
                "game_summary": {"your_color": "white", "result": "1-0", "total_moves": 20},
                "key_positions": [{"player": "White", "move_number": 3, "label": "good", "tactical_flag": "none", "material_change": 3}],
            },
        )
        scores_latest_two = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=2)
        scores_latest_three = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=3)
    finally:
        conn.close()

    assert scores_latest_two == {
        "tactical_awareness": 100,
        "material_discipline": 100,
        "conversion_ability": 100,
        "defensive_resilience": 100,
        "blunder_frequency": 100,
    }
    assert scores_latest_three == {
        "tactical_awareness": 74,
        "material_discipline": 76,
        "conversion_ability": 67,
        "defensive_resilience": 0,
        "blunder_frequency": 97,
    }


def test_traits_change_when_backfill_payload_changes(tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        _insert_payload(
            conn,
            game_id=201,
            end_time=1_706_000_100,
            payload={
                "game_summary": {"your_color": "white", "result": "1-0", "total_moves": 20},
                "key_positions": [
                    {"player": "White", "move_number": 4, "label": "good", "tactical_flag": "none", "material_change": 3},
                    {"player": "White", "move_number": 5, "label": "blunder", "tactical_flag": "hanging_piece", "material_change": -2},
                ],
            },
        )
        _insert_payload(
            conn,
            game_id=202,
            end_time=1_706_000_200,
            payload={
                "game_summary": {"your_color": "white", "result": "1/2-1/2", "total_moves": 20},
                "key_positions": [
                    {"player": "White", "move_number": 4, "label": "good", "tactical_flag": "none", "material_change": 4},
                    {"player": "White", "move_number": 10, "label": "good", "tactical_flag": "none", "material_change": -4},
                ],
            },
        )
        _insert_payload(
            conn,
            game_id=203,
            end_time=1_706_000_300,
            payload={
                "game_summary": {"your_color": "white", "result": "0-1", "total_moves": 20},
                "key_positions": [
                    {"player": "White", "move_number": 3, "label": "good", "tactical_flag": "none", "material_change": 3},
                    {"player": "White", "move_number": 6, "label": "blunder", "tactical_flag": "tactical_miss", "material_change": -3},
                    {"player": "White", "move_number": 8, "label": "mistake", "tactical_flag": "hanging_piece", "material_change": -1},
                ],
            },
        )
        before = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=3)

        _insert_payload(
            conn,
            game_id=203,
            end_time=1_706_000_300,
            payload={
                "game_summary": {"your_color": "white", "result": "1-0", "total_moves": 20},
                "key_positions": [
                    {"player": "White", "move_number": 3, "label": "good", "tactical_flag": "none", "material_change": 3},
                    {"player": "White", "move_number": 6, "label": "good", "tactical_flag": "none", "material_change": 0},
                    {"player": "White", "move_number": 8, "label": "good", "tactical_flag": "none", "material_change": 0},
                ],
            },
        )
        after = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=3)
    finally:
        conn.close()

    assert before == {
        "tactical_awareness": 68,
        "material_discipline": 70,
        "conversion_ability": 33,
        "defensive_resilience": 50,
        "blunder_frequency": 97,
    }
    assert after == {
        "tactical_awareness": 86,
        "material_discipline": 82,
        "conversion_ability": 67,
        "defensive_resilience": 100,
        "blunder_frequency": 98,
    }
    assert after != before
