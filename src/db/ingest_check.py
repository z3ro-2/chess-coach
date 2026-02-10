"""Optional Postgres ingest dedupe checks for the poller.

This module is lazy-initialized and always falls back to SQLite-only behavior
when Postgres is unavailable or schema inspection fails.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from src.db._pg_utils import _connect_db, _fetchone, _table_columns

logger = logging.getLogger(__name__)

_INGEST_DB_CONN: Any = None
_INGEST_DB_CLOSE: Callable[[], None] | None = None
_INGEST_DB_URL_COLUMN: str | None = None
_INGEST_DB_INIT_ATTEMPTED = False


def is_game_ingested_in_db(game_url: str) -> bool:
    _init_ingest_db_check()
    if _INGEST_DB_CONN is None or _INGEST_DB_URL_COLUMN is None:
        return False

    try:
        row = _fetchone(
            _INGEST_DB_CONN,
            f"SELECT 1 FROM games WHERE {_INGEST_DB_URL_COLUMN} = %s LIMIT 1",
            (game_url,),
        )
        return row is not None
    except Exception:
        logger.debug("DB ingest check failed; falling back to SQLite state.", exc_info=True)
        return False


def close_ingest_db_check() -> None:
    global _INGEST_DB_CONN, _INGEST_DB_CLOSE, _INGEST_DB_URL_COLUMN, _INGEST_DB_INIT_ATTEMPTED
    if _INGEST_DB_CLOSE is not None:
        try:
            _INGEST_DB_CLOSE()
        except Exception:
            logger.debug("Failed to close ingest DB check connection.", exc_info=True)
    _INGEST_DB_CONN = None
    _INGEST_DB_CLOSE = None
    _INGEST_DB_URL_COLUMN = None
    _INGEST_DB_INIT_ATTEMPTED = False


def _init_ingest_db_check() -> None:
    global _INGEST_DB_CONN, _INGEST_DB_CLOSE, _INGEST_DB_URL_COLUMN, _INGEST_DB_INIT_ATTEMPTED

    if _INGEST_DB_INIT_ATTEMPTED:
        return
    _INGEST_DB_INIT_ATTEMPTED = True

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        return

    try:
        _INGEST_DB_CONN, _INGEST_DB_CLOSE = _connect_db(database_url)
        columns = _table_columns(_INGEST_DB_CONN, "games")
        if "game_url" in columns:
            _INGEST_DB_URL_COLUMN = "game_url"
        elif "url" in columns:
            _INGEST_DB_URL_COLUMN = "url"
        else:
            _INGEST_DB_CONN = None
            if _INGEST_DB_CLOSE is not None:
                _INGEST_DB_CLOSE()
            _INGEST_DB_CLOSE = None
            logger.debug("DB ingest check disabled: games table has no URL column.")
    except Exception:
        logger.debug("Could not initialize DB ingest check.", exc_info=True)
        _INGEST_DB_CONN = None
        _INGEST_DB_CLOSE = None
        _INGEST_DB_URL_COLUMN = None
