#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from src.telegram_formatter import render_game_review_for_telegram

_ALLOWED_TAGS = {"b", "/b", "pre", "/pre"}


def _latest_markdown_file(md_dir: Path) -> Path | None:
    files = sorted(md_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _validate_telegram_html(text: str) -> list[str]:
    issues: list[str] = []
    if re.search(r"&(?!amp;|lt;|gt;)", text):
        issues.append("contains unescaped '&' entity")

    for match in re.finditer(r"<([^>]+)>", text):
        tag = match.group(1).strip()
        if tag not in _ALLOWED_TAGS:
            issues.append(f"contains unsupported HTML tag: <{tag}>")

    if "```" in text:
        issues.append("contains markdown code fences")
    return issues


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render latest markdown review as Telegram-safe HTML and validate output.")
    parser.add_argument("--md-path", type=Path, default=None, help="Explicit markdown file path to render.")
    parser.add_argument("--md-dir", type=Path, default=Path("output") / "md", help="Directory to scan for latest markdown file.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    md_path: Path | None
    if args.md_path is not None:
        md_path = args.md_path
    else:
        md_path = _latest_markdown_file(args.md_dir)

    if md_path is None or not md_path.exists():
        print("No markdown file found for Telegram formatter inspection.", file=sys.stderr)
        return 2

    content = md_path.read_text(encoding="utf-8")
    rendered = render_game_review_for_telegram(content)
    print(rendered)

    issues = _validate_telegram_html(rendered)
    if issues:
        for issue in issues:
            print(f"UNSAFE: {issue}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
