from __future__ import annotations

from src.engine_traits import (
    _WindowAggregates,
    _compute_window_scores,
    blunder_frequency,
    compute_engine_trait_scores,
    conversion_ability,
    defensive_resilience,
    material_discipline,
    tactical_awareness,
)


def _payload(
    *,
    your_color: str,
    result: str,
    total_moves: int | None,
    total_plies: int | None,
    label_counts: dict | None,
    key_positions: list[dict],
) -> dict:
    summary: dict = {
        "your_color": your_color,
        "result": result,
    }
    if total_moves is not None:
        summary["total_moves"] = total_moves
    if total_plies is not None:
        summary["total_plies"] = total_plies
    if label_counts is not None:
        summary["label_counts"] = label_counts
    return {
        "game_summary": summary,
        "key_positions": key_positions,
    }


def test_bad_player_payload_produces_low_scores() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="0-1",
            total_moves=40,
            total_plies=80,
            label_counts={"good": 18, "inaccuracy": 8, "mistake": 6, "blunder": 5, "brilliant": 0},
            key_positions=[
                {"player": "White", "move_number": 30, "label": "blunder", "tactical_flag": "mate_threat", "material_change": -4},
                {"player": "White", "move_number": 36, "label": "mistake", "tactical_flag": "mate_threat", "material_change": -3},
            ],
        )
    ]

    scores = compute_engine_trait_scores(payloads)
    assert scores["tactical_awareness"] <= 20
    assert scores["material_discipline"] <= 20
    assert scores["defensive_resilience"] <= 40
    assert scores["blunder_frequency"] <= 40
    # Non-win games are neutral for conversion_ability by definition.
    assert scores["conversion_ability"] == 50


def test_average_player_payload_produces_mid_scores() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1/2-1/2",
            total_moves=40,
            total_plies=80,
            label_counts={"good": 25, "inaccuracy": 7, "mistake": 5, "blunder": 2, "brilliant": 1},
            key_positions=[
                {"player": "White", "move_number": 28, "label": "mistake", "tactical_flag": "none", "material_change": -3},
                {"player": "White", "move_number": 34, "label": "mistake", "tactical_flag": "mate_threat", "material_change": -1},
            ],
        )
    ]

    scores = compute_engine_trait_scores(payloads)
    assert 20 <= scores["tactical_awareness"] <= 40
    assert 40 <= scores["material_discipline"] <= 70
    assert 50 <= scores["defensive_resilience"] <= 80
    assert 60 <= scores["blunder_frequency"] <= 85
    assert scores["conversion_ability"] == 50


def test_clean_games_produce_high_scores() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_moves=50,
            total_plies=100,
            label_counts={"good": 45, "inaccuracy": 3, "mistake": 1, "blunder": 0, "brilliant": 1},
            key_positions=[{"player": "White", "move_number": 45, "label": "good", "tactical_flag": "none", "material_change": 0}],
        ),
        _payload(
            your_color="white",
            result="1/2-1/2",
            total_moves=48,
            total_plies=96,
            label_counts={"good": 42, "inaccuracy": 4, "mistake": 1, "blunder": 0, "brilliant": 1},
            key_positions=[{"player": "White", "move_number": 40, "label": "good", "tactical_flag": "none", "material_change": 0}],
        ),
    ]

    scores = compute_engine_trait_scores(payloads)
    assert scores["tactical_awareness"] >= 85
    assert scores["material_discipline"] >= 85
    assert scores["conversion_ability"] >= 70
    assert scores["defensive_resilience"] >= 70
    assert scores["blunder_frequency"] >= 90


def test_missing_primary_fields_returns_neutral_scores() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="0-1",
            total_moves=None,
            total_plies=None,
            label_counts=None,
            key_positions=[
                {"player": "White", "move_number": 24, "label": "blunder", "tactical_flag": "mate_threat", "material_change": -9}
            ],
        )
    ]

    scores = compute_engine_trait_scores(payloads)
    assert scores == {
        "tactical_awareness": 50,
        "material_discipline": 50,
        "conversion_ability": 50,
        "defensive_resilience": 50,
        "blunder_frequency": 50,
    }


def test_individual_trait_functions_match_aggregate_output() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_moves=32,
            total_plies=64,
            label_counts={"good": 23, "inaccuracy": 5, "mistake": 3, "blunder": 1, "brilliant": 0},
            key_positions=[{"player": "White", "move_number": 26, "label": "mistake", "tactical_flag": "none", "material_change": -2}],
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert tactical_awareness(payloads) == scores["tactical_awareness"]
    assert material_discipline(payloads) == scores["material_discipline"]
    assert conversion_ability(payloads) == scores["conversion_ability"]
    assert defensive_resilience(payloads) == scores["defensive_resilience"]
    assert blunder_frequency(payloads) == scores["blunder_frequency"]


def test_scores_are_integers_and_clamped() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="0-1",
            total_moves=10,
            total_plies=20,
            label_counts={"good": 0, "inaccuracy": 0, "mistake": 3, "blunder": 7, "brilliant": 0},
            key_positions=[
                {"player": "White", "move_number": 7, "label": "blunder", "tactical_flag": "mate_threat", "material_change": -9},
                {"player": "White", "move_number": 8, "label": "blunder", "tactical_flag": "mate_threat", "material_change": -9},
            ],
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    for value in scores.values():
        assert isinstance(value, int)
        assert 0 <= value <= 100


def test_window_of_twenty_mixed_games_produces_non_perfect_scores() -> None:
    payloads: list[dict] = []
    for idx in range(20):
        is_win = idx % 3 == 0
        result = "1-0" if is_win else ("1/2-1/2" if idx % 2 == 0 else "0-1")
        label_counts = {
            "good": 26,
            "inaccuracy": 7,
            "mistake": 4,
            "blunder": 3,
            "brilliant": 0,
        }
        key_positions = [
            {"player": "White", "move_number": 32, "label": "mistake", "tactical_flag": "mate_threat", "material_change": -3},
            {"player": "White", "move_number": 36, "label": "blunder", "tactical_flag": "none", "material_change": -4},
        ]
        payloads.append(
            _payload(
                your_color="white",
                result=result,
                total_moves=40,
                total_plies=80,
                label_counts=label_counts,
                key_positions=key_positions,
            )
        )

    scores = compute_engine_trait_scores(payloads)
    assert scores["tactical_awareness"] < 100
    assert scores["material_discipline"] < 100
    assert scores["conversion_ability"] < 100
    assert scores["defensive_resilience"] < 100
    assert scores["blunder_frequency"] < 100


def test_adding_more_blunders_monotonically_decreases_window_scores() -> None:
    base_payloads: list[dict] = []
    for idx in range(20):
        result = "1-0" if idx % 2 == 0 else "1/2-1/2"
        base_payloads.append(
            _payload(
                your_color="white",
                result=result,
                total_moves=40,
                total_plies=80,
                label_counts={"good": 31, "inaccuracy": 5, "mistake": 3, "blunder": 1, "brilliant": 0},
                key_positions=[{"player": "White", "move_number": 35, "label": "mistake", "tactical_flag": "none", "material_change": -2}],
            )
        )

    noisier_payloads = list(base_payloads)
    for _ in range(5):
        noisier_payloads.append(
            _payload(
                your_color="white",
                result="0-1",
                total_moves=40,
                total_plies=80,
                label_counts={"good": 20, "inaccuracy": 6, "mistake": 7, "blunder": 7, "brilliant": 0},
                key_positions=[{"player": "White", "move_number": 34, "label": "blunder", "tactical_flag": "mate_threat", "material_change": -5}],
            )
        )

    base_scores = compute_engine_trait_scores(base_payloads)
    noisier_scores = compute_engine_trait_scores(noisier_payloads)

    assert noisier_scores["tactical_awareness"] < base_scores["tactical_awareness"]
    assert noisier_scores["material_discipline"] < base_scores["material_discipline"]
    assert noisier_scores["blunder_frequency"] < base_scores["blunder_frequency"]


def test_errors_present_prevent_perfect_hundred_scores() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_moves=1000,
            total_plies=2000,
            label_counts={"good": 999, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
            key_positions=[],
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert max(scores.values()) <= 95
    assert scores["tactical_awareness"] == 95
    assert scores["material_discipline"] == 95
    assert scores["conversion_ability"] == 95
    assert scores["blunder_frequency"] == 95


def test_low_volume_caps_scores_to_eighty() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_moves=40,
            total_plies=80,
            label_counts={"good": 40, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
            key_positions=[],
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert max(scores.values()) <= 80
    assert scores["tactical_awareness"] == 80
    assert scores["material_discipline"] == 80
    assert scores["conversion_ability"] == 80
    assert scores["blunder_frequency"] == 80


def test_strict_error_rate_cap_blocks_ninety_plus_scores() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_moves=1000,
            total_plies=2000,
            label_counts={"good": 947, "inaccuracy": 10, "mistake": 40, "blunder": 3, "brilliant": 0},
            key_positions=[],
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert scores["tactical_awareness"] < 90


def test_malformed_window_integrity_returns_neutral_scores(caplog) -> None:
    window = _WindowAggregates(
        payload_count=3,
        primary_games=5,
        total_moves=4,
    )

    with caplog.at_level("ERROR"):
        scores, components = _compute_window_scores(window)

    assert scores == {
        "tactical_awareness": 50,
        "material_discipline": 50,
        "conversion_ability": 50,
        "defensive_resilience": 50,
        "blunder_frequency": 50,
    }
    assert components["integrity_violation"] is True
    assert "Trait window integrity violation" in caplog.text


def test_tactical_awareness_is_low_with_high_blunder_rate() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="0-1",
            total_moves=100,
            total_plies=200,
            label_counts={"good": 65, "inaccuracy": 10, "mistake": 5, "blunder": 20, "brilliant": 0},
            key_positions=[],
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert scores["tactical_awareness"] <= 25


def test_tactical_awareness_is_mid_with_moderate_mistake_and_blunder_rates() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1/2-1/2",
            total_moves=100,
            total_plies=200,
            label_counts={"good": 82, "inaccuracy": 7, "mistake": 8, "blunder": 3, "brilliant": 0},
            key_positions=[],
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert 40 <= scores["tactical_awareness"] <= 70


def test_tactical_awareness_is_high_but_not_inflated_for_brilliant_only_payload() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_moves=120,
            total_plies=240,
            label_counts={"good": 117, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 3},
            key_positions=[],
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert 95 <= scores["tactical_awareness"] <= 100


def test_tactical_awareness_never_exceeds_guardrail_max_allowed_score() -> None:
    window = _WindowAggregates(
        payload_count=1,
        primary_games=1,
        total_moves=40,
        total_good=40,
        total_inaccuracy=0,
        total_mistake=0,
        total_blunder=0,
        total_brilliant=0,
        win_games=1,
        win_moves=40,
    )
    scores, components = _compute_window_scores(window)
    assert scores["tactical_awareness"] <= int(components["max_allowed_score"])
