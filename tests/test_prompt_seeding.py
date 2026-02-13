from __future__ import annotations

import subprocess
from pathlib import Path


def _run_entrypoint_logic(*, prompts_dir: Path, prompts_default_dir: Path) -> None:
    script = f"""#!/usr/bin/env sh
set -eu
PROMPTS_DIR="{prompts_dir}"
PROMPTS_DEFAULT_DIR="{prompts_default_dir}"
if [ ! -d "$PROMPTS_DIR" ] || [ -z "$(ls -A "$PROMPTS_DIR" 2>/dev/null)" ]; then
  mkdir -p "$PROMPTS_DIR"
  cp -R "$PROMPTS_DEFAULT_DIR"/* "$PROMPTS_DIR"/
fi
"""
    subprocess.run(["/bin/sh", "-c", script], check=True)


def test_prompt_seeds_when_host_empty(tmp_path) -> None:
    host_prompts = tmp_path / "host_prompts"
    baked_defaults = tmp_path / "prompts_default"
    baked_defaults.mkdir(parents=True, exist_ok=True)
    (baked_defaults / "review_system.md").write_text("system default", encoding="utf-8")
    (baked_defaults / "review_user_strict.md").write_text("user default", encoding="utf-8")

    _run_entrypoint_logic(prompts_dir=host_prompts, prompts_default_dir=baked_defaults)

    assert (host_prompts / "review_system.md").exists()
    assert (host_prompts / "review_user_strict.md").exists()
    assert (host_prompts / "review_system.md").read_text(encoding="utf-8") == "system default"
    assert (host_prompts / "review_user_strict.md").read_text(encoding="utf-8") == "user default"


def test_prompt_does_not_overwrite_existing(tmp_path) -> None:
    host_prompts = tmp_path / "host_prompts"
    baked_defaults = tmp_path / "prompts_default"
    host_prompts.mkdir(parents=True, exist_ok=True)
    baked_defaults.mkdir(parents=True, exist_ok=True)

    existing = host_prompts / "review_user_strict.md"
    existing.write_text("host customized", encoding="utf-8")
    (baked_defaults / "review_user_strict.md").write_text("default user", encoding="utf-8")
    (baked_defaults / "review_system.md").write_text("default system", encoding="utf-8")

    _run_entrypoint_logic(prompts_dir=host_prompts, prompts_default_dir=baked_defaults)

    assert existing.read_text(encoding="utf-8") == "host customized"
    assert not (host_prompts / "review_system.md").exists()
