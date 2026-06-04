FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# tzdata: required so the TZ env var maps to real timezone files.
# curl:   required for the HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# UID-agnostic non-root user (OpenShift / Kubernetes / Docker compatible).
# The container is set up to run as ANY UID by ensuring files are owned by
# group 0 with group permissions equal to user permissions. The default UID
# is 1000, but the runtime can override it via --user, securityContext, or
# OpenShift's arbitrary UID assignment.
RUN useradd -r -u 1000 -g 0 -d /app -s /sbin/nologin appuser \
    && mkdir -p /data

COPY app/ ./app/

# Files belong to root group (gid 0) with g=u permissions so the container
# can write to them as any UID (provided that UID is in group 0, which is
# the default on Docker, OpenShift, and on Kubernetes with fsGroup: 0).
RUN chgrp -R 0 /app /data \
    && chmod -R g=u /app /data

VOLUME ["/data"]
EXPOSE 8080

# Defaults — override via docker-compose / Unraid template / Kubernetes.
# INVERTER_IP has NO default. The container fails to start without it.
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

USER 1000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
