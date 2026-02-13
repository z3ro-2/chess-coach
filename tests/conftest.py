from __future__ import annotations


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require external integrations (e.g., live Ollama).",
    )
