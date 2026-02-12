from __future__ import annotations

import logging
import re
import sys
from types import SimpleNamespace

import pytest

import chess_review
import analysis_pipeline as pipeline_module
from analysis_pipeline import run_analysis_pipeline
from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION
from engine.stockfish_oracle import StockfishOracle, _score_to_pawns, classify_move
from llm.safe_payload import build_llm_safe_payload


class _Game:
    game_url = "https://www.chess.com/game/live/1"
    pgn = '[Event "Live Chess"]\n[Result "1-0"]\n1. e4 e5 1-0\n'
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


def _san_tokens(text: str) -> set[str]:
    pattern = re.compile(
        r"\b(?:O-O-O|O-O|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?|[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?|[a-h][1-8](?:=[QRBN])?[+#]?)\b"
    )
    return set(pattern.findall(text))


def _four_positions() -> list[dict[str, object]]:
    return [
        {
            "move_number": 1,
            "player": "White",
            "label": "mistake",
            "material_change": 0,
            "mate_threat": False,
            "forcing": False,
            "tactical_flag": "tactical_miss",
            "played_san": "e4",
            "best_san": "Nf3",
        },
        {
            "move_number": 1,
            "player": "Black",
            "label": "inaccuracy",
            "material_change": 0,
            "mate_threat": False,
            "forcing": False,
            "tactical_flag": "none",
            "played_san": "e5",
            "best_san": "c5",
        },
        {
            "move_number": 2,
            "player": "White",
            "label": "blunder",
            "material_change": -3,
            "mate_threat": False,
            "forcing": True,
            "tactical_flag": "hanging_piece",
            "played_san": "Qh5",
            "best_san": "Nc3",
        },
        {
            "move_number": 2,
            "player": "Black",
            "label": "good",
            "material_change": 0,
            "mate_threat": False,
            "forcing": False,
            "tactical_flag": "none",
            "played_san": "Nc6",
            "best_san": "Nc6",
        },
    ]


def test_classify_move_thresholds() -> None:
    assert classify_move(eval_before=0.0, played_eval=-0.2, best_eval=0.0, played_is_best_move=False) == "good"
    assert classify_move(eval_before=0.0, played_eval=-0.7, best_eval=0.0, played_is_best_move=False) == "inaccuracy"
    assert classify_move(eval_before=0.0, played_eval=-1.8, best_eval=0.0, played_is_best_move=False) == "mistake"
    assert classify_move(eval_before=0.0, played_eval=-3.0, best_eval=0.0, played_is_best_move=False) == "blunder"
    assert classify_move(eval_before=-0.5, played_eval=2.0, best_eval=2.0, played_is_best_move=True) == "brilliant"
    assert classify_move(eval_before=-2.5, played_eval=-1.2, best_eval=-1.1, played_is_best_move=True) == "brilliant"


def test_build_llm_safe_payload_strips_engine_eval_fields() -> None:
    payload = build_llm_safe_payload(
        {
            "game_summary": {
                "result": "1-0",
                "total_moves": 42,
                "best_eval": 3.1,
                "depth": 15,
            },
            "key_positions": [
                {
                    "move_number": 23,
                    "player": "White",
                    "label": "blunder",
                    "eval_before": 0.3,
                    "eval_after": -3.2,
                    "eval_swing": -3.5,
                    "material_change": -3,
                    "mate_threat": False,
                    "forcing": True,
                    "tactical_flag": "hanging_piece",
                    "pv": ["Qh5+"],
                    "multipv": 1,
                }
            ],
        }
    )

    assert "best_eval" not in payload["game_summary"]
    assert "depth" not in payload["game_summary"]
    item = payload["key_positions"][0]
    assert sorted(item.keys()) == [
        "best_san",
        "forcing",
        "label",
        "mate_threat",
        "material_change",
        "move_number",
        "played_san",
        "player",
        "tactical_flag",
    ]
    assert "eval_before" not in item
    assert "eval_after" not in item
    assert "eval_swing" not in item
    assert "pv" not in item
    assert "multipv" not in item
    assert item["label"] == "blunder"
    assert item["tactical_flag"] == "hanging_piece"
    assert "played_san" in item
    assert "best_san" in item


def test_run_analysis_pipeline_engine_failure_raises_and_llm_not_called(monkeypatch) -> None:
    args = SimpleNamespace(enable_engine=True)
    game = _Game()
    llm_called = {"value": False}

    monkeypatch.setattr(pipeline_module, "_run_stockfish_oracle", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="Stockfish engine failed or produced no output."):
        run_analysis_pipeline(
            game=game,
            args=args,
            llm_runner=lambda _system_msg, _user_msg: llm_called.update(value=True) or "unexpected",
            logger=logging.getLogger("test"),
        )
    assert llm_called["value"] is False


def test_oracle_uses_single_analyse_call_per_position(monkeypatch) -> None:
    chess = pytest.importorskip("chess")
    oracle = StockfishOracle(stockfish_path="/fake/stockfish", depth=10)
    pgn_text = '[Event "Live Chess"]\n[Result "1/2-1/2"]\n1. e4 e5 1/2-1/2\n'

    class _FakeEngine:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def configure(self, _opts):
            return None

        def analyse(self, board, _limit, multipv=1):
            self.calls.append(board.fen())
            move = next(iter(board.legal_moves))
            return [
                {
                    "score": chess.engine.PovScore(chess.engine.Cp(20), board.turn),
                    "pv": [move],
                }
            ]

    fake_engine = _FakeEngine()
    monkeypatch.setattr(
        chess.engine.SimpleEngine,
        "popen_uci",
        lambda _path: fake_engine,
    )

    output = oracle.analyze_game(pgn_text)

    # 2 plies => 3 unique board positions (initial, after white move, after black move).
    assert len(fake_engine.calls) == 3
    assert len(output["key_positions"]) == 4
    assert not hasattr(StockfishOracle, "_best_line_analysis")
    assert not hasattr(StockfishOracle, "_evaluate_position")


def test_mate_scores_prefer_faster_mate() -> None:
    chess = pytest.importorskip("chess")
    mate_in_2 = chess.engine.Mate(2)
    mate_in_10 = chess.engine.Mate(10)
    assert _score_to_pawns(mate_in_2) > _score_to_pawns(mate_in_10)


def test_parse_args_engine_flags_and_values(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CHESS_USERNAME", "logan")
    monkeypatch.setenv("CHESS_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ENABLE_ENGINE", "false")
    monkeypatch.setenv("STOCKFISH_PATH", "/env/stockfish")
    monkeypatch.setenv("ENGINE_DEPTH", "13")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "chess_review.py",
            "--once",
            "--enable-engine",
            "--stockfish-path",
            "/cli/stockfish",
            "--engine-depth",
            "19",
        ],
    )
    args = chess_review.parse_args()
    assert args.enable_engine is True
    assert args.stockfish_path == "/cli/stockfish"
    assert args.engine_depth == 19

    monkeypatch.setattr(sys, "argv", ["chess_review.py", "--once", "--disable-engine"])
    args_disabled = chess_review.parse_args()
    assert args_disabled.enable_engine is False


def test_main_rejects_disable_engine(monkeypatch) -> None:
    monkeypatch.setattr(chess_review, "parse_args", lambda: SimpleNamespace(enable_engine=False))
    with pytest.raises(RuntimeError, match="Engine cannot be disabled in strict mode."):
        chess_review.main()


def test_strict_prompt_template_contains_required_phrase() -> None:
    template = pipeline_module.load_prompt_file("review_user_strict.md")
    assert "## Four Critical Positions" in template
    assert "Exactly 4 critical positions. No more, no fewer." in template


def test_engine_enabled_uses_prompt_templates_and_includes_best_san(monkeypatch) -> None:
    args = SimpleNamespace(enable_engine=True)
    game = _Game()
    captured = {"system": "", "user": ""}

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
                "label_counts_by_side": {
                    "white": {"good": 1, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
                    "black": {"good": 2, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
                },
                "label_counts": {"good": 3, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
                "forced_mate_events": 0,
                "illegal_moves": 0,
            },
            "key_positions": _four_positions(),
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: None)

    def _llm_runner(system_msg: str, user_msg: str) -> str:
        captured["system"] = system_msg
        captured["user"] = user_msg
        return "# strict"

    result = run_analysis_pipeline(
        game=game,
        args=args,
        llm_runner=_llm_runner,
        logger=logging.getLogger("test"),
    )

    assert result == "# strict"
    assert "Do not suggest any move not present in payload." in captured["user"]
    assert '"best_san":"Nf3"' in captured["user"]
    assert '"engine_depth":15' in captured["user"]


def test_engine_mode_rejects_san_not_in_allowed_set(monkeypatch) -> None:
    chess = pytest.importorskip("chess")
    args = SimpleNamespace(enable_engine=True)
    game = _Game()
    board = chess.Board()

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
                "label_counts_by_side": {
                    "white": {"good": 1, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
                    "black": {"good": 2, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
                },
                "label_counts": {"good": 3, "inaccuracy": 1, "mistake": 0, "blunder": 0, "brilliant": 0},
                "forced_mate_events": 0,
                "illegal_moves": 0,
            },
            "key_positions": [
                {
                    "move_number": 1,
                    "player": "White",
                    "label": "inaccuracy",
                    "material_change": 0,
                    "mate_threat": False,
                    "forcing": False,
                    "tactical_flag": "none",
                    "played_san": "d4",
                    "best_san": "d5",
                },
                {
                    "move_number": 1,
                    "player": "Black",
                    "label": "good",
                    "material_change": 0,
                    "mate_threat": False,
                    "forcing": False,
                    "tactical_flag": "none",
                    "played_san": "Nf6",
                    "best_san": "Nf6",
                },
                {
                    "move_number": 2,
                    "player": "White",
                    "label": "mistake",
                    "material_change": 0,
                    "mate_threat": False,
                    "forcing": False,
                    "tactical_flag": "tactical_miss",
                    "played_san": "c4",
                    "best_san": "Nc3",
                },
                {
                    "move_number": 2,
                    "player": "Black",
                    "label": "good",
                    "material_change": 0,
                    "mate_threat": False,
                    "forcing": False,
                    "tactical_flag": "none",
                    "played_san": "d5",
                    "best_san": "d5",
                },
            ],
        },
    )
    monkeypatch.setattr(pipeline_module, "_board_from_pgn", lambda _pgn_text: board)

    output = run_analysis_pipeline(
        game=game,
        args=args,
        llm_runner=lambda _sys, _user: "Play d4, but avoid e4 and consider d5.",
        logger=logging.getLogger("test"),
    )

    allowed_sans = {"d4", "d5", "Nf6", "c4", "Nc3"}
    assert "e4" not in output
    assert _san_tokens(output) <= allowed_sans
