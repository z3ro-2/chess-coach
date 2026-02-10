FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHESS_OUTPUT_DIR=/data

WORKDIR /app

# System deps (add more here if needed later)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    tzdata \
 && rm -rf /var/lib/apt/lists/*
 
# Python deps
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
 && pip install --no-cache-dir -r /app/requirements.txt

# App code
COPY . /app

# Ensure expected runtime directories exist
RUN mkdir -p /data /data/md /data/pgn

ENTRYPOINT ["python"]
CMD ["-m", "src.main"]
