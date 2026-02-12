from __future__ import annotations

from src.engine_traits import (
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
    total_moves: int,
    key_positions: list[dict],
) -> dict:
    return {
        "game_summary": {
            "your_color": your_color,
            "result": result,
            "total_moves": total_moves,
        },
        "key_positions": key_positions,
    }


def test_tactical_awareness_uses_deterministic_penalties() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_moves=30,
            key_positions=[
                {"player": "White", "label": "blunder", "tactical_flag": "hanging_piece"},
                {"player": "White", "label": "mistake", "tactical_flag": "tactical_miss"},
                {"player": "Black", "label": "blunder", "tactical_flag": "hanging_piece"},
            ],
        )
    ]

    # 100 - 8 (blunder) - 6 (hanging_piece) - 4 (tactical_miss) = 82
    assert tactical_awareness(payloads) == 82


def test_material_discipline_uses_negative_material_change_only() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_moves=30,
            key_positions=[
                {"player": "White", "material_change": -2},
                {"player": "White", "material_change": -3},
                {"player": "White", "material_change": 1},
                {"player": "Black", "material_change": -9},
            ],
        )
    ]

    # material_loss_total = 5 -> 100 - (5*3) = 85
    assert material_discipline(payloads) == 85


def test_conversion_ability_penalizes_non_win_after_early_plus_three() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_moves=30,
            key_positions=[{"player": "White", "move_number": 4, "material_change": 3}],
        ),
        _payload(
            your_color="white",
            result="1/2-1/2",
            total_moves=30,
            key_positions=[{"player": "White", "move_number": 5, "material_change": 4}],
        ),
        _payload(
            your_color="white",
            result="0-1",
            total_moves=30,
            key_positions=[{"player": "White", "move_number": 20, "material_change": 5}],
        ),
    ]

    # 2 opportunities (early +3+) and 1 conversion -> 50
    assert conversion_ability(payloads) == 50


def test_defensive_resilience_rewards_non_losses_under_material_pressure() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1/2-1/2",
            total_moves=40,
            key_positions=[
                {"player": "White", "material_change": -4},
            ],
        ),
        _payload(
            your_color="white",
            result="0-1",
            total_moves=40,
            key_positions=[
                {"player": "White", "material_change": -5},
            ],
        ),
    ]

    # 2 pressure games, 1 resilient game -> 50
    assert defensive_resilience(payloads) == 50


def test_blunder_frequency_transforms_ratio_to_zero_to_hundred() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="1-0",
            total_moves=20,
            key_positions=[
                {"player": "White", "label": "blunder"},
                {"player": "White", "label": "mistake"},
            ],
        ),
        _payload(
            your_color="white",
            result="0-1",
            total_moves=20,
            key_positions=[
                {"player": "White", "label": "blunder"},
                {"player": "Black", "label": "blunder"},
            ],
        ),
    ]

    # total_blunders = 2, total_moves = 40 -> 1 - 0.05 = 0.95 -> 95
    assert blunder_frequency(payloads) == 95


def test_scores_are_integers_and_clamped() -> None:
    payloads = [
        _payload(
            your_color="white",
            result="0-1",
            total_moves=1,
            key_positions=[
                {"player": "White", "label": "blunder", "tactical_flag": "hanging_piece", "material_change": -20},
                {"player": "White", "label": "blunder", "tactical_flag": "tactical_miss", "material_change": -20},
                {"player": "White", "label": "blunder", "tactical_flag": "hanging_piece", "material_change": -20},
            ],
        )
    ]

    scores = compute_engine_trait_scores(payloads)
    for value in scores.values():
        assert isinstance(value, int)
        assert 0 <= value <= 100

