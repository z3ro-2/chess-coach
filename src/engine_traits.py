"""Deterministic trait scoring from validated engine payloads."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Sequence

from engine.payload_schema import EnginePayloadValidationResult, LABEL_KEYS, normalize_label_counts, validate_engine_payload

NEUTRAL_SCORE = 50
LOW_VOLUME_MOVE_CAP = 50
LOW_VOLUME_SCORE_MAX = 80
ERROR_PRESENT_SCORE_MAX = 95
ERROR_RATE_STRICT_CAP_THRESHOLD = 0.02
ERROR_RATE_STRICT_CAP_MAX = 90
PLAYER_PLY_COUNT_TOLERANCE = 1
SANITY_NON_GOOD_RATE_MAX = 0.75
SANITY_BLUNDER_RATE_MAX = 0.30
logger = logging.getLogger(__name__)
_LAST_AGGREGATE_SCORES_AFTER_CLAMP: Dict[str, int] | None = None
_LAST_AGGREGATE_COMPONENTS: Dict[str, Any] | None = None


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
    excluded_invalid_payloads: int = 0
    excluded_missing_primary_fields: int = 0
    excluded_integrity_payloads: int = 0
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
        window = _WindowAggregates(payload_count=0)
        scores, aggregate_components = _compute_window_scores(window)
        _set_last_aggregate_snapshot(scores=scores, aggregate_components=aggregate_components)
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
            window.excluded_invalid_payloads += 1
            debug_rows.append(
                {
                    "payload_index": int(idx),
                    "game_url": _extract_game_url(payload=payload, summary=_game_summary(payload)),
                    "schema_version": int(validation.schema_version),
                    "excluded": True,
                    "exclude_reason": "invalid_payload",
                    "errors": list(validation.errors),
                }
            )
            continue

        primary_projection = _extract_primary_projection(validation=validation, payload=payload)
        if primary_projection is None:
            logger.error(
                "Trait payload missing primary v2 fields at index=%s schema=%s",
                int(idx),
                int(validation.schema_version),
            )
            window.excluded_missing_primary_fields += 1
            debug_rows.append(
                {
                    "payload_index": int(idx),
                    "game_url": _extract_game_url(payload=payload, summary=_game_summary(payload)),
                    "schema_version": int(validation.schema_version),
                    "excluded": True,
                    "exclude_reason": "missing_primary_v2_fields",
                }
            )
            continue

        player_plies = int(primary_projection["player_plies"])
        player_label_counts = dict(primary_projection["player_label_counts"])
        labeled_total = int(sum(int(player_label_counts.get(key, 0)) for key in LABEL_KEYS))
        if int(player_plies) < 1:
            logger.error("Trait payload ignored due to player_plies<1 at index=%s", int(idx))
            window.excluded_integrity_payloads += 1
            debug_rows.append(
                {
                    "payload_index": int(idx),
                    "game_url": _extract_game_url(payload=payload, summary=_game_summary(payload)),
                    "schema_version": int(validation.schema_version),
                    "excluded": True,
                    "exclude_reason": "player_plies_lt_one",
                    "player_plies": int(player_plies),
                }
            )
            continue
        if int(labeled_total) > int(player_plies + PLAYER_PLY_COUNT_TOLERANCE):
            logger.error(
                "Trait payload ignored due to player label sum integrity violation index=%s player_plies=%s labeled_total=%s tolerance=%s",
                int(idx),
                int(player_plies),
                int(labeled_total),
                int(PLAYER_PLY_COUNT_TOLERANCE),
            )
            window.excluded_integrity_payloads += 1
            debug_rows.append(
                {
                    "payload_index": int(idx),
                    "game_url": _extract_game_url(payload=payload, summary=_game_summary(payload)),
                    "schema_version": int(validation.schema_version),
                    "excluded": True,
                    "exclude_reason": "player_label_sum_integrity_violation",
                    "player_plies": int(player_plies),
                    "player_label_sum": int(labeled_total),
                }
            )
            continue

        signals = _collect_game_signals(payload, player_plies=player_plies, player_label_counts=player_label_counts)
        _accumulate_window(window, signals)
        debug_rows.append(_build_payload_debug_row(idx=idx, payload=payload, validation=validation, signals=signals))

    scores, aggregate_components = _compute_window_scores(window)
    _set_last_aggregate_snapshot(scores=scores, aggregate_components=aggregate_components)
    if _traits_debug_enabled():
        _emit_traits_debug(debug_rows, aggregate_components, scores)
    return scores


def get_last_aggregate_scores_after_clamp() -> Dict[str, int] | None:
    if _LAST_AGGREGATE_SCORES_AFTER_CLAMP is None:
        return None
    return dict(_LAST_AGGREGATE_SCORES_AFTER_CLAMP)


def get_last_aggregate_components() -> Dict[str, Any] | None:
    if _LAST_AGGREGATE_COMPONENTS is None:
        return None
    return dict(_LAST_AGGREGATE_COMPONENTS)


def get_last_traits_debug_aggregate() -> Dict[str, Any] | None:
    scores = get_last_aggregate_scores_after_clamp()
    if scores is None:
        return None
    aggregate_components = get_last_aggregate_components()
    payload: Dict[str, Any] = {"scores_after_clamp": dict(scores)}
    if isinstance(aggregate_components, Mapping):
        payload["aggregate_components"] = dict(aggregate_components)
    return payload


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
    *,
    player_plies: int,
    player_label_counts: Mapping[str, int],
) -> _GameSignals:
    summary = _game_summary(payload)

    player_positions = list(_iter_player_positions(payload))
    key_positions_count = len(payload.get("key_positions") or [])
    player_key_positions_count = len(player_positions)

    severe_material_events = 0
    mate_threat_events = 0
    late_error_events = 0
    late_threshold = max(1, int(round(float(max(1, int(player_plies))) * 0.7)))

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
        player_plies=int(player_plies),
        player_label_counts=dict(player_label_counts),
        key_positions_count=key_positions_count,
        player_key_positions_count=player_key_positions_count,
        severe_material_events=severe_material_events,
        mate_threat_events=mate_threat_events,
        late_error_events=late_error_events,
        is_win=is_win,
        is_non_win=not is_win,
    )


def _build_payload_debug_row(
    *,
    idx: int,
    payload: Mapping[str, Any],
    validation: EnginePayloadValidationResult,
    signals: _GameSignals,
) -> Dict[str, Any]:
    your_color_raw = str(validation.your_color or signals.summary.get("your_color") or "").strip().lower()
    your_color = your_color_raw if your_color_raw in {"white", "black"} else None
    opponent_color = "black" if your_color == "white" else "white" if your_color == "black" else None
    label_counts = dict(validation.label_counts)
    label_counts_sum = int(sum(int(v) for v in label_counts.values()))
    player_label_counts = dict(signals.player_label_counts)
    player_label_sum = int(sum(int(v) for v in player_label_counts.values()))
    player_error_count = int(
        int(player_label_counts.get("inaccuracy", 0))
        + int(player_label_counts.get("mistake", 0))
        + int(player_label_counts.get("blunder", 0))
    )
    player_rates = _derive_player_rates(player_label_counts=player_label_counts, player_plies=int(signals.player_plies))

    return {
        "payload_index": idx,
        "game_url": _extract_game_url(payload=payload, summary=signals.summary),
        "your_color": your_color,
        "schema_version": int(validation.schema_version),
        "total_plies": int(validation.total_plies),
        "total_moves": int(validation.total_moves),
        "label_counts": label_counts,
        "label_counts_sum": int(label_counts_sum),
        "label_counts_to_total_moves_ratio": _ratio_or_none(label_counts_sum, int(validation.total_moves)),
        "label_counts_to_total_plies_ratio": _ratio_or_none(label_counts_sum, int(validation.total_plies)),
        "label_counts_by_side": {
            "white": dict(validation.label_counts_by_side.get("white", {})),
            "black": dict(validation.label_counts_by_side.get("black", {})),
        },
        "player_side_label_counts": (
            dict(validation.label_counts_by_side.get(your_color, {})) if your_color is not None else None
        ),
        "opponent_side_label_counts": (
            dict(validation.label_counts_by_side.get(opponent_color, {})) if opponent_color is not None else None
        ),
        "player_total_plies": int(signals.player_plies),
        "player_label_counts": player_label_counts,
        "player_label_sum": int(player_label_sum),
        "player_error_count": int(player_error_count),
        "player_inaccuracy_rate": player_rates.get("inaccuracy_rate"),
        "player_mistake_rate": player_rates.get("mistake_rate"),
        "player_blunder_rate": player_rates.get("blunder_rate"),
        "player_error_rate": player_rates.get("error_rate"),
        "key_positions_count": signals.key_positions_count,
        "player_key_positions_count": signals.player_key_positions_count,
        "severe_material_events": signals.severe_material_events,
        "mate_threat_events": signals.mate_threat_events,
        "late_error_events": signals.late_error_events,
        "is_win": signals.is_win,
        "is_non_win": signals.is_non_win,
    }


def _accumulate_window(window: _WindowAggregates, signals: _GameSignals) -> None:
    plies = int(signals.player_plies)
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
        allowed = int(window.total_player_plies + (max(0, window.primary_games) * PLAYER_PLY_COUNT_TOLERANCE))
        assert labeled_total <= int(allowed)
    except AssertionError:
        logger.error(
            "Trait window integrity violation: total_player_plies=%s primary_games=%s labeled_total=%s",
            int(window.total_player_plies),
            int(window.primary_games),
            int(
                window.total_good
                + window.total_inaccuracy
                + window.total_mistake
                + window.total_blunder
                + window.total_brilliant
            ),
        )
        scores = _neutral_scores()
        return scores, {
            "coverage": 0.0,
            "integrity_violation": True,
            "integrity_warning": True,
            "integrity_warning_reasons": ["window_integrity_violation"],
            "trait_update_refused": False,
            "missing_primary_data": True,
            "excluded_invalid_payloads": int(window.excluded_invalid_payloads),
            "excluded_missing_primary_fields": int(window.excluded_missing_primary_fields),
            "excluded_integrity_payloads": int(window.excluded_integrity_payloads),
            "primary_games": int(window.primary_games),
            "total_player_plies": int(window.total_player_plies),
            "player_label_sum": int(
                window.total_good
                + window.total_inaccuracy
                + window.total_mistake
                + window.total_blunder
                + window.total_brilliant
            ),
            "total_errors": 0,
            "max_allowed_score": int(NEUTRAL_SCORE),
            "guardrails": {"max_allowed_score": int(NEUTRAL_SCORE)},
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
            "integrity_warning": False,
            "integrity_warning_reasons": [],
            "trait_update_refused": False,
            "missing_primary_data": True,
            "excluded_invalid_payloads": int(window.excluded_invalid_payloads),
            "excluded_missing_primary_fields": int(window.excluded_missing_primary_fields),
            "excluded_integrity_payloads": int(window.excluded_integrity_payloads),
            "primary_games": int(window.primary_games),
            "total_player_plies": int(window.total_player_plies),
            "player_label_sum": 0,
            "total_errors": 0,
            "max_allowed_score": int(NEUTRAL_SCORE),
            "guardrails": {"max_allowed_score": int(NEUTRAL_SCORE)},
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
    tactical_brilliant_bonus = min(3.0, brilliant_rate * 100.0)
    tactical_mate_penalty = min(12.0, mate_threat_rate * 12.0)
    tactical_raw = tactical_base + tactical_brilliant_bonus - tactical_mate_penalty

    weighted_material_error = (blunder_rate * 1.8) + (mistake_rate * 0.4)
    material_base = _score_from_error_rate(weighted_material_error)
    material_severe_penalty = min(18.0, severe_material_rate * 12.0)
    material_raw = material_base - material_severe_penalty

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
        # Club-level conversion slope:
        # 0.05 late error -> 90
        # 0.10 late error -> 80
        # 0.20 late error -> 60
        # 0.30 late error -> 40
        # 0.50 late error -> 0
        conversion_raw = 100.0 - (win_late_error_rate * 200.0)
        if conversion_raw < 0.0:
            conversion_raw = 0.0
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
        defensive_mate_penalty = min(20.0, non_win_mate_rate * 10.0)
        defensive_raw = defensive_base - defensive_mate_penalty
        defensive_data = {
            "non_win_games": int(window.non_win_games),
            "non_win_player_plies": int(window.non_win_player_plies),
            "non_win_player_positions": int(window.non_win_player_positions),
            "pressure_rate": round(pressure_rate, 4),
            "non_win_mate_threat_rate": round(non_win_mate_rate, 4),
            "non_win_mate_threat_penalty": round(defensive_mate_penalty, 2),
            "base_score": round(defensive_base, 2),
            "raw_before_clamp": round(defensive_raw, 2),
        }

    blunder_raw = _score_from_blunder_rate(blunder_rate)

    # Exclude invalid/missing payloads entirely; keep coverage only for downstream confidence.
    coverage = float(window.primary_games) / float(max(1, window.payload_count))

    scores = {
        "tactical_awareness": _clamp_score(tactical_raw),
        "material_discipline": _clamp_score(material_raw),
        "conversion_ability": _clamp_score(conversion_raw),
        "defensive_resilience": _clamp_score(defensive_raw),
        "blunder_frequency": _clamp_score(blunder_raw),
    }
    labeled_total = int(
        window.total_good
        + window.total_inaccuracy
        + window.total_mistake
        + window.total_blunder
        + window.total_brilliant
    )
    total_errors = int(window.total_inaccuracy + window.total_mistake + window.total_blunder)
    sanity_warning_reasons = _window_sanity_warning_reasons(
        non_good_rate=non_good_rate,
        blunder_rate=blunder_rate,
        player_label_sum=labeled_total,
        player_total_plies=int(window.total_player_plies),
        total_errors=total_errors,
    )
    integrity_warning = bool(sanity_warning_reasons)
    refuse_update = bool(integrity_warning and _refuse_trait_update_on_sanity_enabled())
    if integrity_warning:
        logger.warning(
            "Trait window sanity warning: reasons=%s non_good_rate=%.4f blunder_rate=%.4f player_label_sum=%s total_player_plies=%s total_errors=%s",
            ",".join(sanity_warning_reasons),
            float(non_good_rate),
            float(blunder_rate),
            int(labeled_total),
            int(window.total_player_plies),
            int(total_errors),
        )
    if refuse_update:
        scores = _neutral_scores()
        guardrails = {
            "total_errors": int(total_errors),
            "total_player_plies": int(window.total_player_plies),
            "error_rate": round(float(total_errors) / float(max(1, int(window.total_player_plies))), 6),
            "error_rate_strict_cap_threshold": float(ERROR_RATE_STRICT_CAP_THRESHOLD),
            "low_volume_threshold_player_plies": int(LOW_VOLUME_MOVE_CAP),
            "error_cap_applied": False,
            "error_rate_strict_cap_applied": False,
            "low_volume_cap_applied": False,
            "max_allowed_score": int(NEUTRAL_SCORE),
            "sanity_refusal_applied": True,
        }
    else:
        scores, guardrails = _apply_score_guardrails(
            scores,
            total_errors=total_errors,
            total_player_plies=int(window.total_player_plies),
        )

    components = {
        "coverage": round(coverage, 4),
        "integrity_violation": False,
        "integrity_warning": bool(integrity_warning),
        "integrity_warning_reasons": list(sanity_warning_reasons),
        "trait_update_refused": bool(refuse_update),
        "missing_primary_data": bool(window.primary_games < window.payload_count),
        "excluded_invalid_payloads": int(window.excluded_invalid_payloads),
        "excluded_missing_primary_fields": int(window.excluded_missing_primary_fields),
        "excluded_integrity_payloads": int(window.excluded_integrity_payloads),
        "primary_games": int(window.primary_games),
        "total_player_plies": int(window.total_player_plies),
        "player_label_sum": int(labeled_total),
        "total_errors": int(total_errors),
        "max_allowed_score": int(guardrails.get("max_allowed_score", 100)),
        "guardrails": guardrails,
        "window_totals": {
            "payload_count": int(window.payload_count),
            "primary_games": int(window.primary_games),
            "excluded_invalid_payloads": int(window.excluded_invalid_payloads),
            "excluded_missing_primary_fields": int(window.excluded_missing_primary_fields),
            "excluded_integrity_payloads": int(window.excluded_integrity_payloads),
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
            "brilliant_bonus": round(tactical_brilliant_bonus, 2),
            "mate_threat_penalty": round(tactical_mate_penalty, 2),
            "base_score": round(tactical_base, 2),
            "raw_before_clamp": round(tactical_raw, 2),
        },
        "material_discipline_components": {
            "weighted_error_rate": round(weighted_material_error, 4),
            "severe_material_rate_per_position": round(severe_material_rate, 4),
            "severe_material_penalty": round(material_severe_penalty, 2),
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


def _window_sanity_warning_reasons(
    *,
    non_good_rate: float,
    blunder_rate: float,
    player_label_sum: int,
    player_total_plies: int,
    total_errors: int,
) -> list[str]:
    reasons: list[str] = []
    if float(non_good_rate) > SANITY_NON_GOOD_RATE_MAX:
        reasons.append("non_good_rate_gt_0_75")
    if float(blunder_rate) > SANITY_BLUNDER_RATE_MAX:
        reasons.append("blunder_rate_gt_0_30")
    if int(player_label_sum) != int(player_total_plies):
        reasons.append("player_label_sum_ne_player_total_plies")
    if int(total_errors) > int(player_total_plies):
        reasons.append("total_errors_gt_total_player_plies")
    return reasons


def _refuse_trait_update_on_sanity_enabled() -> bool:
    return str(os.environ.get("TRAITS_REFUSE_ON_SANITY", "")).strip() == "1"


def _coverage_blend(raw_score: float, coverage: float) -> float:
    c = min(1.0, max(0.0, float(coverage)))
    return (float(raw_score) * c) + (float(NEUTRAL_SCORE) * (1.0 - c))


def _score_from_error_rate(rate: float) -> float:
    bounded = max(0.0, float(rate))
    # Monotonic piecewise calibration for club-level (900-1400) player-only error rates.
    # Anchors: 0.00->100, 0.10->80, 0.20->60, 0.30->40, 0.40->20, 0.50->0.
    return _piecewise_linear_score(
        bounded,
        (
            (0.00, 100.0),
            (0.10, 80.0),
            (0.20, 60.0),
            (0.30, 40.0),
            (0.40, 20.0),
            (0.50, 0.0),
        ),
    )


def _score_from_blunder_rate(rate: float) -> float:
    bounded = max(0.0, float(rate))
    # Gentler monotonic curve for club-level blunder rates to avoid extreme saturation.
    return _piecewise_linear_score(
        bounded,
        (
            (0.00, 100.0),
            (0.03, 92.0),
            (0.06, 84.0),
            (0.10, 72.0),
            (0.15, 56.0),
            (0.20, 40.0),
            (0.30, 16.0),
            (0.40, 0.0),
        ),
    )


def _piecewise_linear_score(x: float, points: Sequence[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation over monotonic x breakpoints."""
    if not points:
        return 0.0
    px = float(x)
    ordered = list(points)
    if px <= float(ordered[0][0]):
        return float(ordered[0][1])
    for i in range(1, len(ordered)):
        x0, y0 = ordered[i - 1]
        x1, y1 = ordered[i]
        fx0 = float(x0)
        fx1 = float(x1)
        if px <= fx1:
            if fx1 <= fx0:
                return float(y1)
            t = (px - fx0) / (fx1 - fx0)
            return float(y0) + (float(y1) - float(y0)) * t
    return float(ordered[-1][1])


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


def _extract_primary_projection(
    *,
    validation: EnginePayloadValidationResult,
    payload: Mapping[str, Any],
) -> Dict[str, Any] | None:
    summary = _game_summary(payload)
    your_color = str(summary.get("your_color", "")).strip().lower()
    if your_color not in {"white", "black"}:
        return None

    white_plies = _as_int(summary.get("white_plies", -1))
    black_plies = _as_int(summary.get("black_plies", -1))
    if white_plies < 0 or black_plies < 0:
        return None
    if int(white_plies + black_plies) != int(validation.total_plies):
        return None

    white_counts = normalize_label_counts(summary.get("label_counts_white"))
    black_counts = normalize_label_counts(summary.get("label_counts_black"))
    if white_counts is None or black_counts is None:
        return None

    player_counts = dict(white_counts if your_color == "white" else black_counts)
    player_plies = int(white_plies if your_color == "white" else black_plies)
    return {
        "player_plies": int(player_plies),
        "player_label_counts": player_counts,
    }


def _derive_player_rates(
    *,
    player_label_counts: Mapping[str, int],
    player_plies: int,
) -> Dict[str, float]:
    denom = float(max(1, int(player_plies)))
    inaccuracy = int(player_label_counts.get("inaccuracy", 0))
    mistake = int(player_label_counts.get("mistake", 0))
    blunder = int(player_label_counts.get("blunder", 0))
    error_total = int(inaccuracy + mistake + blunder)
    return {
        "inaccuracy_rate": round(float(inaccuracy) / denom, 4),
        "mistake_rate": round(float(mistake) / denom, 4),
        "blunder_rate": round(float(blunder) / denom, 4),
        "error_rate": round(float(error_total) / denom, 4),
    }


def _is_player_win(summary: Mapping[str, Any]) -> bool:
    result = str(summary.get("result", "")).strip()
    color = str(summary.get("your_color", "")).strip().lower()
    if color == "white":
        return result == "1-0"
    if color == "black":
        return result == "0-1"
    return False


def _extract_game_url(*, payload: Mapping[str, Any], summary: Mapping[str, Any]) -> str | None:
    for key in ("game_url", "url"):
        value = summary.get(key)
        if value is None:
            value = payload.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _ratio_or_none(numerator: int, denominator: int) -> float | None:
    if int(denominator) <= 0:
        return None
    return round(float(numerator) / float(denominator), 4)


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


def _set_last_aggregate_scores_after_clamp(scores: Mapping[str, Any]) -> None:
    global _LAST_AGGREGATE_SCORES_AFTER_CLAMP
    _LAST_AGGREGATE_SCORES_AFTER_CLAMP = {str(k): int(v) for k, v in dict(scores).items()}


def _set_last_aggregate_components(aggregate_components: Mapping[str, Any]) -> None:
    global _LAST_AGGREGATE_COMPONENTS
    _LAST_AGGREGATE_COMPONENTS = dict(aggregate_components)


def _set_last_aggregate_snapshot(*, scores: Mapping[str, Any], aggregate_components: Mapping[str, Any]) -> None:
    _set_last_aggregate_scores_after_clamp(scores)
    _set_last_aggregate_components(aggregate_components)
