FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Curl is needed for the HEALTHCHECK below; keep image lean otherwise
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

VOLUME ["/data"]
EXPOSE 8080

# Defaults — override via docker-compose
ENV INVERTER_IP=192.168.1.194 \
    INVERTER_PORT=8050 \
    POLL_INTERVAL=60 \
    DB_PATH=/data/ez1.db \
    INSTALL_KWP=1.0 \
    DEFAULT_LANG=en \
    CURRENCY=EUR \
    PRICE_PER_KWH=0.35 \
    CO2_KG_PER_KWH=0.38 \
    LOG_LEVEL=INFO

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]