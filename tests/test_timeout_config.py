from __future__ import annotations

from types import SimpleNamespace

import pytest

import chess_review
from src.config.provider_config import set_provider


@pytest.fixture(autouse=True)
def _reset_runtime_provider() -> None:
    set_provider("ollama")
    yield
    set_provider("ollama")


def test_llm_timeout_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CHESS_USERNAME", "logan")
    monkeypatch.setenv("CHESS_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("LLM_TIMEOUT", "300")

    args = chess_review.parse_args([])

    assert args.timeout == 300


def test_call_selected_llm_backend_passes_timeout_to_openai(monkeypatch) -> None:
    set_provider("gpt")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured: dict[str, object] = {}

    def _fake_openai_chat(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(chess_review, "call_openai_chat", _fake_openai_chat)
    args = SimpleNamespace(
        gpt_model="gpt-4o-mini",
        max_tokens=256,
        timeout=321,
        ollama_url="",
        ollama_model="llama3.1:8b",
    )

    result = chess_review._call_selected_llm_backend(args=args, system_msg="sys", user_msg="usr")

    assert result == "ok"
    assert captured["timeout"] == 321


def test_call_selected_llm_backend_passes_timeout_to_ollama(monkeypatch) -> None:
    set_provider("ollama")
    captured: dict[str, object] = {}

    def _fake_ollama_generate(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(chess_review, "_validate_ollama_endpoint", lambda **_kwargs: None)
    monkeypatch.setattr(chess_review, "call_ollama_generate", _fake_ollama_generate)
    args = SimpleNamespace(
        gpt_model="gpt-4o-mini",
        max_tokens=256,
        timeout=222,
        ollama_url="http://127.0.0.1:11434",
        ollama_model="llama3.1:8b",
    )

    result = chess_review._call_selected_llm_backend(args=args, system_msg="sys", user_msg="usr")

    assert result == "ok"
    assert captured["timeout"] == 222
