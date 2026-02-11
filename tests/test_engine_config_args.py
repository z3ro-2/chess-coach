from __future__ import annotations

import sys

import chess_review


def test_parse_args_engine_config_from_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CHESS_USERNAME", "logan")
    monkeypatch.setenv("CHESS_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ENABLE_ENGINE", "true")
    monkeypatch.setenv("STOCKFISH_PATH", "/opt/stockfish")
    monkeypatch.setenv("ENGINE_DEPTH", "17")
    monkeypatch.setattr(sys, "argv", ["chess_review.py", "--once"])

    args = chess_review.parse_args()

    assert args.enable_engine is True
    assert args.stockfish_path == "/opt/stockfish"
    assert args.engine_depth == 17


def test_parse_args_disable_engine_flag_overrides_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CHESS_USERNAME", "logan")
    monkeypatch.setenv("CHESS_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ENABLE_ENGINE", "true")
    monkeypatch.setattr(sys, "argv", ["chess_review.py", "--once", "--disable-engine"])

    args = chess_review.parse_args()

    assert args.enable_engine is False
