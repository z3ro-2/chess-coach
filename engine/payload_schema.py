"""Schema helpers and invariants for deterministic Stockfish payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

ENGINE_PAYLOAD_SCHEMA_VERSION = 2
LABEL_KEYS: tuple[str, ...] = ("good", "inaccuracy", "mistake", "blunder", "brilliant")
SIDE_KEYS: tuple[str, ...] = ("white", "black")


@dataclass(frozen=True)
class EnginePayloadValidationResult:
    """Normalized validation output for one engine payload."""

    is_valid: bool
    errors: tuple[str, ...]
    schema_version: int
    total_plies: int
    total_moves: int
    label_counts: Mapping[str, int]
    label_counts_by_side: Mapping[str, Mapping[str, int]]
    your_color: str | None
    player_total_plies: int
    player_label_counts: Mapping[str, int]


def empty_label_counts() -> dict[str, int]:
    return {key: 0 for key in LABEL_KEYS}


def normalize_label_counts(raw_counts: Any) -> dict[str, int] | None:
    if not isinstance(raw_counts, Mapping):
        return None
    counts = empty_label_counts()
    for key in LABEL_KEYS:
        counts[key] = max(0, _as_int(raw_counts.get(key, 0)))
    return counts


def expected_side_plies(total_plies: int) -> dict[str, int]:
    plies = max(0, int(total_plies))
    return {
        "white": (plies + 1) // 2,
        "black": plies // 2,
    }


def sum_label_counts(counts: Mapping[str, Any]) -> int:
    return sum(max(0, _as_int(counts.get(key, 0))) for key in LABEL_KEYS)


def player_color_from_summary(summary: Mapping[str, Any]) -> str | None:
    raw = str(summary.get("your_color", "")).strip().lower()
    if raw in SIDE_KEYS:
        return raw
    return None


def enrich_summary_with_player_fields(
    summary: Mapping[str, Any],
    *,
    your_color: str,
) -> dict[str, Any]:
    """Add deterministic player-only fields to a game summary."""
    normalized = dict(summary)
    normalized["schema_version"] = int(ENGINE_PAYLOAD_SCHEMA_VERSION)
    normalized["your_color"] = str(your_color).strip().lower()

    total_plies = _as_int(normalized.get("total_plies", 0))
    side_plies = expected_side_plies(total_plies)
    if "total_moves" not in normalized:
        normalized["total_moves"] = (total_plies + 1) // 2 if total_plies > 0 else 0

    side_counts_raw = normalized.get("label_counts_by_side")
    side_counts: dict[str, dict[str, int]] = {
        "white": empty_label_counts(),
        "black": empty_label_counts(),
    }
    if isinstance(side_counts_raw, Mapping):
        for side in SIDE_KEYS:
            normalized_side = normalize_label_counts(side_counts_raw.get(side))
            if normalized_side is not None:
                side_counts[side] = normalized_side
    normalized["label_counts_by_side"] = side_counts

    merged_counts = empty_label_counts()
    for key in LABEL_KEYS:
        merged_counts[key] = int(side_counts["white"][key]) + int(side_counts["black"][key])
    normalized["label_counts"] = merged_counts

    color = player_color_from_summary(normalized)
    if color is not None:
        normalized["player_total_plies"] = int(side_plies[color])
        normalized["player_total_moves"] = int(side_plies[color])
        normalized["player_label_counts"] = dict(side_counts[color])
    return normalized


def validate_engine_payload(
    payload: Mapping[str, Any],
    *,
    require_schema_version: bool,
    require_player_fields: bool,
    require_key_positions: bool,
) -> EnginePayloadValidationResult:
    """Validate payload structure and math invariants for schema v2."""
    errors: list[str] = []
    summary_raw = payload.get("game_summary")
    summary = summary_raw if isinstance(summary_raw, Mapping) else {}

    schema_version = _as_int(summary.get("schema_version", 1))
    if require_schema_version and schema_version != ENGINE_PAYLOAD_SCHEMA_VERSION:
        errors.append(
            f"unsupported_schema_version:{schema_version} expected:{ENGINE_PAYLOAD_SCHEMA_VERSION}"
        )

    total_plies = _as_int(summary.get("total_plies", 0))
    if total_plies <= 0:
        errors.append("total_plies_must_be_positive")

    total_moves = _as_int(summary.get("total_moves", 0))
    if total_moves <= 0:
        errors.append("total_moves_must_be_positive")
    expected_moves = (max(0, total_plies) + 1) // 2
    if total_plies > 0 and total_moves != expected_moves:
        errors.append(f"total_moves_mismatch expected:{expected_moves} actual:{total_moves}")

    label_counts = normalize_label_counts(summary.get("label_counts"))
    if label_counts is None:
        errors.append("label_counts_missing_or_invalid")
        label_counts = empty_label_counts()

    side_counts = _normalized_side_counts(summary.get("label_counts_by_side"))
    if side_counts is None:
        errors.append("label_counts_by_side_missing_or_invalid")
        side_counts = {"white": empty_label_counts(), "black": empty_label_counts()}

    expected_side = expected_side_plies(total_plies)
    for side in SIDE_KEYS:
        if sum_label_counts(side_counts[side]) != expected_side[side]:
            errors.append(
                f"side_plies_mismatch:{side} expected:{expected_side[side]} actual:{sum_label_counts(side_counts[side])}"
            )

    for key in LABEL_KEYS:
        expected_total = int(side_counts["white"][key]) + int(side_counts["black"][key])
        actual_total = int(label_counts[key])
        if actual_total != expected_total:
            errors.append(
                f"label_total_mismatch:{key} expected:{expected_total} actual:{actual_total}"
            )
    if sum_label_counts(label_counts) != total_plies:
        errors.append(
            f"total_plies_label_sum_mismatch expected:{total_plies} actual:{sum_label_counts(label_counts)}"
        )

    key_positions = payload.get("key_positions")
    if require_key_positions:
        if not isinstance(key_positions, list):
            errors.append("key_positions_missing_or_invalid")
        elif len(key_positions) != 4:
            errors.append(f"key_positions_must_have_exactly_four actual:{len(key_positions)}")

    your_color = player_color_from_summary(summary)
    player_total_plies = 0
    player_label_counts: Mapping[str, int] = empty_label_counts()
    if your_color is None:
        if require_player_fields:
            errors.append("your_color_missing_or_invalid")
    else:
        player_total_plies = _as_int(summary.get("player_total_plies", expected_side[your_color]))
        player_total_moves = _as_int(summary.get("player_total_moves", player_total_plies))
        player_raw = summary.get("player_label_counts")
        normalized_player = normalize_label_counts(player_raw)
        if normalized_player is None:
            normalized_player = dict(side_counts[your_color])
        player_label_counts = normalized_player

        if player_total_plies != expected_side[your_color]:
            errors.append(
                f"player_total_plies_mismatch expected:{expected_side[your_color]} actual:{player_total_plies}"
            )
        if player_total_moves != player_total_plies:
            errors.append(
                f"player_total_moves_mismatch expected:{player_total_plies} actual:{player_total_moves}"
            )
        if sum_label_counts(player_label_counts) != player_total_plies:
            errors.append(
                f"player_plies_label_sum_mismatch expected:{player_total_plies} actual:{sum_label_counts(player_label_counts)}"
            )
        if dict(player_label_counts) != dict(side_counts[your_color]):
            errors.append("player_label_counts_mismatch_side_counts")

    if require_player_fields and your_color is None:
        player_total_plies = 0
        player_label_counts = empty_label_counts()

    return EnginePayloadValidationResult(
        is_valid=not errors,
        errors=tuple(errors),
        schema_version=int(schema_version),
        total_plies=int(total_plies),
        total_moves=int(total_moves),
        label_counts=dict(label_counts),
        label_counts_by_side={
            "white": dict(side_counts["white"]),
            "black": dict(side_counts["black"]),
        },
        your_color=your_color,
        player_total_plies=int(player_total_plies),
        player_label_counts=dict(player_label_counts),
    )


def _normalized_side_counts(raw_side_counts: Any) -> dict[str, dict[str, int]] | None:
    if not isinstance(raw_side_counts, Mapping):
        return None
    normalized: dict[str, dict[str, int]] = {}
    for side in SIDE_KEYS:
        side_counts = normalize_label_counts(raw_side_counts.get(side))
        if side_counts is None:
            return None
        normalized[side] = side_counts
    return normalized


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0
