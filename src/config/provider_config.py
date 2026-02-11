"""Thread-safe runtime provider configuration."""

from __future__ import annotations

import os
from threading import Lock

_PROVIDER_LOCK = Lock()
_current_provider = str(os.environ.get("PROVIDER", "ollama") or "ollama").strip().lower()


def get_provider() -> str:
    with _PROVIDER_LOCK:
        return _current_provider


def set_provider(provider: str) -> None:
    normalized = str(provider or "").strip().lower()
    if normalized not in {"gpt", "ollama"}:
        raise ValueError("provider must be 'gpt' or 'ollama'")
    with _PROVIDER_LOCK:
        global _current_provider
        _current_provider = normalized
