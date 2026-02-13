from __future__ import annotations

from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION
from src.engine_traits import (
    _WindowAggregates,
    _compute_window_scores,
    blunder_frequency,
    compute_engine_trait_scores,
    conversion_ability,
    defensive_resilience,
    get_last_aggregate_components,
    material_discipline,
    tactical_awareness,
)


def _payload(
    *,
    your_color: str,
    result: str,
    total_plies: int,
    player_label_counts: dict[str, int],
    key_positions: list[dict],
    opponent_label_counts: dict[str, int] | None = None,
) -> dict:
    white_plies = (int(total_plies) + 1) // 2
    black_plies = int(total_plies) // 2
    expected_player_plies = int(white_plies if your_color == "white" else black_plies)
    assert sum(int(v) for v in player_label_counts.values()) == expected_player_plies

    if opponent_label_counts is None:
        expected_opponent_plies = int(total_plies) - expected_player_plies
        opponent_label_counts = {
            "good": expected_opponent_plies,
            "inaccuracy": 0,
            "mistake": 0,
            "blunder": 0,
            "brilliant": 0,
        }

    if your_color == "white":
        by_side = {
            "white": dict(player_label_counts),
            "black": dict(opponent_label_counts),
        }
    else:
        by_side = {
            "white": dict(opponent_label_counts),
            "black": dict(player_label_counts),
        }

    total_counts = {
        key: int(by_side["white"][key]) + int(by_side["black"][key])
        for key in ("good", "inaccuracy", "mistake", "blunder", "brilliant")
    }
    unlabeled_white_plies = max(0, int(white_plies) - sum(int(v) for v in by_side["white"].values()))
    unlabeled_black_plies = max(0, int(black_plies) - sum(int(v) for v in by_side["black"].values()))

    return {
        "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
        "game_summary": {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "your_color": your_color,
            "result": result,
            "total_moves": (int(total_plies) + 1) // 2,
            "total_plies": int(total_plies),
            "white_plies": int(white_plies),
            "black_plies": int(black_plies),
            "unlabeled_white_plies": int(unlabeled_white_plies),
            "unlabeled_black_plies": int(unlabeled_black_plies),
            "label_counts_total": dict(total_counts),
            "label_counts_white": dict(by_side["white"]),
            "label_counts_black": dict(by_side["black"]),
            "player_total_plies": int(expected_player_plies),
            "player_total_moves": int(expected_player_plies),
            "player_label_counts": dict(player_label_counts),
            "label_counts": dict(total_counts),
            "label_counts_by_side": dict(by_side),
        },
        "key_positions": key_positions,
    }


def test_invalid_payload_returns_neutral_scores() -> None:
    payloads = [{"game_summary": {"your_color": "white", "result": "1-0"}, "key_positions": []}]
    scores = compute_engine_trait_scores(payloads)
    assert scores == {
        "tactical_awareness": 50,
        "material_discipline": 50,
        "conversion_ability": 50,
        "defensive_resilience": 50,
        "blunder_frequency": 50,
    }


def test_v1_payload_is_rejected_for_trait_scoring() -> None:
    payloads = [
        {
            "game_summary": {
                # Missing schema_version => treated as schema v1.
                "your_color": "white",
                "result": "1-0",
                "total_plies": 40,
                "total_moves": 20,
                "label_counts": {"good": 34, "inaccuracy": 3, "mistake": 2, "blunder": 1, "brilliant": 0},
                "label_counts_by_side": {
                    "white": {"good": 17, "inaccuracy": 2, "mistake": 1, "blunder": 0, "brilliant": 0},
                    "black": {"good": 17, "inaccuracy": 1, "mistake": 1, "blunder": 1, "brilliant": 0},
                },
            },
            "key_positions": [],
        }
    ]
    scores = compute_engine_trait_scores(payloads)
    assert scores == {
        "tactical_awareness": 50,
        "material_discipline": 50,
        "conversion_ability": 50,
        "defensive_resilience": 50,
        "blunder_frequency": 50,
    }


def test_player_only_counts_ignore_opponent_side() -> None:
    base = _payload(
        your_color="white",
        result="1-0",
        total_plies=80,
        player_label_counts={"good": 28, "inaccuracy": 6, "mistake": 4, "blunder": 2, "brilliant": 0},
        key_positions=[],
        opponent_label_counts={"good": 40, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
    )
    noisy_opponent = _payload(
        your_color="white",
        result="1-0",
        total_plies=80,
        player_label_counts={"good": 28, "inaccuracy": 6, "mistake": 4, "blunder": 2, "brilliant": 0},
        key_positions=[],
        opponent_label_counts={"good": 5, "inaccuracy": 10, "mistake": 12, "blunder": 13, "brilliant": 0},
    )

    assert compute_engine_trait_scores([base]) == compute_engine_trait_scores([noisy_opponent])


def test_invalid_payload_is_excluded_without_overwriting_valid_scores() -> None:
    valid = _payload(
        your_color="white",
        result="1-0",
        total_plies=80,
        player_label_counts={"good": 32, "inaccuracy": 5, "mistake": 2, "blunder": 1, "brilliant": 0},
        key_positions=[],
    )
    invalid = {
        "game_summary": {"schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION, "your_color": "white", "result": "1-0"},
        "key_positions": [],
    }
    mixed_scores = compute_engine_trait_scores([valid, invalid])
    valid_scores = compute_engine_trait_scores([valid])
    assert mixed_scores == valid_scores


def test_regression_combined_side_counts_do_not_double_player_error_rates() -> None:
    payload = _payload(
        your_color="white",
        result="1-0",
        total_plies=80,
        player_label_counts={"good": 38, "inaccuracy": 2, "mistake": 0, "blunder": 0, "brilliant": 0},
        key_positions=[],
        opponent_label_counts={"good": 10, "inaccuracy": 10, "mistake": 10, "blunder": 10, "brilliant": 0},
    )
    scores = compute_engine_trait_scores([payload])
    assert scores["tactical_awareness"] >= 70
    assert scores["blunder_frequency"] >= 75


def test_bad_player_payload_produces_low_scores() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="0-1",
            total_plies=80,
            player_label_counts={"good": 18, "inaccuracy": 8, "mistake": 6, "blunder": 8, "brilliant": 0},
            key_positions=[
                {
                    "player": "White",
                    "move_number": 30,
                    "label": "blunder",
                    "tactical_flag": "mate_threat",
                    "material_change": -4,
                },
                {
                    "player": "White",
                    "move_number": 36,
                    "label": "mistake",
                    "tactical_flag": "mate_threat",
                    "material_change": -3,
                },
            ],
        )
    ]

    scores = compute_engine_trait_scores(payloads)
    assert scores["tactical_awareness"] <= 45
    assert scores["material_discipline"] <= 50
    assert scores["defensive_resilience"] <= 55
    assert scores["blunder_frequency"] <= 45
    assert scores["conversion_ability"] == 50


def test_average_player_payload_produces_mid_scores() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1/2-1/2",
            total_plies=80,
            player_label_counts={"good": 24, "inaccuracy": 8, "mistake": 5, "blunder": 2, "brilliant": 1},
            key_positions=[
                {"player": "White", "move_number": 28, "label": "mistake", "tactical_flag": "none", "material_change": -3},
                {"player": "White", "move_number": 34, "label": "mistake", "tactical_flag": "mate_threat", "material_change": -1},
            ],
        )
    ]

    scores = compute_engine_trait_scores(payloads)
    assert 35 <= scores["tactical_awareness"] <= 70
    assert 40 <= scores["material_discipline"] <= 75
    assert 40 <= scores["defensive_resilience"] <= 80
    assert 45 <= scores["blunder_frequency"] <= 85
    assert scores["conversion_ability"] == 50


def test_plausible_1100ish_player_rates_produce_believable_non_extreme_scores() -> None:
    payloads: list[dict] = []
    for idx in range(12):
        result = "1-0" if idx % 3 == 0 else "0-1" if idx % 3 == 1 else "1/2-1/2"
        payloads.append(
            _payload(
                your_color="white",
                result=result,
                total_plies=80,
                player_label_counts={"good": 28, "inaccuracy": 5, "mistake": 5, "blunder": 2, "brilliant": 0},
                key_positions=[
                    {"player": "White", "move_number": 12, "label": "good", "tactical_flag": "none", "material_change": 0},
                    {"player": "White", "move_number": 20, "label": "inaccuracy", "tactical_flag": "none", "material_change": 0},
                    {"player": "White", "move_number": 28, "label": "good", "tactical_flag": "mate_threat" if idx % 4 == 0 else "none", "material_change": 0},
                    {
                        "player": "White",
                        "move_number": 36,
                        "label": "mistake" if idx % 3 == 0 else "good",
                        "tactical_flag": "none",
                        "material_change": -3 if idx % 5 == 0 else 0,
                    },
                ],
            )
        )
    scores = compute_engine_trait_scores(payloads)
    for value in scores.values():
        assert 0 < value < 100
    assert 30 <= scores["tactical_awareness"] <= 75
    assert 45 <= scores["material_discipline"] <= 85
    assert 55 <= scores["blunder_frequency"] <= 90


def test_clean_games_produce_high_scores() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_plies=100,
            player_label_counts={"good": 46, "inaccuracy": 3, "mistake": 1, "blunder": 0, "brilliant": 0},
            key_positions=[{"player": "White", "move_number": 45, "label": "good", "tactical_flag": "none", "material_change": 0}],
        ),
        _payload(
            your_color="white",
            result="1/2-1/2",
            total_plies=96,
            player_label_counts={"good": 43, "inaccuracy": 4, "mistake": 1, "blunder": 0, "brilliant": 0},
            key_positions=[{"player": "White", "move_number": 40, "label": "good", "tactical_flag": "none", "material_change": 0}],
        ),
    ]

    scores = compute_engine_trait_scores(payloads)
    assert scores["tactical_awareness"] >= 75
    assert scores["material_discipline"] >= 80
    assert scores["conversion_ability"] >= 70
    assert scores["defensive_resilience"] >= 70
    assert scores["blunder_frequency"] >= 80


def test_conversion_ability_sane_for_club_player() -> None:
    window = _WindowAggregates(
        payload_count=1,
        primary_games=1,
        total_player_plies=100,
        total_good=100,
        total_inaccuracy=0,
        total_mistake=0,
        total_blunder=0,
        total_brilliant=0,
        win_games=1,
        win_player_plies=100,
        win_player_positions=20,
        win_late_error_events=2,
    )

    scores, _ = _compute_window_scores(window)
    assert 75 <= scores["conversion_ability"] <= 85


def test_conversion_ability_low_when_throwing_wins() -> None:
    window = _WindowAggregates(
        payload_count=1,
        primary_games=1,
        total_player_plies=100,
        total_good=100,
        total_inaccuracy=0,
        total_mistake=0,
        total_blunder=0,
        total_brilliant=0,
        win_games=1,
        win_player_plies=100,
        win_player_positions=20,
        win_late_error_events=7,
    )

    scores, _ = _compute_window_scores(window)
    assert scores["conversion_ability"] < 50


def test_conversion_never_negative() -> None:
    window = _WindowAggregates(
        payload_count=1,
        primary_games=1,
        total_player_plies=100,
        total_good=100,
        total_inaccuracy=0,
        total_mistake=0,
        total_blunder=0,
        total_brilliant=0,
        win_games=1,
        win_player_plies=100,
        win_player_positions=20,
        win_late_error_events=16,
    )

    scores, _ = _compute_window_scores(window)
    assert scores["conversion_ability"] == 0


def test_individual_trait_functions_match_aggregate_output() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_plies=64,
            player_label_counts={"good": 23, "inaccuracy": 5, "mistake": 3, "blunder": 1, "brilliant": 0},
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
            total_plies=20,
            player_label_counts={"good": 0, "inaccuracy": 0, "mistake": 3, "blunder": 7, "brilliant": 0},
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


def test_adding_more_blunders_monotonically_decreases_window_scores() -> None:
    base_payloads: list[dict] = []
    for idx in range(20):
        result = "1-0" if idx % 2 == 0 else "1/2-1/2"
        base_payloads.append(
            _payload(
                your_color="white",
                result=result,
                total_plies=80,
                player_label_counts={"good": 31, "inaccuracy": 5, "mistake": 3, "blunder": 1, "brilliant": 0},
                key_positions=[{"player": "White", "move_number": 35, "label": "mistake", "tactical_flag": "none", "material_change": -2}],
            )
        )

    noisier_payloads = list(base_payloads)
    for _ in range(5):
        noisier_payloads.append(
            _payload(
                your_color="white",
                result="0-1",
                total_plies=80,
                player_label_counts={"good": 20, "inaccuracy": 6, "mistake": 7, "blunder": 7, "brilliant": 0},
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
            total_plies=2000,
            player_label_counts={"good": 999, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
            key_positions=[],
            opponent_label_counts={"good": 1000, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert max(scores.values()) <= 95


def test_low_volume_caps_scores_to_eighty() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_plies=80,
            player_label_counts={"good": 40, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
            key_positions=[],
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert max(scores.values()) <= 80


def test_strict_error_rate_cap_blocks_ninety_plus_scores() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_plies=2000,
            player_label_counts={"good": 947, "inaccuracy": 10, "mistake": 40, "blunder": 3, "brilliant": 0},
            key_positions=[],
            opponent_label_counts={"good": 1000, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert scores["tactical_awareness"] <= 90


def test_malformed_window_integrity_returns_neutral_scores(caplog) -> None:
    window = _WindowAggregates(
        payload_count=3,
        primary_games=5,
        total_player_plies=4,
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


def test_tactical_awareness_is_high_for_nearly_clean_payload() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_plies=240,
            player_label_counts={"good": 117, "inaccuracy": 2, "mistake": 1, "blunder": 0, "brilliant": 0},
            key_positions=[],
        )
    ]
    scores = compute_engine_trait_scores(payloads)
    assert 85 <= scores["tactical_awareness"] <= 95


def test_tactical_awareness_never_exceeds_guardrail_max_allowed_score() -> None:
    window = _WindowAggregates(
        payload_count=1,
        primary_games=1,
        total_player_plies=40,
        total_good=40,
        total_inaccuracy=0,
        total_mistake=0,
        total_blunder=0,
        total_brilliant=0,
        win_games=1,
        win_player_plies=40,
    )
    scores, components = _compute_window_scores(window)
    assert scores["tactical_awareness"] <= int(components["max_allowed_score"])


def test_window_sanity_warning_detects_extreme_error_rates() -> None:
    payload = _payload(
        your_color="white",
        result="0-1",
        total_plies=200,
        player_label_counts={"good": 19, "inaccuracy": 30, "mistake": 20, "blunder": 31, "brilliant": 0},
        key_positions=[],
    )
    compute_engine_trait_scores([payload])
    components = get_last_aggregate_components()
    assert isinstance(components, dict)
    assert components["integrity_warning"] is True
    assert "non_good_rate_gt_0_75" in components["integrity_warning_reasons"]
    assert "blunder_rate_gt_0_30" in components["integrity_warning_reasons"]
    assert components["trait_update_refused"] is False


def test_sanity_warning_can_refuse_trait_update(monkeypatch) -> None:
    window = _WindowAggregates(
        payload_count=1,
        primary_games=10,
        total_player_plies=100,
        total_good=2,
        total_inaccuracy=50,
        total_mistake=30,
        total_blunder=25,
        total_brilliant=0,
    )
    monkeypatch.setenv("TRAITS_REFUSE_ON_SANITY", "1")
    scores, components = _compute_window_scores(window)
    assert components["integrity_warning"] is True
    assert components["trait_update_refused"] is True
    assert "player_label_sum_ne_player_total_plies" in components["integrity_warning_reasons"]
    assert "total_errors_gt_total_player_plies" in components["integrity_warning_reasons"]
    assert scores == {
        "tactical_awareness": 50,
        "material_discipline": 50,
        "conversion_ability": 50,
        "defensive_resilience": 50,
        "blunder_frequency": 50,
    }
