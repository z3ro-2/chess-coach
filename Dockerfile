FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY chess_review.py /app/chess_review.py

RUN mkdir -p /data

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/chess_review.py"]
