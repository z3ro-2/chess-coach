from __future__ import annotations

from pathlib import Path

import pytest

import analysis_pipeline as pipeline_module


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
