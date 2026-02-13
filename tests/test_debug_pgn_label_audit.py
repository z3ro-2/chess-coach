from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import chess_review
from cli.debug_pgn_label_audit import run_label_audit
from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION


def _fixture_pgn_path() -> Path:
    return Path(__file__).parent / "fixtures" / "label_audit_sample.pgn"


def _payload_fixture() -> dict:
    return {
        "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
        "game_summary": {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "your_color": "white",
            "result": "1-0",
            "total_plies": 8,
            "total_moves": 4,
            "white_plies": 4,
            "black_plies": 4,
            "unlabeled_white_plies": 0,
            "unlabeled_black_plies": 0,
            "label_counts_total": {
                "good": 5,
                "inaccuracy": 1,
                "mistake": 1,
                "blunder": 1,
                "brilliant": 0,
            },
            "label_counts_white": {
                "good": 1,
                "inaccuracy": 1,
                "mistake": 1,
                "blunder": 1,
                "brilliant": 0,
            },
            "label_counts_black": {
                "good": 4,
                "inaccuracy": 0,
                "mistake": 0,
                "blunder": 0,
                "brilliant": 0,
            },
            "label_counts_by_side": {
                "white": {
                    "good": 1,
                    "inaccuracy": 1,
                    "mistake": 1,
                    "blunder": 1,
                    "brilliant": 0,
                },
                "black": {
                    "good": 4,
                    "inaccuracy": 0,
                    "mistake": 0,
                    "blunder": 0,
                    "brilliant": 0,
                },
            },
            "player_total_plies": 4,
            "player_total_moves": 4,
            "player_label_counts": {
                "good": 1,
                "inaccuracy": 1,
                "mistake": 1,
                "blunder": 1,
                "brilliant": 0,
            },
            "label_counts": {
                "good": 5,
                "inaccuracy": 1,
                "mistake": 1,
                "blunder": 1,
                "brilliant": 0,
            },
        },
        "key_positions": [
            {
                "move_number": 1,
                "player": "White",
                "label": "good",
                "tactical_flag": "none",
                "material_change": 0,
                "mate_threat": False,
                "forcing": False,
                "played_san": "e4",
                "best_san": "e4",
            },
            {
                "move_number": 2,
                "player": "White",
                "label": "inaccuracy",
                "tactical_flag": "none",
                "material_change": 0,
                "mate_threat": False,
                "forcing": False,
                "played_san": "Nf3",
                "best_san": "Nf3",
            },
            {
                "move_number": 3,
                "player": "White",
                "label": "mistake",
                "tactical_flag": "tactical_miss",
                "material_change": -1,
                "mate_threat": False,
                "forcing": False,
                "played_san": "Bb5",
                "best_san": "Bb5",
            },
            {
                "move_number": 4,
                "player": "White",
                "label": "blunder",
                "tactical_flag": "hanging_piece",
                "material_change": -3,
                "mate_threat": False,
                "forcing": False,
                "played_san": "Ba4",
                "best_san": "Ba4",
            },
        ],
    }


def _recomputed_trace(*, mismatch: bool, game_index: int) -> dict:
    first_label = "inaccuracy" if (mismatch and game_index == 0) else "good"
    return {
        "game_summary": {
            "label_counts_total": {
                "good": 5,
                "inaccuracy": 1,
                "mistake": 1,
                "blunder": 1,
                "brilliant": 0,
            }
        },
        "all_positions": [
            {
                "move_number": 1,
                "player": "White",
                "played_san": "e4",
                "best_san": "e4",
                "label": first_label,
                "eval_before": 0.2,
                "played_eval": 0.1,
                "best_eval": 0.2,
                "eval_loss": 0.1,
                "abs_eval_swing": 0.1,
            },
            {
                "move_number": 1,
                "player": "Black",
                "played_san": "e5",
                "best_san": "e5",
                "label": "good",
                "eval_before": 0.1,
                "played_eval": 0.1,
                "best_eval": 0.1,
                "eval_loss": 0.0,
                "abs_eval_swing": 0.0,
            },
            {
                "move_number": 2,
                "player": "White",
                "played_san": "Nf3",
                "best_san": "Nf3",
                "label": "inaccuracy",
                "eval_before": 0.1,
                "played_eval": -0.6,
                "best_eval": 0.1,
                "eval_loss": 0.7,
                "abs_eval_swing": 0.7,
            },
            {
                "move_number": 2,
                "player": "Black",
                "played_san": "Nc6",
                "best_san": "Nc6",
                "label": "good",
                "eval_before": -0.6,
                "played_eval": -0.6,
                "best_eval": -0.6,
                "eval_loss": 0.0,
                "abs_eval_swing": 0.0,
            },
            {
                "move_number": 3,
                "player": "White",
                "played_san": "Bb5",
                "best_san": "Bb5",
                "label": "mistake",
                "eval_before": -0.6,
                "played_eval": -1.8,
                "best_eval": -0.6,
                "eval_loss": 1.2,
                "abs_eval_swing": 1.2,
            },
            {
                "move_number": 3,
                "player": "Black",
                "played_san": "a6",
                "best_san": "a6",
                "label": "good",
                "eval_before": -1.8,
                "played_eval": -1.8,
                "best_eval": -1.8,
                "eval_loss": 0.0,
                "abs_eval_swing": 0.0,
            },
            {
                "move_number": 4,
                "player": "White",
                "played_san": "Ba4",
                "best_san": "Ba4",
                "label": "blunder",
                "eval_before": -1.8,
                "played_eval": -3.2,
                "best_eval": -1.8,
                "eval_loss": 1.4,
                "abs_eval_swing": 1.4,
            },
            {
                "move_number": 4,
                "player": "Black",
                "played_san": "Nf6",
                "best_san": "Nf6",
                "label": "good",
                "eval_before": -3.2,
                "played_eval": -3.2,
                "best_eval": -3.2,
                "eval_loss": 0.0,
                "abs_eval_swing": 0.0,
            },
        ],
    }


def _seed_db(conn: sqlite3.Connection, *, pgn_path: Path) -> None:
    now = int(time.time())
    payload = _payload_fixture()
    payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    for i in range(5):
        game_url = f"https://www.chess.com/game/live/{1000 + i}"
        end_time = 1_706_000_000 + i
        conn.execute(
            """
            INSERT INTO processed_games
              (game_url, end_time, created_at, md_path, pgn_path, provider, model, hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                game_url,
                end_time,
                now,
                "__audit__/g.md",
                str(pgn_path),
                "stockfish",
                "stockfish-depth-15",
                f"h{i}",
            ),
        )
        conn.execute(
            """
            INSERT INTO engine_payloads
              (game_url, end_time, created_at, engine_depth, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (game_url, end_time, now, 15, payload_json),
        )
    conn.commit()


def test_label_audit_passes_with_fixture_pgn(tmp_path) -> None:
    db_path = tmp_path / "state.sqlite"
    conn = chess_review.init_db(db_path)
    try:
        _seed_db(conn, pgn_path=_fixture_pgn_path())
        calls = {"idx": 0}

        def _fake_recompute(pgn_text: str, depth: int, stockfish_path: str) -> dict:
            assert "1. e4 e5" in pgn_text
            assert depth == 15
            assert stockfish_path == "/fake/stockfish"
            idx = calls["idx"]
            calls["idx"] += 1
            return _recomputed_trace(mismatch=False, game_index=idx)

        summary = run_label_audit(
            conn,
            stockfish_path="/fake/stockfish",
            sample_size=5,
            seed=17,
            max_mismatch_rate=0.02,
            recompute_fn=_fake_recompute,
        )
    finally:
        conn.close()

    assert summary.passed is True
    assert summary.sampled_games == 5
    assert summary.compared_labels == 20
    assert summary.mismatched_labels == 0
    assert summary.mismatch_rate == 0.0


def test_label_audit_fails_when_mismatch_rate_exceeds_threshold(tmp_path) -> None:
    db_path = tmp_path / "state.sqlite"
    conn = chess_review.init_db(db_path)
    try:
        _seed_db(conn, pgn_path=_fixture_pgn_path())
        calls = {"idx": 0}

        def _fake_recompute(pgn_text: str, depth: int, stockfish_path: str) -> dict:
            assert "1. e4 e5" in pgn_text
            _ = depth
            _ = stockfish_path
            idx = calls["idx"]
            calls["idx"] += 1
            return _recomputed_trace(mismatch=True, game_index=idx)

        summary = run_label_audit(
            conn,
            stockfish_path="/fake/stockfish",
            sample_size=5,
            seed=17,
            max_mismatch_rate=0.02,
            recompute_fn=_fake_recompute,
        )
    finally:
        conn.close()

    # One mismatch out of 20 compared labels => 5% > 2% threshold.
    assert summary.passed is False
    assert summary.compared_labels == 20
    assert summary.mismatched_labels == 1
    assert summary.mismatch_rate > 0.02
