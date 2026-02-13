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

    side_counts = _normalized_side_counts(normalized)
    if side_counts is None:
        side_counts = {
            "white": empty_label_counts(),
            "black": empty_label_counts(),
        }
    normalized["label_counts_by_side"] = side_counts
    normalized["label_counts_white"] = dict(side_counts["white"])
    normalized["label_counts_black"] = dict(side_counts["black"])

    merged_counts = empty_label_counts()
    for key in LABEL_KEYS:
        merged_counts[key] = int(side_counts["white"][key]) + int(side_counts["black"][key])
    normalized["label_counts_total"] = merged_counts
    normalized["label_counts"] = merged_counts
    normalized["white_plies"] = int(side_plies["white"])
    normalized["black_plies"] = int(side_plies["black"])
    normalized["unlabeled_white_plies"] = max(0, int(side_plies["white"]) - sum_label_counts(side_counts["white"]))
    normalized["unlabeled_black_plies"] = max(0, int(side_plies["black"]) - sum_label_counts(side_counts["black"]))

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

    schema_version = _as_int(payload.get("schema_version", summary.get("schema_version", 1)))
    summary_schema_version = _as_int(summary.get("schema_version", schema_version))
    if "schema_version" in payload and "schema_version" in summary and schema_version != summary_schema_version:
        errors.append(
            f"schema_version_mismatch payload:{schema_version} game_summary:{summary_schema_version}"
        )
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

    expected_side = expected_side_plies(total_plies)
    white_plies = _as_int(summary.get("white_plies", expected_side["white"]))
    black_plies = _as_int(summary.get("black_plies", expected_side["black"]))
    if total_plies > 0 and int(white_plies + black_plies) != int(total_plies):
        errors.append(f"total_plies_side_split_mismatch expected:{total_plies} actual:{white_plies + black_plies}")

    side_counts = _normalized_side_counts(summary)
    if side_counts is None:
        errors.append("side_label_counts_missing_or_invalid")
        side_counts = {"white": empty_label_counts(), "black": empty_label_counts()}

    unlabeled_by_side: dict[str, int] = {"white": 0, "black": 0}
    for side in SIDE_KEYS:
        side_plies = int(white_plies if side == "white" else black_plies)
        labeled_plies = int(sum_label_counts(side_counts[side]))
        unlabeled_key = f"unlabeled_{side}_plies"
        default_unlabeled = max(0, side_plies - labeled_plies)
        unlabeled_plies = max(0, _as_int(summary.get(unlabeled_key, default_unlabeled)))
        unlabeled_by_side[side] = int(unlabeled_plies)
        if labeled_plies > side_plies:
            errors.append(
                f"side_labeled_plies_exceed_total:{side} labeled:{labeled_plies} total:{side_plies}"
            )
        if labeled_plies + unlabeled_plies != side_plies:
            errors.append(
                f"side_plies_accounting_mismatch:{side} expected:{side_plies} actual:{labeled_plies + unlabeled_plies}"
            )

    label_counts = normalize_label_counts(summary.get("label_counts_total"))
    if label_counts is None:
        label_counts = normalize_label_counts(summary.get("label_counts"))
    if label_counts is None:
        errors.append("label_counts_total_missing_or_invalid")
        label_counts = empty_label_counts()

    for key in LABEL_KEYS:
        expected_total = int(side_counts["white"][key]) + int(side_counts["black"][key])
        actual_total = int(label_counts[key])
        if actual_total != expected_total:
            errors.append(
                f"label_total_mismatch:{key} expected:{expected_total} actual:{actual_total}"
            )
    legacy_label_counts = normalize_label_counts(summary.get("label_counts"))
    if legacy_label_counts is not None and dict(legacy_label_counts) != dict(label_counts):
        errors.append("label_counts_legacy_mismatch_label_counts_total")

    total_unlabeled = int(unlabeled_by_side["white"] + unlabeled_by_side["black"])
    if int(sum_label_counts(label_counts) + total_unlabeled) != int(total_plies):
        errors.append(
            f"total_plies_label_sum_mismatch expected:{total_plies} actual:{sum_label_counts(label_counts) + total_unlabeled}"
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
        expected_player_plies = int(white_plies if your_color == "white" else black_plies)
        player_total_plies = _as_int(summary.get("player_total_plies", expected_player_plies))
        player_total_moves = _as_int(summary.get("player_total_moves", player_total_plies))
        player_raw = summary.get("player_label_counts")
        normalized_player = normalize_label_counts(player_raw)
        if normalized_player is None:
            normalized_player = dict(side_counts[your_color])
        player_label_counts = normalized_player
        player_unlabeled_plies = int(unlabeled_by_side[your_color])

        if player_total_plies != expected_player_plies:
            errors.append(
                f"player_total_plies_mismatch expected:{expected_player_plies} actual:{player_total_plies}"
            )
        if player_total_moves != player_total_plies:
            errors.append(
                f"player_total_moves_mismatch expected:{player_total_plies} actual:{player_total_moves}"
            )
        if int(sum_label_counts(player_label_counts) + player_unlabeled_plies) != int(player_total_plies):
            errors.append(
                f"player_plies_label_sum_mismatch expected:{player_total_plies} actual:{sum_label_counts(player_label_counts) + player_unlabeled_plies}"
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

    white_counts = normalize_label_counts(raw_side_counts.get("label_counts_white"))
    black_counts = normalize_label_counts(raw_side_counts.get("label_counts_black"))
    if white_counts is not None and black_counts is not None:
        return {
            "white": white_counts,
            "black": black_counts,
        }

    side_counts_raw = raw_side_counts.get("label_counts_by_side")
    if not isinstance(side_counts_raw, Mapping):
        side_counts_raw = raw_side_counts

    normalized: dict[str, dict[str, int]] = {}
    for side in SIDE_KEYS:
        side_counts = normalize_label_counts(side_counts_raw.get(side) if isinstance(side_counts_raw, Mapping) else None)
        if side_counts is None:
            return None
        normalized[side] = side_counts
    return normalized


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0
