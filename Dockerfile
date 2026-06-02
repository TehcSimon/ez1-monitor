FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

VOLUME ["/data"]
EXPOSE 8080

# Default environment (override via docker-compose or Unraid template)
ENV INVERTER_IP=192.168.1.100 \
    INVERTER_PORT=8050 \
    POLL_INTERVAL=60 \
    DB_PATH=/data/ez1.db \
    INSTALL_KWP=1.0 \
    LOG_LEVEL=INFO

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
