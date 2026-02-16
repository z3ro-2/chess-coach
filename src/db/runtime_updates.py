"""Optional runtime Postgres updates for poller-processed games.

This module is intentionally best-effort: failures are debug-logged and never
raise into the poller flow.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Mapping

from src.db.eligibility import eligibility_rejection_reasons, is_game_eligible_for_processing
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


def get_game_processing_flags(
    *,
    player_username: str,
    game_url: str,
) -> dict[str, Any]:
    """Fetch current processing flags for one game without mutating state."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {
            "available": False,
            "reason": "no_database_url",
            "found": False,
            "success_notified": False,
            "engine_failed": False,
            "attempt_count": 0,
        }

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping game-processing-flags read: DB unavailable.", exc_info=True)
        return {
            "available": False,
            "reason": "db_unreachable",
            "found": False,
            "success_notified": False,
            "engine_failed": False,
            "attempt_count": 0,
        }

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns:
            return {
                "available": False,
                "reason": "schema_missing",
                "found": False,
                "success_notified": False,
                "engine_failed": False,
                "attempt_count": 0,
            }
        player_row = _find_player_row(conn, player_username, player_columns)
        if player_row is None:
            return {
                "available": True,
                "reason": "player_not_found",
                "found": False,
                "success_notified": False,
                "engine_failed": False,
                "attempt_count": 0,
            }
        player_id = int(player_row["id"])
        url_col = "game_url" if "game_url" in game_columns else ("url" if "url" in game_columns else None)
        if not url_col:
            return {
                "available": False,
                "reason": "schema_missing",
                "found": False,
                "success_notified": False,
                "engine_failed": False,
                "attempt_count": 0,
            }
        row = _fetchone(
            conn,
            f"""
            SELECT
              COALESCE(success_notified, FALSE) AS success_notified,
              COALESCE(engine_failed, FALSE) AS engine_failed,
              COALESCE(attempt_count, 0) AS attempt_count
            FROM games
            WHERE player_id = %s AND {url_col} = %s
            LIMIT 1
            """,
            (player_id, str(game_url or "").strip()),
        )
        if not row:
            return {
                "available": True,
                "reason": "row_not_found",
                "found": False,
                "success_notified": False,
                "engine_failed": False,
                "attempt_count": 0,
            }
        return {
            "available": True,
            "reason": "ok",
            "found": True,
            "success_notified": bool(row.get("success_notified", False)),
            "engine_failed": bool(row.get("engine_failed", False)),
            "attempt_count": int(row.get("attempt_count", 0) or 0),
        }
    except Exception:
        logger.debug("Skipping game-processing-flags read due to unexpected error.", exc_info=True)
        return {
            "available": False,
            "reason": "runtime_error",
            "found": False,
            "success_notified": False,
            "engine_failed": False,
            "attempt_count": 0,
        }
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


def should_notify_review_success(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Check if review-success Telegram notification should be sent.

    Notification gate is keyed on ``success_notified``.
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "should_notify": True}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping review-success notification dedupe: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "should_notify": True}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns or "success_notified" not in game_columns:
            return {"available": False, "reason": "schema_missing", "should_notify": True}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "should_notify": True}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        row = _fetchone(conn, "SELECT success_notified FROM games WHERE id = %s LIMIT 1", (game_id,))
        already_notified = bool(row and row.get("success_notified"))
        conn.commit()
        if already_notified:
            return {"available": True, "reason": "already_notified", "should_notify": False}
        return {"available": True, "reason": "notify_pending", "should_notify": True}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping review-success notification dedupe due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "should_notify": True}
    finally:
        cleanup()


def mark_review_notified(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Mark the game row as having sent review-success Telegram notification."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "updated": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping review-success notification mark: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "updated": False}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns or "review_notified" not in game_columns:
            return {"available": False, "reason": "schema_missing", "updated": False}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "updated": False}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        _execute(conn, "UPDATE games SET review_notified = TRUE WHERE id = %s", (game_id,))
        conn.commit()
        return {"available": True, "reason": "marked_notified", "updated": True}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping review-success notification mark due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "updated": False}
    finally:
        cleanup()


def consume_success_notification_once(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically consume the review-success notification budget for one game.

    First invocation flips success_notified=false->true and returns should_notify=True.
    Subsequent invocations return should_notify=False.
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "should_notify": True, "consumed": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping success-notification consume: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "should_notify": True, "consumed": False}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns or "success_notified" not in game_columns:
            return {"available": False, "reason": "schema_missing", "should_notify": True, "consumed": False}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "should_notify": True, "consumed": False}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE games SET success_notified = TRUE WHERE id = %s AND COALESCE(success_notified, FALSE) = FALSE",
                (game_id,),
            )
            consumed = bool(int(getattr(cursor, "rowcount", 0) or 0) > 0)
        finally:
            cursor.close()

        conn.commit()
        if consumed:
            return {"available": True, "reason": "consumed", "should_notify": True, "consumed": True}
        return {"available": True, "reason": "already_consumed", "should_notify": False, "consumed": False}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping success-notification consume due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "should_notify": True, "consumed": False}
    finally:
        cleanup()


def record_game_attempt(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
    last_error: str | None,
) -> dict[str, Any]:
    """Increment attempt_count and stamp last_attempt_at for this game."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "updated": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping game attempt stamp: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "updated": False}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        required = {"attempt_count", "last_attempt_at", "last_error"}
        if not player_columns or not game_columns or not required.issubset(game_columns):
            return {"available": False, "reason": "schema_missing", "updated": False}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "updated": False}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        _execute(
            conn,
            """
            UPDATE games
            SET attempt_count = COALESCE(attempt_count, 0) + 1,
                last_attempt_at = NOW(),
                last_error = %s
            WHERE id = %s
            """,
            ((str(last_error)[:4000] if last_error else None), game_id),
        )
        conn.commit()
        return {"available": True, "reason": "updated", "updated": True}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping game attempt stamp due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "updated": False}
    finally:
        cleanup()


def mark_review_success_flags(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Mark both success_notified and review_notified true after successful review generation."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "updated": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping review-success flag mark: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "updated": False}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        required = {"success_notified", "review_notified"}
        if not player_columns or not game_columns or not required.issubset(game_columns):
            return {"available": False, "reason": "schema_missing", "updated": False}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "updated": False}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        set_parts = ["success_notified = TRUE", "review_notified = TRUE"]
        if "analysis_complete" in game_columns:
            set_parts.append("analysis_complete = TRUE")
        if "last_error" in game_columns:
            set_parts.append("last_error = NULL")
        if "tg_send_failed" in game_columns:
            set_parts.append("tg_send_failed = FALSE")
        if "tg_last_error" in game_columns:
            set_parts.append("tg_last_error = NULL")
        if "tg_send_attempts" in game_columns:
            set_parts.append("tg_send_attempts = 0")
        if "tg_last_send_at" in game_columns:
            set_parts.append("tg_last_send_at = NULL")
        _execute(conn, "UPDATE games SET " + ", ".join(set_parts) + " WHERE id = %s", (game_id,))
        if "completed_at" in game_columns:
            tg_max_attempts = max(1, _env_int("TG_MAX_SEND_ATTEMPTS", 5))
            if "tg_send_attempts" in game_columns:
                _execute(
                    conn,
                    """
                    UPDATE games
                    SET completed_at = NOW()
                    WHERE id = %s
                      AND COALESCE(success_notified, FALSE) = TRUE
                      AND COALESCE(analysis_complete, FALSE) = TRUE
                      AND COALESCE(tg_send_attempts, 0) <= %s
                    """,
                    (game_id, int(tg_max_attempts)),
                )
            else:
                _execute(
                    conn,
                    """
                    UPDATE games
                    SET completed_at = NOW()
                    WHERE id = %s
                      AND COALESCE(success_notified, FALSE) = TRUE
                      AND COALESCE(analysis_complete, FALSE) = TRUE
                    """,
                    (game_id,),
                )
        conn.commit()
        return {"available": True, "reason": "marked_success", "updated": True}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping review-success flag mark due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "updated": False}
    finally:
        cleanup()


def mark_analysis_complete(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
    md_path: str,
) -> dict[str, Any]:
    """Mark analysis as complete and persist markdown path."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "updated": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping analysis-complete mark: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "updated": False}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        required = {"analysis_complete", "md_path"}
        if not player_columns or not game_columns or not required.issubset(game_columns):
            return {"available": False, "reason": "schema_missing", "updated": False}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "updated": False}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        set_parts = ["analysis_complete = TRUE", "md_path = %s"]
        params: list[Any] = [str(md_path or "")]
        if "tg_send_failed" in game_columns:
            set_parts.append("tg_send_failed = FALSE")
        if "tg_last_error" in game_columns:
            set_parts.append("tg_last_error = NULL")
        params.append(game_id)
        _execute(conn, "UPDATE games SET " + ", ".join(set_parts) + " WHERE id = %s", tuple(params))
        conn.commit()
        return {"available": True, "reason": "marked_analysis_complete", "updated": True}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping analysis-complete mark due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "updated": False}
    finally:
        cleanup()


def mark_telegram_send_failed(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
    error_message: str,
) -> dict[str, Any]:
    """Persist Telegram send failure state for later send-only retries."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "updated": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping tg-send-failed mark: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "updated": False}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        required = {"tg_send_failed", "tg_last_error"}
        if not player_columns or not game_columns or not required.issubset(game_columns):
            return {"available": False, "reason": "schema_missing", "updated": False}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "updated": False}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        set_parts = ["tg_send_failed = TRUE", "tg_last_error = %s"]
        params: list[Any] = [str(error_message or "")[:4000]]
        if "tg_send_attempts" in game_columns:
            set_parts.append("tg_send_attempts = COALESCE(tg_send_attempts, 0) + 1")
        if "tg_last_send_at" in game_columns:
            set_parts.append("tg_last_send_at = NOW()")
        params.append(game_id)
        _execute(conn, "UPDATE games SET " + ", ".join(set_parts) + " WHERE id = %s", tuple(params))
        conn.commit()
        return {"available": True, "reason": "marked_tg_send_failed", "updated": True}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping tg-send-failed mark due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "updated": False}
    finally:
        cleanup()


def should_skip_game_due_to_attempt_backoff(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
    max_attempts: int,
    window_hours: int,
    ignore_backoff: bool = False,
) -> dict[str, Any]:
    """Return skip=true when attempt threshold is exceeded within recent window."""
    if bool(ignore_backoff):
        return {"available": True, "reason": "override", "skip": False}

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "skip": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping attempt-backoff check: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "skip": False}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        required = {"attempt_count", "last_attempt_at"}
        if not player_columns or not game_columns or not required.issubset(game_columns):
            return {"available": False, "reason": "schema_missing", "skip": False}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "skip": False}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        row = _fetchone(
            conn,
            """
            SELECT
              COALESCE(attempt_count, 0) AS attempt_count,
              CASE
                WHEN COALESCE(attempt_count, 0) >= %s
                 AND last_attempt_at IS NOT NULL
                 AND last_attempt_at >= (NOW() - (%s * INTERVAL '1 hour'))
                THEN TRUE
                ELSE FALSE
              END AS blocked
            FROM games
            WHERE id = %s
            LIMIT 1
            """,
            (max(1, int(max_attempts)), max(1, int(window_hours)), game_id),
        )
        conn.commit()
        blocked = bool(row and row.get("blocked"))
        if blocked:
            return {
                "available": True,
                "reason": "attempt_backoff",
                "skip": True,
                "attempt_count": int(row.get("attempt_count", 0) or 0) if row else 0,
            }
        return {"available": True, "reason": "ok", "skip": False}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping attempt-backoff check due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "skip": False}
    finally:
        cleanup()


def mark_engine_failed(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Mark game row as engine_failed for retry-failures selection."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "updated": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping engine_failed mark: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "updated": False}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns or "engine_failed" not in game_columns:
            return {"available": False, "reason": "schema_missing", "updated": False}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "updated": False}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        _execute(conn, "UPDATE games SET engine_failed = TRUE WHERE id = %s", (game_id,))
        conn.commit()
        return {"available": True, "reason": "marked_engine_failed", "updated": True}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping engine_failed mark due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "updated": False}
    finally:
        cleanup()


def clear_engine_failed(
    *,
    player_username: str,
    game_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Clear engine_failed after successful processing."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "updated": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping engine_failed clear: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "updated": False}

    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns or "engine_failed" not in game_columns:
            return {"available": False, "reason": "schema_missing", "updated": False}

        player_id = _resolve_or_create_player_id(conn, player_username, player_columns)
        if player_id is None:
            conn.rollback()
            return {"available": False, "reason": "player_unresolved", "updated": False}

        game_id, _inserted = _upsert_game(conn, player_id=player_id, game_columns=game_columns, game_payload=game_payload)
        _execute(conn, "UPDATE games SET engine_failed = FALSE WHERE id = %s", (game_id,))
        conn.commit()
        return {"available": True, "reason": "cleared_engine_failed", "updated": True}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping engine_failed clear due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_sync_failed", "updated": False}
    finally:
        cleanup()


def load_retry_failure_game_payloads(
    *,
    player_username: str,
    limit: int = 200,
    max_attempts: int = 5,
    window_hours: int = 6,
    ignore_backoff: bool = False,
) -> list[dict[str, Any]]:
    """Load retry-failure payloads eligible under poll retry rules.

    Eligibility:
    - success_notified = FALSE
    - (engine_failed = TRUE OR attempt_count < POLL_MAX_ATTEMPTS)
    - last_attempt_at is NULL OR older than POLL_COOLDOWN_SECONDS
    - pgn present and non-empty
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return []
    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping retry-failures fetch: DB unavailable.", exc_info=True)
        return []
    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        if not player_columns or not game_columns:
            return []
        required = {
            "engine_failed",
            "success_notified",
            "attempt_count",
            "last_attempt_at",
        }
        if not required.issubset(game_columns):
            return []
        player_row = _find_player_row(conn, player_username, player_columns)
        if player_row is None:
            return []
        player_id = int(player_row["id"])

        poll_max_attempts = _env_int("POLL_MAX_ATTEMPTS", _env_int("MAX_ATTEMPTS", max(1, int(max_attempts))))
        poll_cooldown_seconds = _env_int(
            "POLL_COOLDOWN_SECONDS",
            _env_int("ATTEMPT_COOLDOWN_SECONDS", max(0, int(window_hours) * 3600)),
        )
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0, int(poll_cooldown_seconds)))

        completed_at_select = "completed_at" if "completed_at" in game_columns else "NULL AS completed_at"
        rows = _fetchall(
            conn,
            """
            SELECT game_url, pgn, raw_pgn, game_pgn, end_time, time_control, rated, rules, result,
                   white_username, black_username, white_rating, black_rating, player_color,
                   success_notified, engine_failed, attempt_count, last_attempt_at,
                   """
            + completed_at_select
            + """
            FROM games
            WHERE player_id = %s
            ORDER BY end_time DESC, id DESC
            LIMIT %s
            """,
            (player_id, max(1, int(limit))),
        )
        eligible_rows: list[dict[str, Any]] = []
        for row in rows:
            row_dict = dict(row)
            if _coerce_datetime_utc(row_dict.get("completed_at")) is not None:
                continue
            if bool(row_dict.get("success_notified", False)):
                continue
            pgn = str(row_dict.get("pgn", "") or "").strip()
            if not pgn:
                continue
            attempts = int(row_dict.get("attempt_count", 0) or 0)
            engine_failed = bool(row_dict.get("engine_failed", False))
            if not (engine_failed or attempts < max(1, int(poll_max_attempts))):
                continue
            if not bool(ignore_backoff):
                last_attempt_at = _coerce_datetime_utc(row_dict.get("last_attempt_at"))
                if last_attempt_at is not None and last_attempt_at >= cutoff:
                    continue
            eligible_rows.append(row_dict)
        return eligible_rows
    except Exception:
        logger.debug("Skipping retry-failures fetch due to unexpected error.", exc_info=True)
        return []
    finally:
        cleanup()


def cleanup_completed_games(
    *,
    player_username: str,
    limit: int = 500,
) -> dict[str, Any]:
    """Mark permanently-completed rows with completed_at.

    Rows are marked completed when:
    - analysis_complete = TRUE
    - success_notified = TRUE
    - tg_send_attempts <= TG_MAX_SEND_ATTEMPTS (when column exists)

    Cooldown gate:
    - last_attempt_at is NULL OR older than POLL_COOLDOWN_SECONDS
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "marked_count": 0}
    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping completed-row cleanup: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "marked_count": 0}
    try:
        player_columns = _table_columns(conn, "players")
        game_columns = _table_columns(conn, "games")
        required = {"success_notified", "analysis_complete", "last_attempt_at", "completed_at"}
        if not player_columns or not game_columns or not required.issubset(game_columns):
            return {"available": False, "reason": "schema_missing", "marked_count": 0}
        player_row = _find_player_row(conn, player_username, player_columns)
        if player_row is None:
            return {"available": False, "reason": "player_missing", "marked_count": 0}
        player_id = int(player_row["id"])

        cooldown_seconds = _env_int(
            "POLL_COOLDOWN_SECONDS",
            _env_int("ATTEMPT_COOLDOWN_SECONDS", 600),
        )
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0, int(cooldown_seconds)))

        rows = _fetchall(
            conn,
            """
            SELECT
              id, game_url, success_notified, analysis_complete, last_attempt_at, completed_at,
              """
            + ("tg_send_attempts" if "tg_send_attempts" in game_columns else "0 AS tg_send_attempts")
            + """
            FROM games
            WHERE player_id = %s
              AND completed_at IS NULL
            ORDER BY id ASC
            LIMIT %s
            """,
            (player_id, max(1, int(limit))),
        )
        marked = 0
        for row in rows:
            row_dict = dict(row)
            if _coerce_datetime_utc(row_dict.get("completed_at")) is not None:
                continue
            last_attempt_at = _coerce_datetime_utc(row_dict.get("last_attempt_at"))
            if last_attempt_at is not None and last_attempt_at >= cutoff:
                continue
            success_notified = bool(row_dict.get("success_notified", False))
            analysis_complete = bool(row_dict.get("analysis_complete", False))
            tg_attempts = int(row_dict.get("tg_send_attempts", 0) or 0)
            tg_max_attempts = max(1, _env_int("TG_MAX_SEND_ATTEMPTS", 5))
            should_complete = success_notified and analysis_complete and (tg_attempts <= tg_max_attempts)
            if not should_complete:
                continue
            game_id = int(row_dict.get("id", 0) or 0)
            if game_id <= 0:
                continue
            _execute(conn, "UPDATE games SET completed_at = NOW() WHERE id = %s AND completed_at IS NULL", (game_id,))
            marked += 1
        conn.commit()
        return {"available": True, "reason": "ok", "marked_count": int(marked)}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping completed-row cleanup due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_error", "marked_count": 0}
    finally:
        cleanup()


def get_pending_games_for_processing(limit: int) -> list[dict[str, Any]]:
    """Return pending game rows eligible for processing.

    Eligibility:
    - completed_at IS NULL
    - success_notified = FALSE
    - engine_failed = FALSE
    - attempt_count < max attempts (env POLL_MAX_ATTEMPTS, default 5)
    - last_attempt_at is null OR older than cooldown window
      (env POLL_COOLDOWN_SECONDS, default 600)
    - send-only retries (analysis_complete=TRUE) respect
      TG_MAX_SEND_ATTEMPTS/TG_RETRY_COOLDOWN_SECONDS
    - pgn present and non-empty
    Ordered by recency (played_at DESC NULLS LAST, created_at DESC, id DESC),
    limited by ``limit``.
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return []

    diag = get_pending_games_for_processing_diagnostics(limit=limit)
    return list(diag.get("eligible_rows") or [])


def get_pending_games_for_processing_diagnostics(limit: int) -> dict[str, Any]:
    """Return pending-game eligibility diagnostics + limited eligible rows."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {
            "available": False,
            "reason": "no_database_url",
            "pending_total": 0,
            "eligible_now": 0,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "excluded_by_pgn_missing_terminal": 0,
            "excluded_by_tg_send_backoff": 0,
            "sample_excluded_by_success_notified": [],
            "sample_excluded_by_engine_failed": [],
            "sample_excluded_by_attempt_cap": [],
            "sample_excluded_by_cooldown": [],
            "sample_excluded_by_pgn_missing_terminal": [],
            "sample_excluded_by_tg_send_backoff": [],
            "top_newest_pending": [],
            "eligible_rows": [],
        }

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping pending-game diagnostics: DB unavailable.", exc_info=True)
        return {
            "available": False,
            "reason": "db_unreachable",
            "pending_total": 0,
            "eligible_now": 0,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "excluded_by_pgn_missing_terminal": 0,
            "excluded_by_tg_send_backoff": 0,
            "sample_excluded_by_success_notified": [],
            "sample_excluded_by_engine_failed": [],
            "sample_excluded_by_attempt_cap": [],
            "sample_excluded_by_cooldown": [],
            "sample_excluded_by_pgn_missing_terminal": [],
            "sample_excluded_by_tg_send_backoff": [],
            "top_newest_pending": [],
            "eligible_rows": [],
        }
    try:
        game_columns = _table_columns(conn, "games")
        required = {
            "pgn",
            "played_at",
            "success_notified",
            "engine_failed",
            "attempt_count",
            "last_attempt_at",
        }
        if not game_columns or not required.issubset(game_columns):
            return {
                "available": False,
                "reason": "schema_missing",
                "pending_total": 0,
                "eligible_now": 0,
                "excluded_by_cooldown": 0,
                "excluded_by_attempt_cap": 0,
                "excluded_by_engine_failed": 0,
                "excluded_by_success_notified": 0,
                "excluded_by_pgn_missing_terminal": 0,
                "excluded_by_tg_send_backoff": 0,
                "sample_excluded_by_success_notified": [],
                "sample_excluded_by_engine_failed": [],
                "sample_excluded_by_attempt_cap": [],
                "sample_excluded_by_cooldown": [],
                "sample_excluded_by_pgn_missing_terminal": [],
                "sample_excluded_by_tg_send_backoff": [],
                "top_newest_pending": [],
                "eligible_rows": [],
            }

        rows = _fetchall(
            conn,
            """
            SELECT
              id, game_url, pgn, end_time, time_control, rated, rules, result,
              white_username, black_username, white_rating, black_rating, player_color,
              played_at, success_notified, engine_failed, attempt_count, last_attempt_at,
              """
            + ("analysis_complete" if "analysis_complete" in game_columns else "FALSE AS analysis_complete")
            + ", "
            + ("md_path" if "md_path" in game_columns else "NULL AS md_path")
            + ", "
            + ("tg_send_failed" if "tg_send_failed" in game_columns else "FALSE AS tg_send_failed")
            + ", "
            + ("tg_last_error" if "tg_last_error" in game_columns else "NULL AS tg_last_error")
            + ", "
            + ("tg_send_attempts" if "tg_send_attempts" in game_columns else "0 AS tg_send_attempts")
            + ", "
            + ("tg_last_send_at" if "tg_last_send_at" in game_columns else "NULL AS tg_last_send_at")
            + ", "
            + ("pgn_missing" if "pgn_missing" in game_columns else "FALSE AS pgn_missing")
            + ", "
            + ("pgn_missing_terminal" if "pgn_missing_terminal" in game_columns else "FALSE AS pgn_missing_terminal")
            + ", "
            + """
              """
            + ("completed_at" if "completed_at" in game_columns else "NULL AS completed_at")
            + ", "
            + ("created_at" if "created_at" in game_columns else "NULL AS created_at")
            + """
            FROM games
            ORDER BY played_at DESC NULLS LAST, created_at DESC, id DESC
            """,
        )
        # Prefer poll-specific knobs; keep legacy names as compatibility fallback.
        max_attempts = _env_int("POLL_MAX_ATTEMPTS", _env_int("MAX_ATTEMPTS", 5))
        cooldown_seconds = _env_int(
            "POLL_COOLDOWN_SECONDS",
            _env_int("ATTEMPT_COOLDOWN_SECONDS", 600),
        )
        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc - timedelta(seconds=max(0, int(cooldown_seconds)))

        pending_rows: list[dict[str, Any]] = []
        eligible_rows_all: list[dict[str, Any]] = []
        excluded_by_success_notified = 0
        excluded_by_engine_failed = 0
        excluded_by_attempt_cap = 0
        excluded_by_cooldown = 0
        excluded_by_pgn_missing_terminal = 0
        excluded_by_tg_send_backoff = 0
        sample_excluded_by_success_notified: list[str] = []
        sample_excluded_by_engine_failed: list[str] = []
        sample_excluded_by_attempt_cap: list[str] = []
        sample_excluded_by_cooldown: list[str] = []
        sample_excluded_by_pgn_missing_terminal: list[str] = []
        sample_excluded_by_tg_send_backoff: list[str] = []

        for row in rows:
            row_dict = dict(row)
            if _coerce_datetime_utc(row_dict.get("completed_at")) is not None:
                continue
            if bool(row_dict.get("success_notified", False)):
                excluded_by_success_notified += 1
                if len(sample_excluded_by_success_notified) < 5:
                    sample_excluded_by_success_notified.append(str(row_dict.get("game_url", "") or ""))
            else:
                pending_rows.append(row_dict)

            if bool(row_dict.get("success_notified", False)):
                continue
            send_only_retry = bool(row_dict.get("analysis_complete", False))
            if send_only_retry:
                reasons = eligibility_rejection_reasons(
                    row_dict,
                    now=now_utc,
                    max_attempts=max_attempts,
                    cooldown_seconds=cooldown_seconds,
                )
                if bool(reasons.get("tg_attempt_cap", False)) or bool(reasons.get("tg_cooldown_active", False)):
                    excluded_by_tg_send_backoff += 1
                    if len(sample_excluded_by_tg_send_backoff) < 5:
                        sample_excluded_by_tg_send_backoff.append(str(row_dict.get("game_url", "") or ""))
                    continue
                if is_game_eligible_for_processing(
                    row_dict,
                    now=now_utc,
                    max_attempts=max_attempts,
                    cooldown_seconds=cooldown_seconds,
                ):
                    eligible_rows_all.append(row_dict)
                continue
            if bool(row_dict.get("pgn_missing_terminal", False)):
                excluded_by_pgn_missing_terminal += 1
                if len(sample_excluded_by_pgn_missing_terminal) < 5:
                    sample_excluded_by_pgn_missing_terminal.append(str(row_dict.get("game_url", "") or ""))
                continue
            if bool(row_dict.get("engine_failed", False)):
                excluded_by_engine_failed += 1
                if len(sample_excluded_by_engine_failed) < 5:
                    sample_excluded_by_engine_failed.append(str(row_dict.get("game_url", "") or ""))
                continue
            attempts = int(row_dict.get("attempt_count", 0) or 0)
            if attempts >= max(1, int(max_attempts)):
                excluded_by_attempt_cap += 1
                if len(sample_excluded_by_attempt_cap) < 5:
                    sample_excluded_by_attempt_cap.append(str(row_dict.get("game_url", "") or ""))
                continue
            last_attempt_at = _coerce_datetime_utc(row_dict.get("last_attempt_at"))
            if last_attempt_at is not None and last_attempt_at >= cutoff:
                excluded_by_cooldown += 1
                if len(sample_excluded_by_cooldown) < 5:
                    sample_excluded_by_cooldown.append(str(row_dict.get("game_url", "") or ""))
                continue
            if str(row_dict.get("rules", "chess") or "chess").strip().lower() != "chess":
                continue
            if not str(row_dict.get("time_control", "") or "").strip():
                continue
            if is_game_eligible_for_processing(
                row_dict,
                now=now_utc,
                max_attempts=max_attempts,
                cooldown_seconds=cooldown_seconds,
            ):
                eligible_rows_all.append(row_dict)

        eligible_rows_all.sort(
            key=lambda item: (
                _coerce_datetime_utc(item.get("played_at"))
                or _coerce_datetime_utc(item.get("created_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
                _coerce_datetime_utc(item.get("created_at"))
                or datetime.min.replace(tzinfo=timezone.utc),
                int(item.get("id", 0) or 0),
            ),
            reverse=True,
        )
        newest_pending = sorted(
            pending_rows,
            key=lambda item: (
                _coerce_datetime_utc(item.get("played_at")) is None,
                _coerce_datetime_utc(item.get("played_at")) or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )[:10]
        top_newest_pending = [
            {
                "game_url": str(item.get("game_url", "") or ""),
                "success_notified": bool(item.get("success_notified", False)),
                "engine_failed": bool(item.get("engine_failed", False)),
                "attempt_count": int(item.get("attempt_count", 0) or 0),
                "last_attempt_at": str(item.get("last_attempt_at", "") or ""),
                "analysis_complete": bool(item.get("analysis_complete", False)),
                "tg_send_failed": bool(item.get("tg_send_failed", False)),
                "tg_send_attempts": int(item.get("tg_send_attempts", 0) or 0),
                "tg_last_send_at": str(item.get("tg_last_send_at", "") or ""),
            }
            for item in newest_pending
        ]

        return {
            "available": True,
            "reason": "ok",
            "total_games_in_db": int(len(rows)),
            "total_pending_success_notified_false": int(len(pending_rows)),
            "pending_total": int(len(pending_rows)),
            "eligible_now": int(len(eligible_rows_all)),
            "excluded_by_cooldown": int(excluded_by_cooldown),
            "excluded_by_attempt_cap": int(excluded_by_attempt_cap),
            "excluded_by_engine_failed": int(excluded_by_engine_failed),
            "excluded_by_success_notified": int(excluded_by_success_notified),
            "excluded_by_pgn_missing_terminal": int(excluded_by_pgn_missing_terminal),
            "excluded_by_tg_send_backoff": int(excluded_by_tg_send_backoff),
            "sample_excluded_by_success_notified": sample_excluded_by_success_notified,
            "sample_excluded_by_engine_failed": sample_excluded_by_engine_failed,
            "sample_excluded_by_attempt_cap": sample_excluded_by_attempt_cap,
            "sample_excluded_by_cooldown": sample_excluded_by_cooldown,
            "sample_excluded_by_pgn_missing_terminal": sample_excluded_by_pgn_missing_terminal,
            "sample_excluded_by_tg_send_backoff": sample_excluded_by_tg_send_backoff,
            "top_newest_pending": top_newest_pending,
            "eligible_rows": eligible_rows_all[: max(1, int(limit))],
        }
    except Exception:
        logger.debug("Skipping pending-game diagnostics due to unexpected error.", exc_info=True)
        return {
            "available": False,
            "reason": "runtime_error",
            "pending_total": 0,
            "eligible_now": 0,
            "excluded_by_cooldown": 0,
            "excluded_by_attempt_cap": 0,
            "excluded_by_engine_failed": 0,
            "excluded_by_success_notified": 0,
            "excluded_by_pgn_missing_terminal": 0,
            "excluded_by_tg_send_backoff": 0,
            "sample_excluded_by_success_notified": [],
            "sample_excluded_by_engine_failed": [],
            "sample_excluded_by_attempt_cap": [],
            "sample_excluded_by_cooldown": [],
            "sample_excluded_by_pgn_missing_terminal": [],
            "sample_excluded_by_tg_send_backoff": [],
            "top_newest_pending": [],
            "eligible_rows": [],
        }
    finally:
        cleanup()


def load_games_missing_pgn(limit: int = 100) -> list[dict[str, Any]]:
    """Return game rows that have a URL but are missing PGN text."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return []
    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping missing-PGN scan: DB unavailable.", exc_info=True)
        return []
    try:
        game_columns = _table_columns(conn, "games")
        if not game_columns:
            return []
        if "game_url" not in game_columns and "url" not in game_columns:
            return []
        if "pgn" not in game_columns and "raw_pgn" not in game_columns and "game_pgn" not in game_columns:
            return []

        url_col = "game_url" if "game_url" in game_columns else "url"
        missing_parts: list[str] = []
        if "pgn" in game_columns:
            missing_parts.append("(pgn IS NULL OR TRIM(pgn) = '')")
        if "raw_pgn" in game_columns:
            missing_parts.append("(raw_pgn IS NULL OR TRIM(raw_pgn) = '')")
        if "game_pgn" in game_columns:
            missing_parts.append("(game_pgn IS NULL OR TRIM(game_pgn) = '')")
        if not missing_parts:
            return []

        retry_seconds = max(0, _env_int("PGN_MISSING_RETRY_SECONDS", 3600))
        query = (
            f"""
            SELECT id, {url_col} AS game_url,
                   """
            + ("pgn_missing" if "pgn_missing" in game_columns else "FALSE AS pgn_missing")
            + ", "
            + ("pgn_missing_terminal" if "pgn_missing_terminal" in game_columns else "FALSE AS pgn_missing_terminal")
            + ", "
            + ("pgn_missing_attempts" if "pgn_missing_attempts" in game_columns else "0 AS pgn_missing_attempts")
            + ", "
            + ("pgn_missing_count" if "pgn_missing_count" in game_columns else "0 AS pgn_missing_count")
            + ", "
            + ("pgn_missing_last_attempt_at" if "pgn_missing_last_attempt_at" in game_columns else "NULL AS pgn_missing_last_attempt_at")
            + f"""
            FROM games
            WHERE {url_col} IS NOT NULL
              AND TRIM({url_col}) <> ''
              AND ({' OR '.join(missing_parts)})
              AND (
                """
            + ("COALESCE(pgn_missing, FALSE) = FALSE" if "pgn_missing" in game_columns else "TRUE")
            + """
              )
              AND (
                """
            + (
                "(COALESCE(pgn_missing_terminal, FALSE) = FALSE)"
                if "pgn_missing_terminal" in game_columns
                else "TRUE"
            )
            + """
              )
              AND (
                """
            + (
                "(pgn_missing_last_attempt_at IS NULL OR pgn_missing_last_attempt_at < (NOW() - (%s * INTERVAL '1 second')))"
                if "pgn_missing_last_attempt_at" in game_columns
                else "TRUE"
            )
            + """
              )
            ORDER BY played_at ASC NULLS LAST, id ASC
            LIMIT %s
            """
        )
        params: list[Any] = []
        if "pgn_missing_last_attempt_at" in game_columns:
            params.append(retry_seconds)
        params.append(max(1, int(limit)))
        rows = _fetchall(conn, query, tuple(params))
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("Skipping missing-PGN scan due to unexpected error.", exc_info=True)
        return []
    finally:
        cleanup()


def update_game_pgn_for_url(*, game_url: str, pgn: str) -> bool:
    """Update PGN fields for one game URL; returns True when updated."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return False
    clean_url = str(game_url or "").strip()
    clean_pgn = str(pgn or "").strip()
    if not clean_url or not clean_pgn:
        return False
    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping PGN update: DB unavailable.", exc_info=True)
        return False
    try:
        game_columns = _table_columns(conn, "games")
        if not game_columns:
            return False
        if "game_url" not in game_columns and "url" not in game_columns:
            return False
        set_parts: list[str] = []
        params: list[Any] = []
        for field in ("pgn", "raw_pgn", "game_pgn"):
            if field in game_columns:
                set_parts.append(f"{field} = %s")
                params.append(clean_pgn)
        if "pgn_missing" in game_columns:
            set_parts.append("pgn_missing = FALSE")
        if "pgn_missing_attempts" in game_columns:
            set_parts.append("pgn_missing_attempts = 0")
        if "pgn_missing_count" in game_columns:
            set_parts.append("pgn_missing_count = 0")
        if "pgn_missing_last_attempt_at" in game_columns:
            set_parts.append("pgn_missing_last_attempt_at = NULL")
        if "pgn_missing_terminal" in game_columns:
            set_parts.append("pgn_missing_terminal = FALSE")
        if not set_parts:
            return False
        url_col = "game_url" if "game_url" in game_columns else "url"
        params.append(clean_url)
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE games SET {', '.join(set_parts)} WHERE {url_col} = %s",
                tuple(params),
            )
            updated = bool(int(getattr(cursor, "rowcount", 0) or 0) > 0)
        finally:
            cursor.close()
        conn.commit()
        return updated
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping PGN update due to unexpected error.", exc_info=True)
        return False
    finally:
        cleanup()


def record_pgn_missing_not_found(*, game_url: str) -> dict[str, Any]:
    """Record a PGN not-found event and optionally mark row as permanently missing."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "updated": False}
    clean_url = str(game_url or "").strip()
    if not clean_url:
        return {"available": False, "reason": "empty_url", "updated": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping pgn-missing record: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "updated": False}

    try:
        game_columns = _table_columns(conn, "games")
        required = {"pgn_missing", "pgn_missing_last_attempt_at", "pgn_missing_count", "pgn_missing_terminal"}
        if not game_columns or "game_url" not in game_columns or not required.issubset(game_columns):
            return {"available": False, "reason": "schema_missing", "updated": False}

        max_attempts = max(1, _env_int("PGN_MISSING_TERMINAL_THRESHOLD", 3))
        row = _fetchone(
            conn,
            "SELECT id, COALESCE(pgn_missing_count, 0) AS attempts FROM games WHERE game_url = %s LIMIT 1",
            (clean_url,),
        )
        if not row:
            conn.commit()
            return {"available": False, "reason": "row_not_found", "updated": False}

        game_id = int(row.get("id", 0) or 0)
        attempts = int(row.get("attempts", 0) or 0) + 1
        permanently_missing = attempts >= max_attempts
        _execute(
            conn,
            """
            UPDATE games
            SET pgn_missing_attempts = %s,
                pgn_missing_count = %s,
                pgn_missing_last_attempt_at = NOW(),
                pgn_missing = %s,
                pgn_missing_terminal = %s
            WHERE id = %s
            """,
            (attempts, attempts, permanently_missing, permanently_missing, game_id),
        )
        conn.commit()
        return {
            "available": True,
            "reason": "updated",
            "updated": True,
            "attempts": attempts,
            "pgn_missing": permanently_missing,
            "pgn_missing_terminal": permanently_missing,
            "max_attempts": max_attempts,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping pgn-missing record due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_error", "updated": False}
    finally:
        cleanup()


def reset_game_processing_state(*, game_url: str) -> dict[str, Any]:
    """Reset processing/notification/failure flags for one game URL."""
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not _is_postgres_url(database_url):
        return {"available": False, "reason": "no_database_url", "updated": False}
    clean_url = str(game_url or "").strip()
    if not clean_url:
        return {"available": False, "reason": "empty_url", "updated": False}

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Skipping game reset: DB unavailable.", exc_info=True)
        return {"available": False, "reason": "db_unreachable", "updated": False}

    try:
        game_columns = _table_columns(conn, "games")
        if not game_columns:
            return {"available": False, "reason": "schema_missing", "updated": False}
        url_col = "game_url" if "game_url" in game_columns else ("url" if "url" in game_columns else None)
        if not url_col:
            return {"available": False, "reason": "schema_missing", "updated": False}

        row = _fetchone(conn, f"SELECT id FROM games WHERE {url_col} = %s LIMIT 1", (clean_url,))
        if not row:
            conn.commit()
            return {"available": True, "reason": "row_not_found", "updated": False}

        game_id = int(row.get("id", 0) or 0)
        if game_id <= 0:
            return {"available": False, "reason": "invalid_row", "updated": False}

        field_values: dict[str, Any] = {
            "engine_failed": False,
            "failure_notified": False,
            "attempt_count": 0,
            "last_attempt_at": None,
            "success_notified": False,
            "review_notified": False,
            "completed_at": None,
            "last_error": None,
            "analysis_complete": False,
            "md_path": None,
            "tg_send_failed": False,
            "tg_last_error": None,
            "tg_send_attempts": 0,
            "tg_last_send_at": None,
            "pgn_missing": False,
            "pgn_missing_attempts": 0,
            "pgn_missing_count": 0,
            "pgn_missing_last_attempt_at": None,
            "pgn_missing_terminal": False,
        }
        set_parts: list[str] = []
        params: list[Any] = []
        changed_fields: list[str] = []
        for field, value in field_values.items():
            if field not in game_columns:
                continue
            set_parts.append(f"{field} = %s")
            params.append(value)
            changed_fields.append(field)

        if not set_parts:
            conn.commit()
            return {"available": False, "reason": "schema_missing", "updated": False}

        params.append(game_id)
        _execute(conn, "UPDATE games SET " + ", ".join(set_parts) + " WHERE id = %s", tuple(params))
        conn.commit()
        return {
            "available": True,
            "reason": "reset",
            "updated": True,
            "changed_fields": changed_fields,
            "game_url": clean_url,
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Skipping game reset due to unexpected error.", exc_info=True)
        return {"available": False, "reason": "runtime_error", "updated": False}
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


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


def _coerce_datetime_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None
