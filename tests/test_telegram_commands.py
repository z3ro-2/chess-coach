from __future__ import annotations

from types import SimpleNamespace

import chess_review
from src import telegram_commands


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


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
