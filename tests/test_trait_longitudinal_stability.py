from __future__ import annotations

from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION, LABEL_KEYS
from src.engine_traits import compute_engine_trait_scores

TRAIT_KEYS: tuple[str, ...] = (
    "tactical_awareness",
    "material_discipline",
    "conversion_ability",
    "defensive_resilience",
    "blunder_frequency",
)


def _phase_error_rates(game_number: int) -> tuple[float, float, float]:
    # Inject known player-only error-rate regimes across 200 games.
    if game_number <= 70:
        base_inaccuracy, base_mistake, base_blunder = 0.13, 0.09, 0.05
    elif game_number <= 140:
        base_inaccuracy, base_mistake, base_blunder = 0.16, 0.12, 0.08
    else:
        base_inaccuracy, base_mistake, base_blunder = 0.12, 0.08, 0.04

    jitter_cycle = (-0.01, 0.0, 0.01, 0.0, -0.005)
    jitter = float(jitter_cycle[(game_number - 1) % len(jitter_cycle)])
    inaccuracy = max(0.0, base_inaccuracy + jitter)
    mistake = max(0.0, base_mistake + (jitter * 0.5))
    blunder = max(0.0, base_blunder + (jitter * 0.3))
    return inaccuracy, mistake, blunder


def _build_payload(game_number: int, *, player_plies: int = 40) -> dict:
    inaccuracy_rate, mistake_rate, blunder_rate = _phase_error_rates(game_number)
    inaccuracy = int(round(float(inaccuracy_rate) * float(player_plies)))
    mistake = int(round(float(mistake_rate) * float(player_plies)))
    blunder = int(round(float(blunder_rate) * float(player_plies)))
    brilliant = 0
    good = int(player_plies) - int(inaccuracy) - int(mistake) - int(blunder) - int(brilliant)
    if good < 0:
        inaccuracy = max(0, int(inaccuracy) + int(good))
        good = int(player_plies) - int(inaccuracy) - int(mistake) - int(blunder) - int(brilliant)
    assert good >= 0

    result_cycle = ("1-0", "0-1", "1/2-1/2", "0-1", "1-0")
    result = str(result_cycle[(game_number - 1) % len(result_cycle)])
    # Black is the modeled player. In black wins, inject one late error in the worse regime.
    late_error_count = 1 if (result == "0-1" and game_number > 70 and game_number <= 140) else 0

    player_counts = {
        "good": int(good),
        "inaccuracy": int(inaccuracy),
        "mistake": int(mistake),
        "blunder": int(blunder),
        "brilliant": int(brilliant),
    }
    opponent_counts = {
        "good": int(player_plies),
        "inaccuracy": 0,
        "mistake": 0,
        "blunder": 0,
        "brilliant": 0,
    }
    side_counts = {
        "white": dict(opponent_counts),
        "black": dict(player_counts),
    }
    merged = {key: int(side_counts["white"][key]) + int(side_counts["black"][key]) for key in LABEL_KEYS}

    key_positions = [
        {
            "player": "Black",
            "move_number": 10,
            "label": "inaccuracy" if int(inaccuracy) > 0 else "good",
            "tactical_flag": "none",
            "material_change": 0,
        },
        {
            "player": "Black",
            "move_number": 20,
            "label": "mistake" if int(mistake) > 0 else "good",
            "tactical_flag": "none",
            "material_change": -1 if int(mistake) > 0 else 0,
        },
        {
            "player": "Black",
            "move_number": 32,
            "label": "mistake" if int(late_error_count) >= 1 else "good",
            "tactical_flag": "none",
            "material_change": 0,
        },
        {
            "player": "Black",
            "move_number": 40,
            "label": "blunder" if int(blunder) > 0 else "good",
            "tactical_flag": "none",
            "material_change": -3 if int(blunder) > 0 else 0,
        },
    ]

    return {
        "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
        "game_summary": {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "your_color": "black",
            "result": str(result),
            "total_plies": int(player_plies) * 2,
            "total_moves": int(player_plies),
            "white_plies": int(player_plies),
            "black_plies": int(player_plies),
            "unlabeled_white_plies": 0,
            "unlabeled_black_plies": 0,
            "label_counts_total": dict(merged),
            "label_counts_white": dict(side_counts["white"]),
            "label_counts_black": dict(side_counts["black"]),
            "player_total_plies": int(player_plies),
            "player_total_moves": int(player_plies),
            "player_label_counts": dict(player_counts),
            "label_counts_by_side": side_counts,
            "label_counts": dict(merged),
        },
        "key_positions": key_positions,
    }


def _synthetic_rolling_series(*, games: int = 200, window_size: int = 20) -> list[tuple[int, dict[str, int]]]:
    payloads = [_build_payload(game_number) for game_number in range(1, int(games) + 1)]
    series: list[tuple[int, dict[str, int]]] = []
    for end_game in range(int(window_size), int(games) + 1):
        window = payloads[end_game - int(window_size) : end_game]
        scores = compute_engine_trait_scores(window)
        series.append((int(end_game), dict(scores)))
    return series


def _segment_values(
    series: list[tuple[int, dict[str, int]]],
    *,
    trait: str,
    start_end_game: int,
    end_end_game: int,
) -> list[int]:
    values = [
        int(scores[trait])
        for end_game, scores in series
        if int(start_end_game) <= int(end_game) <= int(end_end_game)
    ]
    assert values
    return values


def test_rolling_200_game_simulation_avoids_wild_trait_jumps() -> None:
    series = _synthetic_rolling_series(games=200, window_size=20)
    assert len(series) == 181

    for trait in TRAIT_KEYS:
        step_deltas = [
            abs(int(series[idx + 1][1][trait]) - int(series[idx][1][trait]))
            for idx in range(len(series) - 1)
        ]
        # Longitudinal safety lock: traits must evolve gradually, not jump by 40.
        assert max(step_deltas) < 40, (trait, max(step_deltas))


def test_rolling_200_game_simulation_stabilizes_in_regimes() -> None:
    series = _synthetic_rolling_series(games=200, window_size=20)

    for trait in TRAIT_KEYS:
        early = _segment_values(series, trait=trait, start_end_game=50, end_end_game=70)
        middle = _segment_values(series, trait=trait, start_end_game=120, end_end_game=140)
        late = _segment_values(series, trait=trait, start_end_game=180, end_end_game=200)
        # Inside a stable regime, scores should not oscillate wildly.
        assert (max(early) - min(early)) <= 20, (trait, "early", min(early), max(early))
        assert (max(middle) - min(middle)) <= 20, (trait, "middle", min(middle), max(middle))
        assert (max(late) - min(late)) <= 20, (trait, "late", min(late), max(late))

    # Directional sanity: as injected rates worsen then recover, core traits follow smoothly.
    early_tactical = _segment_values(series, trait="tactical_awareness", start_end_game=50, end_end_game=70)
    middle_tactical = _segment_values(series, trait="tactical_awareness", start_end_game=120, end_end_game=140)
    late_tactical = _segment_values(series, trait="tactical_awareness", start_end_game=180, end_end_game=200)
    assert sum(middle_tactical) / float(len(middle_tactical)) < sum(early_tactical) / float(len(early_tactical))
    assert sum(late_tactical) / float(len(late_tactical)) > sum(middle_tactical) / float(len(middle_tactical))
