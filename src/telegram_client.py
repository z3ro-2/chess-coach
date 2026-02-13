"""Shared Telegram sending client.

Centralizes sendMessage/sendDocument behavior so callers do not diverge on
parse mode or preview flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


class TelegramClientError(RuntimeError):
    """Raised when Telegram API calls fail."""


@dataclass(slots=True)
class TelegramClient:
    bot_token: str
    timeout: int

    def _api_base(self) -> str:
        token = str(self.bot_token or "").strip()
        if not token:
            raise TelegramClientError("Missing bot_token")
        return f"https://api.telegram.org/bot{token}"

    def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        disable_preview: bool | None = None,
        disable_notification: bool = False,
    ) -> None:
        chat = str(chat_id or "").strip()
        if not chat:
            raise TelegramClientError("Missing chat_id")

        payload: dict[str, Any] = {
            "chat_id": chat,
            "text": str(text or "")[:4096],
            "disable_notification": bool(disable_notification),
        }
        if disable_preview is not None:
            payload["disable_web_page_preview"] = bool(disable_preview)
        resp = requests.post(self._api_base() + "/sendMessage", data=payload, timeout=self.timeout)
        if resp.status_code >= 400:
            raise TelegramClientError(f"Telegram API error {resp.status_code}: {resp.text[:800]}")

        try:
            body = resp.json()
        except Exception as exc:  # pragma: no cover
            raise TelegramClientError(f"Telegram returned non-JSON: {resp.text[:800]}") from exc
        if not body.get("ok", False):
            raise TelegramClientError(f"Telegram sendMessage failed: {str(body)[:800]}")

    def send_document(
        self,
        chat_id: str,
        filepath: Path,
        *,
        caption: str | None = None,
        disable_notification: bool = False,
    ) -> None:
        chat = str(chat_id or "").strip()
        if not chat:
            raise TelegramClientError("Missing chat_id")
        path = Path(filepath)
        if not path.exists():
            raise TelegramClientError(f"File not found: {path}")

        payload: dict[str, Any] = {
            "chat_id": chat,
            "disable_notification": bool(disable_notification),
        }
        if caption is not None:
            payload["caption"] = str(caption)[:1024]
        with path.open("rb") as file_handle:
            files = {"document": (path.name, file_handle, "text/markdown")}
            resp = requests.post(self._api_base() + "/sendDocument", data=payload, files=files, timeout=self.timeout)

        if resp.status_code >= 400:
            raise TelegramClientError(f"Telegram API error {resp.status_code}: {resp.text[:800]}")

        try:
            body = resp.json()
        except Exception as exc:  # pragma: no cover
            raise TelegramClientError(f"Telegram returned non-JSON: {resp.text[:800]}") from exc
        if not body.get("ok", False):
            raise TelegramClientError(f"Telegram sendDocument failed: {str(body)[:800]}")


def create_telegram_client(*, bot_token: str, timeout: int) -> TelegramClient:
    return TelegramClient(bot_token=str(bot_token or ""), timeout=int(timeout))
