from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from src.traits.update_player_trait_state import update_player_trait_state


def apply_trait_updates_for_game(
    player_id: int,
    game_id: int,
    *,
    db_session_or_conn: Any,
) -> dict[str, int]:
    """
    Apply per-trait player state updates for one game in a single transaction.

    Supports either:
    - SQLAlchemy Session (preferred when available)
    - DB-API connection (psycopg2-style)
    """
    if _looks_like_dbapi_connection(db_session_or_conn):
        return _apply_with_dbapi_connection(player_id, game_id, db_session_or_conn)
    return _apply_with_sqlalchemy_session(player_id, game_id, db_session_or_conn)


def _looks_like_dbapi_connection(candidate: Any) -> bool:
    return all(hasattr(candidate, attr) for attr in ("cursor", "commit", "rollback"))


def _group_events_by_trait_key(events: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        trait_key = event.get("trait_key")
        if isinstance(trait_key, str):
            grouped[trait_key].append(dict(event))
    return grouped


def _apply_updates(
    *,
    player_id: int,
    game_id: int,
    canonical_traits: list[Mapping[str, Any]],
    existing_player_traits: list[Mapping[str, Any]],
    game_events: list[Mapping[str, Any]],
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    existing_by_trait_id = {int(row["trait_id"]): row for row in existing_player_traits}
    events_by_key = _group_events_by_trait_key(game_events)

    updates: list[tuple[Any, ...]] = []
    inserts: list[tuple[Any, ...]] = []

    for trait in canonical_traits:
        trait_id = int(trait["id"])
        trait_key = str(trait["key"])
        severity = float(trait.get("severity_weight", 1.0) or 1.0)
        relevant_events = events_by_key.get(trait_key, [])

        existing = existing_by_trait_id.get(trait_id)
        if existing is not None:
            current_trait = {
                "trait_key": trait_key,
                "trend_ema": float(existing.get("trend_ema", 0.0) or 0.0),
                "confidence": float(existing.get("confidence", 0.0) or 0.0),
                "last_seen_game_id": existing.get("last_seen_game_id"),
            }
            updated = update_player_trait_state(
                current_trait,
                {"events": relevant_events},
                severity,
                game_id,
            )
            updates.append(
                (
                    float(updated["trend_ema"]),
                    float(updated["confidence"]),
                    updated.get("last_seen_game_id"),
                    player_id,
                    trait_id,
                )
            )
            continue

        if not relevant_events:
            continue

        new_trait_state = {
            "trait_key": trait_key,
            "trend_ema": 0.0,
            "confidence": 0.0,
            "last_seen_game_id": None,
        }
        updated = update_player_trait_state(
            new_trait_state,
            {"events": relevant_events},
            severity,
            game_id,
        )
        inserts.append(
            (
                player_id,
                trait_id,
                float(updated["trend_ema"]),
                float(updated["confidence"]),
                updated.get("last_seen_game_id"),
            )
        )

    return updates, inserts


def _apply_with_dbapi_connection(player_id: int, game_id: int, conn: Any) -> dict[str, int]:
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT id, key, COALESCE(severity_weight, 1.0) AS severity_weight
            FROM traits
            """
        )
        canonical_traits = [
            {"id": row[0], "key": row[1], "severity_weight": row[2]}
            for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT trait_id, trend_ema, confidence, last_seen_game_id
            FROM player_traits
            WHERE player_id = %s
            """,
            (player_id,),
        )
        existing_player_traits = [
            {
                "trait_id": row[0],
                "trend_ema": row[1],
                "confidence": row[2],
                "last_seen_game_id": row[3],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT trait_key, direction, weight, confidence, phase, move_number, note, evidence_strength
            FROM trait_events
            WHERE game_id = %s AND player_id = %s
            """,
            (game_id, player_id),
        )
        game_events = [
            {
                "trait_key": row[0],
                "direction": row[1],
                "weight": row[2],
                "confidence": row[3],
                "phase": row[4],
                "move_number": row[5],
                "note": row[6],
                "evidence_strength": row[7],
            }
            for row in cur.fetchall()
        ]

        updates, inserts = _apply_updates(
            player_id=player_id,
            game_id=game_id,
            canonical_traits=canonical_traits,
            existing_player_traits=existing_player_traits,
            game_events=game_events,
        )

        if updates:
            cur.executemany(
                """
                UPDATE player_traits
                SET trend_ema = %s,
                    confidence = %s,
                    last_seen_game_id = %s
                WHERE player_id = %s AND trait_id = %s
                """,
                updates,
            )

        if inserts:
            cur.executemany(
                """
                INSERT INTO player_traits
                    (player_id, trait_id, trend_ema, confidence, last_seen_game_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                inserts,
            )

        conn.commit()
        return {"updated": len(updates), "inserted": len(inserts)}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def _apply_with_sqlalchemy_session(player_id: int, game_id: int, session: Any) -> dict[str, int]:
    from sqlalchemy import text

    try:
        with session.begin():
            canonical_traits = list(
                session.execute(
                    text(
                        """
                        SELECT id, key, COALESCE(severity_weight, 1.0) AS severity_weight
                        FROM traits
                        """
                    )
                ).mappings()
            )

            existing_player_traits = list(
                session.execute(
                    text(
                        """
                        SELECT trait_id, trend_ema, confidence, last_seen_game_id
                        FROM player_traits
                        WHERE player_id = :player_id
                        """
                    ),
                    {"player_id": player_id},
                ).mappings()
            )

            game_events = list(
                session.execute(
                    text(
                        """
                        SELECT trait_key, direction, weight, confidence, phase, move_number, note, evidence_strength
                        FROM trait_events
                        WHERE game_id = :game_id AND player_id = :player_id
                        """
                    ),
                    {"game_id": game_id, "player_id": player_id},
                ).mappings()
            )

            updates, inserts = _apply_updates(
                player_id=player_id,
                game_id=game_id,
                canonical_traits=canonical_traits,
                existing_player_traits=existing_player_traits,
                game_events=game_events,
            )

            if updates:
                session.execute(
                    text(
                        """
                        UPDATE player_traits
                        SET trend_ema = :trend_ema,
                            confidence = :confidence,
                            last_seen_game_id = :last_seen_game_id
                        WHERE player_id = :player_id AND trait_id = :trait_id
                        """
                    ),
                    [
                        {
                            "trend_ema": row[0],
                            "confidence": row[1],
                            "last_seen_game_id": row[2],
                            "player_id": row[3],
                            "trait_id": row[4],
                        }
                        for row in updates
                    ],
                )

            if inserts:
                session.execute(
                    text(
                        """
                        INSERT INTO player_traits
                            (player_id, trait_id, trend_ema, confidence, last_seen_game_id)
                        VALUES
                            (:player_id, :trait_id, :trend_ema, :confidence, :last_seen_game_id)
                        """
                    ),
                    [
                        {
                            "player_id": row[0],
                            "trait_id": row[1],
                            "trend_ema": row[2],
                            "confidence": row[3],
                            "last_seen_game_id": row[4],
                        }
                        for row in inserts
                    ],
                )

        return {"updated": len(updates), "inserted": len(inserts)}
    except Exception:
        session.rollback()
        raise
