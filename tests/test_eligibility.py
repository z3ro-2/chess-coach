from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.db.eligibility import is_game_eligible_for_processing


def _row(**overrides):
    base = {
        "completed_at": None,
        "success_notified": False,
        "engine_failed": False,
        "pgn_missing_terminal": False,
        "pgn_missing": False,
        "attempt_count": 0,
        "last_attempt_at": None,
        "pgn": '[Event "Live Chess"]\n1. e4 e5 1-0\n',
    }
    base.update(overrides)
    return base


def test_is_game_eligible_for_processing_true_when_all_rules_pass() -> None:
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(last_attempt_at=now - timedelta(minutes=20))
    assert is_game_eligible_for_processing(row, now, max_attempts=5, cooldown_seconds=600) is True


def test_is_game_eligible_for_processing_false_when_success_notified() -> None:
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(success_notified=True)
    assert is_game_eligible_for_processing(row, now, max_attempts=5, cooldown_seconds=600) is False


def test_is_game_eligible_for_processing_false_when_completed() -> None:
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(completed_at=now - timedelta(minutes=1))
    assert is_game_eligible_for_processing(row, now, max_attempts=5, cooldown_seconds=600) is False


def test_is_game_eligible_for_processing_false_when_engine_failed() -> None:
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(engine_failed=True)
    assert is_game_eligible_for_processing(row, now, max_attempts=5, cooldown_seconds=600) is False


def test_is_game_eligible_for_processing_false_when_pgn_terminal_missing() -> None:
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(pgn_missing_terminal=True)
    assert is_game_eligible_for_processing(row, now, max_attempts=5, cooldown_seconds=600) is False


def test_is_game_eligible_for_processing_false_when_pgn_marked_permanently_missing() -> None:
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(pgn_missing=True)
    assert is_game_eligible_for_processing(row, now, max_attempts=5, cooldown_seconds=600) is False


def test_is_game_eligible_for_processing_false_when_attempt_count_hits_cap() -> None:
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(attempt_count=5)
    assert is_game_eligible_for_processing(row, now, max_attempts=5, cooldown_seconds=600) is False


def test_is_game_eligible_for_processing_false_when_within_cooldown() -> None:
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(last_attempt_at=now - timedelta(seconds=300))
    assert is_game_eligible_for_processing(row, now, max_attempts=5, cooldown_seconds=600) is False


def test_is_game_eligible_for_processing_false_when_pgn_missing_or_empty() -> None:
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert is_game_eligible_for_processing(_row(pgn=""), now, max_attempts=5, cooldown_seconds=600) is False
    assert is_game_eligible_for_processing(_row(pgn="   "), now, max_attempts=5, cooldown_seconds=600) is False
    assert is_game_eligible_for_processing(_row(pgn=None), now, max_attempts=5, cooldown_seconds=600) is False


def test_send_only_retry_blocked_by_tg_backoff(monkeypatch) -> None:
    monkeypatch.setenv("TG_RETRY_COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("TG_MAX_SEND_ATTEMPTS", "5")
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(
        analysis_complete=True,
        tg_send_attempts=1,
        tg_last_send_at=now - timedelta(seconds=120),
    )
    assert is_game_eligible_for_processing(row, now, max_attempts=5, cooldown_seconds=600) is False


def test_send_only_retry_blocked_by_tg_attempt_cap(monkeypatch) -> None:
    monkeypatch.setenv("TG_RETRY_COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("TG_MAX_SEND_ATTEMPTS", "3")
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(
        analysis_complete=True,
        tg_send_attempts=3,
        tg_last_send_at=now - timedelta(hours=3),
    )
    assert is_game_eligible_for_processing(row, now, max_attempts=5, cooldown_seconds=600) is False


def test_send_only_retry_eligible_after_tg_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("TG_RETRY_COOLDOWN_SECONDS", "600")
    monkeypatch.setenv("TG_MAX_SEND_ATTEMPTS", "5")
    now = datetime(2026, 2, 14, 12, 0, 0, tzinfo=timezone.utc)
    row = _row(
        analysis_complete=True,
        tg_send_attempts=2,
        tg_last_send_at=now - timedelta(seconds=601),
    )
    assert is_game_eligible_for_processing(row, now, max_attempts=1, cooldown_seconds=99999) is True
