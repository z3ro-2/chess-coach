"""Engine-only backfill helpers."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from hashlib import sha256
from types import SimpleNamespace
from typing import Any, Dict, Mapping

from analysis_pipeline import _run_stockfish_oracle
from chess_review import fetch_recent_games, parse_game

logger = logging.getLogger(__name__)


def backfill_recent_games(conn: sqlite3.Connection, username: str, limit: int) -> Dict[str, int]:
    """
    Fetch up to `limit` recent games from chess.com for `username`,
    run Stockfish engine analysis on each, and store engine payloads.
    """
    if int(limit) > 200:
        raise ValueError("Backfill limit exceeded: max 200 games at once.")

    max_games = max(0, int(limit))
    if max_games == 0:
        return {"games_considered": 0, "engine_analyses": 0, "stored": 0}

    _ensure_backfill_tables(conn)
    args = _engine_args_from_env()
    raw_games = fetch_recent_games(str(username), lookback_days=3650)

    parsed_games = []
    for raw in raw_games:
        game = parse_game(raw, str(username))
        if game is None:
            continue
        parsed_games.append(game)

    parsed_games.sort(key=lambda game: int(game.end_time), reverse=True)
    selected_games = parsed_games[:max_games]

    engine_analyses = 0
    stored = 0
    for game in selected_games:
        if _engine_payload_exists(conn, game.game_url):
            _upsert_processed_game(conn, game=game, engine_depth=int(args.engine_depth))
            _upsert_processed_game_meta(conn, game=game)
            conn.commit()
            continue

        engine_output = _run_stockfish_oracle(game=game, args=args, logger=logger)
        if engine_output is None:
            raise RuntimeError(f"Stockfish engine failed for game {game.game_url}")

        payload = {
            "game_summary": dict(engine_output.get("game_summary") or {}),
            "key_positions": list(engine_output.get("key_positions") or []),
        }
        payload["game_summary"]["your_color"] = game.your_color
        payload["game_summary"]["result"] = game.result

        _upsert_processed_game(conn, game=game, engine_depth=int(args.engine_depth))
        _upsert_processed_game_meta(conn, game=game)
        inserted = _upsert_engine_payload(
            conn,
            game_url=game.game_url,
            end_time=int(game.end_time),
            engine_depth=int(args.engine_depth),
            payload=payload,
        )
        engine_analyses += 1
        if inserted:
            stored += 1

    return {
        "games_considered": len(selected_games),
        "engine_analyses": engine_analyses,
        "stored": stored,
    }


def _engine_args_from_env() -> SimpleNamespace:
    depth_raw = str(os.environ.get("ENGINE_DEPTH", "15") or "15").strip()
    try:
        depth = int(depth_raw)
    except Exception:
        depth = 15
    if depth <= 0:
        depth = 15

    return SimpleNamespace(
        enable_engine=True,
        stockfish_path=str(os.environ.get("STOCKFISH_PATH", "/usr/bin/stockfish") or "/usr/bin/stockfish"),
        engine_depth=depth,
    )


def _ensure_backfill_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_games (
          game_url TEXT PRIMARY KEY,
          end_time INTEGER NOT NULL,
          created_at INTEGER NOT NULL,
          md_path TEXT NOT NULL,
          pgn_path TEXT NOT NULL,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          hash TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_game_meta (
          game_url TEXT PRIMARY KEY,
          end_time INTEGER NOT NULL,
          result TEXT NOT NULL,
          player_color TEXT NOT NULL,
          player_rating INTEGER,
          created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_payloads (
          game_url TEXT PRIMARY KEY,
          end_time INTEGER NOT NULL,
          created_at INTEGER NOT NULL,
          engine_depth INTEGER NOT NULL,
          payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_engine_payloads_end_time
        ON engine_payloads(end_time)
        """
    )


def _engine_payload_exists(conn: sqlite3.Connection, game_url: str) -> bool:
    row = conn.execute("SELECT 1 FROM engine_payloads WHERE game_url = ?", (str(game_url),)).fetchone()
    return row is not None


def _load_engine_payload_json(conn: sqlite3.Connection, game_url: str) -> str | None:
    row = conn.execute("SELECT payload_json FROM engine_payloads WHERE game_url = ?", (str(game_url),)).fetchone()
    if row is None:
        return None
    return str(row[0])


def _upsert_processed_game(conn: sqlite3.Connection, *, game: Any, engine_depth: int) -> None:
    provider = "stockfish"
    model = f"stockfish-depth-{int(engine_depth)}"
    content_hash = _backfill_content_hash(game=game, provider=provider, model=model)
    now = int(time.time())
    conn.execute(
        """
        INSERT OR REPLACE INTO processed_games
          (game_url, end_time, created_at, md_path, pgn_path, provider, model, hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(game.game_url),
            int(game.end_time),
            now,
            "__backfill__/no_markdown.md",
            "__backfill__/inline_pgn",
            provider,
            model,
            content_hash,
        ),
    )


def _upsert_processed_game_meta(conn: sqlite3.Connection, *, game: Any) -> None:
    player_rating = None
    if str(game.your_color).lower() == "white":
        player_rating = game.white_rating
    elif str(game.your_color).lower() == "black":
        player_rating = game.black_rating
    conn.execute(
        """
        INSERT OR REPLACE INTO processed_game_meta
          (game_url, end_time, result, player_color, player_rating, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(game.game_url),
            int(game.end_time),
            str(game.result),
            str(game.your_color),
            int(player_rating) if player_rating is not None else None,
            int(time.time()),
        ),
    )


def _upsert_engine_payload(
    conn: sqlite3.Connection,
    *,
    game_url: str,
    end_time: int,
    engine_depth: int,
    payload: Mapping[str, Any],
) -> bool:
    payload_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    existing = _load_engine_payload_json(conn, game_url)
    if existing is not None and existing == payload_json:
        conn.commit()
        return False
    conn.execute(
        """
        INSERT OR REPLACE INTO engine_payloads
          (game_url, end_time, created_at, engine_depth, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(game_url),
            int(end_time),
            int(time.time()),
            int(engine_depth),
            payload_json,
        ),
    )
    # Required behavior: persist each successfully processed game immediately.
    conn.commit()
    return True


def _backfill_content_hash(*, game: Any, provider: str, model: str) -> str:
    h = sha256()
    h.update(str(game.game_url).encode("utf-8"))
    h.update(str(game.end_time).encode("utf-8"))
    h.update(str(provider).encode("utf-8"))
    h.update(str(model).encode("utf-8"))
    h.update(sha256(str(game.pgn).encode("utf-8")).digest())
    return h.hexdigest()
