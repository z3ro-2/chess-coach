"""Optional runtime Postgres updates for poller-processed games.

This module is intentionally best-effort: failures are debug-logged and never
raise into the poller flow.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from src.db._pg_utils import _connect_db, _execute, _fetchall, _fetchone, _table_columns

logger = logging.getLogger(__name__)

USERNAME_COLUMNS = ("platform_user", "username", "handle", "chess_username", "name")
_POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def sync_game_record_and_traits(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Upsert game record and apply trait updates if trait event data exists."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url"}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping Postgres runtime sync: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable"}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns:
            return {"available": False, "reason": "schema_missing"}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved"}

        game_id, inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        conn.commit()

        traits_applied = False
        try:
            traits_applied = _maybe_apply_trait_updates(conn, player_id=player_id, game_id=game_id)
        except Exception:
            logger.debug("Skipping trait state update for game_id=%s", game_id, exc_info=True)

        return {
            "available": True,
            "reason": "ok",
            "player_id": player_id,
            "game_id": game_id,
            "game_inserted": inserted,
            "traits_applied": traits_applied,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping Postgres runtime sync due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed"}
    finally:
        cleanup()


def should_notify_engine_failure(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Check if engine failure Telegram notification should be sent."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "should_notify": True}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping engine failure-notification dedupe: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "should_notify": True}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns or "failure_notified" not in game_columns:
            return {"available": False, "reason": "schema_missing", "should_notify": True}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "should_notify": True}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        row = _fetchone(conn, "SELECT failure_notified FROM games WHERE id = %s LIMIT 1", (game_id,))
        already_notified = bool(row and row.get("failure_notified"))
        conn.commit()
        if already_notified:
            return {"available": True, "reason": "already_notified", "should_notify": False}
        return {"available": True, "reason": "notify_pending", "should_notify": True}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping engine failure-notification dedupe due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "should_notify": True}
    finally:
        cleanup()


def mark_engine_failure_notified(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Mark the game row as having sent engine-failure Telegram notification."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "updated": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping engine failure-notification mark: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "updated": False}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns or "failure_notified" not in game_columns:
            return {"available": False, "reason": "schema_missing", "updated": False}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "updated": False}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        _execute(conn, "UPDATE games SET failure_notified = TRUE WHERE id = %s", (game_id,))
        conn.commit()
        return {"available": True, "reason": "marked_notified", "updated": True}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping engine failure-notification mark due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "updated": False}
    finally:
        cleanup()


def fetch_player_runtime_snapshot(
    *,
    player_username: str,
    recent_games: int = 20,
    trait_limit: int = 10,
) -> dict[str, Any]:
    """Fetch rating/performance/trait snapshot from Postgres when available."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url"}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping Postgres snapshot fetch: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable"}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns:
            return {"available": False, "reason": "schema_missing"}

        player_row = _find_player_row(conn, player_username, player_columns)
        if player_row is None:
            return {"available": False, "reason": "player_not_found"}

        player_id = int(player_row["id"])
        ratings = _fetchall(
            conn,
            """
            SELECT player_username, game_url, end_time, rating, time_control, rated
            FROM player_ratings
            WHERE player_username = %s
            ORDER BY end_time DESC
            LIMIT %s
            """,
            (player_username, max(1, int(recent_games))),
        ) if _table_columns(conn, "player_ratings") else []

        performance_rows = _fetch_recent_game_rows(conn, player_id=player_id, game_columns=game_columns, limit=recent_games)
        perf = _summarize_performance(performance_rows)

        trait_rows = []
        if _table_columns(conn, "traits") and _table_columns(conn, "player_traits"):
            trait_rows = _fetchall(
                conn,
                """
                SELECT t.key, t.name, t.category, t.description, pt.confidence, pt.trend_ema
                FROM player_traits pt
                JOIN traits t ON t.id = pt.trait_id
                WHERE pt.player_id = %s
                ORDER BY pt.confidence DESC, ABS(pt.trend_ema) DESC
                LIMIT %s
                """,
                (player_id, max(1, int(trait_limit))),
            )

        return {
            "available": True,
            "reason": "ok",
            "player_id": player_id,
            "ratings": ratings,
            "performance": perf,
            "traits": trait_rows,
        }
    except Exception:
        logger.debug("Skipping Postgres snapshot fetch due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "snapshot_fetch_failed"}
    finally:
        cleanup()


def _resolve_or_create_player_id(conn: Any, player_username: str, player_columns: set[str]) -> int | None:
    row = _find_player_row(conn, player_username, player_columns)
    if row is not None:
        return int(row["id"])

    username_column = next((c for c in USERNAME_COLUMNS if c in player_columns), None)
    if username_column is None:
        return None

    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {username_column: player_username}
    if "created_at" in player_columns:
        values["created_at"] = now
    if "updated_at" in player_columns:
        values["updated_at"] = now

    cols = list(values.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    _execute(
        conn,
        "INSERT INTO players (" + ", ".join(cols) + ") VALUES (" + placeholders + ")",
        tuple(values[c] for c in cols),
    )

    row = _find_player_row(conn, player_username, player_columns)
    if row is None:
        return None
    return int(row["id"])


def _find_player_row(conn: Any, player_username: str, player_columns: set[str]) -> Mapping[str, Any] | None:
    for col in USERNAME_COLUMNS:
        if col not in player_columns:
            continue
        row = _fetchone(conn, f"SELECT id FROM players WHERE {col} = %s LIMIT 1", (player_username,))
        if row is not None:
            return row
    return None


def _upsert_game(
    conn: Any,
    *,
    player_id: int,
    game_columns: set[str],
    game_payload: Mapping[str, Any],
) -> tuple[int, bool]:
    url_column = "game_url" if "game_url" in game_columns else ("url" if "url" in game_columns else None)
    if url_column is None:
        raise RuntimeError("games table has no URL column")

    game_url = str(game_payload.get("game_url", "")).strip()
    existing = _fetchone(conn, f"SELECT id FROM games WHERE {url_column} = %s LIMIT 1", (game_url,))
    if existing is not None:
        return int(existing["id"]), False

    values: dict[str, Any] = {"player_id": player_id}
    if "game_url" in game_columns:
        values["game_url"] = game_url
    if "url" in game_columns:
        values["url"] = game_url
    if "pgn" in game_columns:
        values["pgn"] = game_payload.get("pgn")
    if "raw_pgn" in game_columns:
        values["raw_pgn"] = game_payload.get("pgn")
    if "game_pgn" in game_columns:
        values["game_pgn"] = game_payload.get("pgn")
    if "end_time" in game_columns:
        values["end_time"] = int(game_payload.get("end_time", 0) or 0)
    if "played_at" in game_columns:
        values["played_at"] = datetime.fromtimestamp(int(game_payload.get("end_time", 0) or 0), tz=timezone.utc)
    if "time_control" in game_columns:
        values["time_control"] = str(game_payload.get("time_control", "") or "")
    if "rated" in game_columns:
        values["rated"] = bool(game_payload.get("rated", False))
    if "rules" in game_columns:
        values["rules"] = str(game_payload.get("rules", "chess") or "chess")
    if "result" in game_columns:
        values["result"] = str(game_payload.get("result", "*") or "*")
    if "white_username" in game_columns:
        values["white_username"] = str(game_payload.get("white_username", "") or "")
    if "black_username" in game_columns:
        values["black_username"] = str(game_payload.get("black_username", "") or "")
    if "white_rating" in game_columns:
        values["white_rating"] = game_payload.get("white_rating")
    if "black_rating" in game_columns:
        values["black_rating"] = game_payload.get("black_rating")
    if "player_color" in game_columns:
        values["player_color"] = str(game_payload.get("player_color", "") or "")

    cols = list(values.keys())
    placeholders = ", ".join(["%s"] * len(cols))
    _execute(conn, "INSERT INTO games (" + ", ".join(cols) + ") VALUES (" + placeholders + ")", tuple(values[c] for c in cols))

    inserted = _fetchone(conn, f"SELECT id FROM games WHERE {url_column} = %s LIMIT 1", (game_url,))
    if inserted is None:
        raise RuntimeError("unable to resolve inserted game id")
    return int(inserted["id"]), True


def _maybe_apply_trait_updates(conn: Any, *, player_id: int, game_id: int) -> bool:
    if not (_table_columns(conn, "traits") and _table_columns(conn, "player_traits") and _table_columns(conn, "trait_events")):
        return False

    trait_event_columns = _table_columns(conn, "trait_events")
    if "player_id" in trait_event_columns:
        event = _fetchone(
            conn,
            "SELECT 1 FROM trait_events WHERE game_id = %s AND player_id = %s LIMIT 1",
            (game_id, player_id),
        )
    else:
        event = _fetchone(conn, "SELECT 1 FROM trait_events WHERE game_id = %s LIMIT 1", (game_id,))

    if event is None:
        return False

    from src.db.trait_updates import apply_trait_updates_for_game

    apply_trait_updates_for_game(player_id, game_id, db_session_or_conn=conn)
    return True


def _fetch_recent_game_rows(conn: Any, *, player_id: int, game_columns: set[str], limit: int) -> list[Mapping[str, Any]]:
    order_by = "id DESC"
    if "played_at" in game_columns:
        order_by = "played_at DESC, id DESC"
    elif "end_time" in game_columns:
        order_by = "end_time DESC, id DESC"

    return _fetchall(
        conn,
        f"""
        SELECT result, player_color
        FROM games
        WHERE player_id = %s
        ORDER BY {order_by}
        LIMIT %s
        """,
        (player_id, max(1, int(limit))),
    )


def _summarize_performance(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    wins = 0
    losses = 0
    draws = 0
    for row in rows:
        result = str(row.get("result", "*") or "*")
        player_color = str(row.get("player_color", "") or "").lower()
        if result == "1/2-1/2":
            draws += 1
            continue
        if player_color == "white":
            if result == "1-0":
                wins += 1
            elif result == "0-1":
                losses += 1
        elif player_color == "black":
            if result == "0-1":
                wins += 1
            elif result == "1-0":
                losses += 1
    return {"wins": wins, "losses": losses, "draws": draws}


def _is_postgres_url(database_url: str) -> bool:
    lower = (database_url or "").strip().lower()
    return lower.startswith(_POSTGRES_PREFIXES)
