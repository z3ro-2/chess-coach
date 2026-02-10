from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.config.output_paths import get_trait_books_root
from src.traits.trait_book import generate_trait_book_markdown


SNAPSHOT_CADENCE = 20


def maybe_create_trait_snapshot(player_id: int, *, db_session_or_conn) -> dict[str, Any]:
    if _looks_like_dbapi_connection(db_session_or_conn):
        return _maybe_create_with_dbapi_connection(player_id, db_session_or_conn)
    return _maybe_create_with_sqlalchemy_session(player_id, db_session_or_conn)


def _looks_like_dbapi_connection(candidate: Any) -> bool:
    return all(hasattr(candidate, attr) for attr in ("cursor", "commit", "rollback"))


def _maybe_create_with_dbapi_connection(player_id: int, conn: Any) -> dict[str, Any]:
    markdown_path: Path | None = None
    markdown_content: str | None = None
    inserted_snapshot_row = False
    cutoff_game_count = 0

    try:
        total_games = _dbapi_fetch_total_games(conn, player_id)
        cutoff_game_count = total_games
        if total_games == 0 or total_games % SNAPSHOT_CADENCE != 0:
            conn.rollback()
            return {"created": False, "reason": "not_on_cutoff", "total_games": total_games}

        player_row = _dbapi_fetch_player_row(conn, player_id)
        if not player_row:
            conn.rollback()
            return {"created": False, "reason": "player_not_found", "total_games": total_games}

        platform_user = _platform_user(player_row)
        markdown_path = _build_snapshot_path(platform_user, total_games)

        snapshot_table_exists = _dbapi_table_exists(conn, "player_snapshots")
        snapshot_exists = (
            _dbapi_snapshot_exists(conn, player_id, total_games) if snapshot_table_exists else False
        )
        if snapshot_exists or markdown_path.exists():
            conn.rollback()
            return {
                "created": False,
                "reason": "already_exists",
                "total_games": total_games,
                "cutoff_game_count": total_games,
            }

        traits_rows = _dbapi_fetch_traits(conn)
        player_traits_rows = _dbapi_fetch_player_traits(conn, player_id)
        window_info = _dbapi_fetch_window_info(conn, player_id, SNAPSHOT_CADENCE)

        snapshot_utc = _snapshot_utc_now()
        markdown_content = generate_trait_book_markdown(
            player_row,
            traits_rows,
            player_traits_rows,
            games_analyzed=total_games,
            cutoff_label=f"{total_games} games",
            snapshot_utc=snapshot_utc,
            window_info=window_info,
        )

        if snapshot_table_exists:
            inserted_snapshot_row = _dbapi_insert_snapshot_record(
                conn,
                player_id=player_id,
                cutoff_game_count=total_games,
                snapshot_utc=snapshot_utc,
                markdown_content=markdown_content,
                markdown_path=str(markdown_path),
                window_info=window_info,
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if markdown_path is None or markdown_content is None:
        raise RuntimeError("Snapshot generation failed before file output.")

    _write_snapshot_file(markdown_path, markdown_content)
    return {
        "created": True,
        "cutoff_game_count": cutoff_game_count,
        "path": str(markdown_path),
        "snapshot_row_inserted": inserted_snapshot_row,
    }


def _maybe_create_with_sqlalchemy_session(player_id: int, session: Any) -> dict[str, Any]:
    from sqlalchemy import text

    markdown_path: Path | None = None
    markdown_content: str | None = None
    inserted_snapshot_row = False
    cutoff_game_count = 0

    try:
        with session.begin():
            total_games = int(
                session.execute(
                    text("SELECT COUNT(*) AS total_games FROM games WHERE player_id = :player_id"),
                    {"player_id": player_id},
                ).scalar_one()
            )
            cutoff_game_count = total_games
            if total_games == 0 or total_games % SNAPSHOT_CADENCE != 0:
                return {"created": False, "reason": "not_on_cutoff", "total_games": total_games}

            player_row = session.execute(
                text("SELECT * FROM players WHERE id = :player_id"),
                {"player_id": player_id},
            ).mappings().first()
            if player_row is None:
                return {"created": False, "reason": "player_not_found", "total_games": total_games}

            platform_user = _platform_user(player_row)
            markdown_path = _build_snapshot_path(platform_user, total_games)

            snapshot_table_exists = _sqlalchemy_table_exists(session, "player_snapshots")
            snapshot_exists = (
                _sqlalchemy_snapshot_exists(session, player_id, total_games)
                if snapshot_table_exists
                else False
            )
            if snapshot_exists or markdown_path.exists():
                return {
                    "created": False,
                    "reason": "already_exists",
                    "total_games": total_games,
                    "cutoff_game_count": total_games,
                }

            traits_rows = list(
                session.execute(
                    text(
                        """
                        SELECT id, key, name, category, description, COALESCE(severity_weight, 1.0) AS severity_weight
                        FROM traits
                        ORDER BY id ASC
                        """
                    )
                ).mappings()
            )
            player_traits_rows = list(
                session.execute(
                    text(
                        """
                        SELECT trait_id, confidence, trend_ema, last_seen_game_id
                        FROM player_traits
                        WHERE player_id = :player_id
                        """
                    ),
                    {"player_id": player_id},
                ).mappings()
            )
            window_info = _sqlalchemy_fetch_window_info(session, player_id, SNAPSHOT_CADENCE)

            snapshot_utc = _snapshot_utc_now()
            markdown_content = generate_trait_book_markdown(
                player_row,
                traits_rows,
                player_traits_rows,
                games_analyzed=total_games,
                cutoff_label=f"{total_games} games",
                snapshot_utc=snapshot_utc,
                window_info=window_info,
            )

            if snapshot_table_exists:
                inserted_snapshot_row = _sqlalchemy_insert_snapshot_record(
                    session,
                    player_id=player_id,
                    cutoff_game_count=total_games,
                    snapshot_utc=snapshot_utc,
                    markdown_content=markdown_content,
                    markdown_path=str(markdown_path),
                    window_info=window_info,
                )
    except Exception:
        session.rollback()
        raise

    if markdown_path is None or markdown_content is None:
        raise RuntimeError("Snapshot generation failed before file output.")

    _write_snapshot_file(markdown_path, markdown_content)
    return {
        "created": True,
        "cutoff_game_count": cutoff_game_count,
        "path": str(markdown_path),
        "snapshot_row_inserted": inserted_snapshot_row,
    }


def _dbapi_fetch_total_games(conn: Any, player_id: int) -> int:
    row = _dbapi_fetchone(conn, "SELECT COUNT(*) AS total_games FROM games WHERE player_id = %s", (player_id,))
    return int(row.get("total_games", 0) or 0)


def _dbapi_fetch_player_row(conn: Any, player_id: int) -> Mapping[str, Any] | None:
    return _dbapi_fetchone(conn, "SELECT * FROM players WHERE id = %s", (player_id,))


def _dbapi_fetch_traits(conn: Any) -> list[Mapping[str, Any]]:
    return _dbapi_fetchall(
        conn,
        """
        SELECT id, key, name, category, description, COALESCE(severity_weight, 1.0) AS severity_weight
        FROM traits
        ORDER BY id ASC
        """,
    )


def _dbapi_fetch_player_traits(conn: Any, player_id: int) -> list[Mapping[str, Any]]:
    return _dbapi_fetchall(
        conn,
        """
        SELECT trait_id, confidence, trend_ema, last_seen_game_id
        FROM player_traits
        WHERE player_id = %s
        """,
        (player_id,),
    )


def _dbapi_fetch_window_info(conn: Any, player_id: int, window_size: int) -> dict[str, Any]:
    rows: list[Mapping[str, Any]]
    try:
        rows = _dbapi_fetchall(
            conn,
            """
            SELECT id, played_at
            FROM games
            WHERE player_id = %s
            ORDER BY played_at DESC, id DESC
            LIMIT %s
            """,
            (player_id, window_size),
        )
    except Exception:
        rows = _dbapi_fetchall(
            conn,
            """
            SELECT id
            FROM games
            WHERE player_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (player_id, window_size),
        )

    if not rows:
        return {
            "window_size": window_size,
            "start_game_id": None,
            "end_game_id": None,
            "start_played_at": None,
            "end_played_at": None,
        }

    newest = rows[0]
    oldest = rows[-1]
    return {
        "window_size": window_size,
        "start_game_id": oldest.get("id"),
        "end_game_id": newest.get("id"),
        "start_played_at": oldest.get("played_at"),
        "end_played_at": newest.get("played_at"),
    }


def _dbapi_snapshot_exists(conn: Any, player_id: int, cutoff_game_count: int) -> bool:
    columns = _dbapi_table_columns(conn, "player_snapshots")
    if "player_id" not in columns or "cutoff_game_count" not in columns:
        return False

    row = _dbapi_fetchone(
        conn,
        """
        SELECT COUNT(*) AS snapshot_count
        FROM player_snapshots
        WHERE player_id = %s AND cutoff_game_count = %s
        """,
        (player_id, cutoff_game_count),
    )
    return int(row.get("snapshot_count", 0) or 0) > 0


def _dbapi_insert_snapshot_record(
    conn: Any,
    *,
    player_id: int,
    cutoff_game_count: int,
    snapshot_utc: str,
    markdown_content: str,
    markdown_path: str,
    window_info: dict[str, Any],
) -> bool:
    columns = _dbapi_table_columns(conn, "player_snapshots")
    if "player_id" not in columns or "cutoff_game_count" not in columns:
        return False

    data: dict[str, Any] = {
        "player_id": player_id,
        "cutoff_game_count": cutoff_game_count,
    }
    if "snapshot_utc" in columns:
        data["snapshot_utc"] = snapshot_utc
    if "file_path" in columns:
        data["file_path"] = markdown_path
    if "window_start_game_id" in columns:
        data["window_start_game_id"] = window_info.get("start_game_id")
    if "window_end_game_id" in columns:
        data["window_end_game_id"] = window_info.get("end_game_id")
    if "window_start_played_at" in columns:
        data["window_start_played_at"] = window_info.get("start_played_at")
    if "window_end_played_at" in columns:
        data["window_end_played_at"] = window_info.get("end_played_at")

    for markdown_column in ("markdown", "content_markdown", "trait_book_markdown"):
        if markdown_column in columns:
            data[markdown_column] = markdown_content
            break

    col_names = list(data.keys())
    placeholders = ", ".join(["%s"] * len(col_names))
    sql = (
        "INSERT INTO player_snapshots ("
        + ", ".join(col_names)
        + ") VALUES ("
        + placeholders
        + ")"
    )
    _dbapi_execute(conn, sql, tuple(data[name] for name in col_names))
    return True


def _sqlalchemy_table_exists(session: Any, table_name: str) -> bool:
    from sqlalchemy import text

    try:
        row = session.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_name = :table_name
                ) AS table_exists
                """
            ),
            {"table_name": table_name},
        ).mappings().first()
        if row is not None:
            return bool(row.get("table_exists", False))
    except Exception:
        pass

    try:
        row = session.execute(
            text(
                """
                SELECT 1 AS table_exists
                FROM sqlite_master
                WHERE type = 'table' AND name = :table_name
                LIMIT 1
                """
            ),
            {"table_name": table_name},
        ).mappings().first()
        return row is not None
    except Exception:
        return False


def _sqlalchemy_table_columns(session: Any, table_name: str) -> set[str]:
    from sqlalchemy import text

    try:
        rows = list(
            session.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = :table_name
                    """
                ),
                {"table_name": table_name},
            ).mappings()
        )
        if rows:
            return {str(row["column_name"]) for row in rows}
    except Exception:
        pass

    try:
        rows = list(session.execute(text(f"PRAGMA table_info({table_name})")).mappings())
        return {str(row["name"]) for row in rows}
    except Exception:
        return set()


def _sqlalchemy_snapshot_exists(session: Any, player_id: int, cutoff_game_count: int) -> bool:
    from sqlalchemy import text

    columns = _sqlalchemy_table_columns(session, "player_snapshots")
    if "player_id" not in columns or "cutoff_game_count" not in columns:
        return False

    row = session.execute(
        text(
            """
            SELECT COUNT(*) AS snapshot_count
            FROM player_snapshots
            WHERE player_id = :player_id AND cutoff_game_count = :cutoff_game_count
            """
        ),
        {"player_id": player_id, "cutoff_game_count": cutoff_game_count},
    ).mappings().first()
    return int((row or {}).get("snapshot_count", 0) or 0) > 0


def _sqlalchemy_fetch_window_info(session: Any, player_id: int, window_size: int) -> dict[str, Any]:
    from sqlalchemy import text

    try:
        rows = list(
            session.execute(
                text(
                    """
                    SELECT id, played_at
                    FROM games
                    WHERE player_id = :player_id
                    ORDER BY played_at DESC, id DESC
                    LIMIT :window_size
                    """
                ),
                {"player_id": player_id, "window_size": window_size},
            ).mappings()
        )
    except Exception:
        rows = list(
            session.execute(
                text(
                    """
                    SELECT id
                    FROM games
                    WHERE player_id = :player_id
                    ORDER BY id DESC
                    LIMIT :window_size
                    """
                ),
                {"player_id": player_id, "window_size": window_size},
            ).mappings()
        )

    if not rows:
        return {
            "window_size": window_size,
            "start_game_id": None,
            "end_game_id": None,
            "start_played_at": None,
            "end_played_at": None,
        }

    newest = rows[0]
    oldest = rows[-1]
    return {
        "window_size": window_size,
        "start_game_id": oldest.get("id"),
        "end_game_id": newest.get("id"),
        "start_played_at": oldest.get("played_at"),
        "end_played_at": newest.get("played_at"),
    }


def _sqlalchemy_insert_snapshot_record(
    session: Any,
    *,
    player_id: int,
    cutoff_game_count: int,
    snapshot_utc: str,
    markdown_content: str,
    markdown_path: str,
    window_info: dict[str, Any],
) -> bool:
    from sqlalchemy import text

    columns = _sqlalchemy_table_columns(session, "player_snapshots")
    if "player_id" not in columns or "cutoff_game_count" not in columns:
        return False

    data: dict[str, Any] = {
        "player_id": player_id,
        "cutoff_game_count": cutoff_game_count,
    }
    if "snapshot_utc" in columns:
        data["snapshot_utc"] = snapshot_utc
    if "file_path" in columns:
        data["file_path"] = markdown_path
    if "window_start_game_id" in columns:
        data["window_start_game_id"] = window_info.get("start_game_id")
    if "window_end_game_id" in columns:
        data["window_end_game_id"] = window_info.get("end_game_id")
    if "window_start_played_at" in columns:
        data["window_start_played_at"] = window_info.get("start_played_at")
    if "window_end_played_at" in columns:
        data["window_end_played_at"] = window_info.get("end_played_at")

    for markdown_column in ("markdown", "content_markdown", "trait_book_markdown"):
        if markdown_column in columns:
            data[markdown_column] = markdown_content
            break

    col_names = list(data.keys())
    placeholders = ", ".join(f":{name}" for name in col_names)
    sql = (
        "INSERT INTO player_snapshots ("
        + ", ".join(col_names)
        + ") VALUES ("
        + placeholders
        + ")"
    )
    session.execute(text(sql), data)
    return True


def _dbapi_table_exists(conn: Any, table_name: str) -> bool:
    row = _dbapi_fetchone(
        conn,
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_name = %s
        ) AS table_exists
        """,
        (table_name,),
        swallow_errors=True,
    )
    if row is not None:
        return bool(row.get("table_exists", False))

    row = _dbapi_fetchone(
        conn,
        """
        SELECT 1 AS table_exists
        FROM sqlite_master
        WHERE type = 'table' AND name = %s
        LIMIT 1
        """,
        (table_name,),
        swallow_errors=True,
    )
    return row is not None


def _dbapi_table_columns(conn: Any, table_name: str) -> set[str]:
    rows = _dbapi_fetchall(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
        """,
        (table_name,),
        swallow_errors=True,
    )
    if rows:
        return {str(row["column_name"]) for row in rows}

    pragma_rows = _dbapi_fetchall(
        conn,
        f"PRAGMA table_info({table_name})",
        swallow_errors=True,
    )
    return {str(row["name"]) for row in pragma_rows}


def _dbapi_execute(conn: Any, query: str, params: tuple[Any, ...] = ()) -> None:
    cur = conn.cursor()
    try:
        cur.execute(query, params)
    finally:
        cur.close()


def _dbapi_fetchone(
    conn: Any,
    query: str,
    params: tuple[Any, ...] = (),
    *,
    swallow_errors: bool = False,
) -> Mapping[str, Any] | None:
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        row = cur.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in (cur.description or [])]
        if columns:
            return {columns[i]: row[i] for i in range(len(columns))}
        return {}
    except Exception:
        if swallow_errors:
            return None
        raise
    finally:
        cur.close()


def _dbapi_fetchall(
    conn: Any,
    query: str,
    params: tuple[Any, ...] = (),
    *,
    swallow_errors: bool = False,
) -> list[Mapping[str, Any]]:
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        rows = cur.fetchall()
        columns = [desc[0] for desc in (cur.description or [])]
        if not columns:
            return []
        return [{columns[i]: row[i] for i in range(len(columns))} for row in rows]
    except Exception:
        if swallow_errors:
            return []
        raise
    finally:
        cur.close()


def _platform_user(player_row: Mapping[str, Any]) -> str:
    for key in ("platform_user", "username", "handle", "chess_username", "name"):
        value = player_row.get(key)
        if value:
            return _safe_path_component(str(value))
    return f"player_{player_row.get('id', 'unknown')}"


def _safe_path_component(value: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in ("-", "_", ".")) else "_" for ch in value.strip())
    return cleaned or "unknown_player"


def _build_snapshot_path(platform_user: str, cutoff_game_count: int) -> Path:
    base_dir = get_trait_books_root()
    return base_dir / platform_user / f"trait_book_{cutoff_game_count:04d}.md"


def _write_snapshot_file(path: Path, markdown_content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown_content, encoding="utf-8")


def _snapshot_utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
