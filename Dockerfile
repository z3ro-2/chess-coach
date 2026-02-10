FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHESS_OUTPUT_DIR=/data

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
 && pip install --no-cache-dir -r /app/requirements.txt \
 && addgroup --system app \
 && adduser --system --ingroup app --home /home/app app \
 && chown -R app:app /app

COPY . /app

USER app

ENTRYPOINT ["python"]
CMD ["-m", "src.main"]
