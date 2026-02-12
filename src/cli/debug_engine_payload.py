"""Debug utility for validating engine payload invariants against PGN-derived move counts."""

from __future__ import annotations

import argparse
import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any

from engine.payload_schema import enrich_summary_with_player_fields, expected_side_plies, validate_engine_payload


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _count_pgn_plies_by_side(pgn_text: str) -> tuple[int, int, int]:
    try:
        import chess.pgn
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("python-chess is required to parse PGN") from exc

    game = chess.pgn.read_game(StringIO(pgn_text))
    if game is None:
        raise RuntimeError("PGN could not be parsed")

    board = game.board()
    total = 0
    white = 0
    black = 0
    for move in game.mainline_moves():
        if board.turn:
            white += 1
        else:
            black += 1
        board.push(move)
        total += 1
    return total, white, black


def _load_payload_from_json(path: str) -> dict[str, Any]:
    raw = json.loads(_read_text(path))
    if not isinstance(raw, dict):
        raise RuntimeError("payload JSON must be an object")
    return raw


def _build_payload_from_oracle(*, pgn_text: str, stockfish_path: str, depth: int, your_color: str) -> dict[str, Any]:
    from engine.stockfish_oracle import StockfishOracle

    oracle = StockfishOracle(stockfish_path=stockfish_path, depth=depth)
    output = oracle.analyze_game(pgn_text)
    summary = enrich_summary_with_player_fields(
        dict(output.get("game_summary") or {}),
        your_color=your_color,
    )
    return {
        "game_summary": summary,
        "key_positions": list(output.get("key_positions") or []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate engine payload math invariants")
    parser.add_argument("--payload-json", type=str, default="", help="Path to stored payload JSON")
    parser.add_argument("--pgn-file", type=str, default="", help="Optional PGN file for move-count verification")
    parser.add_argument("--run-oracle", action="store_true", help="Generate payload from Stockfish instead of reading JSON")
    parser.add_argument("--stockfish-path", type=str, default="", help="Stockfish binary path when --run-oracle is set")
    parser.add_argument("--engine-depth", type=int, default=15, help="Stockfish depth when --run-oracle is set")
    parser.add_argument("--your-color", type=str, default="white", help="white or black")
    parser.add_argument("--require-four", action="store_true", help="Require exactly four key positions")
    args = parser.parse_args(argv)

    payload: dict[str, Any]
    pgn_text = _read_text(args.pgn_file) if args.pgn_file else ""

    if args.run_oracle:
        if not pgn_text:
            raise RuntimeError("--run-oracle requires --pgn-file")
        if not str(args.stockfish_path).strip():
            raise RuntimeError("--run-oracle requires --stockfish-path")
        payload = _build_payload_from_oracle(
            pgn_text=pgn_text,
            stockfish_path=str(args.stockfish_path),
            depth=max(1, int(args.engine_depth)),
            your_color=str(args.your_color).strip().lower(),
        )
    else:
        if not str(args.payload_json).strip():
            raise RuntimeError("Provide --payload-json or use --run-oracle")
        payload = _load_payload_from_json(str(args.payload_json))

    validation = validate_engine_payload(
        payload,
        require_schema_version=True,
        require_player_fields=True,
        require_key_positions=bool(args.require_four),
    )

    summary = payload.get("game_summary") if isinstance(payload.get("game_summary"), dict) else {}
    total_plies = int(summary.get("total_plies", 0) or 0)
    expected = expected_side_plies(total_plies)

    print("Engine Payload Debug Report")
    print(f"- valid: {validation.is_valid}")
    print(f"- schema_version: {validation.schema_version}")
    print(f"- total_plies: {validation.total_plies}")
    print(f"- total_moves: {validation.total_moves}")
    print(f"- expected_side_plies.white: {expected['white']}")
    print(f"- expected_side_plies.black: {expected['black']}")
    print(f"- player_color: {validation.your_color}")
    print(f"- player_total_plies: {validation.player_total_plies}")
    print(f"- player_label_counts: {dict(validation.player_label_counts)}")
    print(f"- label_counts_by_side.white: {dict(validation.label_counts_by_side.get('white', {}))}")
    print(f"- label_counts_by_side.black: {dict(validation.label_counts_by_side.get('black', {}))}")

    if pgn_text:
        pgn_total, pgn_white, pgn_black = _count_pgn_plies_by_side(pgn_text)
        print(f"- pgn_total_plies: {pgn_total}")
        print(f"- pgn_white_plies: {pgn_white}")
        print(f"- pgn_black_plies: {pgn_black}")

    if validation.errors:
        print("- errors:")
        for err in validation.errors:
            print(f"  - {err}")

    return 0 if validation.is_valid else 1


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
