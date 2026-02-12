"""Deterministic trait scoring from engine payloads."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Sequence

NEUTRAL_SCORE = 50
_LABEL_KEYS = ("good", "inaccuracy", "mistake", "blunder", "brilliant")


@dataclass(frozen=True)
class _GameSignals:
    summary: Mapping[str, Any]
    moves: int | None
    label_counts: Mapping[str, int] | None
    inaccuracy_rate: float | None
    mistake_rate: float | None
    blunder_rate: float | None
    non_good_rate: float | None
    brilliant_rate: float | None
    key_positions_count: int
    player_key_positions_count: int
    severe_material_events: int
    mate_threat_events: int
    late_error_events: int
    is_win: bool
    is_loss: bool


def compute_engine_trait_scores(payloads: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """Compute all deterministic engine-trait scores."""
    if not payloads:
        scores = _neutral_scores()
        if _traits_debug_enabled():
            _emit_empty_traits_debug(scores)
        return scores

    tactical_scores: list[int] = []
    material_scores: list[int] = []
    conversion_scores: list[int] = []
    defensive_scores: list[int] = []
    blunder_scores: list[int] = []
    debug_rows: list[Dict[str, Any]] = []

    for idx, payload in enumerate(payloads, start=1):
        signals = _collect_game_signals(payload)

        tactical_score, tactical_components = _tactical_awareness_components(signals)
        material_score, material_components = _material_discipline_components(signals)
        conversion_score, conversion_components = _conversion_ability_components(signals)
        defensive_score, defensive_components = _defensive_resilience_components(signals)
        blunder_score, blunder_components = _blunder_frequency_components(signals)

        tactical_scores.append(tactical_score)
        material_scores.append(material_score)
        conversion_scores.append(conversion_score)
        defensive_scores.append(defensive_score)
        blunder_scores.append(blunder_score)

        debug_rows.append(
            {
                "payload_index": idx,
                "total_plies": _as_int(signals.summary.get("total_plies", 0)),
                "total_moves": int(signals.moves or 0),
                "label_counts": dict(signals.label_counts or {}),
                "key_positions_count": signals.key_positions_count,
                "player_key_positions_count": signals.player_key_positions_count,
                "tactical_awareness_components": tactical_components,
                "material_discipline_components": material_components,
                "conversion_ability_components": conversion_components,
                "defensive_resilience_components": defensive_components,
                "blunder_frequency_components": blunder_components,
            }
        )

    scores = {
        "tactical_awareness": _aggregate_scores(tactical_scores),
        "material_discipline": _aggregate_scores(material_scores),
        "conversion_ability": _aggregate_scores(conversion_scores),
        "defensive_resilience": _aggregate_scores(defensive_scores),
        "blunder_frequency": _aggregate_scores(blunder_scores),
    }
    if _traits_debug_enabled():
        _emit_traits_debug(debug_rows, scores)
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


def _collect_game_signals(payload: Mapping[str, Any]) -> _GameSignals:
    summary = _game_summary(payload)
    moves = _effective_moves(summary)
    label_counts = _normalized_label_counts(summary)

    if moves is not None and label_counts is not None:
        denom = float(max(1, moves))
        inaccuracy_rate = float(label_counts["inaccuracy"]) / denom
        mistake_rate = float(label_counts["mistake"]) / denom
        blunder_rate = float(label_counts["blunder"]) / denom
        non_good_rate = float(label_counts["inaccuracy"] + label_counts["mistake"] + label_counts["blunder"]) / denom
        brilliant_rate = float(label_counts["brilliant"]) / denom
    else:
        inaccuracy_rate = None
        mistake_rate = None
        blunder_rate = None
        non_good_rate = None
        brilliant_rate = None

    player_positions = list(_iter_player_positions(payload))
    key_positions_count = len(payload.get("key_positions") or [])
    player_key_positions_count = len(player_positions)
    severe_material_events = 0
    mate_threat_events = 0
    late_error_events = 0
    late_threshold = max(1, int((moves or 0) * 0.7))

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

    return _GameSignals(
        summary=summary,
        moves=moves,
        label_counts=label_counts,
        inaccuracy_rate=inaccuracy_rate,
        mistake_rate=mistake_rate,
        blunder_rate=blunder_rate,
        non_good_rate=non_good_rate,
        brilliant_rate=brilliant_rate,
        key_positions_count=key_positions_count,
        player_key_positions_count=player_key_positions_count,
        severe_material_events=severe_material_events,
        mate_threat_events=mate_threat_events,
        late_error_events=late_error_events,
        is_win=_is_player_win(summary),
        is_loss=_is_player_loss(summary),
    )


def _tactical_awareness_components(signals: _GameSignals) -> tuple[int, Dict[str, Any]]:
    if signals.mistake_rate is None or signals.blunder_rate is None or signals.brilliant_rate is None:
        raw = float(NEUTRAL_SCORE)
        return NEUTRAL_SCORE, {
            "missing_primary_data": True,
            "mistake_rate": None,
            "blunder_rate": None,
            "brilliant_rate": None,
            "mate_threat_events": int(signals.mate_threat_events),
            "raw_before_clamp": raw,
        }

    error_rate = float(signals.mistake_rate) + float(signals.blunder_rate)
    base = _score_from_error_rate(error_rate)
    brilliant_bonus = min(6.0, float(signals.brilliant_rate) * 200.0)
    mate_penalty = min(10.0, float(signals.mate_threat_events) * 3.0)
    raw = base + brilliant_bonus - mate_penalty
    return _clamp_score(raw), {
        "missing_primary_data": False,
        "mistake_rate": round(float(signals.mistake_rate), 4),
        "blunder_rate": round(float(signals.blunder_rate), 4),
        "brilliant_rate": round(float(signals.brilliant_rate), 4),
        "mate_threat_events": int(signals.mate_threat_events),
        "base_score": round(base, 2),
        "brilliant_bonus": round(brilliant_bonus, 2),
        "mate_penalty": round(mate_penalty, 2),
        "raw_before_clamp": round(raw, 2),
    }


def _material_discipline_components(signals: _GameSignals) -> tuple[int, Dict[str, Any]]:
    if signals.mistake_rate is None or signals.blunder_rate is None:
        raw = float(NEUTRAL_SCORE)
        return NEUTRAL_SCORE, {
            "missing_primary_data": True,
            "mistake_rate": None,
            "blunder_rate": None,
            "severe_material_events": int(signals.severe_material_events),
            "raw_before_clamp": raw,
        }

    weighted_error_rate = (float(signals.blunder_rate) * 1.8) + (float(signals.mistake_rate) * 0.4)
    base = _score_from_error_rate(weighted_error_rate)
    severe_penalty = min(18.0, float(signals.severe_material_events) * 4.0)
    raw = base - severe_penalty
    return _clamp_score(raw), {
        "missing_primary_data": False,
        "mistake_rate": round(float(signals.mistake_rate), 4),
        "blunder_rate": round(float(signals.blunder_rate), 4),
        "weighted_error_rate": round(weighted_error_rate, 4),
        "severe_material_events": int(signals.severe_material_events),
        "base_score": round(base, 2),
        "severe_penalty": round(severe_penalty, 2),
        "raw_before_clamp": round(raw, 2),
    }


def _conversion_ability_components(signals: _GameSignals) -> tuple[int, Dict[str, Any]]:
    if signals.moves is None:
        raw = float(NEUTRAL_SCORE)
        return NEUTRAL_SCORE, {
            "missing_primary_data": True,
            "is_win": bool(signals.is_win),
            "late_error_rate": None,
            "raw_before_clamp": raw,
        }

    if not signals.is_win:
        raw = float(NEUTRAL_SCORE)
        return NEUTRAL_SCORE, {
            "missing_primary_data": False,
            "is_win": False,
            "late_error_events": int(signals.late_error_events),
            "late_error_rate": None,
            "raw_before_clamp": raw,
        }

    late_error_rate = float(signals.late_error_events) / float(max(1, signals.moves))
    amplified_late_error = late_error_rate * 3.0
    raw = _score_from_error_rate(amplified_late_error)
    return _clamp_score(raw), {
        "missing_primary_data": False,
        "is_win": True,
        "late_error_events": int(signals.late_error_events),
        "late_error_rate": round(late_error_rate, 4),
        "amplified_late_error_rate": round(amplified_late_error, 4),
        "raw_before_clamp": round(raw, 2),
    }


def _defensive_resilience_components(signals: _GameSignals) -> tuple[int, Dict[str, Any]]:
    if signals.mistake_rate is None or signals.blunder_rate is None:
        raw = float(NEUTRAL_SCORE)
        return NEUTRAL_SCORE, {
            "missing_primary_data": True,
            "is_win": bool(signals.is_win),
            "mistake_rate": None,
            "blunder_rate": None,
            "mate_threat_events": int(signals.mate_threat_events),
            "raw_before_clamp": raw,
        }

    if signals.is_win:
        raw = float(NEUTRAL_SCORE)
        return NEUTRAL_SCORE, {
            "missing_primary_data": False,
            "is_win": True,
            "mistake_rate": round(float(signals.mistake_rate), 4),
            "blunder_rate": round(float(signals.blunder_rate), 4),
            "mate_threat_events": int(signals.mate_threat_events),
            "raw_before_clamp": raw,
        }

    pressure_rate = float(signals.blunder_rate) + (float(signals.mistake_rate) * 0.5)
    base = _score_from_error_rate(pressure_rate)
    mate_penalty = min(20.0, float(signals.mate_threat_events) * 5.0)
    raw = base - mate_penalty
    return _clamp_score(raw), {
        "missing_primary_data": False,
        "is_win": False,
        "mistake_rate": round(float(signals.mistake_rate), 4),
        "blunder_rate": round(float(signals.blunder_rate), 4),
        "pressure_rate": round(pressure_rate, 4),
        "mate_threat_events": int(signals.mate_threat_events),
        "base_score": round(base, 2),
        "mate_penalty": round(mate_penalty, 2),
        "raw_before_clamp": round(raw, 2),
    }


def _blunder_frequency_components(signals: _GameSignals) -> tuple[int, Dict[str, Any]]:
    if signals.blunder_rate is None:
        raw = float(NEUTRAL_SCORE)
        return NEUTRAL_SCORE, {
            "missing_primary_data": True,
            "blunder_rate": None,
            "raw_before_clamp": raw,
        }

    raw = _score_from_blunder_rate(float(signals.blunder_rate))
    return _clamp_score(raw), {
        "missing_primary_data": False,
        "blunder_rate": round(float(signals.blunder_rate), 4),
        "raw_before_clamp": round(raw, 2),
    }


def _score_from_error_rate(rate: float) -> float:
    bounded = max(0.0, float(rate))
    # Linear map: 0.00 -> 100, 0.10 -> 70, 0.20 -> 40, 0.30 -> 10.
    return 100.0 - (bounded * 300.0)


def _score_from_blunder_rate(rate: float) -> float:
    bounded = max(0.0, float(rate))
    # Linear map: 0.00 -> 100, 0.05 -> 75, 0.10 -> 50, 0.20 -> 0.
    return 100.0 - (bounded * 500.0)


def _effective_moves(summary: Mapping[str, Any]) -> int | None:
    total_moves = _as_int(summary.get("total_moves", 0))
    if total_moves > 0:
        return total_moves
    total_plies = _as_int(summary.get("total_plies", 0))
    if total_plies > 0:
        return max(1, int(round(total_plies / 2.0)))
    return None


def _normalized_label_counts(summary: Mapping[str, Any]) -> Mapping[str, int] | None:
    raw_counts = summary.get("label_counts")
    if not isinstance(raw_counts, Mapping):
        return None
    counts: Dict[str, int] = {}
    for key in _LABEL_KEYS:
        counts[key] = max(0, _as_int(raw_counts.get(key, 0)))
    return counts


def _aggregate_scores(values: Sequence[int]) -> int:
    if not values:
        return NEUTRAL_SCORE
    total = sum(int(v) for v in values)
    return _clamp_score(total / float(len(values)))


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


def _traits_debug_enabled() -> bool:
    return str(os.environ.get("TRAITS_DEBUG", "")).strip() == "1"


def _emit_traits_debug(debug_rows: Sequence[Mapping[str, Any]], scores: Mapping[str, int]) -> None:
    for row in debug_rows:
        print(f"[traits-debug] {json.dumps(dict(row), ensure_ascii=True, sort_keys=True)}", file=sys.stderr)

    # Aggregate raw values across payloads to keep the debug output deterministic.
    def _avg_raw(component_key: str) -> float:
        raws: list[float] = []
        for row in debug_rows:
            component = row.get(component_key)
            if isinstance(component, Mapping):
                try:
                    raws.append(float(component.get("raw_before_clamp", 0.0)))
                except Exception:
                    continue
        if not raws:
            return float(NEUTRAL_SCORE)
        return sum(raws) / float(len(raws))

    aggregate = {
        "aggregate": {
            "payload_count": len(debug_rows),
            "tactical_awareness_components": {"raw_before_clamp": round(_avg_raw("tactical_awareness_components"), 2)},
            "material_discipline_components": {"raw_before_clamp": round(_avg_raw("material_discipline_components"), 2)},
            "conversion_ability_components": {"raw_before_clamp": round(_avg_raw("conversion_ability_components"), 2)},
            "defensive_resilience_components": {"raw_before_clamp": round(_avg_raw("defensive_resilience_components"), 2)},
            "blunder_frequency_components": {"raw_before_clamp": round(_avg_raw("blunder_frequency_components"), 2)},
            "scores_after_clamp": dict(scores),
        }
    }
    print(f"[traits-debug] {json.dumps(aggregate, ensure_ascii=True, sort_keys=True)}", file=sys.stderr)


def _iter_player_positions(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    summary = _game_summary(payload)
    your_color_raw = str(summary.get("your_color", "")).strip().lower()
    expected_player: str | None = None
    if your_color_raw == "white":
        expected_player = "white"
    elif your_color_raw == "black":
        expected_player = "black"

    for row in payload.get("key_positions") or []:
        if not isinstance(row, Mapping):
            continue
        if expected_player is None:
            yield row
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


def _is_player_loss(summary: Mapping[str, Any]) -> bool:
    result = str(summary.get("result", "")).strip()
    color = str(summary.get("your_color", "")).strip().lower()
    if color == "white":
        return result == "0-1"
    if color == "black":
        return result == "1-0"
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
