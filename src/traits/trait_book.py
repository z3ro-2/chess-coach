from __future__ import annotations

from collections import Counter
from typing import Any, Mapping


_EXCLUDE_CONFIDENCE = 0.35
_EXCLUDE_TREND = 0.12

_CORE_STRENGTH_CONFIDENCE = 0.60
_CORE_STRENGTH_MAX_TREND = 0.12

_WEAKNESS_CONFIDENCE = 0.55
_WEAKNESS_MIN_TREND = 0.18

_TRANSITION_MIN_CONFIDENCE = 0.35
_TRANSITION_MAX_CONFIDENCE = 0.55
_TRANSITION_TREND_THRESHOLD = 0.18

_WATCHLIST_MIN_CONFIDENCE = 0.55
_WATCHLIST_MIN_ABS_TREND = 0.12
_WATCHLIST_MAX_ABS_TREND = 0.17


def generate_trait_book_markdown(
    player_row,
    traits_rows,
    player_traits_rows,
    *,
    games_analyzed: int,
    cutoff_label: str,
    snapshot_utc: str,
    window_info: dict,
) -> str:
    merged_traits = _merge_traits(traits_rows, player_traits_rows)
    classified = _classify_traits(merged_traits)

    strengths_all = sorted(
        classified["core_strengths"],
        key=lambda t: (t["trend_ema"], -t["confidence"], -t["severity_weight"], t["name"]),
    )
    weaknesses_all = sorted(
        classified["recurring_weaknesses"],
        key=lambda t: (-t["trend_ema"], -t["confidence"], -t["severity_weight"], t["name"]),
    )
    transition_improving_all = sorted(
        classified["transition_improving"],
        key=lambda t: (-abs(t["trend_ema"]), -t["confidence"], t["name"]),
    )
    transition_worsening_all = sorted(
        classified["transition_worsening"],
        key=lambda t: (-abs(t["trend_ema"]), -t["confidence"], t["name"]),
    )
    watchlist_all = sorted(
        classified["watchlist"],
        key=lambda t: (-abs(t["trend_ema"]), -t["confidence"], -t["severity_weight"], t["name"]),
    )

    strengths = strengths_all[:5]
    weaknesses = weaknesses_all[:5]
    transition_improving, transition_worsening = _limit_transition_traits(
        transition_improving_all, transition_worsening_all
    )
    watchlist = watchlist_all

    included_traits = strengths + weaknesses + transition_improving + transition_worsening + watchlist
    included_unique = _unique_by_key(included_traits)
    excluded_low_signal = len(classified["excluded_low_signal"])

    summary_text = _build_snapshot_summary(strengths, weaknesses, included_unique)
    coach_line = _build_coach_one_liner(strengths, weaknesses)
    focus_areas = _build_focus_areas(weaknesses_all, transition_worsening_all, transition_improving_all, watchlist_all)

    player_name = _player_label(player_row)
    metadata_lines = _metadata_lines(
        player_row=player_row,
        player_name=player_name,
        snapshot_utc=snapshot_utc,
        games_analyzed=games_analyzed,
        cutoff_label=cutoff_label,
        window_info=window_info,
        traits_with_signal=len(included_unique),
        traits_omitted=excluded_low_signal,
    )

    lines: list[str] = []
    lines.append("# Trait Book Snapshot")
    lines.append("")
    lines.append("## Metadata")
    lines.extend(metadata_lines)
    lines.append("")
    lines.append("## Player Snapshot")
    lines.append(summary_text)
    lines.append("")
    lines.append(f"**Coach one-liner:** {coach_line}")
    lines.append("")
    lines.append("## Core Strengths")
    lines.extend(_format_trait_section(strengths))
    lines.append("")
    lines.append("## Recurring Weaknesses")
    lines.extend(_format_trait_section(weaknesses))
    lines.append("")
    lines.append("## Traits in Transition")
    lines.append("### Improving")
    lines.extend(_format_trait_section(transition_improving))
    lines.append("")
    lines.append("### Worsening")
    lines.extend(_format_trait_section(transition_worsening))
    if watchlist:
        lines.append("")
        lines.append("### Watchlist")
        lines.extend(_format_trait_section(watchlist))
    lines.append("")
    lines.append("## Focus Areas (Next 20 Games)")
    lines.extend(_format_focus_areas(focus_areas))
    lines.append("")
    lines.append("## Methodology")
    lines.append(
        "This snapshot summarizes recurring human patterns from recent games. "
        "Confidence reflects recurrence of a trait over time, while trend shows whether it is improving or worsening."
    )
    lines.append(
        "Severity weight affects impact on trend ranking, and low-signal traits are intentionally filtered to reduce noise."
    )
    return "\n".join(lines).strip() + "\n"


def _merge_traits(traits_rows, player_traits_rows) -> list[dict[str, Any]]:
    traits_by_id: dict[int, dict[str, Any]] = {}
    for row in traits_rows:
        trait_id = int(_row_get(row, "id", 0))
        traits_by_id[trait_id] = {
            "id": trait_id,
            "key": str(_row_get(row, "key", "")),
            "name": str(_row_get(row, "name", _row_get(row, "key", ""))),
            "category": str(_row_get(row, "category", "strategy")),
            "description": str(_row_get(row, "description", "")),
            "severity_weight": float(_row_get(row, "severity_weight", 1.0) or 1.0),
            "confidence": 0.0,
            "trend_ema": 0.0,
            "last_seen_game_id": None,
        }

    for row in player_traits_rows:
        trait_id = int(_row_get(row, "trait_id", -1))
        trait = traits_by_id.get(trait_id)
        if trait is None:
            continue
        trait["confidence"] = float(_row_get(row, "confidence", 0.0) or 0.0)
        trait["trend_ema"] = float(_row_get(row, "trend_ema", 0.0) or 0.0)
        trait["last_seen_game_id"] = _row_get(row, "last_seen_game_id", None)

    return list(traits_by_id.values())


def _classify_traits(traits: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "core_strengths": [],
        "recurring_weaknesses": [],
        "transition_improving": [],
        "transition_worsening": [],
        "watchlist": [],
        "excluded_low_signal": [],
    }

    for trait in traits:
        confidence = float(trait["confidence"])
        trend = float(trait["trend_ema"])
        abs_trend = abs(trend)

        if confidence < _EXCLUDE_CONFIDENCE and abs_trend < _EXCLUDE_TREND:
            result["excluded_low_signal"].append(trait)
            continue

        # Priority: Weaknesses > Strengths > Transition > Watchlist
        if confidence >= _WEAKNESS_CONFIDENCE and trend >= _WEAKNESS_MIN_TREND:
            result["recurring_weaknesses"].append(trait)
            continue

        if confidence >= _CORE_STRENGTH_CONFIDENCE and trend <= _CORE_STRENGTH_MAX_TREND:
            result["core_strengths"].append(trait)
            continue

        if _TRANSITION_MIN_CONFIDENCE <= confidence < _TRANSITION_MAX_CONFIDENCE:
            if trend <= -_TRANSITION_TREND_THRESHOLD:
                result["transition_improving"].append(trait)
                continue
            if trend >= _TRANSITION_TREND_THRESHOLD:
                result["transition_worsening"].append(trait)
                continue

        if (
            confidence >= _WATCHLIST_MIN_CONFIDENCE
            and _WATCHLIST_MIN_ABS_TREND <= abs_trend <= _WATCHLIST_MAX_ABS_TREND
        ):
            result["watchlist"].append(trait)

    return result


def _limit_transition_traits(
    improving_all: list[dict[str, Any]],
    worsening_all: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    improving = improving_all[:2]
    worsening = worsening_all[:2]

    total = len(improving) + len(worsening)
    if total >= 4:
        return improving, worsening

    remaining_improving = improving_all[2:]
    remaining_worsening = worsening_all[2:]
    remaining = sorted(
        remaining_improving + remaining_worsening,
        key=lambda t: (-abs(t["trend_ema"]), -t["confidence"], t["name"]),
    )
    for trait in remaining:
        if len(improving) + len(worsening) >= 4:
            break
        if trait in improving or trait in worsening:
            continue
        if trait["trend_ema"] < 0:
            improving.append(trait)
        else:
            worsening.append(trait)
    return improving, worsening


def _build_snapshot_summary(
    strengths: list[dict[str, Any]],
    weaknesses: list[dict[str, Any]],
    included_traits: list[dict[str, Any]],
) -> str:
    strengths_theme = _dominant_category_label(strengths, fallback="no single area yet")
    weaknesses_theme = _dominant_category_label(weaknesses, fallback="no single pressure point yet")

    positive_count = sum(1 for t in included_traits if t["trend_ema"] >= _EXCLUDE_TREND)
    negative_count = sum(1 for t in included_traits if t["trend_ema"] <= -_EXCLUDE_TREND)
    if negative_count >= positive_count + 2:
        trajectory = "Improving"
    elif positive_count >= negative_count + 2:
        trajectory = "Under pressure"
    else:
        trajectory = "Mixed/Stable"

    avg_conf = (
        sum(float(t["confidence"]) for t in included_traits) / len(included_traits)
        if included_traits
        else 0.0
    )
    if avg_conf >= 0.65:
        stability = "Signals are stable across this sample."
    elif avg_conf >= 0.50:
        stability = "Signals are moderately stable and still evolving."
    else:
        stability = "Signals are still early and should be treated as provisional."

    sentence_1 = f"Current strengths cluster most around {strengths_theme}."
    sentence_2 = f"Recurring pressure appears most in {weaknesses_theme}."
    sentence_3 = f"Overall trajectory is {trajectory} at this checkpoint."
    sentence_4 = stability
    return " ".join([sentence_1, sentence_2, sentence_3, sentence_4])


def _build_coach_one_liner(
    strengths: list[dict[str, Any]],
    weaknesses: list[dict[str, Any]],
) -> str:
    strength = strengths[0]["name"] if strengths else "your stable habits"
    weakness = weaknesses[0]["name"] if weaknesses else "your highest-friction decision pattern"
    weakness_category = weaknesses[0]["category"] if weaknesses else "strategy"
    cue = _focus_cue(weakness_category)
    return f"Keep leveraging {strength}, and over the next 20 games reduce {weakness} by consistently applying: {cue}"


def _build_focus_areas(
    weaknesses_all: list[dict[str, Any]],
    transition_worsening_all: list[dict[str, Any]],
    transition_improving_all: list[dict[str, Any]],
    watchlist_all: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    focus: list[dict[str, Any]] = []
    used_keys: set[str] = set()

    for trait in weaknesses_all:
        if len(focus) >= 2:
            break
        if trait["key"] in used_keys:
            continue
        focus.append(trait)
        used_keys.add(trait["key"])

    third_candidate_pool = transition_worsening_all + transition_improving_all + watchlist_all
    third = next((t for t in third_candidate_pool if t["key"] not in used_keys), None)

    if third is not None and len(focus) >= 2:
        primary_categories = {focus[0]["category"], focus[1]["category"]}
        if third["category"] in primary_categories:
            alternative = next(
                (
                    t
                    for t in third_candidate_pool
                    if t["key"] not in used_keys and t["category"] not in primary_categories
                ),
                None,
            )
            if alternative is not None:
                third = alternative

    if third is not None:
        focus.append(third)
        used_keys.add(third["key"])

    filler_pool = (
        weaknesses_all[2:]
        + transition_worsening_all
        + transition_improving_all
        + watchlist_all
    )
    for trait in filler_pool:
        if len(focus) >= 3:
            break
        if trait["key"] in used_keys:
            continue
        focus.append(trait)
        used_keys.add(trait["key"])

    while len(focus) < 3:
        focus.append(
            {
                "name": "No clear signal yet",
                "category": "strategy",
                "description": "Continue collecting games to increase signal.",
                "key": f"placeholder_{len(focus)}",
            }
        )

    return focus[:3]


def _metadata_lines(
    *,
    player_row,
    player_name: str,
    snapshot_utc: str,
    games_analyzed: int,
    cutoff_label: str,
    window_info: dict,
    traits_with_signal: int,
    traits_omitted: int,
) -> list[str]:
    window_start_game = window_info.get("start_game_id", "n/a")
    window_end_game = window_info.get("end_game_id", "n/a")
    window_start_played = window_info.get("start_played_at", "n/a")
    window_end_played = window_info.get("end_played_at", "n/a")
    return [
        f"- Player: {player_name}",
        f"- Player ID: {_row_get(player_row, 'id', 'n/a')}",
        f"- Snapshot UTC: {snapshot_utc}",
        f"- Games analyzed: {games_analyzed}",
        f"- Cutoff: {cutoff_label}",
        (
            f"- Window: games {window_start_game} to {window_end_game}; "
            f"played {window_start_played} to {window_end_played}"
        ),
        f"- Traits with signal: {traits_with_signal}",
        f"- Traits omitted (low signal): {traits_omitted}",
    ]


def _format_trait_section(traits: list[dict[str, Any]]) -> list[str]:
    if not traits:
        return ["No clear signal yet"]

    lines: list[str] = []
    for trait in traits:
        lines.append(
            "- "
            f"**{trait['name']}** ({trait['category']}): "
            f"trend={trait['trend_ema']:.2f}, confidence={trait['confidence']:.2f}. "
            f"{trait['description']}"
        )
    return lines


def _format_focus_areas(focus_areas: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for idx, trait in enumerate(focus_areas[:3], start=1):
        category = str(trait.get("category", "strategy"))
        lines.append(
            f"{idx}. **{trait.get('name', 'No clear signal yet')}** ({category}): {_focus_cue(category)}"
        )
    return lines


def _focus_cue(category: str) -> str:
    cues = {
        "opening": "build a clear setup and complete development before side plans",
        "tactics": "run a quick threat-check and candidate scan before each commitment",
        "strategy": "anchor decisions to structure, piece activity, and opponent plans",
        "endgame": "simplify with purpose and activate king plus passers on a clear plan",
        "time": "spend extra time on critical positions while preserving a clock buffer",
        "psych": "reset quickly after mistakes and return to practical decision-making",
    }
    return cues.get(category, cues["strategy"])


def _dominant_category_label(traits: list[dict[str, Any]], *, fallback: str) -> str:
    if not traits:
        return fallback
    counts = Counter(str(t["category"]) for t in traits)
    most_common = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return most_common


def _unique_by_key(traits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for trait in traits:
        key = str(trait["key"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(trait)
    return unique


def _row_get(row: Any, key: str, default: Any) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    return getattr(row, key, default)


def _player_label(player_row: Any) -> str:
    for key in ("platform_user", "username", "handle", "chess_username", "name"):
        value = _row_get(player_row, key, None)
        if value:
            return str(value)
    return f"player_{_row_get(player_row, 'id', 'unknown')}"
