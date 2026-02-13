"""Transform engine output into a strict LLM-safe payload."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

SAFE_GAME_SUMMARY_KEYS: Sequence[str] = (
    "schema_version",
    "engine_depth",
    "result",
    "total_plies",
    "total_moves",
    "white_plies",
    "black_plies",
    "unlabeled_white_plies",
    "unlabeled_black_plies",
    "label_counts_total",
    "label_counts_white",
    "label_counts_black",
    "player_total_plies",
    "player_total_moves",
    "label_counts",
    "player_label_counts",
    "label_counts_by_side",
    "forced_mate_events",
    "illegal_moves",
    "date_utc",
    "your_color",
    "opponent",
    "time_control",
    "rated",
    "rules",
    "url",
)


def build_llm_safe_payload(
    engine_output: Mapping[str, Any],
    game_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    raw_summary = dict(engine_output.get("game_summary") or {})
    schema_version = int(engine_output.get("schema_version", raw_summary.get("schema_version", 1)) or 1)
    # For review text, expose only player-scoped counts under `label_counts`.
    player_label_counts = raw_summary.get("player_label_counts")
    if isinstance(player_label_counts, Mapping):
        raw_summary["label_counts"] = dict(player_label_counts)

    game_summary = {key: raw_summary.get(key) for key in SAFE_GAME_SUMMARY_KEYS if key in raw_summary}
    if game_context:
        for key in SAFE_GAME_SUMMARY_KEYS:
            if key in game_context:
                game_summary[key] = game_context.get(key)

    key_positions = []

    for item in engine_output.get("key_positions") or []:
        row = dict(item or {})
        key_positions.append(
            {
                "move_number": row.get("move_number"),
                "player": row.get("player"),
                "label": row.get("label"),
                "tactical_flag": row.get("tactical_flag"),
                "material_change": row.get("material_change"),
                "mate_threat": row.get("mate_threat"),
                "forcing": row.get("forcing"),
                "played_san": row.get("played_san"),
                "best_san": row.get("best_san"),
            }
        )

    return {
        "schema_version": int(schema_version),
        "game_summary": game_summary,
        "key_positions": key_positions,
    }
