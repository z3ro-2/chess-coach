from src.traits.update_player_trait_state import update_player_trait_state


def _base_trait() -> dict:
    return {
        "trait_key": "opening_plan_clarity",
        "trend_ema": 0.0,
        "confidence": 0.2,
        "last_seen_game_id": None,
    }


def test_confidence_increases_with_worsening_and_scales_with_worsening_sum() -> None:
    current = _base_trait()

    low_worsening = {
        "events": [
            {
                "trait_key": "opening_plan_clarity",
                "direction": 1,
                "weight": 0.5,
                "confidence": 0.6,
                "phase": "opening",
                "note": "Allowed repeated opponent threats in the opening.",
            }
        ]
    }
    high_worsening = {
        "events": [
            {
                "trait_key": "opening_plan_clarity",
                "direction": 1,
                "weight": 2.0,
                "confidence": 0.6,
                "phase": "opening",
                "note": "Allowed repeated opponent threats in the opening.",
            }
        ]
    }

    low = update_player_trait_state(current, low_worsening, 1.0, 1, confidence_alpha=1.0)
    high = update_player_trait_state(current, high_worsening, 1.0, 1, confidence_alpha=1.0)

    assert low["confidence"] > current["confidence"]
    assert high["confidence"] > low["confidence"]


def test_confidence_does_not_increase_for_improving_only_events() -> None:
    current = {
        "trait_key": "opening_plan_clarity",
        "trend_ema": 0.1,
        "confidence": 0.7,
        "last_seen_game_id": None,
    }
    improving_only = {
        "events": [
            {
                "trait_key": "opening_plan_clarity",
                "direction": -1,
                "weight": 1.5,
                "confidence": 1.0,
                "phase": "opening",
                "note": "Recovered initiative with cleaner development choices.",
            }
        ]
    }

    updated = update_player_trait_state(current, improving_only, 1.0, 2)
    assert updated["confidence"] <= current["confidence"]


def test_severity_changes_trend_but_not_confidence() -> None:
    current = _base_trait()
    payload = {
        "events": [
            {
                "trait_key": "opening_plan_clarity",
                "direction": 1,
                "weight": 1.0,
                "confidence": 0.55,
                "phase": "opening",
                "note": "Delayed development and gave opponent easy play.",
            }
        ]
    }

    low_severity = update_player_trait_state(current, payload, 0.5, 3, trend_alpha=1.0, confidence_alpha=1.0)
    high_severity = update_player_trait_state(current, payload, 2.0, 3, trend_alpha=1.0, confidence_alpha=1.0)

    assert high_severity["trend_ema"] > low_severity["trend_ema"]
    assert high_severity["confidence"] == low_severity["confidence"]


def test_unseen_decay_moves_confidence_toward_baseline_slowly() -> None:
    state = {
        "trait_key": "opening_plan_clarity",
        "trend_ema": 0.4,
        "confidence": 0.8,
        "last_seen_game_id": 10,
    }

    for _ in range(5):
        state = update_player_trait_state(
            state,
            {"events": []},
            1.0,
            game_id=999,
            unseen_decay=0.015,
            confidence_baseline=0.30,
        )

    assert 0.30 < state["confidence"] < 0.8
