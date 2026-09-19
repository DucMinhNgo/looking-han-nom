# Tra cứu Hán-Nôm — a read-only lookup over a ground-truth dataset.
#
# Nothing is downloaded, rendered or modelled at runtime: the dataset and the
# images are mounted from the host, so this stays a small pure-Python image.

FROM python:3.12-slim

WORKDIR /srv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl is for the healthcheck only.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Defaults; every one is overridable via .env (see .env.example).
ENV DATA_DIR=/data \
    DATASET_PATH=/data/dataset.jsonl \
    IMAGES_DIR=/data/images \
    PAGE_SIZE=24 \
    COOKIE_SECURE=1

COPY app/ ./app/

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
