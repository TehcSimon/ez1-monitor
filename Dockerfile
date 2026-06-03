FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# tzdata: required so the TZ env var actually maps to real timezone files.
# curl:   required for the HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

VOLUME ["/data"]
EXPOSE 8080

# Defaults — override via docker-compose / Unraid template.
# Note: INVERTER_IP has NO default. The container fails to start without it,
# rather than running silently with an empty dashboard.
ENV INVERTER_PORT=8050 \
    POLL_INTERVAL=60 \
    DB_PATH=/data/ez1.db \
    INSTALL_KWP=1.0 \
    RETENTION_DAYS=730 \
    CURRENCY=EUR \
    PRICE_PER_KWH=0.35 \
    CO2_KG_PER_KWH=0.38 \
    TZ=Etc/UTC \
    LOG_LEVEL=INFO

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
