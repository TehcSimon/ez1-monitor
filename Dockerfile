# Multi-stage Alpine build:
# - builder: installs Python wheels into a single target dir
# - runtime: copies just the installed packages, no pip metadata,
#   no build tools, no compiler caches
#
# Alpine was chosen over python:3.12-slim because all our dependencies
# (FastAPI, uvicorn, aiosqlite, apsystems-ez1, prometheus-client) have
# pre-built musl wheels available, so no compiler is needed at install
# time. If a future dependency drops musl wheel support, fall back to
# python:3.12-slim by changing both stages' base image.

# --- Builder stage ----------------------------------------------------
FROM python:3.14-alpine AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY requirements.txt .
# Install into /install so we can copy that single tree into runtime
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# --- Runtime stage ----------------------------------------------------
FROM python:3.14-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# tzdata: required so the TZ env var maps to real timezone files.
# No curl — the HEALTHCHECK uses busybox wget, which is part of the
# Alpine base image (saves ~5 MB of curl + libcurl dependencies).
RUN apk add --no-cache tzdata

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Slim the runtime: pip/ensurepip are never used at runtime (the image is
# immutable; upgrades come as new images), and __pycache__ dirs are dead
# weight with PYTHONDONTWRITEBYTECODE. Together ~12 MB. Paths are derived
# via glob instead of hardcoding the Python minor version so Dependabot
# base-image bumps (3.12 → 3.14 → ...) can't silently break this step.
RUN find /usr/local/lib -depth -type d \
         \( -name __pycache__ -o -name 'pip' -o -name 'pip-*' -o -name ensurepip \) \
         -exec rm -rf {} +

# UID-agnostic non-root user (OpenShift / Kubernetes / Docker compatible).
# The container is set up to run as ANY UID by ensuring files are owned by
# group 0 with group permissions equal to user permissions. The default UID
# is 1000, but the runtime can override it via --user, securityContext, or
# OpenShift's arbitrary UID assignment.
RUN adduser -D -u 1000 -G root -h /app -s /sbin/nologin appuser \
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
    CMD wget -q -T 5 -O /dev/null http://127.0.0.1:8080/health || exit 1

USER 1000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
