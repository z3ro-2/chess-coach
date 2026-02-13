"""Deterministic PGN label audit against stored engine payload labels.

This audit:
1) samples analyzed games from engine_payloads,
2) replays each PGN through Stockfish move-by-move,
3) re-derives eval swings and labels,
4) compares stored key-position labels vs recomputed labels,
5) fails if mismatch rate exceeds threshold (default 2%).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from engine.payload_schema import LABEL_KEYS, normalize_label_counts, validate_engine_payload
from engine.stockfish_oracle import StockfishOracle

MIN_SAMPLE_SIZE = 5
MAX_SAMPLE_SIZE = 10
DEFAULT_SAMPLE_SIZE = 8
DEFAULT_MAX_MISMATCH_RATE = 0.02
DEFAULT_SEED = 17


@dataclass(frozen=True)
class _AuditCandidate:
    game_url: str
    engine_depth: int
    payload: Mapping[str, Any]
    pgn_text: str


@dataclass(frozen=True)
class GameAuditResult:
    game_url: str
    compared_labels: int
    mismatched_labels: int
    mismatch_rate: float
    count_delta_total: int
    mismatch_examples: Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class AuditSummary:
    eligible_games: int
    sampled_games: int
    compared_labels: int
    mismatched_labels: int
    mismatch_rate: float
    total_count_delta: int
    threshold: float
    passed: bool
    games: Sequence[GameAuditResult]
    skipped_missing_pgn: int
    skipped_invalid_payload: int


RecomputeFn = Callable[[str, int, str], Mapping[str, Any]]


def run_label_audit(
    conn: sqlite3.Connection,
    *,
    stockfish_path: str,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SEED,
    max_mismatch_rate: float = DEFAULT_MAX_MISMATCH_RATE,
    engine_depth_override: Optional[int] = None,
    recompute_fn: Optional[RecomputeFn] = None,
) -> AuditSummary:
    sample_n = int(sample_size)
    if sample_n < MIN_SAMPLE_SIZE or sample_n > MAX_SAMPLE_SIZE:
        raise RuntimeError(f"sample_size must be between {MIN_SAMPLE_SIZE} and {MAX_SAMPLE_SIZE}")
    if max_mismatch_rate < 0.0:
        raise RuntimeError("max_mismatch_rate must be >= 0")

    candidates, skipped_missing_pgn, skipped_invalid_payload = _load_audit_candidates(conn)
    if len(candidates) < MIN_SAMPLE_SIZE:
        raise RuntimeError(
            f"Not enough eligible games for label audit: {len(candidates)} available, need at least {MIN_SAMPLE_SIZE}."
        )

    rng = random.Random(int(seed))
    actual_n = min(sample_n, len(candidates))
    sampled = candidates if len(candidates) == actual_n else rng.sample(candidates, k=actual_n)

    resolver: RecomputeFn = recompute_fn or _recompute_with_stockfish
    compared_total = 0
    mismatched_total = 0
    count_delta_total = 0
    game_results: List[GameAuditResult] = []

    for candidate in sampled:
        depth = int(engine_depth_override) if engine_depth_override is not None else int(candidate.engine_depth)
        if depth <= 0:
            depth = 15
        recomputed = resolver(candidate.pgn_text, depth, stockfish_path)
        game_result = _compare_original_to_recomputed(
            game_url=candidate.game_url,
            original_payload=candidate.payload,
            recomputed=recomputed,
        )
        compared_total += int(game_result.compared_labels)
        mismatched_total += int(game_result.mismatched_labels)
        count_delta_total += int(game_result.count_delta_total)
        game_results.append(game_result)

    if compared_total <= 0:
        raise RuntimeError("No comparable labels were found during audit.")

    mismatch_rate = float(mismatched_total) / float(max(1, compared_total))
    passed = mismatch_rate <= float(max_mismatch_rate)
    return AuditSummary(
        eligible_games=len(candidates),
        sampled_games=len(sampled),
        compared_labels=compared_total,
        mismatched_labels=mismatched_total,
        mismatch_rate=mismatch_rate,
        total_count_delta=count_delta_total,
        threshold=float(max_mismatch_rate),
        passed=bool(passed),
        games=tuple(game_results),
        skipped_missing_pgn=int(skipped_missing_pgn),
        skipped_invalid_payload=int(skipped_invalid_payload),
    )


def _load_audit_candidates(conn: sqlite3.Connection) -> tuple[List[_AuditCandidate], int, int]:
    rows = conn.execute(
        """
        SELECT
            ep.game_url,
            ep.engine_depth,
            ep.payload_json,
            COALESCE(pg.pgn_path, '')
        FROM engine_payloads AS ep
        LEFT JOIN processed_games AS pg ON pg.game_url = ep.game_url
        ORDER BY ep.end_time DESC, ep.game_url DESC
        """
    ).fetchall()

    candidates: List[_AuditCandidate] = []
    skipped_missing_pgn = 0
    skipped_invalid_payload = 0
    for row in rows:
        game_url = str(row[0] or "")
        engine_depth = int(row[1] or 0)
        payload_json = str(row[2] or "")
        pgn_path_text = str(row[3] or "")

        try:
            payload = json.loads(payload_json)
        except Exception:
            skipped_invalid_payload += 1
            continue
        if not isinstance(payload, Mapping):
            skipped_invalid_payload += 1
            continue

        validation = validate_engine_payload(
            payload,
            require_schema_version=True,
            require_player_fields=True,
            require_key_positions=True,
        )
        if not validation.is_valid:
            skipped_invalid_payload += 1
            continue

        pgn_path = Path(pgn_path_text)
        if not pgn_path_text or pgn_path_text.startswith("__backfill__/") or not pgn_path.exists():
            skipped_missing_pgn += 1
            continue
        try:
            pgn_text = pgn_path.read_text(encoding="utf-8")
        except Exception:
            skipped_missing_pgn += 1
            continue

        candidates.append(
            _AuditCandidate(
                game_url=game_url,
                engine_depth=max(1, engine_depth),
                payload=payload,
                pgn_text=pgn_text,
            )
        )
    return candidates, skipped_missing_pgn, skipped_invalid_payload


def _recompute_with_stockfish(pgn_text: str, depth: int, stockfish_path: str) -> Mapping[str, Any]:
    oracle = StockfishOracle(stockfish_path=str(stockfish_path), depth=max(1, int(depth)))
    return oracle.analyze_game(pgn_text, include_trace=True)


def _compare_original_to_recomputed(
    *,
    game_url: str,
    original_payload: Mapping[str, Any],
    recomputed: Mapping[str, Any],
) -> GameAuditResult:
    original_positions = list(original_payload.get("key_positions") or [])
    recomputed_positions = list(recomputed.get("all_positions") or [])
    recomputed_exact = _index_positions(recomputed_positions)
    recomputed_fallback = _index_positions_fallback(recomputed_positions)

    compared = 0
    mismatches = 0
    mismatch_examples: List[Mapping[str, Any]] = []
    used_exact: set[tuple[Any, ...]] = set()
    used_fallback: set[tuple[Any, ...]] = set()

    for original_row in original_positions:
        if not isinstance(original_row, Mapping):
            continue
        original_label = str(original_row.get("label", "")).strip().lower()
        if not original_label:
            continue
        compared += 1

        exact_key = _position_identity(original_row)
        fallback_key = _position_identity_fallback(original_row)

        recomputed_row = _consume_match(recomputed_exact, exact_key, used_exact)
        if recomputed_row is None:
            recomputed_row = _consume_match(recomputed_fallback, fallback_key, used_fallback)

        recomputed_label = ""
        if recomputed_row is not None:
            recomputed_label = str(recomputed_row.get("label", "")).strip().lower()

        if recomputed_row is None or recomputed_label != original_label:
            mismatches += 1
            if len(mismatch_examples) < 5:
                mismatch_examples.append(
                    {
                        "move_number": int(original_row.get("move_number", 0) or 0),
                        "player": str(original_row.get("player", "") or ""),
                        "played_san": original_row.get("played_san"),
                        "original_label": original_label,
                        "recomputed_label": recomputed_label or "<missing>",
                        "eval_before": None if recomputed_row is None else recomputed_row.get("eval_before"),
                        "played_eval": None if recomputed_row is None else recomputed_row.get("played_eval"),
                        "best_eval": None if recomputed_row is None else recomputed_row.get("best_eval"),
                        "eval_loss": None if recomputed_row is None else recomputed_row.get("eval_loss"),
                        "abs_eval_swing": None if recomputed_row is None else recomputed_row.get("abs_eval_swing"),
                    }
                )

    count_delta_total = _label_count_delta_total(
        original_summary=original_payload.get("game_summary"),
        recomputed_summary=recomputed.get("game_summary"),
    )

    mismatch_rate = float(mismatches) / float(max(1, compared))
    return GameAuditResult(
        game_url=str(game_url),
        compared_labels=int(compared),
        mismatched_labels=int(mismatches),
        mismatch_rate=float(mismatch_rate),
        count_delta_total=int(count_delta_total),
        mismatch_examples=tuple(mismatch_examples),
    )


def _index_positions(rows: Sequence[Any]) -> Dict[tuple[Any, ...], List[Mapping[str, Any]]]:
    indexed: Dict[tuple[Any, ...], List[Mapping[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = _position_identity(row)
        indexed.setdefault(key, []).append(row)
    return indexed


def _index_positions_fallback(rows: Sequence[Any]) -> Dict[tuple[Any, ...], List[Mapping[str, Any]]]:
    indexed: Dict[tuple[Any, ...], List[Mapping[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = _position_identity_fallback(row)
        indexed.setdefault(key, []).append(row)
    return indexed


def _consume_match(
    indexed: Mapping[tuple[Any, ...], Sequence[Mapping[str, Any]]],
    key: tuple[Any, ...],
    used: set[tuple[Any, ...]],
) -> Optional[Mapping[str, Any]]:
    rows = indexed.get(key) or ()
    for idx, row in enumerate(rows):
        marker = key + (idx,)
        if marker in used:
            continue
        used.add(marker)
        return row
    return None


def _position_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(row.get("move_number", 0) or 0),
        str(row.get("player", "") or "").strip().lower(),
        str(row.get("played_san", "") or "").strip(),
        str(row.get("best_san", "") or "").strip(),
    )


def _position_identity_fallback(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(row.get("move_number", 0) or 0),
        str(row.get("player", "") or "").strip().lower(),
    )


def _label_count_delta_total(
    *,
    original_summary: Any,
    recomputed_summary: Any,
) -> int:
    if not isinstance(original_summary, Mapping) or not isinstance(recomputed_summary, Mapping):
        return 0
    original_counts = normalize_label_counts(
        original_summary.get("label_counts_total", original_summary.get("label_counts"))
    )
    recomputed_counts = normalize_label_counts(
        recomputed_summary.get("label_counts_total", recomputed_summary.get("label_counts"))
    )
    if original_counts is None or recomputed_counts is None:
        return 0
    return int(sum(abs(int(original_counts[k]) - int(recomputed_counts[k])) for k in LABEL_KEYS))


def _print_summary(summary: AuditSummary) -> None:
    print("PGN Label Audit")
    print(f"- eligible_games: {summary.eligible_games}")
    print(f"- sampled_games: {summary.sampled_games}")
    print(f"- compared_labels: {summary.compared_labels}")
    print(f"- mismatched_labels: {summary.mismatched_labels}")
    print(f"- mismatch_rate: {summary.mismatch_rate:.4f}")
    print(f"- mismatch_threshold: {summary.threshold:.4f}")
    print(f"- total_label_count_delta: {summary.total_count_delta}")
    print(f"- skipped_missing_pgn: {summary.skipped_missing_pgn}")
    print(f"- skipped_invalid_payload: {summary.skipped_invalid_payload}")
    print(f"- status: {'PASS' if summary.passed else 'FAIL'}")
    for game in summary.games:
        print(
            f"  * {game.game_url}: compared={game.compared_labels} mismatched={game.mismatched_labels} "
            f"rate={game.mismatch_rate:.4f} count_delta={game.count_delta_total}"
        )
        for example in game.mismatch_examples:
            print(
                "    - mismatch "
                f"move={example.get('move_number')} player={example.get('player')} "
                f"played={example.get('played_san')} original={example.get('original_label')} "
                f"recomputed={example.get('recomputed_label')} swing={example.get('abs_eval_swing')}"
            )


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit stored key-position labels against deterministic Stockfish replay.")
    parser.add_argument(
        "--state-db",
        type=str,
        default=os.environ.get("STATE_DB", "/data/state.sqlite"),
        help="SQLite state DB containing processed_games + engine_payloads.",
    )
    parser.add_argument(
        "--stockfish-path",
        type=str,
        default=os.environ.get("STOCKFISH_PATH", "/usr/bin/stockfish"),
        help="Path to Stockfish binary.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=DEFAULT_SAMPLE_SIZE,
        help=f"Random sample size ({MIN_SAMPLE_SIZE}-{MAX_SAMPLE_SIZE}).",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Deterministic RNG seed.")
    parser.add_argument(
        "--max-mismatch-rate",
        type=float,
        default=DEFAULT_MAX_MISMATCH_RATE,
        help="Fail when mismatched_labels/compared_labels exceeds this value.",
    )
    parser.add_argument(
        "--engine-depth",
        type=int,
        default=0,
        help="Optional depth override for replay. 0 means use stored engine_depth per game.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    if not str(args.stockfish_path).strip():
        raise RuntimeError("--stockfish-path must be set")
    engine_depth_override = int(args.engine_depth) if int(args.engine_depth) > 0 else None

    conn = sqlite3.connect(str(args.state_db))
    try:
        summary = run_label_audit(
            conn,
            stockfish_path=str(args.stockfish_path),
            sample_size=int(args.sample_size),
            seed=int(args.seed),
            max_mismatch_rate=float(args.max_mismatch_rate),
            engine_depth_override=engine_depth_override,
        )
    finally:
        conn.close()

    _print_summary(summary)
    return 0 if summary.passed else 1


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

