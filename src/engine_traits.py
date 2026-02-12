"""Deterministic trait scoring from engine payloads."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Iterable, Mapping, Sequence


def compute_engine_trait_scores(payloads: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """Compute all deterministic engine-trait scores."""
    scores = {
        "tactical_awareness": tactical_awareness(payloads),
        "material_discipline": material_discipline(payloads),
        "conversion_ability": conversion_ability(payloads),
        "defensive_resilience": defensive_resilience(payloads),
        "blunder_frequency": blunder_frequency(payloads),
    }
    if _traits_debug_enabled():
        _emit_traits_debug(payloads, scores)
    return scores


def tactical_awareness(payloads: Sequence[Mapping[str, Any]]) -> int:
    score = 100
    for payload in payloads:
        for position in _iter_player_positions(payload):
            label = str(position.get("label", "")).strip().lower()
            tactical_flag = str(position.get("tactical_flag", "")).strip().lower()
            if label == "blunder":
                score -= 8
            if tactical_flag == "tactical_miss":
                score -= 4
            if tactical_flag == "hanging_piece":
                score -= 6
    return _clamp_score(score)


def material_discipline(payloads: Sequence[Mapping[str, Any]]) -> int:
    material_loss_total = 0
    for payload in payloads:
        for position in _iter_player_positions(payload):
            material_change = _as_int(position.get("material_change", 0))
            if material_change < 0:
                material_loss_total += abs(material_change)
    return _clamp_score(100 - (material_loss_total * 3))


def conversion_ability(payloads: Sequence[Mapping[str, Any]]) -> int:
    opportunities = 0
    conversions = 0

    for payload in payloads:
        summary = _game_summary(payload)
        total_moves = max(1, _as_int(summary.get("total_moves", 0)))
        cutoff_move = max(6, total_moves // 3)
        had_early_advantage = False

        for position in _iter_player_positions(payload):
            move_number = _as_int(position.get("move_number", 0))
            material_change = _as_int(position.get("material_change", 0))
            if move_number <= cutoff_move and material_change >= 3:
                had_early_advantage = True
                break

        if not had_early_advantage:
            continue

        opportunities += 1
        if _is_player_win(summary):
            conversions += 1

    if opportunities <= 0:
        return 100
    return _clamp_score((conversions / opportunities) * 100.0)


def defensive_resilience(payloads: Sequence[Mapping[str, Any]]) -> int:
    pressure_games = 0
    resilient_games = 0

    for payload in payloads:
        summary = _game_summary(payload)
        material_swing = 0
        for position in _iter_player_positions(payload):
            material_change = _as_int(position.get("material_change", 0))
            if material_change < 0:
                material_swing += material_change

        if material_swing < -3:
            pressure_games += 1
            if not _is_player_loss(summary):
                resilient_games += 1

    if pressure_games <= 0:
        return 100
    return _clamp_score((resilient_games / pressure_games) * 100.0)


def blunder_frequency(payloads: Sequence[Mapping[str, Any]]) -> int:
    total_blunders = 0
    total_moves = 0

    for payload in payloads:
        summary = _game_summary(payload)
        total_moves += max(0, _as_int(summary.get("total_moves", 0)))
        for position in _iter_player_positions(payload):
            label = str(position.get("label", "")).strip().lower()
            if label == "blunder":
                total_blunders += 1

    if total_moves <= 0:
        return 100

    blunder_ratio = total_blunders / total_moves
    return _clamp_score((1.0 - blunder_ratio) * 100.0)


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


def _traits_debug_enabled() -> bool:
    return str(os.environ.get("TRAITS_DEBUG", "")).strip() == "1"


def _emit_traits_debug(payloads: Sequence[Mapping[str, Any]], scores: Mapping[str, int]) -> None:
    tactical_blunders = 0
    tactical_misses = 0
    hanging_pieces = 0
    material_loss_total = 0
    opportunities = 0
    conversions = 0
    pressure_games = 0
    resilient_games = 0
    total_blunders = 0
    total_moves = 0

    for idx, payload in enumerate(payloads, start=1):
        summary = _game_summary(payload)
        key_positions_raw = payload.get("key_positions") or []
        key_positions_count = len(key_positions_raw) if isinstance(key_positions_raw, Sequence) else 0
        total_plies = _as_int(summary.get("total_plies", 0))
        payload_total_moves = max(0, _as_int(summary.get("total_moves", 0)))
        total_moves += payload_total_moves
        label_counts = summary.get("label_counts")
        if not isinstance(label_counts, Mapping):
            label_counts = {}

        player_positions = list(_iter_player_positions(payload))
        payload_blunders = 0
        payload_tactical_misses = 0
        payload_hanging_pieces = 0
        payload_material_loss = 0
        payload_material_swing = 0

        for position in player_positions:
            label = str(position.get("label", "")).strip().lower()
            tactical_flag = str(position.get("tactical_flag", "")).strip().lower()
            material_change = _as_int(position.get("material_change", 0))
            if label == "blunder":
                payload_blunders += 1
            if tactical_flag == "tactical_miss":
                payload_tactical_misses += 1
            if tactical_flag == "hanging_piece":
                payload_hanging_pieces += 1
            if material_change < 0:
                payload_material_loss += abs(material_change)
                payload_material_swing += material_change

        tactical_blunders += payload_blunders
        tactical_misses += payload_tactical_misses
        hanging_pieces += payload_hanging_pieces
        material_loss_total += payload_material_loss
        total_blunders += payload_blunders

        cutoff_move = max(6, max(1, payload_total_moves) // 3)
        had_early_advantage = False
        for position in player_positions:
            move_number = _as_int(position.get("move_number", 0))
            material_change = _as_int(position.get("material_change", 0))
            if move_number <= cutoff_move and material_change >= 3:
                had_early_advantage = True
                break

        converted = had_early_advantage and _is_player_win(summary)
        if had_early_advantage:
            opportunities += 1
            if converted:
                conversions += 1

        under_pressure = payload_material_swing < -3
        resilient = under_pressure and not _is_player_loss(summary)
        if under_pressure:
            pressure_games += 1
            if resilient:
                resilient_games += 1

        payload_debug = {
            "payload_index": idx,
            "total_plies": total_plies,
            "total_moves": payload_total_moves,
            "label_counts": dict(label_counts),
            "key_positions_count": key_positions_count,
            "player_key_positions_count": len(player_positions),
            "tactical_awareness_components": {
                "blunders": payload_blunders,
                "tactical_misses": payload_tactical_misses,
                "hanging_pieces": payload_hanging_pieces,
                "raw_before_clamp": 100 - ((payload_blunders * 8) + (payload_tactical_misses * 4) + (payload_hanging_pieces * 6)),
            },
            "material_discipline_components": {
                "material_loss_total": payload_material_loss,
                "raw_before_clamp": 100 - (payload_material_loss * 3),
            },
            "conversion_ability_components": {
                "cutoff_move": cutoff_move,
                "had_early_advantage": had_early_advantage,
                "converted": converted,
                "raw_before_clamp": 100.0 if not had_early_advantage else (100.0 if converted else 0.0),
            },
            "defensive_resilience_components": {
                "material_swing": payload_material_swing,
                "under_pressure": under_pressure,
                "resilient": resilient,
                "raw_before_clamp": 100.0 if not under_pressure else (100.0 if resilient else 0.0),
            },
            "blunder_frequency_components": {
                "blunders": payload_blunders,
                "total_moves": payload_total_moves,
                "raw_before_clamp": 100.0 if payload_total_moves <= 0 else (1.0 - (payload_blunders / payload_total_moves)) * 100.0,
            },
        }
        print(f"[traits-debug] {json.dumps(payload_debug, ensure_ascii=True, sort_keys=True)}", file=sys.stderr)

    aggregate_debug = {
        "aggregate": {
            "payload_count": len(payloads),
            "tactical_awareness_components": {
                "blunders": tactical_blunders,
                "tactical_misses": tactical_misses,
                "hanging_pieces": hanging_pieces,
                "raw_before_clamp": 100 - ((tactical_blunders * 8) + (tactical_misses * 4) + (hanging_pieces * 6)),
            },
            "material_discipline_components": {
                "material_loss_total": material_loss_total,
                "raw_before_clamp": 100 - (material_loss_total * 3),
            },
            "conversion_ability_components": {
                "opportunities": opportunities,
                "conversions": conversions,
                "raw_before_clamp": 100.0 if opportunities <= 0 else (conversions / opportunities) * 100.0,
            },
            "defensive_resilience_components": {
                "pressure_games": pressure_games,
                "resilient_games": resilient_games,
                "raw_before_clamp": 100.0 if pressure_games <= 0 else (resilient_games / pressure_games) * 100.0,
            },
            "blunder_frequency_components": {
                "total_blunders": total_blunders,
                "total_moves": total_moves,
                "raw_before_clamp": 100.0 if total_moves <= 0 else (1.0 - (total_blunders / total_moves)) * 100.0,
            },
            "scores_after_clamp": dict(scores),
        }
    }
    print(f"[traits-debug] {json.dumps(aggregate_debug, ensure_ascii=True, sort_keys=True)}", file=sys.stderr)
