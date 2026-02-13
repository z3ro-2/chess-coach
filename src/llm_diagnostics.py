"""Deterministic helpers for LLM transparency diagnostics."""

from __future__ import annotations

import hashlib
from typing import Optional, Tuple


def hash_text_sha256(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def prompt_hash(
    system_msg: str,
    user_msg: str,
    model_name: str,
    temperature: float,
    top_p: float,
    max_tokens: int,
) -> str:
    return hash_text_sha256(
        f"{system_msg}\n<<USER>>\n{user_msg}\n<<MODEL>>\n{model_name}\n"
        f"<<TEMP>>\n{float(temperature):.8f}\n<<TOP_P>>\n{float(top_p):.8f}\n<<MAX_TOKENS>>\n{int(max_tokens)}"
    )


def split_model_name_version(model: str) -> Tuple[str, Optional[str]]:
    value = str(model or "").strip()
    if ":" in value:
        name, version = value.split(":", 1)
        return name.strip(), (version.strip() or None)
    return value, None
