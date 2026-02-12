"""Deterministic trait scoring from validated engine payloads."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Sequence

from engine.payload_schema import EnginePayloadValidationResult, validate_engine_payload

NEUTRAL_SCORE = 50
LOW_VOLUME_MOVE_CAP = 50
LOW_VOLUME_SCORE_MAX = 80
ERROR_PRESENT_SCORE_MAX = 95
ERROR_RATE_STRICT_CAP_THRESHOLD = 0.02
ERROR_RATE_STRICT_CAP_MAX = 90
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _GameSignals:
    summary: Mapping[str, Any]
    player_plies: int
    player_label_counts: Mapping[str, int]
    key_positions_count: int
    player_key_positions_count: int
    severe_material_events: int
    mate_threat_events: int
    late_error_events: int
    is_win: bool
    is_non_win: bool


@dataclass
class _WindowAggregates:
    payload_count: int = 0
    primary_games: int = 0
    total_player_plies: int = 0
    total_good: int = 0
    total_inaccuracy: int = 0
    total_mistake: int = 0
    total_blunder: int = 0
    total_brilliant: int = 0
    total_player_positions: int = 0
    severe_material_events: int = 0
    mate_threat_events: int = 0
    win_games: int = 0
    win_player_plies: int = 0
    win_player_positions: int = 0
    win_late_error_events: int = 0
    non_win_games: int = 0
    non_win_player_plies: int = 0
    non_win_player_positions: int = 0
    non_win_mistakes: int = 0
    non_win_blunders: int = 0
    non_win_mate_threat_events: int = 0


def compute_engine_trait_scores(payloads: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """Compute deterministic trait scores from player-only move counts."""
    if not payloads:
        scores = _neutral_scores()
        if _traits_debug_enabled():
            _emit_empty_traits_debug(scores)
        return scores

    window = _WindowAggregates(payload_count=len(payloads))
    debug_rows: list[Dict[str, Any]] = []

    for idx, payload in enumerate(payloads, start=1):
        validation = validate_engine_payload(
            payload,
            require_schema_version=True,
            require_player_fields=True,
            require_key_positions=False,
        )
        if not validation.is_valid:
            logger.error(
                "Trait payload invalid at index=%s schema=%s errors=%s",
                int(idx),
                int(validation.schema_version),
                ",".join(validation.errors),
            )
            scores = _neutral_scores()
            if _traits_debug_enabled():
                _emit_invalid_payload_debug(idx, validation, scores)
            return scores

        signals = _collect_game_signals(payload, validation)
        _accumulate_window(window, signals)
        debug_rows.append(
            {
                "payload_index": idx,
                "schema_version": int(validation.schema_version),
                "total_plies": int(validation.total_plies),
                "total_moves": int(validation.total_moves),
                "player_total_plies": int(signals.player_plies),
                "player_label_counts": dict(signals.player_label_counts),
                "key_positions_count": signals.key_positions_count,
                "player_key_positions_count": signals.player_key_positions_count,
                "severe_material_events": signals.severe_material_events,
                "mate_threat_events": signals.mate_threat_events,
                "late_error_events": signals.late_error_events,
                "is_win": signals.is_win,
                "is_non_win": signals.is_non_win,
            }
        )

    scores, aggregate_components = _compute_window_scores(window)
    if _traits_debug_enabled():
        _emit_traits_debug(debug_rows, aggregate_components, scores)
    return scores


def tactical_awareness(payloads: Sequence[Mapping[str, Any]]) -> int:
    return int(compute_engine_trait_scores(payloads)["tactical_awareness"])


def material_discipline(payloads: Sequence[Mapping[str, Any]]) -> int:
    return int(compute_engine_trait_scores(payloads)["material_discipline"])


def conversion_ability(payloads: Sequence[Mapping[str, Any]]) -> int:
    return int(compute_engine_trait_scores(payloads)["conversion_ability"])


def defensive_resilience(payloads: Sequence[Mapping[str, Any]]) -> int:
    return int(compute_engine_trait_scores(payloads)["defensive_resilience"])


def blunder_frequency(payloads: Sequence[Mapping[str, Any]]) -> int:
    return int(compute_engine_trait_scores(payloads)["blunder_frequency"])


def _collect_game_signals(
    payload: Mapping[str, Any],
    validation: EnginePayloadValidationResult,
) -> _GameSignals:
    summary = _game_summary(payload)

    player_positions = list(_iter_player_positions(payload))
    key_positions_count = len(payload.get("key_positions") or [])
    player_key_positions_count = len(player_positions)

    severe_material_events = 0
    mate_threat_events = 0
    late_error_events = 0
    late_threshold = max(1, int(round(float(max(1, validation.player_total_plies)) * 0.7)))

    for position in player_positions:
        material_change = _as_int(position.get("material_change", 0))
        tactical_flag = str(position.get("tactical_flag", "")).strip().lower()
        label = str(position.get("label", "")).strip().lower()
        move_number = _as_int(position.get("move_number", 0))

        if material_change <= -3:
            severe_material_events += 1
        if tactical_flag == "mate_threat":
            mate_threat_events += 1
        if move_number >= late_threshold and label in {"mistake", "blunder"}:
            late_error_events += 1

    is_win = _is_player_win(summary)
    return _GameSignals(
        summary=summary,
        player_plies=max(1, int(validation.player_total_plies)),
        player_label_counts=dict(validation.player_label_counts),
        key_positions_count=key_positions_count,
        player_key_positions_count=player_key_positions_count,
        severe_material_events=severe_material_events,
        mate_threat_events=mate_threat_events,
        late_error_events=late_error_events,
        is_win=is_win,
        is_non_win=not is_win,
    )


def _accumulate_window(window: _WindowAggregates, signals: _GameSignals) -> None:
    plies = int(max(1, signals.player_plies))
    labels = signals.player_label_counts

    window.primary_games += 1
    window.total_player_plies += plies
    window.total_good += int(labels["good"])
    window.total_inaccuracy += int(labels["inaccuracy"])
    window.total_mistake += int(labels["mistake"])
    window.total_blunder += int(labels["blunder"])
    window.total_brilliant += int(labels["brilliant"])

    window.total_player_positions += int(signals.player_key_positions_count)
    window.severe_material_events += int(signals.severe_material_events)
    window.mate_threat_events += int(signals.mate_threat_events)

    if signals.is_win:
        window.win_games += 1
        window.win_player_plies += plies
        window.win_player_positions += int(signals.player_key_positions_count)
        window.win_late_error_events += int(signals.late_error_events)
    else:
        window.non_win_games += 1
        window.non_win_player_plies += plies
        window.non_win_player_positions += int(signals.player_key_positions_count)
        window.non_win_mistakes += int(labels["mistake"])
        window.non_win_blunders += int(labels["blunder"])
        window.non_win_mate_threat_events += int(signals.mate_threat_events)


def _compute_window_scores(window: _WindowAggregates) -> tuple[Dict[str, int], Dict[str, Any]]:
    try:
        assert int(window.total_player_plies) >= int(window.primary_games)
        labeled_total = int(
            window.total_good
            + window.total_inaccuracy
            + window.total_mistake
            + window.total_blunder
            + window.total_brilliant
        )
        assert labeled_total == int(window.total_player_plies)
    except AssertionError:
        logger.error(
            "Trait window integrity violation: total_player_plies=%s primary_games=%s",
            int(window.total_player_plies),
            int(window.primary_games),
        )
        scores = _neutral_scores()
        return scores, {
            "coverage": 0.0,
            "integrity_violation": True,
            "missing_primary_data": True,
            "primary_games": int(window.primary_games),
            "total_player_plies": int(window.total_player_plies),
            "total_errors": 0,
            "max_allowed_score": int(NEUTRAL_SCORE),
            "tactical_awareness_components": {"raw_before_clamp": float(NEUTRAL_SCORE)},
            "material_discipline_components": {"raw_before_clamp": float(NEUTRAL_SCORE)},
            "conversion_ability_components": {"raw_before_clamp": float(NEUTRAL_SCORE)},
            "defensive_resilience_components": {"raw_before_clamp": float(NEUTRAL_SCORE)},
            "blunder_frequency_components": {"raw_before_clamp": float(NEUTRAL_SCORE)},
        }

    if window.primary_games <= 0 or window.total_player_plies <= 0:
        scores = _neutral_scores()
        return scores, {
            "coverage": 0.0,
            "integrity_violation": False,
            "missing_primary_data": True,
            "primary_games": int(window.primary_games),
            "total_player_plies": int(window.total_player_plies),
            "total_errors": 0,
            "max_allowed_score": int(NEUTRAL_SCORE),
            "tactical_awareness_components": {"raw_before_clamp": float(NEUTRAL_SCORE)},
            "material_discipline_components": {"raw_before_clamp": float(NEUTRAL_SCORE)},
            "conversion_ability_components": {"raw_before_clamp": float(NEUTRAL_SCORE)},
            "defensive_resilience_components": {"raw_before_clamp": float(NEUTRAL_SCORE)},
            "blunder_frequency_components": {"raw_before_clamp": float(NEUTRAL_SCORE)},
        }

    total_player_plies = float(max(1, window.total_player_plies))
    inaccuracy_rate = float(window.total_inaccuracy) / total_player_plies
    mistake_rate = float(window.total_mistake) / total_player_plies
    blunder_rate = float(window.total_blunder) / total_player_plies
    non_good_rate = float(window.total_inaccuracy + window.total_mistake + window.total_blunder) / total_player_plies
    brilliant_rate = float(window.total_brilliant) / total_player_plies

    total_positions = float(max(1, window.total_player_positions))
    severe_material_rate = float(window.severe_material_events) / total_positions
    mate_threat_rate = float(window.mate_threat_events) / total_positions

    error_signal = (mistake_rate * 1.0) + (blunder_rate * 2.5)
    tactical_base = _score_from_error_rate(error_signal)
    tactical_raw = tactical_base + min(3.0, brilliant_rate * 100.0) - min(12.0, mate_threat_rate * 12.0)

    weighted_material_error = (blunder_rate * 1.8) + (mistake_rate * 0.4)
    material_base = _score_from_error_rate(weighted_material_error)
    material_raw = material_base - min(18.0, severe_material_rate * 12.0)

    if window.win_games <= 0 or window.win_player_positions <= 0:
        conversion_raw = float(NEUTRAL_SCORE)
        conversion_data = {
            "win_games": int(window.win_games),
            "win_player_positions": int(window.win_player_positions),
            "win_late_error_rate": None,
            "raw_before_clamp": conversion_raw,
        }
    else:
        win_late_error_rate = float(window.win_late_error_events) / float(max(1, window.win_player_positions))
        conversion_raw = _score_from_error_rate(win_late_error_rate * 2.0)
        conversion_data = {
            "win_games": int(window.win_games),
            "win_player_positions": int(window.win_player_positions),
            "win_late_error_rate": round(win_late_error_rate, 4),
            "raw_before_clamp": round(conversion_raw, 2),
        }

    if window.non_win_games <= 0 or window.non_win_player_plies <= 0:
        defensive_raw = float(NEUTRAL_SCORE)
        defensive_data = {
            "non_win_games": int(window.non_win_games),
            "non_win_player_plies": int(window.non_win_player_plies),
            "pressure_rate": None,
            "raw_before_clamp": defensive_raw,
        }
    else:
        pressure_rate = float(window.non_win_blunders + (0.5 * window.non_win_mistakes)) / float(
            max(1, window.non_win_player_plies)
        )
        non_win_mate_rate = float(window.non_win_mate_threat_events) / float(max(1, window.non_win_player_positions))
        defensive_base = _score_from_error_rate(pressure_rate)
        defensive_raw = defensive_base - min(20.0, non_win_mate_rate * 10.0)
        defensive_data = {
            "non_win_games": int(window.non_win_games),
            "non_win_player_plies": int(window.non_win_player_plies),
            "non_win_player_positions": int(window.non_win_player_positions),
            "pressure_rate": round(pressure_rate, 4),
            "non_win_mate_threat_rate": round(non_win_mate_rate, 4),
            "raw_before_clamp": round(defensive_raw, 2),
        }

    blunder_raw = _score_from_blunder_rate(blunder_rate)

    # Blend toward neutral when part of the window lacks required summary data.
    coverage = float(window.primary_games) / float(max(1, window.payload_count))
    tactical_raw = _coverage_blend(tactical_raw, coverage)
    material_raw = _coverage_blend(material_raw, coverage)
    conversion_raw = _coverage_blend(conversion_raw, coverage)
    defensive_raw = _coverage_blend(defensive_raw, coverage)
    blunder_raw = _coverage_blend(blunder_raw, coverage)

    scores = {
        "tactical_awareness": _clamp_score(tactical_raw),
        "material_discipline": _clamp_score(material_raw),
        "conversion_ability": _clamp_score(conversion_raw),
        "defensive_resilience": _clamp_score(defensive_raw),
        "blunder_frequency": _clamp_score(blunder_raw),
    }
    total_errors = int(window.total_inaccuracy + window.total_mistake + window.total_blunder)
    scores, guardrails = _apply_score_guardrails(
        scores,
        total_errors=total_errors,
        total_player_plies=int(window.total_player_plies),
    )

    components = {
        "coverage": round(coverage, 4),
        "integrity_violation": False,
        "missing_primary_data": bool(window.primary_games < window.payload_count),
        "primary_games": int(window.primary_games),
        "total_player_plies": int(window.total_player_plies),
        "total_errors": int(total_errors),
        "max_allowed_score": int(guardrails.get("max_allowed_score", 100)),
        "guardrails": guardrails,
        "window_totals": {
            "payload_count": int(window.payload_count),
            "primary_games": int(window.primary_games),
            "total_player_plies": int(window.total_player_plies),
            "total_good": int(window.total_good),
            "total_inaccuracy": int(window.total_inaccuracy),
            "total_mistake": int(window.total_mistake),
            "total_blunder": int(window.total_blunder),
            "total_brilliant": int(window.total_brilliant),
            "total_player_positions": int(window.total_player_positions),
            "severe_material_events": int(window.severe_material_events),
            "mate_threat_events": int(window.mate_threat_events),
            "win_games": int(window.win_games),
            "win_player_plies": int(window.win_player_plies),
            "win_player_positions": int(window.win_player_positions),
            "win_late_error_events": int(window.win_late_error_events),
            "non_win_games": int(window.non_win_games),
            "non_win_player_plies": int(window.non_win_player_plies),
            "non_win_player_positions": int(window.non_win_player_positions),
            "non_win_mistakes": int(window.non_win_mistakes),
            "non_win_blunders": int(window.non_win_blunders),
            "non_win_mate_threat_events": int(window.non_win_mate_threat_events),
        },
        "window_rates": {
            "inaccuracy_rate": round(inaccuracy_rate, 4),
            "mistake_rate": round(mistake_rate, 4),
            "blunder_rate": round(blunder_rate, 4),
            "non_good_rate": round(non_good_rate, 4),
            "brilliant_rate": round(brilliant_rate, 4),
        },
        "tactical_awareness_components": {
            "mistake_rate": round(mistake_rate, 4),
            "blunder_rate": round(blunder_rate, 4),
            "brilliant_rate": round(brilliant_rate, 4),
            "mate_threat_rate_per_position": round(mate_threat_rate, 4),
            "base_score": round(tactical_base, 2),
            "raw_before_clamp": round(tactical_raw, 2),
        },
        "material_discipline_components": {
            "weighted_error_rate": round(weighted_material_error, 4),
            "severe_material_rate_per_position": round(severe_material_rate, 4),
            "base_score": round(material_base, 2),
            "raw_before_clamp": round(material_raw, 2),
        },
        "conversion_ability_components": conversion_data,
        "defensive_resilience_components": defensive_data,
        "blunder_frequency_components": {
            "blunder_rate": round(blunder_rate, 4),
            "raw_before_clamp": round(blunder_raw, 2),
        },
    }
    return scores, components


def _apply_score_guardrails(
    scores: Mapping[str, int],
    *,
    total_errors: int,
    total_player_plies: int,
) -> tuple[Dict[str, int], Dict[str, Any]]:
    max_allowed = 100
    error_rate = float(total_errors) / float(max(1, total_player_plies))
    if int(total_errors) > 0:
        max_allowed = min(max_allowed, ERROR_PRESENT_SCORE_MAX)
    if int(total_errors) > 0 and float(error_rate) > ERROR_RATE_STRICT_CAP_THRESHOLD:
        max_allowed = min(max_allowed, ERROR_RATE_STRICT_CAP_MAX)
    if int(total_player_plies) < LOW_VOLUME_MOVE_CAP:
        max_allowed = min(max_allowed, LOW_VOLUME_SCORE_MAX)

    capped_scores: Dict[str, int] = {}
    for key, value in scores.items():
        capped_scores[str(key)] = int(min(max_allowed, int(value)))
    return capped_scores, {
        "total_errors": int(total_errors),
        "total_player_plies": int(total_player_plies),
        "error_rate": round(error_rate, 6),
        "error_rate_strict_cap_threshold": float(ERROR_RATE_STRICT_CAP_THRESHOLD),
        "low_volume_threshold_player_plies": int(LOW_VOLUME_MOVE_CAP),
        "error_cap_applied": bool(int(total_errors) > 0),
        "error_rate_strict_cap_applied": bool(int(total_errors) > 0 and float(error_rate) > ERROR_RATE_STRICT_CAP_THRESHOLD),
        "low_volume_cap_applied": bool(int(total_player_plies) < LOW_VOLUME_MOVE_CAP),
        "max_allowed_score": int(max_allowed),
    }


def _coverage_blend(raw_score: float, coverage: float) -> float:
    c = min(1.0, max(0.0, float(coverage)))
    return (float(raw_score) * c) + (float(NEUTRAL_SCORE) * (1.0 - c))


def _score_from_error_rate(rate: float) -> float:
    bounded = max(0.0, float(rate))
    # Calibration target for club-level (~1100) error rates.
    return 100.0 - (bounded * 200.0)


def _score_from_blunder_rate(rate: float) -> float:
    bounded = max(0.0, float(rate))
    # 0.00 -> 100, 0.10 -> 60, 0.20 -> 20.
    return 100.0 - (bounded * 400.0)


def _neutral_scores() -> Dict[str, int]:
    return {
        "tactical_awareness": NEUTRAL_SCORE,
        "material_discipline": NEUTRAL_SCORE,
        "conversion_ability": NEUTRAL_SCORE,
        "defensive_resilience": NEUTRAL_SCORE,
        "blunder_frequency": NEUTRAL_SCORE,
    }


def _emit_empty_traits_debug(scores: Mapping[str, int]) -> None:
    print(
        f"[traits-debug] {json.dumps({'aggregate': {'payload_count': 0, 'scores_after_clamp': dict(scores)}}, ensure_ascii=True, sort_keys=True)}",
        file=sys.stderr,
    )


def _emit_invalid_payload_debug(
    payload_index: int,
    validation: EnginePayloadValidationResult,
    scores: Mapping[str, int],
) -> None:
    print(
        f"[traits-debug] {json.dumps({'payload_index': int(payload_index), 'invalid_payload': True, 'schema_version': int(validation.schema_version), 'errors': list(validation.errors)}, ensure_ascii=True, sort_keys=True)}",
        file=sys.stderr,
    )
    print(
        f"[traits-debug] {json.dumps({'aggregate': {'payload_count': 1, 'scores_after_clamp': dict(scores), 'invalid_payload_fallback': True}}, ensure_ascii=True, sort_keys=True)}",
        file=sys.stderr,
    )


def _traits_debug_enabled() -> bool:
    return str(os.environ.get("TRAITS_DEBUG", "")).strip() == "1"


def _emit_traits_debug(
    debug_rows: Sequence[Mapping[str, Any]],
    aggregate_components: Mapping[str, Any],
    scores: Mapping[str, int],
) -> None:
    for row in debug_rows:
        print(f"[traits-debug] {json.dumps(dict(row), ensure_ascii=True, sort_keys=True)}", file=sys.stderr)

    aggregate = {"aggregate": dict(aggregate_components)}
    aggregate["aggregate"]["scores_after_clamp"] = dict(scores)
    print(f"[traits-debug] {json.dumps(aggregate, ensure_ascii=True, sort_keys=True)}", file=sys.stderr)


def _iter_player_positions(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    summary = _game_summary(payload)
    your_color_raw = str(summary.get("your_color", "")).strip().lower()
    expected_player: str | None = None
    if your_color_raw == "white":
        expected_player = "white"
    elif your_color_raw == "black":
        expected_player = "black"

    if expected_player is None:
        return

    for row in payload.get("key_positions") or []:
        if not isinstance(row, Mapping):
            continue
        player = str(row.get("player", "")).strip().lower()
        if player == expected_player:
            yield row


def _game_summary(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    summary = payload.get("game_summary")
    if isinstance(summary, Mapping):
        return summary
    return {}


def _is_player_win(summary: Mapping[str, Any]) -> bool:
    result = str(summary.get("result", "")).strip()
    color = str(summary.get("your_color", "")).strip().lower()
    if color == "white":
        return result == "1-0"
    if color == "black":
        return result == "0-1"
    return False


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _clamp_score(value: float) -> int:
    if value < 0:
        return 0
    if value > 100:
        return 100
    return int(round(value))
