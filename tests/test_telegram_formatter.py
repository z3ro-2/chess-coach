from __future__ import annotations

from src.telegram_formatter import escape_html, render_game_review_for_telegram, render_summary_for_telegram


def test_escape_html_escapes_ampersand_less_greater() -> None:
    assert escape_html("A&B < C > D") == "A&amp;B &lt; C &gt; D"


def test_html_formatter_replaces_md_headers() -> None:
    md = "# Title\n## Subtitle\nPlain line"
    out = render_game_review_for_telegram(md)
    assert "<b>Title</b>" in out
    assert "<b>Subtitle</b>" in out
    assert "# Title" not in out
    assert "## Subtitle" not in out


def test_render_game_review_handles_json_block() -> None:
    md = """# Diagnostics

```json
{"note": "A&B < C > D"}
```
"""

    out = render_game_review_for_telegram(md)

    assert "<b>Diagnostics</b>" in out
    assert "<pre>" in out
    assert "</pre>" in out
    assert '{"note": "A&amp;B &lt; C &gt; D"}' in out
    assert "&amp;" in out
    assert "&lt;" in out
    assert "&gt;" in out
    assert "```" not in out


def test_render_game_review_escapes_underscores_and_parentheses() -> None:
    md = """## Plan
- Keep_knight_on_outpost
- Play (10+0) daily
- Review _timing_ (critical)
"""

    out = render_game_review_for_telegram(md)

    assert "<b>Plan</b>" in out
    assert "• Keep_knight_on_outpost" in out
    assert "• Play (10+0) daily" in out
    assert "• Review timing (critical)" in out
    assert "## Plan" not in out
    assert "```" not in out


def test_render_game_review_excludes_diagnostics() -> None:
    md = """# Review
- Main point

## LLM Diagnostics
{"prompt_hash":"abc","output_hash":"def"}
"""
    out = render_game_review_for_telegram(md)
    assert "<b>Review</b>" in out
    assert "Main point" in out
    assert "LLM Diagnostics" not in out
    assert "prompt_hash" not in out
    assert "output_hash" not in out


def test_render_summary_excludes_diagnostics() -> None:
    md = """## Snapshot
- Strong middlegame

## LLM Diagnostics
{"model_name":"x","retry_attempted":false}
"""
    out = render_summary_for_telegram(md)
    assert "<b>Snapshot</b>" in out
    assert "Strong middlegame" in out
    assert "LLM Diagnostics" not in out
    assert "model_name" not in out
    assert "retry_attempted" not in out
