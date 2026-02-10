from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)

_POSTGRES_PREFIXES = ("postgres://", "postgresql://")

CREATE_PLAYER_RATINGS_SQL = """
CREATE TABLE IF NOT EXISTS player_ratings (
  player_username TEXT NOT NULL,
  game_url TEXT PRIMARY KEY,
  end_time TIMESTAMPTZ NOT NULL,
  rating INTEGER,
  time_control TEXT,
  rated BOOLEAN
);
"""

CREATE_PLAYER_RATINGS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_player_ratings_username_end_time
ON player_ratings (player_username, end_time DESC);
"""

PGN_HEADER_RE = re.compile(r'^\[(?P<key>[A-Za-z0-9_]+)\s+"(?P<val>.*)"\]\s*$')


def extract_player_rating_from_pgn(*, pgn: str, player_color: str) -> int | None:
    normalized_color = (player_color or "").strip().lower()
    if normalized_color not in {"white", "black"}:
        return None

    if normalized_color == "white":
        header_keys = ("WhiteElo", "WhiteRating")
    else:
        header_keys = ("BlackElo", "BlackRating")

    for key in header_keys:
        value = _extract_pgn_header(pgn, key)
        rating = _to_int_or_none(value)
        if rating is not None:
            return rating
    return None


def record_player_rating_for_game(
    *,
    player_username: str,
    game_url: str,
    end_time: datetime | str | int,
    player_color: str,
    pgn: str,
    time_control: str,
    rated: bool,
) -> bool:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return False

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping player_ratings write: DB unavailable.", exc_info=True)
        return False

    try:
        rating = extract_player_rating_from_pgn(pgn=pgn, player_color=player_color)
        inserted = insert_player_rating_row(
            conn,
            player_username=player_username,
            game_url=game_url,
            end_time=end_time,
            rating=rating,
            time_control=time_control,
            rated=rated,
        )
        conn.commit()
        return inserted
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping player_ratings write for game_url=%s", game_url, exc_info=True)
        return False
    finally:
        cleanup()


def insert_player_rating_row(
    conn: Any,
    *,
    player_username: str,
    game_url: str,
    end_time: datetime | str | int,
    rating: int | None,
    time_control: str | None,
    rated: bool | None,
) -> bool:
    _execute(conn, CREATE_PLAYER_RATINGS_SQL)
    _execute(conn, CREATE_PLAYER_RATINGS_INDEX_SQL)

    normalized_end_time = _normalize_end_time(end_time)
    normalized_username = (player_username or "").strip()
    if not normalized_username:
        return False
    if not str(game_url).strip():
        return False

    existing = _fetchone(
        conn,
        "SELECT 1 FROM player_ratings WHERE game_url = %s LIMIT 1",
        (game_url,),
    )
    if existing is not None:
        return False

    _execute(
        conn,
        """
        INSERT INTO player_ratings
            (player_username, game_url, end_time, rating, time_control, rated)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (game_url) DO NOTHING
        """,
        (
            normalized_username,
            game_url,
            normalized_end_time,
            rating,
            time_control,
            rated,
        ),
    )
    return True


def get_last_n_ratings(
    player_username: str,
    limit: int = 20,
    *,
    db_session_or_conn: Any | None = None,
) -> list[Mapping[str, Any]]:
    if limit <= 0:
        return []
    return _query_ratings(
        player_username=player_username,
        query="""
            SELECT player_username, game_url, end_time, rating, time_control, rated
            FROM player_ratings
            WHERE player_username = %s
            ORDER BY end_time DESC
            LIMIT %s
        """,
        params=(player_username, int(limit)),
        db_session_or_conn=db_session_or_conn,
        many=True,
    )


def get_rating_at_snapshot(
    player_username: str,
    snapshot_time: datetime | str,
    *,
    db_session_or_conn: Any | None = None,
) -> Mapping[str, Any] | None:
    normalized_snapshot = _normalize_snapshot_time(snapshot_time)
    row = _query_ratings(
        player_username=player_username,
        query="""
            SELECT player_username, game_url, end_time, rating, time_control, rated
            FROM player_ratings
            WHERE player_username = %s AND end_time <= %s
            ORDER BY end_time DESC
            LIMIT 1
        """,
        params=(player_username, normalized_snapshot),
        db_session_or_conn=db_session_or_conn,
        many=False,
    )
    return row


def _query_ratings(
    *,
    player_username: str,
    query: str,
    params: tuple[Any, ...],
    db_session_or_conn: Any | None,
    many: bool,
) -> Any:
    normalized_username = (player_username or "").strip()
    if not normalized_username:
        return [] if many else None

    owns_conn = False
    conn = db_session_or_conn
    cleanup: Callable[[], None] | None = None

    if conn is None:
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not _is_postgres_url(database_url):
            return [] if many else None
        try:
            conn, cleanup = _connect_db(database_url)
            owns_conn = True
        except Exception:
            logger.debug("Skipping player_ratings read: DB unavailable.", exc_info=True)
            return [] if many else None

    try:
        if many:
            return _fetchall(conn, query, params)
        return _fetchone(conn, query, params)
    except Exception:
        logger.debug("Skipping player_ratings read for %s", normalized_username, exc_info=True)
        return [] if many else None
    finally:
        if owns_conn and cleanup is not None:
            cleanup()


def _extract_pgn_header(pgn: str, key: str) -> str | None:
    wanted = key.strip()
    for line in pgn.splitlines():
        stripped = line.strip()
        m = PGN_HEADER_RE.match(stripped)
        if not m:
            if stripped and not stripped.startswith("["):
                break
            continue
        if m.group("key") == wanted:
            return m.group("val")
    return None


def _to_int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _normalize_end_time(end_time: datetime | str | int) -> datetime:
    if isinstance(end_time, datetime):
        dt = end_time
    elif isinstance(end_time, int):
        dt = datetime.fromtimestamp(end_time, tz=timezone.utc)
    elif isinstance(end_time, str):
        dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    else:
        raise TypeError("Unsupported end_time type")

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _normalize_snapshot_time(snapshot_time: datetime | str) -> datetime:
    if isinstance(snapshot_time, datetime):
        dt = snapshot_time
    elif isinstance(snapshot_time, str):
        dt = datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
    else:
        raise TypeError("Unsupported snapshot_time type")

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_postgres_url(database_url: str) -> bool:
    lower = (database_url or "").strip().lower()
    return lower.startswith(_POSTGRES_PREFIXES)


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
