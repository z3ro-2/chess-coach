from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import chess_review
from src.config.provider_config import set_provider


class _FakeResponse:
    status_code = 200

    def json(self):
        return {"response": "ok"}


def test_call_ollama_generate_includes_shared_sampling_fields(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.33")
    monkeypatch.setenv("LLM_TOP_P", "0.77")
    monkeypatch.setenv("LLM_MAX_TOKENS", "321")
    captured: dict[str, object] = {}

    def _fake_post(url, headers, data, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["payload"] = json.loads(str(data))
        return _FakeResponse()

    monkeypatch.setattr(chess_review.requests, "post", _fake_post)
    out = chess_review.call_ollama_generate(
        base_url="http://127.0.0.1:11434",
        model="ignored-by-config",
        system_msg="SYS",
        user_msg="USR",
        timeout=9,
    )
    assert out == "ok"
    payload = dict(captured["payload"])  # type: ignore[arg-type]
    assert payload["model"] == "llama3.2:3b"
    assert payload["temperature"] == 0.33
    assert payload["top_p"] == 0.77
    assert payload["num_predict"] == 321
    assert payload["prompt"] == "SYS\n\nUSR"


def test_call_ollama_generate_raises_config_error_on_invalid_config_type(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MAX_TOKENS", "not-an-int")
    with pytest.raises(chess_review.ConfigError, match="LLM_MAX_TOKENS must be an int"):
        chess_review.call_ollama_generate(
            base_url="http://127.0.0.1:11434",
            model="llama3.1:8b",
            system_msg="SYS",
            user_msg="USR",
            timeout=5,
        )


def test_call_ollama_generate_sends_json_format_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.33")
    monkeypatch.setenv("LLM_TOP_P", "0.77")
    monkeypatch.setenv("LLM_MAX_TOKENS", "321")
    monkeypatch.setenv("OLLAMA_JSON_MODE", "1")
    captured: dict[str, object] = {}

    def _fake_post(url, headers, data, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["payload"] = json.loads(str(data))
        return _FakeResponse()

    monkeypatch.setattr(chess_review.requests, "post", _fake_post)
    out = chess_review.call_ollama_generate(
        base_url="http://127.0.0.1:11434",
        model="ignored-by-config",
        system_msg="SYS",
        user_msg="USR",
        timeout=9,
    )
    assert out == "ok"
    payload = dict(captured["payload"])  # type: ignore[arg-type]
    assert payload["format"] == "json"


def test_call_ollama_generate_retries_without_json_format_when_unsupported(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.33")
    monkeypatch.setenv("LLM_TOP_P", "0.77")
    monkeypatch.setenv("LLM_MAX_TOKENS", "321")
    monkeypatch.setenv("OLLAMA_JSON_MODE", "1")
    calls: list[dict[str, object]] = []

    class _BadResponse:
        status_code = 400
        text = "invalid format option"

        def json(self):
            return {"error": "invalid format option"}

    def _fake_post(url, headers, data, timeout):
        payload = json.loads(str(data))
        calls.append(payload)
        if len(calls) == 1:
            return _BadResponse()
        return _FakeResponse()

    monkeypatch.setattr(chess_review.requests, "post", _fake_post)
    out = chess_review.call_ollama_generate(
        base_url="http://127.0.0.1:11434",
        model="ignored-by-config",
        system_msg="SYS",
        user_msg="USR",
        timeout=9,
    )
    assert out == "ok"
    assert len(calls) == 2
    assert calls[0].get("format") == "json"
    assert "format" not in calls[1]


def test_llm_audit_log_appears_during_backend_review_generation(monkeypatch, caplog) -> None:
    set_provider("ollama")
    monkeypatch.setenv("OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.21")
    monkeypatch.setenv("LLM_TOP_P", "0.88")
    monkeypatch.setenv("LLM_MAX_TOKENS", "444")
    monkeypatch.setattr(chess_review, "_validate_ollama_endpoint", lambda **_kwargs: None)

    class _Response:
        status_code = 200

        def json(self):
            return {"response": "ok"}

    monkeypatch.setattr(chess_review.requests, "post", lambda *_args, **_kwargs: _Response())
    caplog.set_level(logging.INFO, logger="chess_review")
    args = SimpleNamespace(
        gpt_model="gpt-4o-mini",
        max_tokens=256,
        timeout=222,
        ollama_url="http://127.0.0.1:11434",
        ollama_model="ignored",
    )
    result = chess_review._call_selected_llm_backend(args=args, system_msg="sys", user_msg="usr")
    assert result == "ok"

    audit_messages = [record.getMessage() for record in caplog.records if "[LLM-AUDIT]" in record.getMessage()]
    assert audit_messages
    payload = json.loads(audit_messages[-1].split("[LLM-AUDIT] ", 1)[1])
    assert payload == {
        "ollama_url": "http://localhost:11434",
        "ollama_model": "llama3.2:3b",
        "temperature": 0.21,
        "top_p": 0.88,
        "max_tokens": 444,
    }
