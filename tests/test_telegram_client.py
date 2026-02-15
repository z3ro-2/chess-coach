from __future__ import annotations

from pathlib import Path

from src.telegram_client import TelegramClientError, create_telegram_client


class _FakeResponse:
    status_code = 200
    text = "ok"

    def json(self) -> dict:
        return {"ok": True}


def test_send_text_includes_disable_preview_false_when_requested(monkeypatch) -> None:
    calls: list[dict] = []

    def _fake_post(url: str, data=None, timeout=0, files=None):
        calls.append({"url": url, "data": dict(data or {}), "timeout": timeout, "files": files})
        return _FakeResponse()

    monkeypatch.setattr("src.telegram_client.requests.post", _fake_post)

    client = create_telegram_client(bot_token="token", timeout=5)
    client.send_text(chat_id="42", text="hello", disable_preview=False)

    assert len(calls) == 1
    payload = calls[0]["data"]
    assert payload["disable_web_page_preview"] is False


def test_send_text_has_no_parse_mode_by_default(monkeypatch) -> None:
    calls: list[dict] = []

    def _fake_post(url: str, data=None, timeout=0, files=None):
        calls.append({"url": url, "data": dict(data or {})})
        return _FakeResponse()

    monkeypatch.setattr("src.telegram_client.requests.post", _fake_post)

    client = create_telegram_client(bot_token="token", timeout=5)
    client.send_text(chat_id="42", text="hello")

    assert len(calls) == 1
    assert "parse_mode" not in calls[0]["data"]


def test_send_document_raises_for_missing_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("src.telegram_client.requests.post", lambda *_args, **_kwargs: _FakeResponse())
    client = create_telegram_client(bot_token="token", timeout=5)

    missing_path = tmp_path / "missing.md"
    try:
        client.send_document(chat_id="42", filepath=missing_path)
        raise AssertionError("expected TelegramClientError")
    except TelegramClientError as exc:
        assert "File not found" in str(exc)


def test_send_document_posts_payload(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    def _fake_post(url: str, data=None, timeout=0, files=None):
        calls.append({"url": url, "data": dict(data or {}), "files": files})
        return _FakeResponse()

    monkeypatch.setattr("src.telegram_client.requests.post", _fake_post)

    path = Path(tmp_path / "review.md")
    path.write_text("hi", encoding="utf-8")

    client = create_telegram_client(bot_token="token", timeout=5)
    client.send_document(chat_id="42", filepath=path, caption="done")

    assert len(calls) == 1
    assert calls[0]["url"].endswith("/sendDocument")
    assert calls[0]["data"]["caption"] == "done"
    assert calls[0]["files"] is not None


def test_plain_text_payload_with_entity_chars_does_not_set_parse_mode(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    def _fake_post(url: str, data=None, timeout=0, files=None):
        calls.append({"url": url, "data": dict(data or {}), "files": files})
        return _FakeResponse()

    monkeypatch.setattr("src.telegram_client.requests.post", _fake_post)

    client = create_telegram_client(bot_token="token", timeout=5)
    tricky = r"name_with_[brackets](parens)#topic<tag>&value"
    client.send_text(chat_id="42", text=tricky, disable_preview=False)

    md_path = Path(tmp_path / "review.md")
    md_path.write_text("review", encoding="utf-8")
    client.send_document(chat_id="42", filepath=md_path, caption=tricky)

    assert len(calls) == 2
    first = calls[0]["data"]
    second = calls[1]["data"]
    assert first["text"] == tricky
    assert second["caption"] == tricky
    assert "parse_mode" not in first
    assert "parse_mode" not in second


def test_plain_text_payload_with_markdown_breakers_does_not_throw(monkeypatch) -> None:
    calls: list[dict] = []

    def _fake_post(url: str, data=None, timeout=0, files=None):
        calls.append({"url": url, "data": dict(data or {}), "files": files})
        return _FakeResponse()

    monkeypatch.setattr("src.telegram_client.requests.post", _fake_post)

    client = create_telegram_client(bot_token="token", timeout=5)
    tricky = "user_name (rapid)[arena] #tag _underscore_"
    client.send_text(chat_id="42", text=tricky, disable_preview=False)

    assert len(calls) == 1
    payload = calls[0]["data"]
    assert payload["text"] == tricky
    assert payload["disable_web_page_preview"] is False
    assert "parse_mode" not in payload
