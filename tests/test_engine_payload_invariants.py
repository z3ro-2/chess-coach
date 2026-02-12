from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import analysis_pipeline as pipeline_module
from analysis_pipeline import run_analysis_pipeline
from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION, enrich_summary_with_player_fields, validate_engine_payload
from engine.stockfish_oracle import StockfishOracle
from llm.safe_payload import build_llm_safe_payload


class _Game:
    game_url = "https://www.chess.com/game/live/42"
    pgn = '[Event "Live Chess"]\n[Result "1-0"]\n1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 1-0\n'
    your_color = "white"
    opponent = "opponent"
    result = "1-0"
    time_control = "600"
    rated = True
    rules = "chess"

    @property
    def end_dt_utc(self):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(1_706_000_000, tz=timezone.utc)


def test_run_analysis_pipeline_rejects_invariant_violations(monkeypatch) -> None:
    args = SimpleNamespace(enable_engine=True)
    game = _Game()

    monkeypatch.setattr(
        pipeline_module,
        "_run_stockfish_oracle",
        lambda **_kwargs: {
            "game_summary": {
                "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
                "engine_depth": 15,
                "result": "1-0",
                "total_plies": 4,
                "total_moves": 2,
                "label_counts": {"good": 2, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
                # Missing label_counts_by_side and invalid sums by construction.
            },
            "key_positions": [],
        },
    )

    with pytest.raises(RuntimeError, match="Stockfish payload invariants failed"):
        run_analysis_pipeline(
            game=game,
            args=args,
            llm_runner=lambda _system_msg, _user_msg: "# should-not-run",
            logger=logging.getLogger("test"),
        )


def test_stockfish_oracle_payload_matches_schema_invariants(monkeypatch) -> None:
    chess = pytest.importorskip("chess")
    oracle = StockfishOracle(stockfish_path="/fake/stockfish", depth=10)
    pgn_text = '[Event "Live Chess"]\n[Result "1-0"]\n1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 1-0\n'

    class _FakeEngine:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def configure(self, _opts):
            return None

        def analyse(self, board, _limit, multipv=1):
            _ = multipv
            move = next(iter(board.legal_moves))
            return [
                {
                    "score": chess.engine.PovScore(chess.engine.Cp(20), board.turn),
                    "pv": [move],
                }
            ]

    monkeypatch.setattr(chess.engine.SimpleEngine, "popen_uci", lambda _path: _FakeEngine())

    output = oracle.analyze_game(pgn_text)
    summary = dict(output["game_summary"])
    assert summary["schema_version"] == ENGINE_PAYLOAD_SCHEMA_VERSION
    assert int(summary["total_plies"]) == 6
    assert int(summary["total_moves"]) == 3

    payload = {
        "game_summary": enrich_summary_with_player_fields(summary, your_color="white"),
        "key_positions": list(output.get("key_positions") or []),
    }
    payload["game_summary"]["result"] = "1-0"

    validation = validate_engine_payload(
        payload,
        require_schema_version=True,
        require_player_fields=True,
        require_key_positions=True,
    )
    assert validation.is_valid, ",".join(validation.errors)

    assert sum(int(v) for v in validation.label_counts.values()) == 6
    assert sum(int(v) for v in validation.label_counts_by_side["white"].values()) == 3
    assert sum(int(v) for v in validation.label_counts_by_side["black"].values()) == 3
    assert validation.player_total_plies == 3
    assert sum(int(v) for v in validation.player_label_counts.values()) == 3


def test_llm_safe_payload_exposes_player_only_label_counts() -> None:
    payload = {
        "game_summary": {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "total_plies": 40,
            "total_moves": 20,
            "label_counts": {"good": 30, "inaccuracy": 4, "mistake": 4, "blunder": 2, "brilliant": 0},
            "player_label_counts": {"good": 16, "inaccuracy": 2, "mistake": 1, "blunder": 1, "brilliant": 0},
        },
        "key_positions": [],
    }
    safe = build_llm_safe_payload(payload)
    assert safe["game_summary"]["label_counts"] == {
        "good": 16,
        "inaccuracy": 2,
        "mistake": 1,
        "blunder": 1,
        "brilliant": 0,
    }
