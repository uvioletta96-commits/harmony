# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Harmony backend image
# ---------------------------------------------------------------------------
# Multi-stage so the runtime image carries no compilers, no pip cache and no
# test dependencies. Non-root by default: a container process has no business
# owning the filesystem it is writing uploads into.

ARG PYTHON_VERSION=3.12
ARG BASE_IMAGE=python:${PYTHON_VERSION}-slim-bookworm

# --- Builder -----------------------------------------------------------------
FROM ${BASE_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# Build tools are needed for psycopg/cryptography wheels on unusual platforms.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY backend/requirements.txt .

# --no-compile keeps .pyc out of the image; the first request pays a tiny cost
# instead of the layer size growing on every source change.
RUN pip wheel --wheel-dir=/wheels -r requirements.txt

# --- Runtime -----------------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime

LABEL org.opencontainers.image.title="harmony-backend" \
      org.opencontainers.image.description="Flask API for the Harmony social network" \
      org.opencontainers.image.source="https://github.com/your-org/harmony" \
      org.opencontainers.image.licenses="Proprietary"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/home/app/.local/bin:${PATH}" \
    APP_ENV=production \
    PORT=8000

# libpq5 is the only runtime library psycopg needs. curl is kept for the
# container healthcheck below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home --shell /usr/sbin/nologin app

COPY --from=builder /wheels /wheels
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /wheels /tmp/requirements.txt

WORKDIR /app

COPY backend/ /app/
COPY spec/ /app/spec/
COPY tools/ /app/tools/

# Writable locations, owned by the unprivileged user.
RUN mkdir -p /app/uploads /app/exports /app/logs /tmp/prometheus \
    && chown -R app:app /app /tmp/prometheus

USER app

EXPOSE 8000

# The health endpoint is served without touching the database, so a slow or
# unhealthy Postgres does not cause the orchestrator to kill an otherwise
# working process. Readiness (/readyz) covers dependencies.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--worker-class", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", \
     "--workers", "1", \
     "--worker-connections", "1000", \
     "--timeout", "60", \
     "--graceful-timeout", "30", \
     "--keep-alive", "5", \
     "--max-requests", "10000", \
     "--max-requests-jitter", "1000", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info", \
     "--preload"]
