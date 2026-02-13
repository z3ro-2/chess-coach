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


def test_success_message_plain_text_url_preview_enabled(monkeypatch) -> None:
    calls: list[dict] = []

    def _fake_post(url: str, data=None, timeout=0, files=None):
        calls.append({"url": url, "data": dict(data or {}), "timeout": timeout, "files": files})
        return _FakeResponse()

    monkeypatch.setattr(chess_review.requests, "post", _fake_post)

    game = chess_review.GameInfo(
        game_url="https://www.chess.com/game/live/123",
        pgn='[Event "Live Chess"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        end_time=1_706_000_000,
        time_control="600",
        rated=True,
        rules="chess",
        white_username="logan",
        black_username="opponent",
        white_rating=1200,
        black_rating=1190,
        result="1-0",
        your_color="white",
        opponent="opponent",
    )
    message = chess_review.build_telegram_success_message(game)
    chess_review.send_telegram_message(
        message,
        bot_token="token",
        chat_id="42",
        timeout=7,
        disable_notification=False,
        disable_web_page_preview=False,
    )

    assert len(calls) == 1
    payload = calls[0]["data"]
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is False

    text = str(payload["text"])
    assert "https://www.chess.com/game/live/123" in text
    assert "<" not in text
    assert ">" not in text
    assert "#" not in text
    assert "`" not in text
    assert text.splitlines()[-1].strip() == "https://www.chess.com/game/live/123"


def test_names_with_ampersand_less_greater_do_not_cause_400_error(monkeypatch) -> None:
    calls: list[dict] = []

    def _fake_post(url: str, data=None, timeout=0, files=None):
        payload = dict(data or {})
        text = str(payload.get("text", ""))
        calls.append({"url": url, "data": payload})
        if "alice <coach>" in text or "bob & carol > dave" in text:
            class _BadResponse:
                status_code = 400
                text = "bad request"

                def json(self) -> dict:
                    return {"ok": False}

            return _BadResponse()
        return _FakeResponse()

    monkeypatch.setattr(chess_review.requests, "post", _fake_post)

    game = chess_review.GameInfo(
        game_url="https://www.chess.com/game/live/456",
        pgn='[Event "Live Chess"]\n[Result "1-0"]\n1. e4 e5 1-0\n',
        end_time=1_706_000_000,
        time_control="10<0",
        rated=True,
        rules="chess",
        white_username="alice <coach>",
        black_username="bob & carol > dave",
        white_rating=1200,
        black_rating=1190,
        result="1-0 & sharp",
        your_color="white",
        opponent="bob & carol > dave",
    )
    msg = chess_review.build_telegram_success_message(game)

    chess_review.send_telegram_message(
        msg,
        bot_token="token",
        chat_id="42",
        timeout=7,
        disable_notification=False,
        disable_web_page_preview=False,
        preformatted_html=True,
    )

    assert len(calls) == 1
    text = str(calls[0]["data"]["text"])
    assert "bob &amp; carol &gt; dave" in text
    assert "1-0 &amp; sharp" in text
    assert "10&lt;0" in text
