from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def update_player_trait_state(
    current_trait: Mapping[str, Any],
    validated_trait_events: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    trait_severity_weight: float,
    game_id: int | str,
    *,
    trend_alpha: float = 0.25,
    confidence_alpha: float = 0.20,
    unseen_decay: float = 0.015,
    confidence_baseline: float = 0.30,
) -> dict[str, Any]:
    """
    Update one player-trait state from a single game's validated trait events.

    - trend_ema: signed EMA where negative means improving and positive means worsening.
    - confidence: bounded [0,1] estimate of recurring weakness likelihood for this trait.
    - trait_severity_weight: scales trend impact only; it does not affect confidence.
    """
    updated = dict(current_trait)
    trait_key = str(updated["trait_key"])

    trend = float(updated.get("trend_ema", 0.0))
    confidence = _clamp(float(updated.get("confidence", 0.0)), 0.0, 1.0)

    if isinstance(validated_trait_events, Mapping):
        events = validated_trait_events.get("events", [])
    else:
        events = list(validated_trait_events)

    relevant = [e for e in events if isinstance(e, Mapping) and e.get("trait_key") == trait_key]
    severity = max(0.0, float(trait_severity_weight))

    if relevant:
        weighted_signal_sum = 0.0
        total_signal_weight = 0.0
        worsening_sum = 0.0

        for e in relevant:
            direction = int(e["direction"])  # validated: -1/0/1
            base_weight = float(e["weight"])
            signal_weight = base_weight * severity

            weighted_signal_sum += direction * signal_weight
            total_signal_weight += base_weight

            if direction == 1:
                worsening_sum += base_weight

        game_signal = (weighted_signal_sum / total_signal_weight) if total_signal_weight > 0 else 0.0
        confidence_target = 1.0 - math.exp(-worsening_sum)

        trend = (1.0 - trend_alpha) * trend + trend_alpha * game_signal
        confidence = (1.0 - confidence_alpha) * confidence + confidence_alpha * confidence_target
        updated["last_seen_game_id"] = game_id
    else:
        trend = trend * (1.0 - unseen_decay)
        confidence = confidence * (1.0 - unseen_decay) + confidence_baseline * unseen_decay

    updated["trend_ema"] = _clamp(trend, -1.0, 1.0)
    updated["confidence"] = _clamp(confidence, 0.0, 1.0)
    return updated
