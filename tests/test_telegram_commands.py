from __future__ import annotations

from types import SimpleNamespace

import chess_review
import pytest
from src.config.provider_config import get_provider, set_provider
from src import telegram_commands


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _reset_runtime_provider() -> None:
    set_provider("ollama")
    yield
    set_provider("ollama")


def test_telegram_command_parsing_and_dispatch(monkeypatch, tmp_path) -> None:
    posted_messages: list[dict] = []

    def _fake_get(url: str, params=None, timeout=0):
        assert url.endswith("/getUpdates")
        return _FakeResponse(
            {
                "ok": True,
                "result": [
                    {"update_id": 100, "message": {"chat": {"id": "42"}, "text": "hello"}},
                    {"update_id": 101, "message": {"chat": {"id": "42"}, "text": "/status"}},
                    {"update_id": 102, "message": {"chat": {"id": "42"}, "text": "/unknown"}},
                ],
            }
        )

    def _fake_post(url: str, data=None, files=None, timeout=0):
        posted_messages.append({"url": url, "data": data, "files": files, "timeout": timeout})
        return _FakeResponse({"ok": True, "result": {}})

    monkeypatch.setattr(telegram_commands.requests, "get", _fake_get)
    monkeypatch.setattr(telegram_commands.requests, "post", _fake_post)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = SimpleNamespace(
            out=tmp_path / "output",
            username="logan",
            provider="ollama",
            timeout=5,
            player_summary_every_n=20,
            ollama_url="http://127.0.0.1:11434",
            ollama_model="llama3.1:8b",
            gpt_model="gpt-4o-mini",
            max_tokens=100,
            telegram_bot_token="token",
            telegram_chat_id="42",
        )
        handled = telegram_commands.poll_telegram_commands(conn, args)
        row = conn.execute("SELECT last_update_id FROM telegram_state WHERE id = 1").fetchone()
    finally:
        conn.close()

    assert handled == 1
    assert row is not None and int(row[0]) == 102
    assert any(item["url"].endswith("/sendMessage") for item in posted_messages)
    send_message_calls = [item for item in posted_messages if item["url"].endswith("/sendMessage")]
    assert send_message_calls
    assert all(str(call.get("data", {}).get("parse_mode", "")) == "HTML" for call in send_message_calls)


def test_setprovider_gpt_updates_runtime_provider(monkeypatch, tmp_path) -> None:
    posted_messages: list[dict] = []
    set_provider("ollama")

    def _fake_get(url: str, params=None, timeout=0):
        assert url.endswith("/getUpdates")
        return _FakeResponse(
            {
                "ok": True,
                "result": [
                    {"update_id": 201, "message": {"chat": {"id": "42"}, "text": "/setprovider gpt"}},
                ],
            }
        )

    def _fake_post(url: str, data=None, files=None, timeout=0):
        posted_messages.append({"url": url, "data": data, "files": files, "timeout": timeout})
        return _FakeResponse({"ok": True, "result": {}})

    monkeypatch.setattr(telegram_commands.requests, "get", _fake_get)
    monkeypatch.setattr(telegram_commands.requests, "post", _fake_post)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = SimpleNamespace(
            out=tmp_path / "output",
            username="logan",
            provider="ollama",
            timeout=5,
            player_summary_every_n=20,
            ollama_url="http://127.0.0.1:11434",
            ollama_model="llama3.1:8b",
            gpt_model="gpt-4o-mini",
            max_tokens=100,
            telegram_bot_token="token",
            telegram_chat_id="42",
            telegram_admin_chat_ids="42",
        )
        handled = telegram_commands.poll_telegram_commands(conn, args)
    finally:
        conn.close()

    assert handled == 1
    assert get_provider() == "gpt"
    assert any("Provider set to gpt" in str(item.get("data", {}).get("text", "")) for item in posted_messages)


def test_setprovider_ollama_updates_runtime_provider(monkeypatch, tmp_path) -> None:
    posted_messages: list[dict] = []
    set_provider("gpt")

    def _fake_get(url: str, params=None, timeout=0):
        return _FakeResponse(
            {
                "ok": True,
                "result": [
                    {"update_id": 251, "message": {"chat": {"id": "42"}, "text": "/setprovider ollama"}},
                ],
            }
        )

    def _fake_post(url: str, data=None, files=None, timeout=0):
        posted_messages.append({"url": url, "data": data, "files": files, "timeout": timeout})
        return _FakeResponse({"ok": True, "result": {}})

    monkeypatch.setattr(telegram_commands.requests, "get", _fake_get)
    monkeypatch.setattr(telegram_commands.requests, "post", _fake_post)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = SimpleNamespace(
            out=tmp_path / "output",
            username="logan",
            provider="gpt",
            timeout=5,
            player_summary_every_n=20,
            ollama_url="http://127.0.0.1:11434",
            ollama_model="llama3.1:8b",
            gpt_model="gpt-4o-mini",
            max_tokens=100,
            telegram_bot_token="token",
            telegram_chat_id="42",
            telegram_admin_chat_ids="42",
        )
        handled = telegram_commands.poll_telegram_commands(conn, args)
    finally:
        conn.close()

    assert handled == 1
    assert get_provider() == "ollama"
    assert any("Provider set to ollama" in str(item.get("data", {}).get("text", "")) for item in posted_messages)


def test_setprovider_invalid_value_returns_error(monkeypatch, tmp_path) -> None:
    posted_messages: list[dict] = []
    set_provider("ollama")

    def _fake_get(url: str, params=None, timeout=0):
        return _FakeResponse(
            {
                "ok": True,
                "result": [
                    {"update_id": 301, "message": {"chat": {"id": "42"}, "text": "/setprovider bad"}},
                ],
            }
        )

    def _fake_post(url: str, data=None, files=None, timeout=0):
        posted_messages.append({"url": url, "data": data, "files": files, "timeout": timeout})
        return _FakeResponse({"ok": True, "result": {}})

    monkeypatch.setattr(telegram_commands.requests, "get", _fake_get)
    monkeypatch.setattr(telegram_commands.requests, "post", _fake_post)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = SimpleNamespace(
            out=tmp_path / "output",
            username="logan",
            provider="ollama",
            timeout=5,
            player_summary_every_n=20,
            ollama_url="http://127.0.0.1:11434",
            ollama_model="llama3.1:8b",
            gpt_model="gpt-4o-mini",
            max_tokens=100,
            telegram_bot_token="token",
            telegram_chat_id="42",
            telegram_admin_chat_ids="42",
        )
        handled = telegram_commands.poll_telegram_commands(conn, args)
    finally:
        conn.close()

    assert handled == 1
    assert get_provider() == "ollama"
    assert any("Invalid provider" in str(item.get("data", {}).get("text", "")) for item in posted_messages)


def test_setprovider_requires_authorized_chat(monkeypatch, tmp_path) -> None:
    posted_messages: list[dict] = []
    set_provider("ollama")

    def _fake_get(url: str, params=None, timeout=0):
        return _FakeResponse(
            {
                "ok": True,
                "result": [
                    {"update_id": 401, "message": {"chat": {"id": "99"}, "text": "/setprovider gpt"}},
                ],
            }
        )

    def _fake_post(url: str, data=None, files=None, timeout=0):
        posted_messages.append({"url": url, "data": data, "files": files, "timeout": timeout})
        return _FakeResponse({"ok": True, "result": {}})

    monkeypatch.setattr(telegram_commands.requests, "get", _fake_get)
    monkeypatch.setattr(telegram_commands.requests, "post", _fake_post)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = SimpleNamespace(
            out=tmp_path / "output",
            username="logan",
            provider="ollama",
            timeout=5,
            player_summary_every_n=20,
            ollama_url="http://127.0.0.1:11434",
            ollama_model="llama3.1:8b",
            gpt_model="gpt-4o-mini",
            max_tokens=100,
            telegram_bot_token="token",
            telegram_chat_id="42",
            telegram_admin_chat_ids="42",
        )
        handled = telegram_commands.poll_telegram_commands(conn, args)
    finally:
        conn.close()

    assert handled == 1
    assert get_provider() == "ollama"
    assert any("Permission denied." in str(item.get("data", {}).get("text", "")) for item in posted_messages)


def test_poll_loop_sync_uses_runtime_provider(monkeypatch) -> None:
    set_provider("gpt")
    args = SimpleNamespace(
        provider="ollama",
        ollama_url="http://127.0.0.1:11434",
    )

    provider = chess_review._sync_runtime_provider(args)

    assert provider == "gpt"
    assert args.provider == "gpt"


def test_summary_command_message_includes_trait_scores(monkeypatch, tmp_path) -> None:
    posted_messages: list[dict] = []

    def _fake_get(url: str, params=None, timeout=0):
        assert url.endswith("/getUpdates")
        return _FakeResponse(
            {
                "ok": True,
                "result": [
                    {"update_id": 501, "message": {"chat": {"id": "42"}, "text": "/summary"}},
                ],
            }
        )

    def _fake_post(url: str, data=None, files=None, timeout=0):
        posted_messages.append({"url": url, "data": data, "files": files, "timeout": timeout})
        return _FakeResponse({"ok": True, "result": {}})

    monkeypatch.setattr(telegram_commands.requests, "get", _fake_get)
    monkeypatch.setattr(telegram_commands.requests, "post", _fake_post)
    monkeypatch.setattr(
        telegram_commands,
        "run_command",
        lambda *_args, **_kwargs: {
            "text": "Summary generated: /tmp/player_summary.md\nTrait scores (last 20 games): tactical_awareness=90",
            "file": None,
        },
    )

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        args = SimpleNamespace(
            out=tmp_path / "output",
            username="logan",
            provider="ollama",
            timeout=5,
            player_summary_every_n=20,
            player_trait_window=20,
            ollama_url="http://127.0.0.1:11434",
            ollama_model="llama3.1:8b",
            gpt_model="gpt-4o-mini",
            max_tokens=100,
            telegram_bot_token="token",
            telegram_chat_id="42",
        )
        handled = telegram_commands.poll_telegram_commands(conn, args)
    finally:
        conn.close()

    assert handled == 1
    assert any("Trait scores" in str(item.get("data", {}).get("text", "")) for item in posted_messages)
