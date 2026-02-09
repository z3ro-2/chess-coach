from pathlib import Path
from unittest.mock import Mock

from src.db.snapshots import maybe_create_trait_snapshot
import src.db.snapshots as snapshots_module


class _DummyCursor:
    def execute(self, *_args, **_kwargs) -> None:
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self) -> None:
        return None


class _DummyConn:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _DummyCursor:
        return _DummyCursor()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_snapshot_not_created_when_not_on_20_game_cutoff(monkeypatch) -> None:
    conn = _DummyConn()

    fetch_total_games = Mock(return_value=39)
    fetch_player_row = Mock(return_value={"id": 1, "platform_user": "logan"})

    monkeypatch.setattr(snapshots_module, "_dbapi_fetch_total_games", fetch_total_games)
    monkeypatch.setattr(snapshots_module, "_dbapi_fetch_player_row", fetch_player_row)

    result = maybe_create_trait_snapshot(1, db_session_or_conn=conn)

    assert result["created"] is False
    assert result["reason"] == "not_on_cutoff"
    assert result["total_games"] == 39
    fetch_player_row.assert_not_called()


def test_snapshot_not_created_if_cutoff_already_exists(monkeypatch, tmp_path: Path) -> None:
    conn = _DummyConn()

    fetch_total_games = Mock(return_value=40)
    fetch_player_row = Mock(return_value={"id": 1, "platform_user": "logan"})
    table_exists = Mock(return_value=True)
    snapshot_exists = Mock(return_value=True)
    render_markdown = Mock(return_value="should-not-be-used")
    fetch_traits = Mock(side_effect=AssertionError("traits query should not run when snapshot exists"))

    target_path = tmp_path / "output" / "trait_books" / "logan" / "trait_book_0040.md"

    monkeypatch.setattr(snapshots_module, "_dbapi_fetch_total_games", fetch_total_games)
    monkeypatch.setattr(snapshots_module, "_dbapi_fetch_player_row", fetch_player_row)
    monkeypatch.setattr(snapshots_module, "_dbapi_table_exists", table_exists)
    monkeypatch.setattr(snapshots_module, "_dbapi_snapshot_exists", snapshot_exists)
    monkeypatch.setattr(snapshots_module, "_dbapi_fetch_traits", fetch_traits)
    monkeypatch.setattr(snapshots_module, "generate_trait_book_markdown", render_markdown)
    monkeypatch.setattr(snapshots_module, "_build_snapshot_path", lambda *_args, **_kwargs: target_path)

    result = maybe_create_trait_snapshot(1, db_session_or_conn=conn)

    assert result["created"] is False
    assert result["reason"] == "already_exists"
    assert result["cutoff_game_count"] == 40
    render_markdown.assert_not_called()
