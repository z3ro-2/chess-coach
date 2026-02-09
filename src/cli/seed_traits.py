#!/usr/bin/env python3
"""One-time historical backfill CLI for seeding player trait state from past games."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
from typing import Any, Callable, Mapping

from src.db import apply_trait_updates_for_game, maybe_create_trait_snapshot
from src.traits.validate_trait_events import validate_trait_events

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed a player's trait profile from historical games.")
    parser.add_argument("--player", required=True, help="Player username/handle or numeric player_id.")
    parser.add_argument("--games", type=int, default=100, help="How many recent games to backfill (default: 100).")
    parser.add_argument("--dry-run", action="store_true", help="Do not write DB updates.")
    parser.add_argument(
        "--skip-snapshots",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip snapshot creation after backfill (default: true). Use --no-skip-snapshots to enable.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", "").strip(),
        help="Database URL (defaults to DATABASE_URL env var).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.database_url:
        raise SystemExit("DATABASE_URL is required (or pass --database-url).")
    if args.games <= 0:
        raise SystemExit("--games must be > 0.")

    conn, cleanup = _connect_db(args.database_url)
    try:
        result = seed_traits_for_player(
            conn=conn,
            player_ref=args.player,
            games_limit=args.games,
            dry_run=args.dry_run,
            skip_snapshots=args.skip_snapshots,
        )
        logger.info("Done: %s", json.dumps(result, sort_keys=True))
        return 0
    finally:
        cleanup()


def seed_traits_for_player(
    *,
    conn: Any,
    player_ref: str,
    games_limit: int,
    dry_run: bool,
    skip_snapshots: bool,
) -> dict[str, Any]:
    player_row = _resolve_player_row(conn, player_ref)
    if player_row is None:
        raise RuntimeError(f"Player not found: {player_ref}")
    player_id = int(player_row["id"])

    games = _fetch_games_oldest_to_newest(conn, player_id=player_id, games_limit=games_limit)
    if not games:
        return {"player_id": player_id, "processed_games": 0, "created_events": 0, "applied_updates": 0}

    review_generator = _load_optional_callable(
        [
            "src.traits.extractor:generate_review_markdown_for_game",
            "src.traits.pipeline:generate_review_markdown_for_game",
            "src.reviews.generator:generate_review_markdown_for_game",
        ]
    )
    trait_extractor = _load_optional_callable(
        [
            "src.traits.extractor:extract_trait_events_for_game",
            "src.traits.pipeline:extract_trait_events_for_game",
        ]
    )

    if not dry_run:
        _reset_player_trait_rows(conn, player_id)

    created_events = 0
    skipped_existing_event_games = 0
    applied_updates = 0
    processed_games = 0
    snapshot_result: dict[str, Any] | None = None

    for idx, game_row in enumerate(games, start=1):
        game_id = int(game_row["id"])

        if _trait_events_exist(conn, game_id=game_id, player_id=player_id):
            skipped_existing_event_games += 1
        else:
            if not dry_run:
                review_markdown = _get_or_generate_review_markdown(
                    conn=conn,
                    game_row=game_row,
                    player_row=player_row,
                    review_generator=review_generator,
                )
                pgn_text = _pick_first_nonempty(game_row, ("pgn", "raw_pgn", "game_pgn"))
                if not pgn_text:
                    raise RuntimeError(f"Game {game_id} is missing PGN; cannot extract trait events.")

                player_color = _infer_player_color(game_row, player_row)
                payload = _extract_trait_events(
                    extractor=trait_extractor,
                    pgn_text=pgn_text,
                    review_markdown=review_markdown,
                    player_color=player_color,
                    game_row=game_row,
                    player_row=player_row,
                )
                is_valid, errors, normalized_payload = validate_trait_events(payload)
                if not is_valid:
                    raise RuntimeError(
                        f"Trait events invalid for game {game_id}: " + "; ".join(errors[:3])
                    )

                created_events += _insert_trait_events(
                    conn=conn,
                    game_id=game_id,
                    player_id=player_id,
                    events=normalized_payload.get("events", []),
                )

        if not dry_run:
            apply_trait_updates_for_game(player_id, game_id, db_session_or_conn=conn)
            applied_updates += 1
            if not skip_snapshots:
                snapshot_result = maybe_create_trait_snapshot(player_id, db_session_or_conn=conn)

        processed_games += 1
        if idx % 10 == 0:
            logger.info(
                "Progress: %d/%d games processed (events_created=%d, skipped_existing=%d, updates_applied=%d)",
                idx,
                len(games),
                created_events,
                skipped_existing_event_games,
                applied_updates,
            )

    return {
        "player_id": player_id,
        "processed_games": processed_games,
        "created_events": created_events,
        "skipped_existing_event_games": skipped_existing_event_games,
        "applied_updates": applied_updates,
        "snapshot": snapshot_result,
        "dry_run": dry_run,
        "skip_snapshots": skip_snapshots,
    }


def _connect_db(database_url: str) -> tuple[Any, Callable[[], None]]:
    try:
        import psycopg2  # type: ignore

        conn = psycopg2.connect(database_url)
        conn.autocommit = False
        return conn, conn.close
    except Exception:
        pass

    try:
        from sqlalchemy import create_engine

        engine = create_engine(database_url)
        raw_conn = engine.raw_connection()

        def _cleanup() -> None:
            try:
                raw_conn.close()
            finally:
                engine.dispose()

        return raw_conn, _cleanup
    except Exception as exc:
        raise RuntimeError(
            "Unable to connect to DB. Install psycopg2 or SQLAlchemy and provide a valid DATABASE_URL."
        ) from exc


def _resolve_player_row(conn: Any, player_ref: str) -> Mapping[str, Any] | None:
    player_columns = _table_columns(conn, "players")
    if not player_columns:
        raise RuntimeError("Table 'players' does not exist or is inaccessible.")

    if re.fullmatch(r"\d+", str(player_ref).strip()):
        row = _fetchone(conn, "SELECT * FROM players WHERE id = %s", (int(player_ref),))
        if row is not None:
            return row

    for col in ("platform_user", "username", "handle", "chess_username", "name"):
        if col not in player_columns:
            continue
        row = _fetchone(conn, f"SELECT * FROM players WHERE {col} = %s LIMIT 1", (player_ref,))
        if row is not None:
            return row
    return None


def _fetch_games_oldest_to_newest(conn: Any, *, player_id: int, games_limit: int) -> list[Mapping[str, Any]]:
    game_columns = _table_columns(conn, "games")
    if not game_columns:
        raise RuntimeError("Table 'games' does not exist or is inaccessible.")

    order_expr_desc = "id DESC"
    order_expr_asc = "id ASC"
    if "played_at" in game_columns:
        order_expr_desc = "played_at DESC, id DESC"
        order_expr_asc = "played_at ASC, id ASC"
    elif "end_time" in game_columns:
        order_expr_desc = "end_time DESC, id DESC"
        order_expr_asc = "end_time ASC, id ASC"

    query = (
        "SELECT * FROM ("
        "  SELECT * FROM games WHERE player_id = %s ORDER BY "
        + order_expr_desc
        + " LIMIT %s"
        + ") AS recent_games ORDER BY "
        + order_expr_asc
    )
    return _fetchall(conn, query, (player_id, games_limit))


def _trait_events_exist(conn: Any, *, game_id: int, player_id: int) -> bool:
    cols = _table_columns(conn, "trait_events")
    if not cols:
        raise RuntimeError("Table 'trait_events' does not exist or is inaccessible.")

    if "player_id" in cols:
        row = _fetchone(
            conn,
            "SELECT 1 FROM trait_events WHERE game_id = %s AND player_id = %s LIMIT 1",
            (game_id, player_id),
        )
        return row is not None

    row = _fetchone(conn, "SELECT 1 FROM trait_events WHERE game_id = %s LIMIT 1", (game_id,))
    return row is not None


def _get_or_generate_review_markdown(
    *,
    conn: Any,
    game_row: Mapping[str, Any],
    player_row: Mapping[str, Any],
    review_generator: Callable[..., Any] | None,
) -> str:
    review = _pick_first_nonempty(game_row, ("review_markdown", "review_md", "markdown_review"))
    if review:
        return review

    if review_generator is None:
        raise RuntimeError(
            "No review generator integration found and game review is missing. "
            "Provide a review column in games or implement "
            "'src.traits.extractor:generate_review_markdown_for_game'."
        )

    generated = review_generator(game_row=game_row, player_row=player_row, db_conn=conn)
    if not isinstance(generated, str) or not generated.strip():
        raise RuntimeError(f"Review generator returned empty/non-string review for game {game_row.get('id')}.")

    _persist_review_if_supported(conn, game_row, generated)
    return generated


def _persist_review_if_supported(conn: Any, game_row: Mapping[str, Any], review_markdown: str) -> None:
    game_id = int(game_row["id"])
    cols = _table_columns(conn, "games")
    if "review_markdown" in cols:
        _execute(conn, "UPDATE games SET review_markdown = %s WHERE id = %s", (review_markdown, game_id))
        return
    if "review_md" in cols:
        _execute(conn, "UPDATE games SET review_md = %s WHERE id = %s", (review_markdown, game_id))


def _extract_trait_events(
    *,
    extractor: Callable[..., Any] | None,
    pgn_text: str,
    review_markdown: str,
    player_color: str,
    game_row: Mapping[str, Any],
    player_row: Mapping[str, Any],
) -> dict[str, Any]:
    if extractor is None:
        raise RuntimeError(
            "No trait extractor integration found. Implement "
            "'src.traits.extractor:extract_trait_events_for_game' or store trait_events beforehand."
        )

    extracted = extractor(
        pgn=pgn_text,
        review_markdown=review_markdown,
        player_color=player_color,
        game_row=game_row,
        player_row=player_row,
    )
    if isinstance(extracted, dict):
        return extracted
    if isinstance(extracted, list):
        return {"events": extracted}
    raise RuntimeError(f"Trait extractor returned unexpected payload type: {type(extracted).__name__}")


def _insert_trait_events(
    *,
    conn: Any,
    game_id: int,
    player_id: int,
    events: list[Mapping[str, Any]],
) -> int:
    if not events:
        return 0

    columns = _table_columns(conn, "trait_events")
    if not columns:
        raise RuntimeError("Table 'trait_events' does not exist or is inaccessible.")

    insert_columns = []
    for col in (
        "game_id",
        "player_id",
        "trait_key",
        "direction",
        "weight",
        "confidence",
        "phase",
        "move_number",
        "note",
        "evidence_strength",
    ):
        if col in columns:
            insert_columns.append(col)

    required_insert_columns = {"game_id", "trait_key", "direction", "weight", "confidence", "phase", "note"}
    if not required_insert_columns.issubset(set(insert_columns)):
        raise RuntimeError(
            "trait_events table is missing required columns for event insertion: "
            + ", ".join(sorted(required_insert_columns - set(insert_columns)))
        )

    placeholder_sql = ", ".join(["%s"] * len(insert_columns))
    sql = (
        "INSERT INTO trait_events (" + ", ".join(insert_columns) + ") VALUES (" + placeholder_sql + ")"
    )

    inserted = 0
    for event in events:
        values = []
        for col in insert_columns:
            if col == "game_id":
                values.append(game_id)
            elif col == "player_id":
                values.append(player_id)
            else:
                values.append(event.get(col))
        _execute(conn, sql, tuple(values))
        inserted += 1
    return inserted


def _infer_player_color(game_row: Mapping[str, Any], player_row: Mapping[str, Any]) -> str:
    explicit = _pick_first_nonempty(game_row, ("player_color", "your_color", "color"))
    if explicit:
        val = explicit.strip().lower()
        if val in ("white", "black"):
            return val

    player_id = player_row.get("id")
    white_player_id = game_row.get("white_player_id")
    black_player_id = game_row.get("black_player_id")
    if player_id is not None:
        if white_player_id is not None and int(white_player_id) == int(player_id):
            return "white"
        if black_player_id is not None and int(black_player_id) == int(player_id):
            return "black"

    return "white"


def _reset_player_trait_rows(conn: Any, player_id: int) -> None:
    _execute(conn, "DELETE FROM player_traits WHERE player_id = %s", (player_id,))
    conn.commit()


def _pick_first_nonempty(row: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _load_optional_callable(candidates: list[str]) -> Callable[..., Any] | None:
    for spec in candidates:
        module_name, attr_name = spec.split(":", 1)
        try:
            module = importlib.import_module(module_name)
            fn = getattr(module, attr_name, None)
            if callable(fn):
                return fn
        except Exception:
            continue
    return None


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
        cols = [d[0] for d in (cur.description or [])]
        return {cols[i]: row[i] for i in range(len(cols))}
    finally:
        cur.close()


def _fetchall(conn: Any, query: str, params: tuple[Any, ...] = ()) -> list[Mapping[str, Any]]:
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        rows = cur.fetchall()
        cols = [d[0] for d in (cur.description or [])]
        return [{cols[i]: row[i] for i in range(len(cols))} for row in rows]
    finally:
        cur.close()


def _table_columns(conn: Any, table_name: str) -> set[str]:
    rows = _fetchall(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
        """,
        (table_name,),
    )
    return {str(row["column_name"]) for row in rows}


if __name__ == "__main__":
    raise SystemExit(main())
