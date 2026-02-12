from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import chess_review


def _build_game(*, url: str, end_time: int, result: str, your_color: str) -> chess_review.GameInfo:
    return chess_review.GameInfo(
        game_url=url,
        pgn=f'[Event "Live Chess"]\n[Result "{result}"]\n1. e4 e5 {result}\n',
        end_time=end_time,
        time_control="600",
        rated=True,
        rules="chess",
        white_username="logan" if your_color == "white" else "opponent",
        black_username="opponent" if your_color == "white" else "logan",
        white_rating=1200,
        black_rating=1200,
        result=result,
        your_color=your_color,
        opponent="opponent",
    )


def test_load_recent_game_reviews_for_traits_uses_rolling_window_order(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    conn = chess_review.init_db(tmp_path / "state.sqlite")
    out = Path(tmp_path) / "output"
    out.mkdir(parents=True, exist_ok=True)

    try:
        games = [
            _build_game(url="https://www.chess.com/game/live/1", end_time=1_706_000_100, result="1-0", your_color="white"),
            _build_game(url="https://www.chess.com/game/live/2", end_time=1_706_000_200, result="0-1", your_color="white"),
            _build_game(url="https://www.chess.com/game/live/3", end_time=1_706_000_300, result="1/2-1/2", your_color="white"),
        ]
        for idx, game in enumerate(games, start=1):
            md_path = out / "md" / f"g{idx}.md"
            pgn_path = out / "pgn" / f"g{idx}.pgn"
            chess_review.write_text(md_path, "# review")
            chess_review.write_text(pgn_path, game.pgn)
            chess_review.mark_processed(
                conn=conn,
                game_url=game.game_url,
                end_time=game.end_time,
                md_path=md_path,
                pgn_path=pgn_path,
                provider="ollama",
                model="llama3.1:8b",
                content_hash=f"h{idx}",
            )
            chess_review._record_processed_game_meta(conn, game)

        rows = chess_review._load_recent_game_reviews_for_traits(conn, 2)
    finally:
        conn.close()

    assert [row["game_url"] for row in rows] == [
        "https://www.chess.com/game/live/3",
        "https://www.chess.com/game/live/2",
    ]


def test_compute_trait_scores_for_window_known_dataset(monkeypatch, tmp_path) -> None:
    payloads = [
        {
            "game_summary": {"your_color": "white", "result": "1-0", "total_moves": 20},
            "key_positions": [
                {"player": "White", "move_number": 4, "label": "good", "tactical_flag": "none", "material_change": 3},
                {"player": "White", "move_number": 5, "label": "blunder", "tactical_flag": "hanging_piece", "material_change": -2},
                {"player": "White", "move_number": 6, "label": "mistake", "tactical_flag": "tactical_miss", "material_change": -1},
            ],
        },
        {
            "game_summary": {"your_color": "white", "result": "1/2-1/2", "total_moves": 20},
            "key_positions": [
                {"player": "White", "move_number": 4, "label": "good", "tactical_flag": "none", "material_change": 4},
                {"player": "White", "move_number": 10, "label": "good", "tactical_flag": "none", "material_change": -4},
            ],
        },
    ]
    monkeypatch.setattr(chess_review, "_load_engine_payloads_for_trait_window", lambda *_args, **_kwargs: payloads)

    conn = chess_review.init_db(tmp_path / "state.sqlite")
    try:
        scores = chess_review._compute_trait_scores_for_window(
            conn,
            SimpleNamespace(),
            window_size=20,
        )
    finally:
        conn.close()

    assert scores == {
        "tactical_awareness": 82,
        "material_discipline": 79,
        "conversion_ability": 50,
        "defensive_resilience": 100,
        "blunder_frequency": 98,
    }

