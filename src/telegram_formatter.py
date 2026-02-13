"""Telegram-safe rendering helpers for markdown-like review content."""

from __future__ import annotations

import html
import re


_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_LIST_RE = re.compile(r"^\s*-\s+(.*)$")


def escape_html(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html.escape(str(text or ""), quote=False)


def _strip_inline_markdown(text: str) -> str:
    out = str(text or "")
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)
    out = re.sub(r"__(.+?)__", r"\1", out)
    out = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", out)
    # Only treat underscores as emphasis markers when they are not part of word characters.
    out = re.sub(r"(?<!\w)_(?!_)(.+?)(?<!_)_(?!\w)", r"\1", out)
    out = re.sub(r"`([^`]+)`", r"\1", out)
    return out


def _render_markdown_for_telegram_html(md_content: str) -> str:
    lines = str(md_content or "").splitlines()
    rendered: list[str] = []
    in_code_fence = False
    code_lines: list[str] = []
    drop_diagnostics = False

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if drop_diagnostics:
            continue
        if line.strip() == "## LLM Diagnostics":
            drop_diagnostics = True
            continue
        if line.strip().startswith("```"):
            if in_code_fence:
                rendered.append(f"<pre>{escape_html(chr(10).join(code_lines))}</pre>")
                in_code_fence = False
                code_lines = []
            else:
                in_code_fence = True
            continue

        if in_code_fence:
            code_lines.append(line)
            continue

        header_match = _HEADER_RE.match(line)
        if header_match:
            rendered.append(f"<b>{escape_html(_strip_inline_markdown(header_match.group(1).strip()))}</b>")
            continue

        list_match = _LIST_RE.match(line)
        if list_match:
            rendered.append(f"• {escape_html(_strip_inline_markdown(list_match.group(1).strip()))}")
            continue

        cleaned = _strip_inline_markdown(line)
        rendered.append(escape_html(cleaned))

    if in_code_fence:
        rendered.append(f"<pre>{escape_html(chr(10).join(code_lines))}</pre>")

    return "\n".join(rendered).strip()


def render_game_review_for_telegram(md_content: str) -> str:
    """Render game-review markdown into Telegram-safe HTML."""
    return _render_markdown_for_telegram_html(md_content)


def render_summary_for_telegram(md_content: str) -> str:
    """Render summary markdown into Telegram-safe HTML."""
    return _render_markdown_for_telegram_html(md_content)
