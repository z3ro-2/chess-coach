"""Deterministic trait scoring from engine payloads."""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Sequence


def compute_engine_trait_scores(payloads: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """Compute all deterministic engine-trait scores."""
    return {
        "tactical_awareness": tactical_awareness(payloads),
        "material_discipline": material_discipline(payloads),
        "conversion_ability": conversion_ability(payloads),
        "defensive_resilience": defensive_resilience(payloads),
        "blunder_frequency": blunder_frequency(payloads),
    }


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

