from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from src.db.player_metrics import extract_player_rating_from_pgn, insert_player_rating_row

logger = logging.getLogger(__name__)

DEFAULT_BOOTSTRAP_GAMES = 25
DEFAULT_LOOKBACK_DAYS = 365
USERNAME_COLUMNS = ("platform_user", "username", "handle", "chess_username", "name")

FetchRecentGamesFn = Callable[[str, int], list[dict[str, Any]]]
ParseGameFn = Callable[[dict[str, Any], str], Any]


def ensure_bootstrap(
    *,
    username: str,
    bootstrap_games: int | None = None,
    fetch_recent_games_fn: FetchRecentGamesFn | None = None,
    parse_game_fn: ParseGameFn | None = None,
) -> dict[str, Any]:
    normalized_username = (username or "").strip()
    try:
        requested_games = _resolve_bootstrap_games(bootstrap_games)
    except Exception:
        logger.warning("Bootstrap skipped: invalid CHESS_BOOTSTRAP_GAMES/--bootstrap-games value.", exc_info=True)
        return _result(False, "invalid_bootstrap_games", requested_games=DEFAULT_BOOTSTRAP_GAMES)

    if not normalized_username:
        return _result(False, "missing_username", requested_games=requested_games)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        logger.debug("Bootstrap skipped: DATABASE_URL not set.")
        return _result(False, "no_database_url", requested_games=requested_games)

    fetch_fn, parse_fn = _resolve_parser_functions(fetch_recent_games_fn, parse_game_fn)
    if fetch_fn is None or parse_fn is None:
        logger.debug("Bootstrap skipped: parser callables unavailable.")
        return _result(False, "parser_unavailable", requested_games=requested_games)

    try:
        conn, cleanup = _connect_db(database_url)
    except Exception:
        logger.debug("Bootstrap skipped: Postgres unreachable.", exc_info=True)
        return _result(False, "db_unreachable", requested_games=requested_games)

    try:
        return _ensure_bootstrap_with_conn(
            conn,
            username=normalized_username,
            requested_games=requested_games,
            fetch_recent_games_fn=fetch_fn,
            parse_game_fn=parse_fn,
        )
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("Bootstrap failed unexpectedly.", exc_info=True)
        return _result(False, "bootstrap_failed", requested_games=requested_games)
    finally:
        cleanup()


def _ensure_bootstrap_with_conn(
    conn: Any,
    *,
    username: str,
    requested_games: int,
    fetch_recent_games_fn: FetchRecentGamesFn,
    parse_game_fn: ParseGameFn,
) -> dict[str, Any]:
    player_columns = _safe_table_columns(conn, "players")
    game_columns = _safe_table_columns(conn, "games")
    if not player_columns or not game_columns or "player_id" not in game_columns:
        logger.warning("Bootstrap skipped: required schema missing (players/games tables).")
        return _result(False, "schema_missing", requested_games=requested_games)

    player_id = _resolve_or_create_player_id(conn, username=username, player_columns=player_columns)
    if player_id is None:
        logger.warning("Bootstrap skipped: could not resolve player row in players table.")
        return _result(False, "schema_missing", requested_games=requested_games)

    existing_games = _count_games_for_player(conn, player_id=player_id)
    if existing_games is None:
        logger.warning("Bootstrap skipped: unable to count games for player_id=%s.", player_id)
        return _result(False, "schema_missing", requested_games=requested_games)
    if existing_games > 0:
        logger.debug("Bootstrap skipped: already seeded for player_id=%s.", player_id)
        return _result(False, "already_seeded", requested_games=requested_games)

    parsed_games = _collect_games_from_existing_parser(
        username=username,
        requested_games=requested_games,
        fetch_recent_games_fn=fetch_recent_games_fn,
        parse_game_fn=parse_game_fn,
    )
    if not parsed_games:
        logger.debug("Bootstrap skipped: no recent games available.")
        return _result(False, "no_recent_games", requested_games=requested_games)

    inserted_games = _insert_games_for_player(
        conn,
        player_id=player_id,
        games=parsed_games,
        game_columns=game_columns,
    )
    if inserted_games <= 0:
        logger.debug("Bootstrap skipped: no new games inserted.")
        return _result(False, "no_new_games", requested_games=requested_games)

    try:
        _seed_player_ratings_for_bootstrap(conn, username=username, games=parsed_games)
    except Exception:
        logger.debug("Bootstrap rating seed skipped due to optional metrics failure.", exc_info=True)

    try:
        _seed_rating_history_if_available(conn, player_id=player_id, games=parsed_games)
    except Exception:
        logger.debug("Bootstrap rating_history seed skipped due to optional metrics failure.", exc_info=True)

    try:
        _seed_traits_if_available(conn, player_id=player_id, games=parsed_games, game_columns=game_columns)
    except Exception:
        logger.warning("Bootstrap trait seed skipped due to trait pipeline failure.", exc_info=True)

    try:
        conn.commit()
    except Exception:
        conn.rollback()
        logger.warning("Bootstrap failed during commit.", exc_info=True)
        return _result(False, "bootstrap_failed", requested_games=requested_games)

    return _result(
        True,
        "bootstrapped",
        inserted_games=inserted_games,
        requested_games=requested_games,
    )


def _resolve_parser_functions(
    fetch_recent_games_fn: FetchRecentGamesFn | None,
    parse_game_fn: ParseGameFn | None,
) -> tuple[FetchRecentGamesFn | None, ParseGameFn | None]:
    if fetch_recent_games_fn is not None and parse_game_fn is not None:
        return fetch_recent_games_fn, parse_game_fn

    try:
        from chess_review import fetch_recent_games, parse_game
    except Exception:
        logger.debug("Bootstrap parser import failed.", exc_info=True)
        return None, None
    return fetch_recent_games, parse_game


def _collect_games_from_existing_parser(
    *,
    username: str,
    requested_games: int,
    fetch_recent_games_fn: FetchRecentGamesFn,
    parse_game_fn: ParseGameFn,
) -> list[dict[str, Any]]:
    lookback_days = _resolve_lookback_days(requested_games)
    try:
        raw_games = fetch_recent_games_fn(username, lookback_days)
    except Exception:
        logger.debug("Bootstrap fetch_recent_games failed.", exc_info=True)
        return []

    collected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for raw in raw_games:
        try:
            parsed = parse_game_fn(raw, username)
        except Exception:
            logger.debug("Bootstrap parse_game failed for a game.", exc_info=True)
            continue
        if parsed is None:
            continue

        payload = _game_payload(parsed)
        if payload is None:
            continue
        game_url = payload["game_url"]
        if game_url in seen_urls:
            continue
        seen_urls.add(game_url)
        collected.append(payload)

    collected.sort(key=lambda row: int(row["end_time"]), reverse=True)
    return collected[:requested_games]


def _resolve_bootstrap_games(bootstrap_games: int | None) -> int:
    if bootstrap_games is not None:
        explicit = int(bootstrap_games)
        if explicit <= 0:
            raise ValueError("bootstrap_games must be > 0")
        return explicit

    raw = os.environ.get("CHESS_BOOTSTRAP_GAMES", "").strip()
    if not raw:
        return DEFAULT_BOOTSTRAP_GAMES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BOOTSTRAP_GAMES
    return max(1, value)


def _resolve_lookback_days(requested_games: int) -> int:
    return max(DEFAULT_LOOKBACK_DAYS, requested_games * 7)


def _safe_table_columns(conn: Any, table_name: str) -> set[str]:
    try:
        return _table_columns(conn, table_name)
    except Exception:
        logger.debug("Bootstrap table introspection failed for %s.", table_name, exc_info=True)
        return set()


def _resolve_or_create_player_id(
    conn: Any,
    *,
    username: str,
    player_columns: set[str],
) -> int | None:
    player_row = _find_player_row(conn, username=username, player_columns=player_columns)
    if player_row is not None:
        return int(player_row["id"])

    username_column = next((col for col in USERNAME_COLUMNS if col in player_columns), None)
    if username_column is None:
        return None

    data: dict[str, Any] = {username_column: username}
    now_utc = datetime.now(timezone.utc)
    if "created_at" in player_columns:
        data["created_at"] = now_utc
    if "updated_at" in player_columns:
        data["updated_at"] = now_utc

    col_names = list(data.keys())
    placeholders = ", ".join(["%s"] * len(col_names))
    sql = (
        "INSERT INTO players ("
        + ", ".join(col_names)
        + ") VALUES ("
        + placeholders
        + ")"
    )
    _execute(conn, sql, tuple(data[name] for name in col_names))

    player_row = _find_player_row(conn, username=username, player_columns=player_columns)
    if player_row is None:
        return None
    return int(player_row["id"])


def _find_player_row(
    conn: Any,
    *,
    username: str,
    player_columns: set[str],
) -> Mapping[str, Any] | None:
    for col in USERNAME_COLUMNS:
        if col not in player_columns:
            continue
        row = _fetchone(conn, f"SELECT id FROM players WHERE {col} = %s LIMIT 1", (username,))
        if row is not None:
            return row
    return None


def _count_games_for_player(conn: Any, *, player_id: int) -> int | None:
    row = _fetchone(conn, "SELECT COUNT(*) AS game_count FROM games WHERE player_id = %s", (player_id,))
    if row is None:
        return None
    return int(row.get("game_count", 0) or 0)


def _game_payload(parsed: Any) -> dict[str, Any] | None:
    game_url = _value(parsed, "game_url", "")
    pgn = _value(parsed, "pgn", "")
    end_time = _to_int_or_none(_value(parsed, "end_time", None))
    if not game_url or not isinstance(game_url, str):
        return None
    if not pgn or not isinstance(pgn, str):
        return None
    if end_time is None:
        return None

    player_color = str(_value(parsed, "your_color", _value(parsed, "player_color", ""))).strip().lower()
    if player_color not in {"white", "black"}:
        return None

    return {
        "game_url": game_url.strip(),
        "pgn": pgn,
        "end_time": end_time,
        "played_at": datetime.fromtimestamp(end_time, tz=timezone.utc),
        "time_control": str(_value(parsed, "time_control", "") or ""),
        "rated": bool(_value(parsed, "rated", False)),
        "rules": str(_value(parsed, "rules", "chess") or "chess"),
        "white_username": str(_value(parsed, "white_username", "") or ""),
        "black_username": str(_value(parsed, "black_username", "") or ""),
        "white_rating": _to_int_or_none(_value(parsed, "white_rating", None)),
        "black_rating": _to_int_or_none(_value(parsed, "black_rating", None)),
        "result": str(_value(parsed, "result", "*") or "*"),
        "player_color": player_color,
    }


def _value(game: Any, key: str, default: Any) -> Any:
    if isinstance(game, Mapping):
        return game.get(key, default)
    return getattr(game, key, default)


def _to_int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _insert_games_for_player(
    conn: Any,
    *,
    player_id: int,
    games: list[Mapping[str, Any]],
    game_columns: set[str],
) -> int:
    url_column = "game_url" if "game_url" in game_columns else ("url" if "url" in game_columns else None)
    pgn_column = next((col for col in ("pgn", "raw_pgn", "game_pgn") if col in game_columns), None)

    inserted_games = 0
    for game in games:
        game_url = str(game["game_url"])
        if url_column is not None:
            existing = _fetchone(conn, f"SELECT id FROM games WHERE {url_column} = %s LIMIT 1", (game_url,))
            if existing is not None:
                continue

        data: dict[str, Any] = {"player_id": player_id}
        if url_column is not None:
            data[url_column] = game_url
        if pgn_column is not None:
            data[pgn_column] = game.get("pgn")
        if "end_time" in game_columns:
            data["end_time"] = int(game["end_time"])
        if "played_at" in game_columns:
            data["played_at"] = game.get("played_at")
        if "time_control" in game_columns:
            data["time_control"] = game.get("time_control")
        if "rated" in game_columns:
            data["rated"] = bool(game.get("rated", False))
        if "rules" in game_columns:
            data["rules"] = game.get("rules")
        if "result" in game_columns:
            data["result"] = game.get("result")
        if "white_username" in game_columns:
            data["white_username"] = game.get("white_username")
        if "black_username" in game_columns:
            data["black_username"] = game.get("black_username")
        if "white_rating" in game_columns:
            data["white_rating"] = game.get("white_rating")
        if "black_rating" in game_columns:
            data["black_rating"] = game.get("black_rating")
        if "player_color" in game_columns:
            data["player_color"] = game.get("player_color")

        col_names = list(data.keys())
        placeholders = ", ".join(["%s"] * len(col_names))
        sql = (
            "INSERT INTO games ("
            + ", ".join(col_names)
            + ") VALUES ("
            + placeholders
            + ")"
        )
        _execute(conn, sql, tuple(data[name] for name in col_names))
        inserted_games += 1

    return inserted_games


def _seed_player_ratings_for_bootstrap(
    conn: Any,
    *,
    username: str,
    games: list[Mapping[str, Any]],
) -> int:
    inserted = 0
    for game in games:
        rating = extract_player_rating_from_pgn(
            pgn=str(game.get("pgn") or ""),
            player_color=str(game.get("player_color") or "white"),
        )
        if insert_player_rating_row(
            conn,
            player_username=username,
            game_url=str(game.get("game_url") or ""),
            end_time=game.get("played_at") or int(game.get("end_time") or 0),
            rating=rating,
            time_control=str(game.get("time_control") or ""),
            rated=bool(game.get("rated", False)),
        ):
            inserted += 1
    return inserted


def _seed_rating_history_if_available(
    conn: Any,
    *,
    player_id: int,
    games: list[Mapping[str, Any]],
) -> int:
    history_columns = _safe_table_columns(conn, "player_rating_history")
    if not history_columns:
        return 0
    if "player_id" not in history_columns or "rating" not in history_columns:
        logger.warning(
            "Bootstrap skipped player_rating_history seed: table exists but required columns are missing."
        )
        return 0

    inserted = 0
    game_columns = _safe_table_columns(conn, "games")
    url_column = "game_url" if "game_url" in game_columns else ("url" if "url" in game_columns else None)
    for game in games:
        player_color = str(game.get("player_color", "white"))
        rating = game.get("white_rating") if player_color == "white" else game.get("black_rating")
        if rating is None:
            continue

        data: dict[str, Any] = {"player_id": player_id, "rating": int(rating)}
        if "color" in history_columns:
            data["color"] = player_color
        if "recorded_at" in history_columns:
            data["recorded_at"] = game.get("played_at")
        if "source" in history_columns:
            data["source"] = "chess.com"

        if "game_id" in history_columns and url_column is not None:
            row = _fetchone(
                conn,
                f"SELECT id FROM games WHERE {url_column} = %s LIMIT 1",
                (str(game.get("game_url") or ""),),
            )
            if row is not None:
                game_id = int(row["id"])
                exists = _fetchone(
                    conn,
                    "SELECT 1 FROM player_rating_history WHERE player_id = %s AND game_id = %s LIMIT 1",
                    (player_id, game_id),
                )
                if exists is not None:
                    continue
                data["game_id"] = game_id

        col_names = list(data.keys())
        placeholders = ", ".join(["%s"] * len(col_names))
        sql = (
            "INSERT INTO player_rating_history ("
            + ", ".join(col_names)
            + ") VALUES ("
            + placeholders
            + ")"
        )
        _execute(conn, sql, tuple(data[name] for name in col_names))
        inserted += 1

    return inserted


def _seed_traits_if_available(
    conn: Any,
    *,
    player_id: int,
    games: list[Mapping[str, Any]],
    game_columns: set[str],
) -> int:
    traits_columns = _safe_table_columns(conn, "traits")
    player_traits_columns = _safe_table_columns(conn, "player_traits")
    trait_events_columns = _safe_table_columns(conn, "trait_events")
    if not traits_columns or not player_traits_columns or not trait_events_columns:
        logger.debug("Bootstrap trait seeding skipped: required trait tables are missing.")
        return 0

    try:
        from src.db.trait_updates import apply_trait_updates_for_game
    except Exception:
        logger.debug("Bootstrap trait seeding skipped: trait update module unavailable.", exc_info=True)
        return 0

    url_column = "game_url" if "game_url" in game_columns else ("url" if "url" in game_columns else None)
    if url_column is None:
        logger.debug("Bootstrap trait seeding skipped: games table has no game URL column.")
        return 0

    updated_count = 0
    for game in games:
        game_url = str(game.get("game_url") or "")
        if not game_url:
            continue

        row = _fetchone(
            conn,
            f"SELECT id FROM games WHERE player_id = %s AND {url_column} = %s LIMIT 1",
            (player_id, game_url),
        )
        if row is None:
            continue

        game_id = int(row["id"])
        try:
            apply_trait_updates_for_game(player_id, game_id, db_session_or_conn=conn)
            updated_count += 1
        except Exception:
            logger.warning(
                "Bootstrap trait update failed for player_id=%s game_id=%s.",
                player_id,
                game_id,
                exc_info=True,
            )

    return updated_count


def _result(
    ran: bool,
    reason: str,
    *,
    inserted_games: int = 0,
    requested_games: int,
) -> dict[str, Any]:
    return {
        "ran": bool(ran),
        "reason": str(reason),
        "inserted_games": int(inserted_games),
        "requested_games": int(requested_games),
    }


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
