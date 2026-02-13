from __future__ import annotations

from pathlib import Path

import pytest

import analysis_pipeline as pipeline_module
from src.llm_diagnostics import hash_text_sha256


def test_load_prompt_file_reads_from_prompts_dir() -> None:
    content = pipeline_module.load_prompt_file("review_system.md")
    assert isinstance(content, str)
    assert len(content) > 0


def test_load_prompt_file_missing_raises_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        pipeline_module.load_prompt_file("does_not_exist_prompt.md")


def test_prompt_path_resolution_uses_project_prompts_dir() -> None:
    expected = Path(__file__).resolve().parents[1] / "prompts"
    assert pipeline_module.PROMPTS_DIR == expected
    assert str(pipeline_module.PROMPTS_DIR) != "/prompts"


def test_strict_prompt_contains_negative_constraints() -> None:
    system_prompt = pipeline_module.load_prompt_file("review_system.md")
    user_prompt = pipeline_module.load_prompt_file("review_user_strict.md")
    required_phrases = (
        "Do not wrap output in markdown, code fences, prose, or comments.",
        "If you cannot produce exactly valid JSON, output this error object only",
        "Required JSON schema (exact keys, exact structure):",
    )
    for phrase in required_phrases:
        assert phrase in system_prompt
        assert phrase in user_prompt


def test_prompt_load_logs_path_and_hash(caplog) -> None:
    caplog.set_level("INFO")
    content = pipeline_module.load_prompt_file("review_system.md")
    expected_hash = hash_text_sha256(content)
    resolved = str((pipeline_module.PROMPTS_DIR / "review_system.md").resolve())
    assert f"filename=review_system.md" in caplog.text
    assert f"path={resolved}" in caplog.text
    assert f"sha256={expected_hash}" in caplog.text
