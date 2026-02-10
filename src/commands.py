"""User-invokable operational commands for chess-coach."""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from src.db._pg_utils import _connect_db

CommandResult = dict[str, Any]
CommandFn = Callable[[sqlite3.Connection, Any], CommandResult]


def list_command_names() -> list[str]:
    return list(_command_registry().keys())


def run_command(name: str, conn: sqlite3.Connection, args: Any) -> CommandResult:
    registry = _command_registry()
    handler = registry.get(name)
    if handler is None:
        return _help_command(conn, args)
    return handler(conn, args)


def _command_registry() -> dict[str, CommandFn]:
    return {
        "status": _status_command,
        "summary": _summary_command,
        "stats": _stats_command,
        "health": _health_command,
        "help": _help_command,
    }


def _command_descriptions() -> dict[str, str]:
    return {
        "status": "Show processed-game status and summary cadence state.",
        "summary": "Force-generate player_summary.md and advance summary cadence state.",
        "stats": "Rebuild player_stats.md from current state.",
        "health": "Check SQLite, Postgres, Telegram, and LLM endpoint reachability.",
        "help": "List available commands.",
    }


def _status_command(conn: sqlite3.Connection, args: Any) -> CommandResult:
    last_row = conn.execute(
        "SELECT game_url, end_time FROM processed_games ORDER BY end_time DESC LIMIT 1"
    ).fetchone()
    processed_count = int(conn.execute("SELECT COUNT(*) FROM processed_games").fetchone()[0] or 0)
    summary_row = conn.execute(
        "SELECT last_summary_processed_count FROM summary_state WHERE id = 1"
    ).fetchone()
    last_summary_count = int(summary_row[0] or 0) if summary_row is not None else 0
    since_last_summary = max(0, processed_count - last_summary_count)

    if last_row is None:
        last_game_text = "none"
    else:
        game_url = str(last_row[0])
        end_time = int(last_row[1])
        last_game_text = (
            f"{datetime.fromtimestamp(end_time, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} | {game_url}"
        )

    postgres_enabled = bool(os.environ.get("DATABASE_URL", "").strip())
    lines = [
        "Status",
        f"- Last processed game: {last_game_text}",
        f"- Games since last summary: {since_last_summary}",
        f"- Postgres: {'enabled' if postgres_enabled else 'disabled'}",
        f"- Output directory: {Path(args.out)}",
    ]
    return {"text": "\n".join(lines), "file": None}


def _stats_command(conn: sqlite3.Connection, args: Any) -> CommandResult:
    import chess_review as app

    stats_path = app._write_player_stats_markdown(
        conn,
        out_dir=Path(args.out),
        username=args.username,
        recent_games=max(20, int(args.player_summary_every_n)),
    )
    return {"text": f"Stats rebuilt: {stats_path}", "file": stats_path}


def _summary_command(conn: sqlite3.Connection, args: Any) -> CommandResult:
    import chess_review as app

    processed_count = int(conn.execute("SELECT COUNT(*) FROM processed_games").fetchone()[0] or 0)
    if processed_count <= 0:
        return {"text": "No processed games yet; summary not generated.", "file": None}

    cadence = max(1, int(args.player_summary_every_n))
    stats_path = app._write_player_stats_markdown(
        conn,
        out_dir=Path(args.out),
        username=args.username,
        recent_games=max(20, cadence),
    )
    recent_meta = app._load_recent_game_meta_for_summary(conn, cadence)
    summary_md = app._generate_player_summary_markdown(
        args,
        processed_count=processed_count,
        cadence=cadence,
        stats_path=stats_path,
        recent_meta=recent_meta,
    )
    summary_path = Path(args.out) / "player_summary.md"
    app.write_text(summary_path, summary_md)
    latest_end_time = int(recent_meta[0][0]) if recent_meta else int(time.time())
    app._set_summary_state(conn, processed_count, latest_end_time)
    return {"text": f"Summary generated: {summary_path}", "file": summary_path}


def _health_command(conn: sqlite3.Connection, args: Any) -> CommandResult:
    sqlite_ok = _sqlite_ok(conn)
    postgres_reachable = _postgres_reachable()
    telegram_configured = bool(getattr(args, "telegram_bot_token", "") and getattr(args, "telegram_chat_id", ""))
    llm_reachable = _llm_reachable(args)

    lines = [
        "Health",
        f"- SQLite: {'ok' if sqlite_ok else 'not ok'}",
        f"- Postgres: {'reachable' if postgres_reachable else 'not reachable'}",
        f"- Telegram: {'configured' if telegram_configured else 'not configured'}",
        f"- LLM endpoint: {'reachable' if llm_reachable else 'not reachable'}",
    ]
    return {"text": "\n".join(lines), "file": None}


def _help_command(_conn: sqlite3.Connection, _args: Any) -> CommandResult:
    lines = ["Available commands:"]
    for name, description in _command_descriptions().items():
        lines.append(f"- {name}: {description}")
    return {"text": "\n".join(lines), "file": None}


def _sqlite_ok(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def _postgres_reachable() -> bool:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        return False
    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        return False
    try:
        conn.cursor().execute("SELECT 1")
        return True
    except Exception:
        return False
    finally:
        try:
            cleanup()
        except Exception:
            pass


def _llm_reachable(args: Any) -> bool:
    provider = str(getattr(args, "provider", "ollama"))
    timeout = int(getattr(args, "timeout", 5) or 5)
    if provider == "gpt":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return False
        try:
            resp = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            return resp.status_code < 500
        except Exception:
            return False
    try:
        url = str(getattr(args, "ollama_url", "http://localhost:11434")).rstrip("/") + "/api/tags"
        resp = requests.get(url, timeout=timeout)
        return resp.status_code < 500
    except Exception:
        return False
