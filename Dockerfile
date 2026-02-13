FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHESS_OUTPUT_DIR=/data

WORKDIR /app

# System deps (add more here if needed later)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    stockfish \
    tzdata \
 && rm -rf /var/lib/apt/lists/*
 
# Python deps
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
 && pip install --no-cache-dir -r /app/requirements.txt

# App code (exclude bind-mounted prompts path)
COPY src /app/src
COPY engine /app/engine
COPY llm /app/llm
COPY review /app/review
COPY migrations /app/migrations
COPY assets /app/assets
COPY analysis_pipeline.py /app/analysis_pipeline.py
COPY chess_review.py /app/chess_review.py
COPY backfill.py /app/backfill.py
COPY README.md /app/README.md

# Bake default prompts into an internal non-mounted location
COPY prompts /app/prompts_default

# Entrypoint wrapper
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Ensure expected runtime directories exist
RUN mkdir -p /data /data/md /data/pgn

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["-m", "src.main"]
