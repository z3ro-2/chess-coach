from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return False
    return probe.returncode == 0


@pytest.mark.integration
def test_prompts_available(tmp_path: Path) -> None:
    if not _docker_available():
        pytest.skip("Docker is not available.")

    host_prompts = tmp_path / "prompts"
    host_prompts.mkdir(parents=True, exist_ok=True)

    image_tag = f"chess-coach-prompts-test:{uuid.uuid4().hex[:12]}"
    build = subprocess.run(
        ["docker", "build", "-t", image_tag, "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=600,
        check=False,
    )
    if build.returncode != 0:
        pytest.fail(f"Docker build failed:\nSTDOUT:\n{build.stdout}\nSTDERR:\n{build.stderr}")

    run = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{host_prompts}:/app/prompts",
            image_tag,
            "/bin/sh",
            "-lc",
            "ls -1 /app/prompts",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
        check=False,
    )
    if run.returncode != 0:
        pytest.fail(f"Docker run failed:\nSTDOUT:\n{run.stdout}\nSTDERR:\n{run.stderr}")

    listing = {line.strip() for line in run.stdout.splitlines() if line.strip()}
    assert "review_system.md" in listing
    assert "review_user_strict.md" in listing
