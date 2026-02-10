from __future__ import annotations

import chess_review


def test_write_pgn_once_writes_new_file(tmp_path) -> None:
    pgn_path = tmp_path / "pgn" / "game.pgn"
    pgn_text = '[Event "Live Chess"]\n1. e4 e5 1-0\n'

    chess_review.write_pgn_once(pgn_path, pgn_text)

    assert pgn_path.exists()
    assert pgn_path.read_text(encoding="utf-8") == pgn_text


def test_write_pgn_once_skips_existing_file_without_exception(tmp_path) -> None:
    pgn_path = tmp_path / "pgn" / "game.pgn"
    original_text = '[Event "Live Chess"]\n1. d4 d5 1/2-1/2\n'
    new_text = '[Event "Live Chess"]\n1. e4 e5 1-0\n'
    pgn_path.parent.mkdir(parents=True, exist_ok=True)
    pgn_path.write_text(original_text, encoding="utf-8")

    chess_review.write_pgn_once(pgn_path, new_text)

    assert pgn_path.read_text(encoding="utf-8") == original_text
