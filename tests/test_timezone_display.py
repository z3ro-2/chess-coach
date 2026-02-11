from __future__ import annotations

from datetime import datetime, timezone


def test_display_timezone_conversion(monkeypatch):
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/Chicago")

    from src.utils.timezone import get_display_timezone

    utc_dt = datetime(2026, 3, 14, 18, 0, tzinfo=timezone.utc)
    local = utc_dt.astimezone(get_display_timezone())
    assert local.hour in {12, 13}  # depending on DST


def test_display_timezone_defaults_to_utc(monkeypatch):
    monkeypatch.delenv("DISPLAY_TIMEZONE", raising=False)

    from src.utils.timezone import get_display_timezone

    utc_dt = datetime(2026, 3, 14, 18, 0, tzinfo=timezone.utc)
    local = utc_dt.astimezone(get_display_timezone())
    assert local.hour == 18


def test_display_timezone_invalid_falls_back_to_utc(monkeypatch):
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Invalid/Zone")

    from src.utils.timezone import get_display_timezone

    utc_dt = datetime(2026, 3, 14, 18, 0, tzinfo=timezone.utc)
    local = utc_dt.astimezone(get_display_timezone())
    assert local.hour == 18
