from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping

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


def _connect_db(database_url: str) -> tuple[Any, Callable[[], None]]:
    try:
        import psycopg2  # type: ignore

        conn = psycopg2.connect(database_url)
        conn.autocommit = False
        return conn, conn.close
    except Exception:
        pass

    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    raw_conn = engine.raw_connection()

    def _cleanup() -> None:
        try:
            raw_conn.close()
        finally:
            engine.dispose()

    return raw_conn, _cleanup


def _fetchone(conn: Any, query: str, params: tuple[Any, ...] = ()) -> Mapping[str, Any] | None:
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        row = cur.fetchone()
        if row is None:
            return None
        columns = [col[0] for col in (cur.description or [])]
        if isinstance(row, Mapping):
            return dict(row)
        return {columns[idx]: row[idx] for idx in range(len(columns))}
    finally:
        cur.close()


def _table_columns(conn: Any, table_name: str) -> set[str]:
    rows = _fetchall(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = ANY(current_schemas(false))
          AND table_name = %s
        """,
        (table_name,),
    )
    return {str(row["column_name"]) for row in rows}


def _fetchall(conn: Any, query: str, params: tuple[Any, ...] = ()) -> list[Mapping[str, Any]]:
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        rows = cur.fetchall()
        columns = [col[0] for col in (cur.description or [])]
        out: list[Mapping[str, Any]] = []
        for row in rows:
            if isinstance(row, Mapping):
                out.append(dict(row))
            else:
                out.append({columns[idx]: row[idx] for idx in range(len(columns))})
        return out
    finally:
        cur.close()
