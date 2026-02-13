"""Coordinator that keeps engine, LLM, and review validation roles separated."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from engine.payload_schema import (
    ENGINE_PAYLOAD_SCHEMA_VERSION,
    LABEL_KEYS,
    enrich_summary_with_player_fields,
    normalize_label_counts,
    validate_engine_payload,
)
from llm.safe_payload import build_llm_safe_payload
from src.utils.timezone import get_display_timezone


def load_prompt_file(name: str) -> str:
    base_path = Path(__file__).resolve().parent
    path = base_path / "prompts" / name
    if not path.exists():
        path = base_path.parent / "prompts" / name
    return path.read_text(encoding="utf-8")


def run_analysis_pipeline(
    *,
    game: Any,
    args: Any,
    llm_runner: Callable[[str, str], str],
    logger: Any,
) -> str:
    engine_output = _run_stockfish_oracle(game=game, args=args, logger=logger)
    if engine_output is None:
        raise RuntimeError("Stockfish engine failed or produced no output.")
    raw_errors = _raw_engine_payload_required_field_errors(engine_output)
    if raw_errors:
        raise RuntimeError(f"Engine payload invalid: {';'.join(raw_errors)}")

    summary = dict(engine_output.get("game_summary") or {})
    summary = enrich_summary_with_player_fields(
        summary,
        your_color=str(getattr(game, "your_color", "") or ""),
    )
    summary["result"] = getattr(game, "result", None)
    engine_output = {
        "schema_version": int(engine_output.get("schema_version", ENGINE_PAYLOAD_SCHEMA_VERSION) or ENGINE_PAYLOAD_SCHEMA_VERSION),
        "game_summary": summary,
        "key_positions": list(engine_output.get("key_positions") or []),
        "all_positions": list(engine_output.get("all_positions") or []),
    }
    validation = validate_engine_payload(
        engine_output,
        require_schema_version=True,
        require_player_fields=True,
        require_key_positions=True,
    )
    invariant_errors = _analysis_payload_invariant_errors(engine_output=engine_output, validation=validation)
    if invariant_errors:
        detail = ";".join(invariant_errors)
        raise RuntimeError(f"Engine payload invalid: {detail}")

    llm_payload = build_llm_safe_payload(
        engine_output,
        game_context={
            "date_utc": getattr(game, "end_dt_utc").astimezone(get_display_timezone()).strftime("%Y-%m-%d"),
            "your_color": getattr(game, "your_color", None),
            "opponent": getattr(game, "opponent", None),
            "result": getattr(game, "result", None),
            "time_control": getattr(game, "time_control", None),
            "rated": getattr(game, "rated", None),
            "rules": getattr(game, "rules", None),
            "url": getattr(game, "game_url", None),
        },
    )
    llm_payload = _to_player_only_prompt_payload(llm_payload)
    key_positions = llm_payload.get("key_positions") or []
    if len(key_positions) != 4:
        raise RuntimeError("Engine payload invalid: key_positions_must_have_exactly_four")

    system_template = load_prompt_file("review_system.md")
    user_template = load_prompt_file("review_user_strict.md")
    review_markdown = llm_runner(
        system_template,
        user_template.format(payload=json.dumps(llm_payload, ensure_ascii=True, separators=(",", ":"))),
    )

    board = _board_from_pgn(getattr(game, "pgn", ""))
    if board is not None:
        try:
            from review.validation import filter_output_to_allowed_sans, validate_suggested_moves

            review_markdown = validate_suggested_moves(board, review_markdown)
            allowed_sans = _collect_allowed_sans(llm_payload)
            review_markdown = filter_output_to_allowed_sans(review_markdown, allowed_sans)
        except Exception:
            logger.debug("Suggested move validation skipped due to validation error.", exc_info=True)

    return review_markdown


def _run_stockfish_oracle(*, game: Any, args: Any, logger: Any) -> Optional[Mapping[str, Any]]:
    if not bool(getattr(args, "enable_engine", False)):
        logger.error("Engine oracle is disabled in strict mode.")
        return None

    try:
        from engine.stockfish_oracle import StockfishOracle

        oracle = StockfishOracle(
            stockfish_path=str(getattr(args, "stockfish_path", "") or ""),
            depth=int(getattr(args, "engine_depth", 15) or 15),
        )
        return oracle.analyze_game(str(getattr(game, "pgn", "") or ""), include_trace=True)
    except Exception as exc:
        logger.error("Stockfish oracle failed: %s", exc)
        return None


def _board_from_pgn(pgn_text: str) -> Optional[Any]:
    if not pgn_text:
        return None

    try:
        import chess.pgn

        parsed = chess.pgn.read_game(StringIO(pgn_text))
        if parsed is None:
            return None
        board = parsed.board()
        for move in parsed.mainline_moves():
            if move not in board.legal_moves:
                break
            board.push(move)
        return board
    except Exception:
        return None


def _collect_allowed_sans(llm_payload: Mapping[str, Any]) -> set[str]:
    allowed: set[str] = set()
    for item in llm_payload.get("key_positions") or []:
        if not isinstance(item, Mapping):
            continue
        for key in ("played_san", "best_san"):
            token = str(item.get(key) or "").strip()
            if token:
                allowed.add(token)
    return allowed


def _raw_engine_payload_required_field_errors(engine_output: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    schema_version = int(engine_output.get("schema_version", 1) or 1)
    if schema_version != ENGINE_PAYLOAD_SCHEMA_VERSION:
        errors.append(f"unsupported_schema_version:{schema_version}")

    summary_raw = engine_output.get("game_summary")
    if not isinstance(summary_raw, Mapping):
        return ["game_summary_missing_or_invalid"]
    summary = dict(summary_raw)
    summary_schema_version = int(summary.get("schema_version", schema_version) or schema_version)
    if summary_schema_version != ENGINE_PAYLOAD_SCHEMA_VERSION:
        errors.append(f"game_summary_schema_version_mismatch:{summary_schema_version}")

    key_positions = engine_output.get("key_positions")
    if not isinstance(key_positions, list):
        errors.append("key_positions_missing_or_invalid")
    elif len(key_positions) != 4:
        errors.append(f"key_positions_must_have_exactly_four actual:{len(key_positions)}")

    white_counts = normalize_label_counts(summary.get("label_counts_white"))
    black_counts = normalize_label_counts(summary.get("label_counts_black"))
    if white_counts is None:
        errors.append("label_counts_white_missing_or_invalid")
        white_counts = {key: 0 for key in LABEL_KEYS}
    if black_counts is None:
        errors.append("label_counts_black_missing_or_invalid")
        black_counts = {key: 0 for key in LABEL_KEYS}

    total_plies = int(summary.get("total_plies", 0) or 0)
    white_plies = int(summary.get("white_plies", -1) or -1)
    black_plies = int(summary.get("black_plies", -1) or -1)
    if total_plies <= 0:
        errors.append("total_plies_must_be_positive")
    if white_plies < 0 or black_plies < 0:
        errors.append("side_plies_missing_or_invalid")
    if total_plies > 0 and white_plies >= 0 and black_plies >= 0 and int(white_plies + black_plies) != int(total_plies):
        errors.append(f"total_plies_side_split_mismatch expected:{total_plies} actual:{white_plies + black_plies}")

    labeled_white = int(sum(int(white_counts.get(key, 0)) for key in LABEL_KEYS))
    labeled_black = int(sum(int(black_counts.get(key, 0)) for key in LABEL_KEYS))
    unlabeled_white = max(0, int(summary.get("unlabeled_white_plies", max(0, white_plies - labeled_white)) or 0))
    unlabeled_black = max(0, int(summary.get("unlabeled_black_plies", max(0, black_plies - labeled_black)) or 0))
    if white_plies >= 0 and int(labeled_white + unlabeled_white) != int(white_plies):
        errors.append(f"white_plies_accounting_mismatch expected:{white_plies} actual:{labeled_white + unlabeled_white}")
    if black_plies >= 0 and int(labeled_black + unlabeled_black) != int(black_plies):
        errors.append(f"black_plies_accounting_mismatch expected:{black_plies} actual:{labeled_black + unlabeled_black}")
    return errors


def _analysis_payload_invariant_errors(
    *,
    engine_output: Mapping[str, Any],
    validation: Any,
) -> list[str]:
    errors: list[str] = []
    if not bool(getattr(validation, "is_valid", False)):
        errors.extend(list(getattr(validation, "errors", ()) or ()))
    summary_raw = engine_output.get("game_summary")
    if not isinstance(summary_raw, Mapping):
        errors.append("game_summary_missing_or_invalid")
        return errors
    summary = dict(summary_raw)

    schema_version = int(engine_output.get("schema_version", summary.get("schema_version", 1)) or 1)
    if schema_version != ENGINE_PAYLOAD_SCHEMA_VERSION:
        errors.append(f"unsupported_schema_version:{schema_version}")

    key_positions = engine_output.get("key_positions")
    if not isinstance(key_positions, list):
        errors.append("key_positions_missing_or_invalid")
    elif len(key_positions) != 4:
        errors.append(f"key_positions_must_have_exactly_four actual:{len(key_positions)}")
    else:
        errors.extend(_key_positions_top_swing_errors(engine_output=engine_output, key_positions=key_positions))

    your_color = str(summary.get("your_color", "")).strip().lower()
    if your_color not in {"white", "black"}:
        errors.append("your_color_missing_or_invalid")

    white_counts = normalize_label_counts(summary.get("label_counts_white"))
    black_counts = normalize_label_counts(summary.get("label_counts_black"))
    if white_counts is None:
        errors.append("label_counts_white_missing_or_invalid")
        white_counts = {key: 0 for key in LABEL_KEYS}
    if black_counts is None:
        errors.append("label_counts_black_missing_or_invalid")
        black_counts = {key: 0 for key in LABEL_KEYS}

    try:
        total_plies = int(summary.get("total_plies", 0) or 0)
        white_plies = int(summary.get("white_plies", -1) or -1)
        black_plies = int(summary.get("black_plies", -1) or -1)
    except Exception:
        total_plies = 0
        white_plies = -1
        black_plies = -1
    if total_plies <= 0:
        errors.append("total_plies_must_be_positive")
    if white_plies < 0 or black_plies < 0:
        errors.append("side_plies_missing_or_invalid")
    if total_plies > 0 and white_plies >= 0 and black_plies >= 0 and int(white_plies + black_plies) != int(total_plies):
        errors.append(f"total_plies_side_split_mismatch expected:{total_plies} actual:{white_plies + black_plies}")

    labeled_white = int(sum(int(white_counts.get(key, 0)) for key in LABEL_KEYS))
    labeled_black = int(sum(int(black_counts.get(key, 0)) for key in LABEL_KEYS))
    unlabeled_white = max(0, int(summary.get("unlabeled_white_plies", max(0, white_plies - labeled_white)) or 0))
    unlabeled_black = max(0, int(summary.get("unlabeled_black_plies", max(0, black_plies - labeled_black)) or 0))
    if white_plies >= 0 and labeled_white + unlabeled_white != white_plies:
        errors.append(
            f"white_plies_accounting_mismatch expected:{white_plies} actual:{labeled_white + unlabeled_white}"
        )
    if black_plies >= 0 and labeled_black + unlabeled_black != black_plies:
        errors.append(
            f"black_plies_accounting_mismatch expected:{black_plies} actual:{labeled_black + unlabeled_black}"
        )

    return errors


def _key_positions_top_swing_errors(
    *,
    engine_output: Mapping[str, Any],
    key_positions: Sequence[Mapping[str, Any]],
) -> list[str]:
    all_positions_raw = engine_output.get("all_positions")
    if not isinstance(all_positions_raw, list):
        return ["all_positions_missing_or_invalid_for_key_position_consistency"]

    all_positions = [row for row in all_positions_raw if isinstance(row, Mapping)]
    if not all_positions:
        return ["all_positions_missing_or_invalid_for_key_position_consistency"]

    expected = _expected_top_swing_positions(all_positions=all_positions, required=4)
    expected_ids = [_position_identity(row) for row in expected]
    actual_ids = [_position_identity(row) for row in key_positions]
    if actual_ids != expected_ids:
        return ["key_positions_not_top_eval_swings"]
    return []


def _expected_top_swing_positions(*, all_positions: Sequence[Mapping[str, Any]], required: int) -> list[Mapping[str, Any]]:
    ranked = sorted(
        all_positions,
        key=lambda row: (
            -_position_abs_eval_swing(row),
            int(row.get("move_number", 0) or 0),
            str(row.get("player", "") or ""),
            str(row.get("played_san", "") or ""),
            str(row.get("best_san", "") or ""),
        ),
    )
    selected: list[Mapping[str, Any]] = list(ranked[:required])
    if len(selected) < required and selected:
        seed = list(selected)
        idx = 0
        while len(selected) < required:
            selected.append(seed[idx % len(seed)])
            idx += 1
    return selected


def _position_abs_eval_swing(row: Mapping[str, Any]) -> float:
    raw = row.get("abs_eval_swing", row.get("_abs_eval_swing", 0.0))
    try:
        return abs(float(raw or 0.0))
    except Exception:
        return 0.0


def _position_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(row.get("move_number", 0) or 0),
        str(row.get("player", "") or ""),
        str(row.get("played_san", "") or ""),
        str(row.get("best_san", "") or ""),
        str(row.get("label", "") or ""),
        str(row.get("tactical_flag", "") or ""),
    )


def _to_player_only_prompt_payload(llm_payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": int(llm_payload.get("schema_version", ENGINE_PAYLOAD_SCHEMA_VERSION) or ENGINE_PAYLOAD_SCHEMA_VERSION),
        "game_summary": dict(llm_payload.get("game_summary") or {}),
        "key_positions": list(llm_payload.get("key_positions") or []),
    }
    summary = dict(payload["game_summary"])
    your_color = str(summary.get("your_color", "")).strip().lower()
    white_counts = normalize_label_counts(summary.get("label_counts_white")) or {key: 0 for key in LABEL_KEYS}
    black_counts = normalize_label_counts(summary.get("label_counts_black")) or {key: 0 for key in LABEL_KEYS}
    white_plies = int(summary.get("white_plies", 0) or 0)
    black_plies = int(summary.get("black_plies", 0) or 0)
    if your_color == "black":
        player_counts = dict(black_counts)
        player_plies = int(max(0, black_plies))
    else:
        player_counts = dict(white_counts)
        player_plies = int(max(0, white_plies))

    player_error_plies = int(
        int(player_counts.get("inaccuracy", 0))
        + int(player_counts.get("mistake", 0))
        + int(player_counts.get("blunder", 0))
    )
    denom = float(max(1, player_plies))
    metadata_keys = (
        "schema_version",
        "engine_depth",
        "date_utc",
        "your_color",
        "opponent",
        "result",
        "time_control",
        "rated",
        "rules",
        "url",
    )
    player_summary: dict[str, Any] = {}
    for key in metadata_keys:
        if key in summary:
            player_summary[key] = summary.get(key)
    player_summary["player_plies_analyzed"] = int(player_plies)
    player_summary["player_label_counts_plies"] = dict(player_counts)
    player_summary["player_error_plies"] = int(player_error_plies)
    player_summary["player_error_rate_per_ply"] = round(float(player_error_plies) / denom, 4)
    player_summary["player_inaccuracy_rate_per_ply"] = round(float(player_counts.get("inaccuracy", 0)) / denom, 4)
    player_summary["player_mistake_rate_per_ply"] = round(float(player_counts.get("mistake", 0)) / denom, 4)
    player_summary["player_blunder_rate_per_ply"] = round(float(player_counts.get("blunder", 0)) / denom, 4)
    payload["game_summary"] = player_summary
    return payload
