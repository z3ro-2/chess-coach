"""Internal Postgres/DB-API helpers shared by DB modules.

These helpers are intentionally lightweight and keep optional database
integration paths resilient: callers can catch exceptions and degrade
gracefully when Postgres is unavailable.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping


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


def _execute(conn: Any, query: str, params: tuple[Any, ...] = ()) -> None:
    cur = conn.cursor()
    try:
        cur.execute(query, params)
    finally:
        cur.close()


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
