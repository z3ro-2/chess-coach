from __future__ import annotations

import sys
from types import SimpleNamespace

import chess_review


def _base_required_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CHESS_USERNAME", "logan")
    monkeypatch.setenv("CHESS_OUTPUT_DIR", str(tmp_path / "out"))


def test_provider_env_gpt(monkeypatch, tmp_path) -> None:
    _base_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("PROVIDER", "gpt")
    monkeypatch.setattr(sys, "argv", ["chess_review.py", "--once"])

    args = chess_review.parse_args()

    assert args.provider == "gpt"


def test_provider_env_ollama(monkeypatch, tmp_path) -> None:
    _base_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("PROVIDER", "ollama")
    monkeypatch.setattr(sys, "argv", ["chess_review.py", "--once"])

    args = chess_review.parse_args()

    assert args.provider == "ollama"


def test_provider_cli_overrides_env(monkeypatch, tmp_path) -> None:
    _base_required_env(monkeypatch, tmp_path)
    monkeypatch.setenv("PROVIDER", "gpt")
    monkeypatch.setattr(sys, "argv", ["chess_review.py", "--once", "--provider", "ollama"])

    args = chess_review.parse_args()

    assert args.provider == "ollama"


def test_provider_fallback_to_gpt_when_ollama_url_missing(monkeypatch) -> None:
    args = SimpleNamespace(provider="ollama", ollama_url="")

    chess_review._apply_provider_runtime_fallback(args)

    assert args.provider == "gpt"
