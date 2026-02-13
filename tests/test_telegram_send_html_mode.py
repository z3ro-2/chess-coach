from __future__ import annotations

import chess_review


class _FakeResponse:
    status_code = 200
    text = "ok"

    def json(self) -> dict:
        return {"ok": True}


def test_telegram_send_uses_html_parse_mode(monkeypatch) -> None:
    calls: list[dict] = []

    def _fake_post(url: str, data=None, timeout=0, files=None):
        calls.append({"url": url, "data": dict(data or {}), "timeout": timeout, "files": files})
        return _FakeResponse()

    monkeypatch.setattr(chess_review.requests, "post", _fake_post)

    chess_review.send_telegram_message(
        "# Diagnostics\n```json\n{\"k\": \"v\"}\n```\n- Review _timing_ (critical)\nhttps://example.com",
        bot_token="token",
        chat_id="42",
        timeout=7,
        disable_notification=False,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["url"].endswith("/sendMessage")
    assert call["data"]["parse_mode"] == "HTML"
    assert call["data"]["disable_web_page_preview"] is True
    assert "<pre>" in call["data"]["text"]
    assert "```" not in call["data"]["text"]
    assert "# Diagnostics" not in call["data"]["text"]
    assert "• Review timing (critical)" in call["data"]["text"]
