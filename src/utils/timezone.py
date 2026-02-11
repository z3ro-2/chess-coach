from __future__ import annotations

import os
from zoneinfo import ZoneInfo


def get_display_timezone() -> ZoneInfo:
    tz_name = os.getenv("DISPLAY_TIMEZONE", "UTC")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")
