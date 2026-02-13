from __future__ import annotations

from src.llm_diagnostics import hash_text_sha256, prompt_hash


def test_hash_is_deterministic() -> None:
    value = "same-input"
    assert hash_text_sha256(value) == hash_text_sha256(value)


def test_same_prompt_produces_same_hash() -> None:
    system_msg = "System"
    user_msg = "User"
    assert prompt_hash(system_msg, user_msg, "llama3.1", 0.4, 1.0, 1400) == prompt_hash(
        system_msg,
        user_msg,
        "llama3.1",
        0.4,
        1.0,
        1400,
    )


def test_prompt_hash_changes_when_prompt_changes() -> None:
    system_msg = "System"
    assert prompt_hash(system_msg, "User A", "llama3.1", 0.4, 1.0, 1400) != prompt_hash(
        system_msg,
        "User B",
        "llama3.1",
        0.4,
        1.0,
        1400,
    )


def test_prompt_hash_changes_when_temperature_changes() -> None:
    assert prompt_hash("S", "U", "llama3.1", 0.3, 1.0, 1400) != prompt_hash(
        "S",
        "U",
        "llama3.1",
        0.4,
        1.0,
        1400,
    )


def test_prompt_hash_changes_when_top_p_changes() -> None:
    assert prompt_hash("S", "U", "llama3.1", 0.4, 0.8, 1400) != prompt_hash(
        "S",
        "U",
        "llama3.1",
        0.4,
        1.0,
        1400,
    )


def test_prompt_hash_changes_when_max_tokens_changes() -> None:
    assert prompt_hash("S", "U", "llama3.1", 0.4, 1.0, 1000) != prompt_hash(
        "S",
        "U",
        "llama3.1",
        0.4,
        1.0,
        1400,
    )
