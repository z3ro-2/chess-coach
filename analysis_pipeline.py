"""Coordinator that keeps engine, LLM, and review validation roles separated."""

from __future__ import annotations

import json
import os
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
from src.config.provider_config import get_provider
from src.llm_diagnostics import prompt_hash, split_model_name_version, hash_text_sha256
from src.utils.timezone import get_display_timezone


class LLMFormatViolationError(RuntimeError):
    """Raised when per-game LLM output is not strict JSON or fails schema validation."""


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
    llm_payload = _to_player_only_prompt_payload(
        llm_payload=llm_payload,
        engine_output=engine_output,
    )
    distilled = llm_payload.get("distilled_insights")
    if not isinstance(distilled, Mapping):
        raise RuntimeError("Engine payload invalid: distilled_insights_missing_or_invalid")
    top_3 = list(distilled.get("top_3_worst_moves") or [])
    if not top_3:
        raise RuntimeError("Engine payload invalid: top_3_worst_moves_missing_or_invalid")
    if len(list(engine_output.get("key_positions") or [])) != 4:
        raise RuntimeError("Engine payload invalid: key_positions_must_have_exactly_four")

    system_template = load_prompt_file("review_system.md")
    user_template = load_prompt_file("review_user_strict.md")
    user_prompt = user_template.replace(
        "{payload}",
        json.dumps(llm_payload, ensure_ascii=True, separators=(",", ":")),
    )
    model_name, model_version = _resolve_model_identity(args)
    hash_temperature, hash_top_p, hash_max_tokens = _resolve_prompt_hash_config(args)
    review_json, llm_diag = _call_llm_review_json_with_retry(
        llm_runner=llm_runner,
        system_template=system_template,
        user_prompt=user_prompt,
        critique_payload=llm_payload,
        self_critique_enabled=_env_flag_enabled("ENABLE_LLM_SELF_CRITIQUE"),
        model_name=model_name,
        model_version=model_version,
        hash_temperature=hash_temperature,
        hash_top_p=hash_top_p,
        hash_max_tokens=hash_max_tokens,
        logger=logger,
    )
    review_json["confidence"] = _derive_system_confidence_for_game_review(llm_payload)
    review_markdown = format_game_review_markdown(review_json)

    board = _board_from_pgn(getattr(game, "pgn", ""))
    if board is not None:
        try:
            from review.validation import filter_output_to_allowed_sans, validate_suggested_moves

            review_markdown = validate_suggested_moves(board, review_markdown)
            allowed_sans = _collect_allowed_sans(llm_payload)
            review_markdown = filter_output_to_allowed_sans(review_markdown, allowed_sans)
        except Exception:
            logger.debug("Suggested move validation skipped due to validation error.", exc_info=True)

    review_markdown = _append_llm_diagnostics_markdown(review_markdown, llm_diag)
    return review_markdown


def validate_game_review_json(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    required_scalar = {
        "game_overview": str,
        "confidence": str,
    }
    for key, expected_type in required_scalar.items():
        if key not in data or not isinstance(data.get(key), expected_type):
            return False
    if str(data.get("confidence")).strip() not in {"LOW", "MEDIUM", "HIGH"}:
        return False

    strengths = data.get("strengths")
    if not isinstance(strengths, list) or any(not isinstance(item, str) for item in strengths):
        return False

    training_focus = data.get("training_focus")
    if not isinstance(training_focus, list) or any(not isinstance(item, str) for item in training_focus):
        return False

    critical = data.get("critical_mistakes")
    if not isinstance(critical, list):
        return False
    required_critical_keys = (
        "move_number",
        "description",
        "why_it_matters",
        "improvement_tip",
    )
    for item in critical:
        if not isinstance(item, dict):
            return False
        for key in required_critical_keys:
            if key not in item:
                return False
        move_number = item.get("move_number")
        if not isinstance(move_number, int) or isinstance(move_number, bool):
            return False
        for key in ("description", "why_it_matters", "improvement_tip"):
            if not isinstance(item.get(key), str):
                return False
    return True


def _parse_game_review_json_or_raise(raw_output: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw_output))
    except Exception as exc:
        raise LLMFormatViolationError("LLM output format violation: output is not valid JSON.") from exc
    if not isinstance(parsed, dict) or not validate_game_review_json(parsed):
        raise LLMFormatViolationError("LLM output format violation: JSON schema validation failed.")
    return dict(parsed)


def _call_llm_review_json_with_retry(
    *,
    llm_runner: Callable[[str, str], str],
    system_template: str,
    user_prompt: str,
    critique_payload: Mapping[str, Any],
    self_critique_enabled: bool,
    model_name: str,
    model_version: Optional[str],
    hash_temperature: float,
    hash_top_p: float,
    hash_max_tokens: int,
    logger: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    first_prompt_hash = prompt_hash(
        system_template,
        user_prompt,
        model_name,
        hash_temperature,
        hash_top_p,
        hash_max_tokens,
    )
    try:
        first_output = llm_runner(system_template, user_prompt)
        first_output_hash = hash_text_sha256(first_output)
        logger.info(
            "LLM generation diagnostics model_name=%s model_version=%s prompt_hash=%s output_hash=%s",
            model_name,
            model_version or "",
            first_prompt_hash,
            first_output_hash,
        )
        first_review_json = _parse_game_review_json_or_raise(first_output)
        if self_critique_enabled and not _self_critique_passes(
            llm_runner=llm_runner,
            critique_payload=critique_payload,
            review_json=first_review_json,
        ):
            raise LLMFormatViolationError("LLM self-critique rejected generated review JSON.")
        return (
            first_review_json,
            {
                "model_name": model_name,
                "model_version": model_version,
                "prompt_hash": first_prompt_hash,
                "output_hash": first_output_hash,
                "format_violation": False,
                "retry_attempted": False,
            },
        )
    except LLMFormatViolationError as first_exc:
        logger.warning(
            "LLM format violation detected for game review; format_violation=true retry_attempted=true error=%s",
            first_exc,
        )
        retry_prompt = (
            f"{user_prompt}\n\n"
            "Your previous response violated format. Return ONLY valid JSON matching schema."
        )
        retry_prompt_hash = prompt_hash(
            system_template,
            retry_prompt,
            model_name,
            hash_temperature,
            hash_top_p,
            hash_max_tokens,
        )
        try:
            retry_output = llm_runner(system_template, retry_prompt)
            retry_output_hash = hash_text_sha256(retry_output)
            logger.info(
                "LLM generation diagnostics model_name=%s model_version=%s prompt_hash=%s output_hash=%s",
                model_name,
                model_version or "",
                retry_prompt_hash,
                retry_output_hash,
            )
            retry_review_json = _parse_game_review_json_or_raise(retry_output)
            if self_critique_enabled and not _self_critique_passes(
                llm_runner=llm_runner,
                critique_payload=critique_payload,
                review_json=retry_review_json,
            ):
                raise LLMFormatViolationError("LLM self-critique rejected retried review JSON.")
            return (
                retry_review_json,
                {
                    "model_name": model_name,
                    "model_version": model_version,
                    "prompt_hash": retry_prompt_hash,
                    "output_hash": retry_output_hash,
                    "format_violation": True,
                    "retry_attempted": True,
                },
            )
        except LLMFormatViolationError:
            logger.info(
                "LLM generation diagnostics model_name=%s model_version=%s prompt_hash=%s output_hash=%s",
                model_name,
                model_version or "",
                retry_prompt_hash,
                retry_output_hash,
            )
            logger.error(
                "LLM format violation persisted for game review after retry; "
                "format_violation=true retry_attempted=true"
            )
            payload = _extract_payload_from_user_prompt(user_prompt)
            return (
                _deterministic_game_review_fallback_json(payload),
                {
                    "model_name": model_name,
                    "model_version": model_version,
                    "prompt_hash": retry_prompt_hash,
                    "output_hash": retry_output_hash,
                    "format_violation": True,
                    "retry_attempted": True,
                },
            )


def _resolve_model_identity(args: Any) -> tuple[str, Optional[str]]:
    provider = str(get_provider() or "").strip().lower()
    if provider == "gpt":
        model = str(getattr(args, "gpt_model", "") or "").strip()
    else:
        model = str(getattr(args, "ollama_model", "") or "").strip()
    name, version = split_model_name_version(model)
    if not name:
        name = "unknown"
    return name, version


def _resolve_prompt_hash_config(args: Any) -> tuple[float, float, int]:
    provider = str(get_provider() or "").strip().lower()
    if provider == "gpt":
        max_tokens = int(getattr(args, "max_tokens", 1400) or 1400)
        return 0.4, 1.0, max_tokens
    temperature = _env_float("LLM_TEMPERATURE", 0.4)
    top_p = _env_float("LLM_TOP_P", 1.0)
    max_tokens = _env_int("LLM_MAX_TOKENS", 1400)
    return float(temperature), float(top_p), int(max_tokens)


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _append_llm_diagnostics_markdown(markdown_text: str, diagnostics: Mapping[str, Any]) -> str:
    clean = str(markdown_text or "").rstrip()
    payload = json.dumps(
        {
            "model_name": str(diagnostics.get("model_name", "unknown") or "unknown"),
            "model_version": diagnostics.get("model_version"),
            "prompt_hash": str(diagnostics.get("prompt_hash", "") or ""),
            "output_hash": str(diagnostics.get("output_hash", "") or ""),
            "format_violation": bool(diagnostics.get("format_violation", False)),
            "retry_attempted": bool(diagnostics.get("retry_attempted", False)),
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"{clean}\n\n## LLM Diagnostics\n{payload}\n"


def _derive_system_confidence_for_game_review(llm_payload: Mapping[str, Any]) -> str:
    distilled = llm_payload.get("distilled_insights")
    if not isinstance(distilled, Mapping):
        return "LOW"
    player_plies = int(distilled.get("player_plies_analyzed", 0) or 0)
    top_worst = list(distilled.get("top_3_worst_moves") or [])
    has_largest = isinstance(distilled.get("largest_eval_swing"), Mapping) and bool(distilled.get("largest_eval_swing"))
    target = max(1, min(3, player_plies))
    coverage = float(len(top_worst)) / float(target)
    if player_plies < 12 or not has_largest or coverage < 0.67:
        return "LOW"
    if player_plies < 30 or coverage < 1.0:
        return "MEDIUM"
    return "HIGH"


def _env_flag_enabled(name: str) -> bool:
    value = str(os.environ.get(name, "") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _self_critique_passes(
    *,
    llm_runner: Callable[[str, str], str],
    critique_payload: Mapping[str, Any],
    review_json: Mapping[str, Any],
) -> bool:
    critique_system = "You are a strict engine-fact compliance checker. Respond with YES or NO only."
    critique_user = (
        "Engine facts JSON:\n"
        f"{json.dumps(critique_payload, ensure_ascii=True, separators=(',', ':'))}\n\n"
        "Generated review JSON:\n"
        f"{json.dumps(review_json, ensure_ascii=True, separators=(',', ':'))}\n\n"
        "Does this advice strictly match the engine facts? YES or NO."
    )
    response = str(llm_runner(critique_system, critique_user) or "").strip().upper()
    return response.startswith("YES")


def _extract_payload_from_user_prompt(user_prompt: str) -> Mapping[str, Any]:
    marker = "Payload JSON:\n"
    if marker not in user_prompt:
        return {}
    raw = user_prompt.split(marker, 1)[1].strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _deterministic_game_review_fallback_json(payload: Mapping[str, Any]) -> dict[str, Any]:
    distilled = dict(payload.get("distilled_insights") or {})
    top_worst = [dict(item or {}) for item in list(distilled.get("top_3_worst_moves") or [])]
    tactical = dict(distilled.get("tactical_error_summary") or {})
    material = dict(distilled.get("material_loss_summary") or {})
    phase = dict(distilled.get("phase_error_distribution") or {})

    top_blunder = next((row for row in top_worst if str(row.get("label", "")).lower() == "blunder"), {})
    if not top_blunder and top_worst:
        top_blunder = dict(top_worst[0])
    move_number = int(top_blunder.get("move_number", 0) or 0)
    played_san = str(top_blunder.get("played_san", "") or "")
    best_san = str(top_blunder.get("best_san", "") or "")
    eval_swing = float(top_blunder.get("abs_eval_swing", 0.0) or 0.0)
    blunder_count = int(tactical.get("blunder_like_events", 0) or 0)
    tactical_counts = dict(tactical.get("by_flag") or {})
    tactical_summary = ", ".join(f"{k}:{int(tactical_counts[k])}" for k in sorted(tactical_counts.keys())) if tactical_counts else "none"
    total_errors = int(phase.get("total_error_moves", len(top_worst)) or 0)
    player_plies = int(distilled.get("player_plies_analyzed", 0) or 0)

    if move_number > 0:
        critical_description = f"Move {move_number}: played {played_san or '?'} instead of {best_san or '?'}."
        critical_why = f"Eval swing estimate: {round(eval_swing, 2)}."
    else:
        critical_description = "No explicit worst move was available in distilled insights."
        critical_why = "Fallback used distilled aggregates only."

    primary_flag = sorted(tactical_counts.keys())[0] if tactical_counts else "general_calculation"
    advice_by_flag = {
        "hanging_piece": "Run a hanging-piece safety check for both sides before every move.",
        "tactical_miss": "Spend 20 seconds on checks, captures, and threats before committing.",
        "mate_threat": "Prioritize king-safety checks when forcing moves appear.",
        "general_calculation": "Use a fixed checks-captures-threats scan before each move.",
    }
    training_tip = advice_by_flag.get(primary_flag, advice_by_flag["general_calculation"])

    overview = (
        f"Deterministic fallback review: {total_errors} errors across {player_plies} player plies; "
        f"blunders recorded: {blunder_count}."
    )
    return {
        "game_overview": overview,
        "critical_mistakes": [
            {
                "move_number": move_number,
                "description": critical_description,
                "why_it_matters": critical_why,
                "improvement_tip": training_tip,
            }
        ],
        "strengths": [
            f"Tactical flag summary: {tactical_summary}.",
            "Key positions remained player-scoped and engine-derived.",
        ],
        "training_focus": [
            f"Blunder count target: reduce from {blunder_count} to {max(0, blunder_count - 1)} next game.",
            training_tip,
            f"Review material-loss events: {int(material.get('material_loss_events', 0) or 0)} "
            f"(total loss {round(float(material.get('total_material_loss', 0.0) or 0.0), 2)}).",
        ],
        "confidence": "LOW",
    }


def format_game_review_markdown(validated_json: Mapping[str, Any]) -> str:
    lines: list[str] = ["# Game Review", "", "## Summary", str(validated_json.get("game_overview", "")), ""]

    lines.append("## Critical Mistakes")
    critical = validated_json.get("critical_mistakes") or []
    if isinstance(critical, list) and critical:
        for idx, item in enumerate(critical, start=1):
            row = item if isinstance(item, Mapping) else {}
            lines.extend(
                [
                    f"### {int(idx)}. Move {int(row.get('move_number', 0) or 0)}",
                    f"- Description: {str(row.get('description', ''))}",
                    f"- Why It Matters: {str(row.get('why_it_matters', ''))}",
                    f"- Improvement Tip: {str(row.get('improvement_tip', ''))}",
                    "",
                ]
            )
    else:
        lines.append("- None provided.")
        lines.append("")

    lines.append("## Strengths")
    strengths = validated_json.get("strengths") or []
    if isinstance(strengths, list) and strengths:
        for item in strengths:
            lines.append(f"- {str(item)}")
    else:
        lines.append("- None provided.")
    lines.append("")

    lines.append("## Training Focus")
    training_focus = validated_json.get("training_focus") or []
    if isinstance(training_focus, list) and training_focus:
        for item in training_focus:
            lines.append(f"- {str(item)}")
    else:
        lines.append("- None provided.")
    lines.append("")
    lines.append("## Confidence")
    lines.append(f"- {str(validated_json.get('confidence', ''))}")
    return "\n".join(lines).strip() + "\n"


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
    distilled = llm_payload.get("distilled_insights")
    if isinstance(distilled, Mapping):
        for item in list(distilled.get("top_3_worst_moves") or []):
            if not isinstance(item, Mapping):
                continue
            for key in ("played_san", "best_san"):
                token = str(item.get(key) or "").strip()
                if token:
                    allowed.add(token)
        largest = distilled.get("largest_eval_swing")
        if isinstance(largest, Mapping):
            for key in ("played_san", "best_san"):
                token = str(largest.get(key) or "").strip()
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


def _to_player_only_prompt_payload(
    *,
    llm_payload: Mapping[str, Any],
    engine_output: Mapping[str, Any],
) -> dict[str, Any]:
    summary = dict(llm_payload.get("game_summary") or {})
    your_color = str(summary.get("your_color", "")).strip().lower()
    white_plies = int(summary.get("white_plies", 0) or 0)
    black_plies = int(summary.get("black_plies", 0) or 0)
    player_plies = int(max(0, black_plies if your_color == "black" else white_plies))
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
    context: dict[str, Any] = {}
    for key in metadata_keys:
        if key in summary:
            context[key] = summary.get(key)

    player_rows = _player_rows_from_engine_output(engine_output, your_color=your_color)
    error_rows = [row for row in player_rows if _is_error_label(str(row.get("label", "") or ""))]
    top_3 = error_rows[:3] if error_rows else player_rows[:3]
    top_3_worst_moves = [_distilled_move_row(row) for row in top_3]
    largest_source = (
        max(player_rows, key=lambda row: _position_abs_eval_swing(row))
        if player_rows
        else {}
    )
    largest_eval_swing = _distilled_move_row(largest_source) if largest_source else {}

    tactical_counts: dict[str, int] = {}
    blunder_like_events = 0
    for row in error_rows:
        flag = str(row.get("tactical_flag", "none") or "none").strip().lower()
        if flag not in {"", "none"}:
            tactical_counts[flag] = tactical_counts.get(flag, 0) + 1
        if str(row.get("label", "")).strip().lower() == "blunder":
            blunder_like_events += 1

    negative_material = [float(row.get("material_change", 0.0) or 0.0) for row in player_rows if float(row.get("material_change", 0.0) or 0.0) < 0.0]
    phase_counts = {"opening": 0, "middlegame": 0, "endgame": 0}
    for row in error_rows:
        phase = _phase_from_move_number(int(row.get("move_number", 0) or 0))
        phase_counts[phase] += 1

    return {
        "schema_version": int(llm_payload.get("schema_version", ENGINE_PAYLOAD_SCHEMA_VERSION) or ENGINE_PAYLOAD_SCHEMA_VERSION),
        "context": context,
        "distilled_insights": {
            "player_plies_analyzed": int(player_plies),
            "top_3_worst_moves": top_3_worst_moves,
            "largest_eval_swing": largest_eval_swing,
            "tactical_error_summary": {
                "total_tactical_errors": int(sum(int(v) for v in tactical_counts.values())),
                "by_flag": {key: int(tactical_counts[key]) for key in sorted(tactical_counts.keys())},
                "blunder_like_events": int(blunder_like_events),
            },
            "material_loss_summary": {
                "material_loss_events": int(len(negative_material)),
                "total_material_loss": round(float(sum(-value for value in negative_material)), 3),
                "largest_single_material_loss": round(float(max((-value for value in negative_material), default=0.0)), 3),
            },
            "phase_error_distribution": {
                "opening": int(phase_counts["opening"]),
                "middlegame": int(phase_counts["middlegame"]),
                "endgame": int(phase_counts["endgame"]),
                "total_error_moves": int(len(error_rows)),
            },
        },
    }


def _player_rows_from_engine_output(engine_output: Mapping[str, Any], *, your_color: str) -> list[dict[str, Any]]:
    all_positions = engine_output.get("all_positions")
    if isinstance(all_positions, list) and all_positions:
        rows = [row for row in all_positions if isinstance(row, Mapping)]
    else:
        rows = [row for row in list(engine_output.get("key_positions") or []) if isinstance(row, Mapping)]
    normalized_color = str(your_color or "").strip().lower()
    player_rows = [dict(row) for row in rows if str(row.get("player", "")).strip().lower() == normalized_color]
    player_rows.sort(
        key=lambda row: (
            _severity_rank(str(row.get("label", "") or "")),
            _position_abs_eval_swing(row),
            int(row.get("move_number", 0) or 0),
        ),
        reverse=True,
    )
    return player_rows


def _is_error_label(label: str) -> bool:
    return str(label or "").strip().lower() in {"inaccuracy", "mistake", "blunder"}


def _severity_rank(label: str) -> int:
    value = str(label or "").strip().lower()
    if value == "blunder":
        return 3
    if value == "mistake":
        return 2
    if value == "inaccuracy":
        return 1
    return 0


def _phase_from_move_number(move_number: int) -> str:
    n = int(max(0, move_number))
    if n <= 10:
        return "opening"
    if n <= 30:
        return "middlegame"
    return "endgame"


def _distilled_move_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "move_number": int(row.get("move_number", 0) or 0),
        "label": str(row.get("label", "") or ""),
        "played_san": str(row.get("played_san", "") or ""),
        "best_san": str(row.get("best_san", "") or ""),
        "tactical_flag": str(row.get("tactical_flag", "none") or "none"),
        "material_change": float(row.get("material_change", 0.0) or 0.0),
        "abs_eval_swing": round(_position_abs_eval_swing(row), 3),
        "phase": _phase_from_move_number(int(row.get("move_number", 0) or 0)),
    }
