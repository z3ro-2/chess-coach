from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_manual_telegram_format_script_runs(tmp_path) -> None:
    md_dir = tmp_path / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    md_file = md_dir / "sample.md"
    md_file.write_text(
        """# Review\n- Good move\n\n## LLM Diagnostics\n{\"prompt_hash\":\"abc\"}\n""",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "tools/test_telegram_format.py", "--md-dir", str(md_dir)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "<b>Review</b>" in proc.stdout
    assert "• Good move" in proc.stdout
    assert "LLM Diagnostics" not in proc.stdout
    assert "prompt_hash" not in proc.stdout
