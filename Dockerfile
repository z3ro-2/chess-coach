FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Required env vars (set at runtime):
# - DATABASE_URL (for src.cli.seed_traits and DB-backed trait workflows)
# - OPENAI_API_KEY (if using provider=gpt)
# - TG_BOT_TOKEN, TG_CHAT_ID (optional Telegram notifications)
# - CHESS_USERNAME, CHESS_OUTPUT_DIR, OLLAMA_MODEL, POLL_SECONDS (used by existing compose/runtime flow)

COPY . /app

RUN set -eux; \
    python -m pip install --upgrade pip; \
    if [ -f /app/requirements.txt ]; then \
        pip install -r /app/requirements.txt; \
    elif [ -f /app/pyproject.toml ]; then \
        pip install /app; \
    else \
        echo "No requirements.txt or pyproject.toml found." >&2; \
        exit 1; \
    fi; \
    addgroup --system app; \
    adduser --system --ingroup app --home /home/app app; \
    chown -R app:app /app

USER app

ENTRYPOINT ["python"]
CMD ["-m", "src.main"]
