"""Core Postgres schema bootstrap for first-run environments.

This module creates the minimal tables required for Postgres-backed bootstrap:
- players
- games

It is safe to call repeatedly and degrades gracefully when DATABASE_URL is not
configured or Postgres is unreachable.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.db._pg_utils import _connect_db, _execute, _table_columns

logger = logging.getLogger(__name__)


def ensure_postgres_core_schema(*, database_url: str | None = None) -> dict[str, Any]:
    """Ensure required Postgres tables exist, returning a structured status."""
    db_url = (database_url or os.environ.get("DATABASE_URL", "")).strip()
    if not db_url:
        return {"ready": False, "reason": "no_database_url", "tables_ready": []}

    try:
        conn, cleanup = _connect_db(db_url)
    except Exception:
        logger.warning("Postgres schema init failed: cannot connect to DATABASE_URL.", exc_info=True)
        return {"ready": False, "reason": "db_unreachable", "tables_ready": []}

    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS players (
              id BIGSERIAL PRIMARY KEY,
              platform_user TEXT UNIQUE,
              username TEXT,
              handle TEXT,
              chess_username TEXT,
              name TEXT,
              created_at TIMESTAMPTZ DEFAULT NOW(),
              updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_players_username ON players(username)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_players_platform_user ON players(platform_user)")

        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS games (
              id BIGSERIAL PRIMARY KEY,
              player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
              game_url TEXT UNIQUE,
              url TEXT,
              pgn TEXT,
              raw_pgn TEXT,
              game_pgn TEXT,
              end_time BIGINT,
              played_at TIMESTAMPTZ,
              time_control TEXT,
              rated BOOLEAN,
              rules TEXT,
              result TEXT,
              white_username TEXT,
              black_username TEXT,
              white_rating INTEGER,
              black_rating INTEGER,
              player_color TEXT,
              failure_notified BOOLEAN NOT NULL DEFAULT FALSE,
              review_notified BOOLEAN NOT NULL DEFAULT FALSE,
              success_notified BOOLEAN NOT NULL DEFAULT FALSE,
              engine_failed BOOLEAN NOT NULL DEFAULT FALSE,
              last_attempt_at TIMESTAMPTZ,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
        )
        _execute(
            conn,
            """
            ALTER TABLE games
            ADD COLUMN IF NOT EXISTS failure_notified BOOLEAN NOT NULL DEFAULT FALSE
            """,
        )
        _execute(
            conn,
            """
            ALTER TABLE games
            ADD COLUMN IF NOT EXISTS review_notified BOOLEAN NOT NULL DEFAULT FALSE
            """,
        )
        _execute(
            conn,
            """
            ALTER TABLE games
            ADD COLUMN IF NOT EXISTS success_notified BOOLEAN NOT NULL DEFAULT FALSE
            """,
        )
        _execute(
            conn,
            """
            ALTER TABLE games
            ADD COLUMN IF NOT EXISTS engine_failed BOOLEAN NOT NULL DEFAULT FALSE
            """,
        )
        _execute(
            conn,
            """
            ALTER TABLE games
            ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ
            """,
        )
        _execute(
            conn,
            """
            ALTER TABLE games
            ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0
            """,
        )
        _execute(
            conn,
            """
            ALTER TABLE games
            ADD COLUMN IF NOT EXISTS last_error TEXT
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_games_player_id ON games(player_id)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_games_game_url ON games(game_url)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_games_url ON games(url)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_games_end_time ON games(end_time)")

        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("Postgres schema init failed while creating core tables.", exc_info=True)
        return {"ready": False, "reason": "schema_init_failed", "tables_ready": []}
    finally:
        cleanup()

    try:
        conn, cleanup = _connect_db(db_url)
    except Exception:
        logger.warning("Postgres schema verification failed: reconnect failed.", exc_info=True)
        return {"ready": False, "reason": "schema_verify_failed", "tables_ready": []}

    try:
        tables_ready: list[str] = []
        if _table_columns(conn, "players"):
            tables_ready.append("players")
        if _table_columns(conn, "games"):
            tables_ready.append("games")
        ready = len(tables_ready) == 2
        if not ready:
            logger.warning(
                "Postgres schema verification incomplete. Found tables: %s",
                ",".join(tables_ready) or "<none>",
            )
            return {"ready": False, "reason": "schema_verify_failed", "tables_ready": tables_ready}
        return {"ready": True, "reason": "ready", "tables_ready": tables_ready}
    except Exception:
        logger.warning("Postgres schema verification failed.", exc_info=True)
        return {"ready": False, "reason": "schema_verify_failed", "tables_ready": []}
    finally:
        cleanup()
