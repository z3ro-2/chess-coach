"""Centralized queue eligibility checks for poll-cycle processing."""

from __future__ import annotations

import os
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
    if _coerce_datetime_utc(row.get("completed_at")) is not None:
        return False
    if bool(row.get("success_notified", False)):
        return False
    if bool(row.get("engine_failed", False)):
        return False
    if bool(row.get("pgn_missing_terminal", False)):
        return False
    if bool(row.get("pgn_missing", False)):
        return False
    attempts = int(row.get("attempt_count", 0) or 0)
    if attempts >= max(1, int(max_attempts)):
        return False
    pgn = str(row.get("pgn", "") or "").strip()
    if not pgn:
        return False

    now_utc = _coerce_datetime_utc(now) or now.replace(tzinfo=timezone.utc)
    send_only_retry = bool(row.get("analysis_complete", False))
    if send_only_retry:
        tg_attempts = int(row.get("tg_send_attempts", 0) or 0)
        tg_max_attempts = max(1, _env_int("TG_MAX_SEND_ATTEMPTS", 5))
        if tg_attempts >= tg_max_attempts:
            return False
        tg_last_send_at = _coerce_datetime_utc(row.get("tg_last_send_at"))
        if tg_last_send_at is None:
            return True
        tg_elapsed = (now_utc - tg_last_send_at).total_seconds()
        return tg_elapsed >= max(0, _env_int("TG_RETRY_COOLDOWN_SECONDS", 600))

    last_attempt_at = _coerce_datetime_utc(row.get("last_attempt_at"))
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
    tg_last_send_at = _coerce_datetime_utc(row.get("tg_last_send_at"))
    now_utc = _coerce_datetime_utc(now) or now.replace(tzinfo=timezone.utc)
    send_only_retry = bool(row.get("analysis_complete", False))

    cooldown_active = False
    if last_attempt_at is not None:
        elapsed = (now_utc - last_attempt_at).total_seconds()
        cooldown_active = elapsed < max(0, int(cooldown_seconds))

    tg_cooldown_seconds = max(0, _env_int("TG_RETRY_COOLDOWN_SECONDS", 600))
    tg_cooldown_active = False
    if tg_last_send_at is not None:
        tg_elapsed = (now_utc - tg_last_send_at).total_seconds()
        tg_cooldown_active = tg_elapsed < tg_cooldown_seconds
    tg_attempt_cap = int(row.get("tg_send_attempts", 0) or 0) >= max(1, _env_int("TG_MAX_SEND_ATTEMPTS", 5))

    return {
        "completed": _coerce_datetime_utc(row.get("completed_at")) is not None,
        "send_only_retry": send_only_retry,
        "success_notified": bool(row.get("success_notified", False)),
        "engine_failed": bool(row.get("engine_failed", False)),
        "pgn_missing_terminal": bool(row.get("pgn_missing_terminal", False)),
        "pgn_missing": bool(row.get("pgn_missing", False)),
        "cooldown_active": bool(cooldown_active),
        "tg_cooldown_active": bool(tg_cooldown_active),
        "attempt_cap": attempts >= max(1, int(max_attempts)),
        "tg_attempt_cap": bool(tg_attempt_cap),
        "missing_pgn": (not bool(pgn)) or bool(row.get("pgn_missing", False)) or bool(row.get("pgn_missing_terminal", False)),
    }


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)
