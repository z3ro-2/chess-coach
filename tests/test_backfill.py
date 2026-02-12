from __future__ import annotations

import json
import sqlite3

import backfill as backfill_module
import chess_review
import pytest
from engine.payload_schema import ENGINE_PAYLOAD_SCHEMA_VERSION


def _raw_game(*, game_id: int, end_time: int, result: str = "1-0") -> dict:
    return {
        "url": f"https://www.chess.com/game/live/{game_id}",
        "pgn": (
            f'[Event "Live Chess"]\n'
            f'[White "logan"]\n'
            f'[Black "opponent"]\n'
            f'[Result "{result}"]\n'
            f"1. e4 e5 {result}\n"
        ),
        "end_time": end_time,
        "time_control": "600",
        "rated": True,
        "rules": "chess",
        "white": {"username": "logan", "rating": 1200},
        "black": {"username": "opponent", "rating": 1190},
    }


def _oracle_output_for_game(
    *,
    player_color: str = "white",
    player_counts: dict[str, int] | None = None,
) -> dict:
    if player_counts is None:
        player_counts = {"good": 15, "inaccuracy": 3, "mistake": 1, "blunder": 1, "brilliant": 0}
    opponent_counts = {"good": 20, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0}
    side_counts = (
        {"white": dict(player_counts), "black": dict(opponent_counts)}
        if player_color == "white"
        else {"white": dict(opponent_counts), "black": dict(player_counts)}
    )
    merged = {k: int(side_counts["white"][k]) + int(side_counts["black"][k]) for k in player_counts}
    return {
        "game_summary": {
            "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
            "engine_depth": 12,
            "total_plies": 40,
            "total_moves": 20,
            "label_counts_by_side": side_counts,
            "label_counts": merged,
            "forced_mate_events": 0,
            "illegal_moves": 0,
        },
        "key_positions": [
            {"move_number": 1, "player": "White", "label": "good", "tactical_flag": "none", "material_change": 0},
            {"move_number": 1, "player": "Black", "label": "good", "tactical_flag": "none", "material_change": 0},
            {"move_number": 2, "player": "White", "label": "inaccuracy", "tactical_flag": "none", "material_change": 0},
            {"move_number": 2, "player": "Black", "label": "good", "tactical_flag": "none", "material_change": 0},
        ],
    }


@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(chess_review.SCHEMA_SQL)
    yield db
    db.close()


def test_backfill_adds_expected_records_when_db_initially_empty(monkeypatch, conn) -> None:
    monkeypatch.setattr(
        backfill_module,
        "fetch_recent_games",
        lambda *_args, **_kwargs: [
            _raw_game(game_id=1, end_time=1_706_000_100),
            _raw_game(game_id=2, end_time=1_706_000_200),
        ],
    )
    monkeypatch.setattr(
        backfill_module,
        "_run_stockfish_oracle",
        lambda **kwargs: _oracle_output_for_game(player_color=kwargs["game"].your_color),
    )

    result = backfill_module.backfill_recent_games(conn, username="logan", limit=10)
    rows = conn.execute("SELECT game_url, payload_json FROM engine_payloads ORDER BY end_time DESC").fetchall()
    games_rows = conn.execute("SELECT game_url FROM processed_games ORDER BY end_time DESC").fetchall()
    meta_rows = conn.execute("SELECT game_url FROM processed_game_meta ORDER BY end_time DESC").fetchall()

    assert result["games_considered"] == 2
    assert result["engine_analyses"] == 2
    assert result["stored"] == 2
    assert len(rows) == 2
    assert len(games_rows) == 2
    assert len(meta_rows) == 2
    payload = json.loads(str(rows[0][1]))
    assert payload["game_summary"]["result"] in {"1-0", "0-1", "1/2-1/2"}
    assert isinstance(payload["key_positions"], list)
    assert "game_summary" in payload
    assert "key_positions" in payload


def test_backfill_does_not_reanalyze_already_stored_games(monkeypatch, conn) -> None:
    # Pre-store one payload.
    conn.execute(
        """
        INSERT INTO engine_payloads (game_url, end_time, created_at, engine_depth, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "https://www.chess.com/game/live/2",
            1_706_000_200,
            1_706_000_200,
            12,
            json.dumps(
                {
                    "game_summary": {
                        "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
                        "your_color": "white",
                        "result": "1-0",
                        "total_plies": 40,
                        "total_moves": 20,
                        "player_total_plies": 20,
                        "player_total_moves": 20,
                        "player_label_counts": {
                            "good": 15,
                            "inaccuracy": 3,
                            "mistake": 1,
                            "blunder": 1,
                            "brilliant": 0,
                        },
                        "label_counts_by_side": {
                            "white": {
                                "good": 15,
                                "inaccuracy": 3,
                                "mistake": 1,
                                "blunder": 1,
                                "brilliant": 0,
                            },
                            "black": {
                                "good": 20,
                                "inaccuracy": 0,
                                "mistake": 0,
                                "blunder": 0,
                                "brilliant": 0,
                            },
                        },
                        "label_counts": {
                            "good": 35,
                            "inaccuracy": 3,
                            "mistake": 1,
                            "blunder": 1,
                            "brilliant": 0,
                        },
                    },
                    "key_positions": [],
                }
            ),
        ),
    )
    conn.commit()

    monkeypatch.setattr(
        backfill_module,
        "fetch_recent_games",
        lambda *_args, **_kwargs: [
            _raw_game(game_id=1, end_time=1_706_000_100),
            _raw_game(game_id=2, end_time=1_706_000_200),
        ],
    )
    calls = {"count": 0}

    def _fake_oracle(**_kwargs):
        calls["count"] += 1
        return _oracle_output_for_game(player_color="white")

    monkeypatch.setattr(backfill_module, "_run_stockfish_oracle", _fake_oracle)

    result = backfill_module.backfill_recent_games(conn, username="logan", limit=10)
    rows = conn.execute("SELECT COUNT(*) FROM engine_payloads").fetchone()
    game_rows = conn.execute("SELECT COUNT(*) FROM processed_games").fetchone()
    meta_rows = conn.execute("SELECT COUNT(*) FROM processed_game_meta").fetchone()

    assert result["games_considered"] == 2
    assert result["engine_analyses"] == 1
    assert result["stored"] == 1
    assert calls["count"] == 1
    assert rows is not None and int(rows[0]) == 2
    assert game_rows is not None and int(game_rows[0]) == 2
    assert meta_rows is not None and int(meta_rows[0]) == 2


def test_backfill_raises_when_engine_fails(monkeypatch, conn) -> None:
    monkeypatch.setattr(
        backfill_module,
        "fetch_recent_games",
        lambda *_args, **_kwargs: [
            _raw_game(game_id=1, end_time=1_706_000_100),
            _raw_game(game_id=2, end_time=1_706_000_200),
        ],
    )
    calls = {"count": 0}

    def _failing_oracle(**kwargs):
        calls["count"] += 1
        # First game succeeds and should commit; second fails and should raise.
        if kwargs["game"].game_url.endswith("/2"):
            return _oracle_output_for_game(player_color=kwargs["game"].your_color)
        return None

    monkeypatch.setattr(backfill_module, "_run_stockfish_oracle", _failing_oracle)

    with pytest.raises(RuntimeError, match="Stockfish engine failed for game"):
        backfill_module.backfill_recent_games(conn, username="logan", limit=10)

    # Ensure successful game before failure was committed.
    rows = conn.execute("SELECT game_url FROM engine_payloads ORDER BY end_time DESC").fetchall()
    game_rows = conn.execute("SELECT game_url FROM processed_games ORDER BY end_time DESC").fetchall()
    meta_rows = conn.execute("SELECT game_url FROM processed_game_meta ORDER BY end_time DESC").fetchall()
    assert len(rows) == 1
    assert str(rows[0][0]).endswith("/2")
    assert len(game_rows) == 1
    assert len(meta_rows) == 1
    assert calls["count"] == 2


def test_backfill_respects_limit(monkeypatch, conn) -> None:
    monkeypatch.setattr(
        backfill_module,
        "fetch_recent_games",
        lambda *_args, **_kwargs: [
            _raw_game(game_id=1, end_time=1_706_000_100),
            _raw_game(game_id=2, end_time=1_706_000_200),
            _raw_game(game_id=3, end_time=1_706_000_300),
            _raw_game(game_id=4, end_time=1_706_000_400),
        ],
    )
    calls = {"count": 0}

    def _fake_oracle(**_kwargs):
        calls["count"] += 1
        return _oracle_output_for_game(player_color="white")

    monkeypatch.setattr(backfill_module, "_run_stockfish_oracle", _fake_oracle)

    result = backfill_module.backfill_recent_games(conn, username="logan", limit=2)
    rows = conn.execute("SELECT game_url FROM engine_payloads ORDER BY end_time DESC").fetchall()
    game_rows = conn.execute("SELECT game_url FROM processed_games ORDER BY end_time DESC").fetchall()
    meta_rows = conn.execute("SELECT game_url FROM processed_game_meta ORDER BY end_time DESC").fetchall()

    assert result["games_considered"] == 2
    assert result["engine_analyses"] == 2
    assert result["stored"] == 2
    assert calls["count"] == 2
    assert len(rows) == 2
    assert len(game_rows) == 2
    assert len(meta_rows) == 2
    assert str(rows[0][0]).endswith("/4")
    assert str(rows[1][0]).endswith("/3")


def test_backfill_rerun_does_not_duplicate_rows(monkeypatch, conn) -> None:
    monkeypatch.setattr(
        backfill_module,
        "fetch_recent_games",
        lambda *_args, **_kwargs: [
            _raw_game(game_id=1, end_time=1_706_000_100),
            _raw_game(game_id=2, end_time=1_706_000_200),
        ],
    )
    calls = {"count": 0}

    def _fake_oracle(**kwargs):
        calls["count"] += 1
        return _oracle_output_for_game(player_color=kwargs["game"].your_color)

    monkeypatch.setattr(backfill_module, "_run_stockfish_oracle", _fake_oracle)

    first = backfill_module.backfill_recent_games(conn, username="logan", limit=10)
    first_calls = calls["count"]
    second = backfill_module.backfill_recent_games(conn, username="logan", limit=10)
    second_calls = calls["count"] - first_calls

    payload_count = int(conn.execute("SELECT COUNT(*) FROM engine_payloads").fetchone()[0])
    game_count = int(conn.execute("SELECT COUNT(*) FROM processed_games").fetchone()[0])
    meta_count = int(conn.execute("SELECT COUNT(*) FROM processed_game_meta").fetchone()[0])

    assert first["stored"] == 2
    assert second["stored"] == 0
    assert second["engine_analyses"] == 0
    assert first_calls == 2
    assert second_calls == 0
    assert payload_count == 2
    assert game_count == 2
    assert meta_count == 2


def test_backfill_engine_payloads_have_unique_game_url_index(conn) -> None:
    backfill_module._ensure_backfill_tables(conn)
    indexes = conn.execute("PRAGMA index_list('engine_payloads')").fetchall()
    index_names = {str(row[1]) for row in indexes}
    unique_index_rows = [row for row in indexes if str(row[1]) == "idx_engine_payloads_game_url_unique"]

    assert "idx_engine_payloads_game_url_unique" in index_names
    assert unique_index_rows
    assert int(unique_index_rows[0][2]) == 1


def test_backfill_raises_when_limit_exceeds_two_hundred(monkeypatch, conn) -> None:
    monkeypatch.setattr(
        backfill_module,
        "fetch_recent_games",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Fetch should not run when limit guard fails.")),
    )

    with pytest.raises(ValueError, match="Backfill limit exceeded: max 200 games at once."):
        backfill_module.backfill_recent_games(conn, username="logan", limit=201)


def test_print_backfill_summary_format_is_deterministic(capsys) -> None:
    chess_review._print_backfill_summary(
        {
            "total_games_requested": 20,
            "games_fetched_from_chess_com": 12,
            "games_analyzed_with_stockfish": 8,
            "trait_scores": {
                "tactical_awareness": 91,
                "material_discipline": 84,
                "conversion_ability": 77,
                "defensive_resilience": 69,
                "blunder_frequency": 95,
            },
        }
    )

    out = capsys.readouterr().out
    assert out == (
        "Backfill Summary:\n"
        "- total games requested: 20\n"
        "- games fetched from chess.com: 12\n"
        "- games analyzed with Stockfish: 8\n"
        "- traits (post-backfill):\n"
        "  tactical_awareness: 91\n"
        "  material_discipline: 84\n"
        "  conversion_ability: 77\n"
        "  defensive_resilience: 69\n"
        "  blunder_frequency: 95\n"
    )


def test_print_backfill_summary_uses_run_backfill_counts(monkeypatch, tmp_path, capsys) -> None:
    args = chess_review.parse_args(
        [
            "--username",
            "logan",
            "--provider",
            "ollama",
            "--backfill",
            "2",
            "--state-db",
            str(tmp_path / "state.sqlite"),
            "--out",
            str(tmp_path / "output"),
        ]
    )
    monkeypatch.setattr(
        chess_review,
        "_fetch_backfill_candidates",
        lambda **_kwargs: (
            [
                chess_review.parse_game(_raw_game(game_id=1, end_time=1_706_000_100), "logan"),
                chess_review.parse_game(_raw_game(game_id=2, end_time=1_706_000_200), "logan"),
                chess_review.parse_game(_raw_game(game_id=3, end_time=1_706_000_300), "logan"),
            ],
            3,
        ),
    )
    monkeypatch.setattr(
        chess_review,
        "_analyze_game_with_stockfish",
        lambda **kwargs: {
            "game_summary": {
                "schema_version": ENGINE_PAYLOAD_SCHEMA_VERSION,
                "your_color": kwargs["game"].your_color,
                "result": kwargs["game"].result,
                "total_plies": 40,
                "total_moves": 20,
                "player_total_plies": 20,
                "player_total_moves": 20,
                "player_label_counts": {"good": 15, "inaccuracy": 3, "mistake": 1, "blunder": 1, "brilliant": 0},
                "label_counts_by_side": {
                    "white": {"good": 15, "inaccuracy": 3, "mistake": 1, "blunder": 1, "brilliant": 0},
                    "black": {"good": 20, "inaccuracy": 0, "mistake": 0, "blunder": 0, "brilliant": 0},
                },
                "label_counts": {"good": 35, "inaccuracy": 3, "mistake": 1, "blunder": 1, "brilliant": 0},
            },
            "key_positions": [],
        },
    )
    monkeypatch.setattr(
        chess_review,
        "_compute_trait_scores_for_window",
        lambda *_args, **_kwargs: {
            "tactical_awareness": 88,
            "material_discipline": 87,
            "conversion_ability": 86,
            "defensive_resilience": 85,
            "blunder_frequency": 84,
        },
    )

    conn = chess_review.init_db(args.state_db)
    try:
        result = chess_review.run_backfill(conn, args)
    finally:
        conn.close()

    chess_review._print_backfill_summary(result)
    out = capsys.readouterr().out

    assert "- total games requested: 2" in out
    assert "- games fetched from chess.com: 3" in out
    assert "- games analyzed with Stockfish: 2" in out
    assert "  tactical_awareness: 88" in out
