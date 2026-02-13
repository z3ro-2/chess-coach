from __future__ import annotations

from copy import deepcopy

from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION, LABEL_KEYS
from src.engine_traits import compute_engine_trait_scores


def _build_payload(
    *,
    player_plies: int,
    result: str,
    inaccuracy: int,
    mistake: int,
    blunder: int,
    mate_threat: bool,
    late_error_count: int,
    severe_material: bool,
) -> dict:
    good = int(player_plies) - int(inaccuracy) - int(mistake) - int(blunder)
    assert good >= 0
    assert 0 <= int(late_error_count) <= 2

    player_counts = {
        "good": int(good),
        "inaccuracy": int(inaccuracy),
        "mistake": int(mistake),
        "blunder": int(blunder),
        "brilliant": 0,
    }
    opponent_counts = {
        "good": int(player_plies),
        "inaccuracy": 0,
        "mistake": 0,
        "blunder": 0,
        "brilliant": 0,
    }
    by_side = {
        "white": dict(opponent_counts),
        "black": dict(player_counts),
    }
    total_counts = {key: int(by_side["white"][key]) + int(by_side["black"][key]) for key in LABEL_KEYS}

    late_threshold = max(1, int(round(float(player_plies) * 0.7)))
    late_move_a = max(late_threshold, int(player_plies) - 2)
    late_move_b = max(late_threshold, int(player_plies))

    key_positions = [
        {
            "player": "Black",
            "move_number": max(2, int(player_plies) // 3),
            "label": "good",
            "tactical_flag": "none",
            "material_change": 0,
        },
        {
            "player": "Black",
            "move_number": max(4, int(player_plies) // 2),
            "label": "inaccuracy",
            "tactical_flag": "mate_threat" if mate_threat else "none",
            "material_change": 0,
        },
        {
            "player": "Black",
            "move_number": late_move_a,
            "label": "mistake" if int(late_error_count) >= 1 else "good",
            "tactical_flag": "none",
            "material_change": -1,
        },
        {
            "player": "Black",
            "move_number": late_move_b,
            "label": "blunder" if int(late_error_count) >= 2 else "good",
            "tactical_flag": "none",
            "material_change": -3 if severe_material else 0,
        },
    ]

    return {
        "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
        "game_summary": {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "your_color": "black",
            "result": result,
            "total_plies": int(player_plies) * 2,
            "total_moves": int(player_plies),
            "white_plies": int(player_plies),
            "black_plies": int(player_plies),
            "unlabeled_white_plies": 0,
            "unlabeled_black_plies": 0,
            "label_counts_total": dict(total_counts),
            "label_counts_white": dict(by_side["white"]),
            "label_counts_black": dict(by_side["black"]),
            "player_total_plies": int(player_plies),
            "player_total_moves": int(player_plies),
            "player_label_counts": dict(player_counts),
            "label_counts_by_side": by_side,
            "label_counts": total_counts,
        },
        "key_positions": key_positions,
    }


def _anchor_payloads() -> list[dict]:
    player_plies_by_game = [
        35,
        36,
        37,
        38,
        39,
        40,
        41,
        42,
        43,
        44,
        45,
        35,
        36,
        37,
        38,
        39,
        40,
        41,
        42,
        43,
    ]
    payloads: list[dict] = []
    wins_seen = 0

    for idx, player_plies in enumerate(player_plies_by_game):
        phase = idx % 5
        if phase in {0, 3}:
            result = "0-1"
        elif phase in {1, 4}:
            result = "1-0"
        else:
            result = "1/2-1/2"

        inaccuracy = 6 if idx % 2 == 0 else 5
        mistake = 5 if idx % 3 == 0 else 4
        blunder = 3 if idx % 2 == 0 else 2

        late_error_count = 0
        if result == "0-1":
            if wins_seen in {0, 1, 3, 5, 7}:
                late_error_count = 1
            wins_seen += 1

        payloads.append(
            _build_payload(
                player_plies=player_plies,
                result=result,
                inaccuracy=inaccuracy,
                mistake=mistake,
                blunder=blunder,
                mate_threat=(idx % 4 == 0),
                late_error_count=late_error_count,
                severe_material=(idx % 6 == 0),
            )
        )

    return payloads


def _double_player_blunders(payloads: list[dict]) -> list[dict]:
    mutated = deepcopy(payloads)
    for payload in mutated:
        summary = payload["game_summary"]
        side_counts = summary["label_counts_by_side"]
        player_counts = dict(summary["player_label_counts"])

        original_blunder = int(player_counts["blunder"])
        doubled_blunder = original_blunder * 2
        delta = doubled_blunder - original_blunder
        assert int(player_counts["good"]) >= delta

        player_counts["blunder"] = int(doubled_blunder)
        player_counts["good"] = int(player_counts["good"]) - int(delta)

        side_counts["black"] = dict(player_counts)
        summary["player_label_counts"] = dict(player_counts)
        merged = {
            key: int(side_counts["white"][key]) + int(side_counts["black"][key]) for key in LABEL_KEYS
        }
        summary["label_counts"] = dict(merged)
        summary["label_counts_total"] = dict(merged)
        summary["label_counts_white"] = dict(side_counts["white"])
        summary["label_counts_black"] = dict(side_counts["black"])
    return mutated


def test_trait_realism_anchor_for_approximately_1100_elo() -> None:
    scores = compute_engine_trait_scores(_anchor_payloads())

    assert 35 <= scores["tactical_awareness"] <= 65
    assert 55 <= scores["material_discipline"] <= 80
    assert 50 <= scores["conversion_ability"] <= 80
    assert 55 <= scores["defensive_resilience"] <= 80
    assert 60 <= scores["blunder_frequency"] <= 85


def test_traits_monotonic_when_blunders_increase() -> None:
    anchor_payloads = _anchor_payloads()
    doubled_blunder_payloads = _double_player_blunders(anchor_payloads)

    anchor_scores = compute_engine_trait_scores(anchor_payloads)
    doubled_scores = compute_engine_trait_scores(doubled_blunder_payloads)

    assert doubled_scores["tactical_awareness"] < anchor_scores["tactical_awareness"]
    assert doubled_scores["material_discipline"] < anchor_scores["material_discipline"]
    assert doubled_scores["blunder_frequency"] < anchor_scores["blunder_frequency"]
