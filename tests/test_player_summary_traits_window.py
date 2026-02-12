from __future__ import annotations

from types import SimpleNamespace

import chess_review


def _payload(
    *,
    your_color: str,
    result: str,
    total_moves: int,
    label_counts: dict[str, int],
    key_positions: list[dict],
) -> dict:
    return {
        "game_summary": {
            "your_color": your_color,
            "result": result,
            "total_moves": total_moves,
            "total_plies": total_moves * 2,
            "label_counts": label_counts,
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
            payload=_payload(
                your_color="white",
                result="1-0",
                total_moves=10,
                label_counts={"good": 7, "inaccuracy": 2, "mistake": 1, "blunder": 0, "brilliant": 0},
                key_positions=[],
            ),
        )
        _insert_payload(
            conn,
            game_id=2,
            end_time=1_706_000_200,
            payload=_payload(
                your_color="white",
                result="1-0",
                total_moves=20,
                label_counts={"good": 16, "inaccuracy": 2, "mistake": 2, "blunder": 0, "brilliant": 0},
                key_positions=[],
            ),
        )
        _insert_payload(
            conn,
            game_id=3,
            end_time=1_706_000_300,
            payload=_payload(
                your_color="white",
                result="1-0",
                total_moves=30,
                label_counts={"good": 23, "inaccuracy": 4, "mistake": 3, "blunder": 0, "brilliant": 0},
                key_positions=[],
            ),
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
            payload=_payload(
                your_color="white",
                result="1-0",
                total_moves=24,
                label_counts={"good": 17, "inaccuracy": 3, "mistake": 3, "blunder": 1, "brilliant": 0},
                key_positions=[{"player": "White", "move_number": 20, "label": "mistake", "tactical_flag": "none", "material_change": -3}],
            ),
        )
        _insert_payload(
            conn,
            game_id=102,
            end_time=1_706_000_200,
            payload=_payload(
                your_color="white",
                result="1/2-1/2",
                total_moves=26,
                label_counts={"good": 20, "inaccuracy": 3, "mistake": 2, "blunder": 1, "brilliant": 0},
                key_positions=[{"player": "White", "move_number": 22, "label": "mistake", "tactical_flag": "mate_threat", "material_change": -1}],
            ),
        )
        _insert_payload(
            conn,
            game_id=103,
            end_time=1_706_000_300,
            payload=_payload(
                your_color="white",
                result="0-1",
                total_moves=28,
                label_counts={"good": 18, "inaccuracy": 4, "mistake": 4, "blunder": 2, "brilliant": 0},
                key_positions=[{"player": "White", "move_number": 24, "label": "blunder", "tactical_flag": "mate_threat", "material_change": -4}],
            ),
        )
        scores = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=3)
    finally:
        conn.close()

    assert scores["tactical_awareness"] < 80
    assert scores["material_discipline"] < 80
    assert scores["blunder_frequency"] < 85
    assert scores["conversion_ability"] <= 70


def test_compute_trait_scores_for_window_respects_limit_n(tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        # Older bad game that should affect scores only when limit includes it.
        _insert_payload(
            conn,
            game_id=1,
            end_time=1_706_000_100,
            payload=_payload(
                your_color="white",
                result="0-1",
                total_moves=24,
                label_counts={"good": 10, "inaccuracy": 4, "mistake": 6, "blunder": 4, "brilliant": 0},
                key_positions=[{"player": "White", "move_number": 20, "label": "blunder", "tactical_flag": "mate_threat", "material_change": -5}],
            ),
        )
        # Two newer clean games.
        _insert_payload(
            conn,
            game_id=2,
            end_time=1_706_000_200,
            payload=_payload(
                your_color="white",
                result="1-0",
                total_moves=24,
                label_counts={"good": 20, "inaccuracy": 2, "mistake": 2, "blunder": 0, "brilliant": 0},
                key_positions=[],
            ),
        )
        _insert_payload(
            conn,
            game_id=3,
            end_time=1_706_000_300,
            payload=_payload(
                your_color="white",
                result="1-0",
                total_moves=24,
                label_counts={"good": 21, "inaccuracy": 2, "mistake": 1, "blunder": 0, "brilliant": 0},
                key_positions=[],
            ),
        )
        scores_latest_two = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=2)
        scores_latest_three = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=3)
    finally:
        conn.close()

    assert scores_latest_two["tactical_awareness"] > scores_latest_three["tactical_awareness"]
    assert scores_latest_two["material_discipline"] > scores_latest_three["material_discipline"]
    assert scores_latest_two["blunder_frequency"] > scores_latest_three["blunder_frequency"]


def test_traits_change_when_backfill_payload_changes(tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        _insert_payload(
            conn,
            game_id=201,
            end_time=1_706_000_100,
            payload=_payload(
                your_color="white",
                result="1-0",
                total_moves=24,
                label_counts={"good": 16, "inaccuracy": 3, "mistake": 4, "blunder": 1, "brilliant": 0},
                key_positions=[{"player": "White", "move_number": 20, "label": "mistake", "tactical_flag": "none", "material_change": -3}],
            ),
        )
        _insert_payload(
            conn,
            game_id=202,
            end_time=1_706_000_200,
            payload=_payload(
                your_color="white",
                result="1/2-1/2",
                total_moves=24,
                label_counts={"good": 17, "inaccuracy": 3, "mistake": 3, "blunder": 1, "brilliant": 0},
                key_positions=[{"player": "White", "move_number": 21, "label": "mistake", "tactical_flag": "mate_threat", "material_change": -1}],
            ),
        )
        _insert_payload(
            conn,
            game_id=203,
            end_time=1_706_000_300,
            payload=_payload(
                your_color="white",
                result="0-1",
                total_moves=24,
                label_counts={"good": 13, "inaccuracy": 4, "mistake": 4, "blunder": 3, "brilliant": 0},
                key_positions=[{"player": "White", "move_number": 20, "label": "blunder", "tactical_flag": "mate_threat", "material_change": -4}],
            ),
        )
        before = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=3)

        _insert_payload(
            conn,
            game_id=203,
            end_time=1_706_000_300,
            payload=_payload(
                your_color="white",
                result="1-0",
                total_moves=24,
                label_counts={"good": 20, "inaccuracy": 2, "mistake": 2, "blunder": 0, "brilliant": 0},
                key_positions=[],
            ),
        )
        after = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=3)
    finally:
        conn.close()

    assert after["tactical_awareness"] > before["tactical_awareness"]
    assert after["material_discipline"] > before["material_discipline"]
    assert after["blunder_frequency"] > before["blunder_frequency"]
    assert after != before


def test_trait_window_metrics_include_moves_and_confidence_tier(tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        _insert_payload(
            conn,
            game_id=301,
            end_time=1_706_000_100,
            payload=_payload(
                your_color="white",
                result="1-0",
                total_moves=120,
                label_counts={"good": 100, "inaccuracy": 10, "mistake": 8, "blunder": 2, "brilliant": 0},
                key_positions=[],
            ),
        )
        _insert_payload(
            conn,
            game_id=302,
            end_time=1_706_000_200,
            payload=_payload(
                your_color="white",
                result="1/2-1/2",
                total_moves=140,
                label_counts={"good": 114, "inaccuracy": 12, "mistake": 10, "blunder": 4, "brilliant": 0},
                key_positions=[],
            ),
        )
        metrics = chess_review._compute_trait_scores_and_window_metrics(
            conn,
            SimpleNamespace(),
            window_size=20,
        )
    finally:
        conn.close()

    assert metrics["trait_window_games"] == 20
    assert metrics["trait_window_moves"] == 260
    assert metrics["confidence"] == "MEDIUM"
