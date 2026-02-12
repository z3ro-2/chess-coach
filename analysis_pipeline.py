"""Coordinator that keeps engine, LLM, and review validation roles separated."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from engine.payload_schema import enrich_summary_with_player_fields, validate_engine_payload
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

    summary = dict(engine_output.get("game_summary") or {})
    summary = enrich_summary_with_player_fields(
        summary,
        your_color=str(getattr(game, "your_color", "") or ""),
    )
    summary["result"] = getattr(game, "result", None)
    engine_output = {
        "game_summary": summary,
        "key_positions": list(engine_output.get("key_positions") or []),
    }
    validation = validate_engine_payload(
        engine_output,
        require_schema_version=True,
        require_player_fields=True,
        require_key_positions=True,
    )
    if not validation.is_valid:
        detail = ";".join(validation.errors)
        raise RuntimeError(f"Stockfish payload invariants failed: {detail}")

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
    key_positions = llm_payload.get("key_positions") or []
    if len(key_positions) != 4:
        raise RuntimeError("Stockfish oracle must return exactly 4 key positions.")

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
        return oracle.analyze_game(str(getattr(game, "pgn", "") or ""))
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
