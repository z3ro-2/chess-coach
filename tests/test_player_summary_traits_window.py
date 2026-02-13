from __future__ import annotations

from types import SimpleNamespace

import chess_review
from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION


def _payload(
    *,
    your_color: str,
    result: str,
    total_moves: int,
    label_counts: dict[str, int],
    key_positions: list[dict],
) -> dict:
    total_plies = int(total_moves) * 2
    player_total_plies = int(total_moves)
    if sum(int(v) for v in label_counts.values()) != player_total_plies:
        raise AssertionError("label_counts must sum to player_total_plies in test payloads")
    opponent_counts = {
        "good": player_total_plies,
        "inaccuracy": 0,
        "mistake": 0,
        "blunder": 0,
        "brilliant": 0,
    }
    by_side = (
        {"white": dict(label_counts), "black": opponent_counts}
        if your_color == "white"
        else {"white": opponent_counts, "black": dict(label_counts)}
    )
    merged = {k: int(by_side["white"][k]) + int(by_side["black"][k]) for k in label_counts}
    return {
        "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
        "game_summary": {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "your_color": your_color,
            "result": result,
            "total_moves": total_moves,
            "total_plies": total_plies,
            "white_plies": int(total_moves),
            "black_plies": int(total_moves),
            "unlabeled_white_plies": 0,
            "unlabeled_black_plies": 0,
            "label_counts_total": dict(merged),
            "label_counts_white": dict(by_side["white"]),
            "label_counts_black": dict(by_side["black"]),
            "player_total_plies": player_total_plies,
            "player_total_moves": player_total_plies,
            "player_label_counts": label_counts,
            "label_counts_by_side": by_side,
            "label_counts": merged,
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
    assert scores["blunder_frequency"] < 90
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
                total_moves=30,
                label_counts={"good": 16, "inaccuracy": 4, "mistake": 6, "blunder": 4, "brilliant": 0},
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
                total_moves=30,
                label_counts={"good": 26, "inaccuracy": 2, "mistake": 2, "blunder": 0, "brilliant": 0},
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
                label_counts={"good": 27, "inaccuracy": 2, "mistake": 1, "blunder": 0, "brilliant": 0},
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

    assert metrics["trait_window_games"] == 2
    assert metrics["trait_window_requested_games"] == 20
    assert metrics["trait_window_moves"] == 260
    assert metrics["confidence"] == "LOW"
    assert str(metrics["confidence_reason"]).startswith("insufficient v2 payloads")


def test_trait_window_metrics_flag_integrity_warning_in_diagnostics(tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        _insert_payload(
            conn,
            game_id=303,
            end_time=1_706_000_400,
            payload=_payload(
                your_color="white",
                result="0-1",
                total_moves=20,
                label_counts={"good": 0, "inaccuracy": 8, "mistake": 5, "blunder": 7, "brilliant": 0},
                key_positions=[],
            ),
        )
        metrics = chess_review._compute_trait_scores_and_window_metrics(
            conn,
            SimpleNamespace(),
            window_size=1,
        )
    finally:
        conn.close()

    assert metrics["integrity_warning"] is True
    assert "trait window integrity warning" in str(metrics["confidence_reason"])
    diagnostics = dict(metrics["trait_diagnostics"])
    assert "window_integrity" in diagnostics
    assert diagnostics["window_integrity"]["warning"] is True
    assert "non_good_rate_gt_0_75" in diagnostics["window_integrity"]["reasons"]


def test_system_confidence_low_when_engine_coverage_low() -> None:
    confidence, reason = chess_review._derive_system_confidence_for_trait_window(
        total_moves=800,
        trait_window_games=20,
        target_window_games=20,
        aggregate_components={"coverage": 0.60, "guardrails": {"sanity_refusal_applied": False}},
        integrity_warning=False,
        integrity_warning_reasons=[],
        trait_update_refused=False,
    )
    assert confidence == "LOW"
    assert "engine coverage low" in reason


def test_system_confidence_low_when_integrity_warning_present() -> None:
    confidence, reason = chess_review._derive_system_confidence_for_trait_window(
        total_moves=800,
        trait_window_games=20,
        target_window_games=20,
        aggregate_components={"coverage": 1.0, "guardrails": {"sanity_refusal_applied": False}},
        integrity_warning=True,
        integrity_warning_reasons=["non_good_rate_gt_0_75"],
        trait_update_refused=False,
    )
    assert confidence == "LOW"
    assert "trait window integrity warning" in reason


def test_system_confidence_high_with_full_clean_coverage() -> None:
    confidence, reason = chess_review._derive_system_confidence_for_trait_window(
        total_moves=800,
        trait_window_games=20,
        target_window_games=20,
        aggregate_components={"coverage": 1.0, "guardrails": {"sanity_refusal_applied": False}},
        integrity_warning=False,
        integrity_warning_reasons=[],
        trait_update_refused=False,
    )
    assert confidence == "HIGH"
    assert reason == ""


def test_load_recent_game_reviews_for_traits_has_stable_tiebreak_order(tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        first = _payload(
            your_color="white",
            result="1-0",
            total_moves=24,
            label_counts={"good": 20, "inaccuracy": 2, "mistake": 2, "blunder": 0, "brilliant": 0},
            key_positions=[],
        )
        first["game_summary"]["marker"] = "game-1"
        second = _payload(
            your_color="white",
            result="1-0",
            total_moves=24,
            label_counts={"good": 20, "inaccuracy": 2, "mistake": 2, "blunder": 0, "brilliant": 0},
            key_positions=[],
        )
        second["game_summary"]["marker"] = "game-2"

        _insert_payload(conn, game_id=1, end_time=1_706_111_111, payload=first)
        _insert_payload(conn, game_id=2, end_time=1_706_111_111, payload=second)
        loaded = chess_review._load_recent_game_reviews_for_traits(conn, 2)
    finally:
        conn.close()

    assert [str(item["game_summary"]["marker"]) for item in loaded] == ["game-2", "game-1"]


def test_trait_window_with_old_schema_payload_ignores_invalid_row_without_reanalysis(monkeypatch, tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    monkeypatch.setattr(
        chess_review,
        "_analyze_game_with_stockfish",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Trait recompute must not run engine analysis.")),
    )
    try:
        _insert_payload(
            conn,
            game_id=401,
            end_time=1_706_000_100,
            payload=_payload(
                your_color="white",
                result="1-0",
                total_moves=60,
                label_counts={"good": 60, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
                key_positions=[],
            ),
        )
        _insert_payload(
            conn,
            game_id=402,
            end_time=1_706_000_200,
            payload={
                "game_summary": {"your_color": "white", "result": "1-0"},
                "key_positions": [],
            },
        )
        scores = chess_review._compute_trait_scores_for_window(conn, SimpleNamespace(), window_size=2)
        metrics = chess_review._compute_trait_scores_and_window_metrics(conn, SimpleNamespace(), window_size=2)
    finally:
        conn.close()

    assert scores == {
        "tactical_awareness": 100,
        "material_discipline": 100,
        "conversion_ability": 50,
        "defensive_resilience": 50,
        "blunder_frequency": 100,
    }
    assert metrics["trait_window_games"] == 1
    assert metrics["trait_window_requested_games"] == 2
    assert metrics["confidence"] == "LOW"
    assert str(metrics["confidence_reason"]).startswith("insufficient v2 payloads")


def test_load_recent_game_reviews_for_traits_skips_legacy_and_collects_older_v2_payloads(tmp_path) -> None:
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        _insert_payload(
            conn,
            game_id=501,
            end_time=1_706_000_300,
            payload={"game_summary": {"your_color": "white", "result": "1-0"}, "key_positions": []},
        )
        second = _payload(
            your_color="white",
            result="1-0",
            total_moves=24,
            label_counts={"good": 20, "inaccuracy": 2, "mistake": 2, "blunder": 0, "brilliant": 0},
            key_positions=[],
        )
        second["game_summary"]["marker"] = "newer-v2"
        third = _payload(
            your_color="white",
            result="1-0",
            total_moves=24,
            label_counts={"good": 19, "inaccuracy": 3, "mistake": 2, "blunder": 0, "brilliant": 0},
            key_positions=[],
        )
        third["game_summary"]["marker"] = "older-v2"
        _insert_payload(conn, game_id=502, end_time=1_706_000_200, payload=second)
        _insert_payload(conn, game_id=503, end_time=1_706_000_100, payload=third)

        loaded = chess_review._load_recent_game_reviews_for_traits(conn, 2)
    finally:
        conn.close()

    assert [str(item["game_summary"]["marker"]) for item in loaded] == ["newer-v2", "older-v2"]
