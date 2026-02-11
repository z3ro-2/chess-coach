"""Telegram command polling and dispatch helpers."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import requests

from src.commands import list_command_names, run_command
from src.config.provider_config import set_provider

logger = logging.getLogger(__name__)


def poll_telegram_commands(conn: sqlite3.Connection, args: Any) -> int:
    bot_token = str(getattr(args, "telegram_bot_token", "") or "").strip()
    configured_chat_id = str(getattr(args, "telegram_chat_id", "") or "").strip()
    if not bot_token or not configured_chat_id:
        return 0
    admin_chat_ids = _admin_chat_ids(args)

    _ensure_telegram_state_table(conn)
    last_update_id = _read_last_update_id(conn)

    params: dict[str, Any] = {"timeout": 0, "allowed_updates": '["message"]'}
    if last_update_id is not None:
        params["offset"] = int(last_update_id) + 1

    poll_timeout = 3
    updates = _telegram_get_updates(bot_token, params=params, timeout=poll_timeout)
    if not updates:
        return 0

    handled = 0
    max_update_id = last_update_id
    for update in updates:
        update_id = int(update.get("update_id", 0) or 0)
        if max_update_id is None or update_id > max_update_id:
            max_update_id = update_id

        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            continue

        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", "") or "")
        command_name = text.split()[0].lstrip("/").split("@")[0].strip().lower()
        if command_name == "setprovider":
            if chat_id not in admin_chat_ids:
                _telegram_send_message(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    text="Permission denied.",
                    timeout=5,
                )
                handled += 1
                continue
            _handle_setprovider_command(
                bot_token=bot_token,
                chat_id=chat_id,
                text=text,
                timeout=5,
                args=args,
            )
            handled += 1
            continue

        if chat_id != configured_chat_id:
            continue

        if command_name not in set(list_command_names()):
            continue

        try:
            result = run_command(command_name, conn, args)
            _telegram_send_message(
                bot_token=bot_token,
                chat_id=chat_id,
                text=str(result.get("text", "")),
                timeout=5,
            )
            file_path = result.get("file")
            if isinstance(file_path, Path) and file_path.exists():
                _telegram_send_document(
                    bot_token=bot_token,
                    chat_id=chat_id,
                    file_path=file_path,
                    caption=f"{command_name} output",
                    timeout=5,
                )
            handled += 1
        except Exception:
            logger.debug("Telegram command dispatch failed.", exc_info=True)

    if max_update_id is not None:
        _write_last_update_id(conn, int(max_update_id))
    return handled


def _admin_chat_ids(args: Any) -> set[str]:
    configured = str(getattr(args, "telegram_chat_id", "") or "").strip()
    raw_admin = str(getattr(args, "telegram_admin_chat_ids", "") or os.environ.get("TG_ADMIN_CHAT_IDS", "")).strip()
    ids = {token.strip() for token in raw_admin.split(",") if token.strip()}
    if configured:
        ids.add(configured)
    return ids


def _handle_setprovider_command(*, bot_token: str, chat_id: str, text: str, timeout: int, args: Any) -> None:
    parts = text.split()
    if len(parts) != 2:
        _telegram_send_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text="Usage: /setprovider <gpt|ollama>",
            timeout=timeout,
        )
        return

    provider = str(parts[1] or "").strip().lower()
    if provider not in {"gpt", "ollama"}:
        _telegram_send_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text="Invalid provider. Use 'gpt' or 'ollama'.",
            timeout=timeout,
        )
        return

    set_provider(provider)
    setattr(args, "provider", provider)
    setattr(args, "_provider_changed", True)
    _telegram_send_message(
        bot_token=bot_token,
        chat_id=chat_id,
        text=f"Provider set to {provider}. Restarting core loop...",
        timeout=timeout,
    )


def _ensure_telegram_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_state (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          last_update_id INTEGER
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO telegram_state (id, last_update_id) VALUES (1, NULL)")
    conn.commit()


def _read_last_update_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT last_update_id FROM telegram_state WHERE id = 1").fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _write_last_update_id(conn: sqlite3.Connection, last_update_id: int) -> None:
    conn.execute("UPDATE telegram_state SET last_update_id = ? WHERE id = 1", (int(last_update_id),))
    conn.commit()


def _telegram_get_updates(bot_token: str, *, params: dict[str, Any], timeout: int) -> list[dict[str, Any]]:
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        return []
    result = payload.get("result", [])
    return result if isinstance(result, list) else []


def _telegram_send_message(*, bot_token: str, chat_id: str, text: str, timeout: int) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": chat_id, "text": text[:4096]},
        timeout=timeout,
    )
    resp.raise_for_status()


def _telegram_send_document(
    *,
    bot_token: str,
    chat_id: str,
    file_path: Path,
    caption: str,
    timeout: int,
) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    with file_path.open("rb") as file_handle:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption[:1024]},
            files={"document": (file_path.name, file_handle, "text/markdown")},
            timeout=timeout,
        )
    resp.raise_for_status()
