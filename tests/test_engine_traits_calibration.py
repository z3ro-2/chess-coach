from __future__ import annotations

import pytest

from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION, LABEL_KEYS
from src.engine_traits import (
    _score_from_blunder_rate,
    _score_from_error_rate,
    compute_engine_trait_scores,
)


def _payload(
    *,
    player_plies: int,
    inaccuracy: int,
    mistake: int,
    blunder: int,
    result: str,
    mate_threat: bool,
    late_error_events: int,
) -> dict:
    good = int(player_plies) - int(inaccuracy) - int(mistake) - int(blunder)
    assert good >= 0

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
    late_a = max(late_threshold, int(player_plies) - 2)
    late_b = max(late_threshold, int(player_plies))
    late_errors = max(0, min(2, int(late_error_events)))
    key_positions = [
        {
            "player": "Black",
            "move_number": max(4, int(player_plies) // 3),
            "label": "inaccuracy",
            "tactical_flag": "mate_threat" if mate_threat else "none",
            "material_change": 0,
        },
        {
            "player": "Black",
            "move_number": max(8, int(player_plies) // 2),
            "label": "mistake",
            "tactical_flag": "none",
            "material_change": -1,
        },
        {
            "player": "Black",
            "move_number": late_a,
            "label": "mistake" if late_errors >= 1 else "good",
            "tactical_flag": "none",
            "material_change": -1,
        },
        {
            "player": "Black",
            "move_number": late_b,
            "label": "blunder" if late_errors >= 2 else "good",
            "tactical_flag": "none",
            "material_change": -3 if late_errors >= 1 else 0,
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


def _club_window_payloads(*, worse: bool = False) -> list[dict]:
    # 20 games x 35 player plies = 700 player plies total.
    blunders = [2, 2, 2, 2, 2] * 4
    mistakes = [4, 3, 4, 5, 3] * 4
    inaccuracies = [6, 5, 6, 5, 7] * 4
    results = ["0-1", "1-0", "1/2-1/2", "0-1", "1-0"] * 4

    payloads: list[dict] = []
    for idx in range(20):
        inaccuracy = int(inaccuracies[idx])
        mistake = int(mistakes[idx])
        blunder = int(blunders[idx])
        late_error_events = 1 if results[idx] == "0-1" and (idx % 3 == 0) else 0

        if worse:
            inaccuracy += 2
            mistake += 1
            blunder += 1
            if results[idx] == "0-1":
                late_error_events = min(2, late_error_events + 1)

        payloads.append(
            _payload(
                player_plies=35,
                inaccuracy=inaccuracy,
                mistake=mistake,
                blunder=blunder,
                result=results[idx],
                mate_threat=(idx % 4 == 0),
                late_error_events=late_error_events,
            )
        )
    return payloads


def test_error_rate_curve_monotonic_and_anchored() -> None:
    assert _score_from_error_rate(0.00) == pytest.approx(100.0)
    assert _score_from_error_rate(0.10) == pytest.approx(80.0)
    assert _score_from_error_rate(0.20) == pytest.approx(60.0)
    assert _score_from_error_rate(0.30) == pytest.approx(40.0)
    assert _score_from_error_rate(0.40) == pytest.approx(20.0)
    assert _score_from_error_rate(0.50) == pytest.approx(0.0)

    samples = [i / 100 for i in range(0, 56, 2)]
    scores = [_score_from_error_rate(x) for x in samples]
    assert all(scores[i + 1] <= scores[i] for i in range(len(scores) - 1))


def test_blunder_rate_curve_monotonic() -> None:
    assert _score_from_blunder_rate(0.00) == pytest.approx(100.0)
    assert _score_from_blunder_rate(0.40) == pytest.approx(0.0)

    samples = [i / 100 for i in range(0, 46, 2)]
    scores = [_score_from_blunder_rate(x) for x in samples]
    assert all(scores[i + 1] <= scores[i] for i in range(len(scores) - 1))


def test_club_window_plausibility_non_extreme() -> None:
    scores = compute_engine_trait_scores(_club_window_payloads())
    for value in scores.values():
        assert 35 <= value <= 85, scores


def test_club_window_scores_worsen_when_error_rates_increase() -> None:
    base_scores = compute_engine_trait_scores(_club_window_payloads())
    worse_scores = compute_engine_trait_scores(_club_window_payloads(worse=True))

    assert worse_scores["tactical_awareness"] < base_scores["tactical_awareness"]
    assert worse_scores["material_discipline"] < base_scores["material_discipline"]
    assert worse_scores["blunder_frequency"] < base_scores["blunder_frequency"]
    assert worse_scores["defensive_resilience"] <= base_scores["defensive_resilience"]
