from __future__ import annotations

import pytest

from chess_review import write_pgn_once


def test_write_pgn_once_creates_new_file(tmp_path) -> None:
    pgn_path = tmp_path / "pgn" / "game_1.pgn"
    pgn_text = '[Event "Live Chess"]\n1. e4 e5 1-0\n'

    write_pgn_once(pgn_path, pgn_text)

    assert pgn_path.exists()
    assert pgn_path.read_text(encoding="utf-8") == pgn_text


def test_write_pgn_once_skips_existing_file_without_modifying_content(tmp_path) -> None:
    pgn_path = tmp_path / "pgn" / "game_2.pgn"
    original = '[Event "Live Chess"]\n1. d4 d5 1/2-1/2\n'
    replacement = '[Event "Live Chess"]\n1. e4 e5 1-0\n'

    write_pgn_once(pgn_path, original)
    write_pgn_once(pgn_path, replacement)

    assert pgn_path.read_text(encoding="utf-8") == original
    assert pgn_path.read_text(encoding="utf-8") != replacement


def test_write_pgn_once_second_call_raises_no_permission_or_overwrite_errors(tmp_path) -> None:
    pgn_path = tmp_path / "pgn" / "game_3.pgn"
    first = '[Event "Live Chess"]\n1. c4 e5 1-0\n'
    second = '[Event "Live Chess"]\n1. Nf3 d5 0-1\n'

    write_pgn_once(pgn_path, first)
    try:
        write_pgn_once(pgn_path, second)
    except PermissionError as exc:
        pytest.fail(f"write_pgn_once raised PermissionError on existing file: {exc}")
    except Exception as exc:
        pytest.fail(f"write_pgn_once raised unexpected exception on existing file: {exc}")

    assert pgn_path.read_text(encoding="utf-8") == first
