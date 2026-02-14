"""Centralized queue eligibility checks for poll-cycle processing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


def _coerce_datetime_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_game_eligible_for_processing(
    row: Mapping[str, Any],
    now: datetime,
    max_attempts: int,
    cooldown_seconds: int,
) -> bool:
    """Return True when a game row is eligible for this poll cycle."""
    if bool(row.get("success_notified", False)):
        return False
    if bool(row.get("engine_failed", False)):
        return False
    attempts = int(row.get("attempt_count", 0) or 0)
    if attempts >= max(1, int(max_attempts)):
        return False
    pgn = str(row.get("pgn", "") or "").strip()
    if not pgn:
        return False

    last_attempt_at = _coerce_datetime_utc(row.get("last_attempt_at"))
    now_utc = _coerce_datetime_utc(now) or now.replace(tzinfo=timezone.utc)
    if last_attempt_at is None:
        return True
    elapsed = (now_utc - last_attempt_at).total_seconds()
    return elapsed >= max(0, int(cooldown_seconds))


def eligibility_rejection_reasons(
    row: Mapping[str, Any],
    now: datetime,
    max_attempts: int,
    cooldown_seconds: int,
) -> dict[str, bool]:
    """Return structured eligibility rejection reasons for observability."""
    attempts = int(row.get("attempt_count", 0) or 0)
    pgn = str(row.get("pgn", "") or "").strip()
    last_attempt_at = _coerce_datetime_utc(row.get("last_attempt_at"))
    now_utc = _coerce_datetime_utc(now) or now.replace(tzinfo=timezone.utc)

    cooldown_active = False
    if last_attempt_at is not None:
        elapsed = (now_utc - last_attempt_at).total_seconds()
        cooldown_active = elapsed < max(0, int(cooldown_seconds))

    return {
        "success_notified": bool(row.get("success_notified", False)),
        "engine_failed": bool(row.get("engine_failed", False)),
        "cooldown_active": bool(cooldown_active),
        "attempt_cap": attempts >= max(1, int(max_attempts)),
        "missing_pgn": not bool(pgn),
    }
