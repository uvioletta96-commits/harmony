#!/bin/sh
# ---------------------------------------------------------------------------
# Container entrypoint.
#
# Runs migrations and a one-off configuration check before starting the server,
# so a bad deploy fails here — loudly, at startup — rather than on the first
# request from a user.
# ---------------------------------------------------------------------------
set -eu

: "${APP_ENV:=production}"
: "${PORT:=8000}"
: "${RUN_MIGRATIONS:=true}"
: "${AUTO_MIGRATE:=false}"

log() {
  printf '%s [entrypoint] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

fail() {
  log "FATAL: $*"
  exit 1
}

# --- Preflight ---------------------------------------------------------------

if [ "$APP_ENV" = "production" ]; then
  [ -n "${SECRET_KEY:-}" ] || fail "SECRET_KEY must be set in production"
  [ -n "${JWT_SECRET_KEY:-}" ] || fail "JWT_SECRET_KEY must be set in production"
  [ "${#SECRET_KEY}" -ge 32 ] || fail "SECRET_KEY must be at least 32 characters"
  [ -n "${SITE_URL:-}" ] || fail "SITE_URL must be set in production"
  [ -n "${DATABASE_URL:-}" ] || fail "DATABASE_URL must be set in production"
  [ "${SECURE_COOKIE:-true}" = "true" ] || fail "SECURE_COOKIE must be true in production"
  [ "${FORCE_HTTPS:-true}" = "true" ] || fail "FORCE_HTTPS must be true in production"
fi

log "APP_ENV=$APP_ENV PORT=$PORT"

# --- Wait for dependencies ---------------------------------------------------
# Postgres and Redis are started together; whichever is slower wins. Bounded so
# a genuinely missing dependency fails instead of hanging the deploy.

if [ "${WAIT_FOR_SERVICES:-true}" = "true" ]; then
  log "Waiting for PostgreSQL…"
  python - <<'PY' || fail "PostgreSQL is not reachable"
import os, sys, time
import psycopg

url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    try:
        with psycopg.connect(url, connect_timeout=3):
            sys.exit(0)
    except Exception:
        time.sleep(1.5)
sys.exit(1)
PY
  log "PostgreSQL is ready"

  log "Waiting for Redis…"
  python - <<'PY' || fail "Redis is not reachable"
import sys, time
import redis

deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    try:
        redis.from_url("redis://redis:6379/0", socket_connect_timeout=2).ping()
        sys.exit(0)
    except Exception:
        time.sleep(1)
sys.exit(1)
PY
  log "Redis is ready"
fi

# --- Migrations --------------------------------------------------------------
# `flask db upgrade` is the correct tool. `AUTO_MIGRATE=true` is offered for
# single-replica deployments; with more than one replica, migrations must run as
# a separate job or the replicas will race each other.

if [ "$RUN_MIGRATIONS" = "true" ] && [ "$AUTO_MIGRATE" = "true" ]; then
  log "Applying database migrations…"
  flask db upgrade || fail "Migrations failed"
  log "Migrations applied"
elif [ "$RUN_MIGRATIONS" = "true" ]; then
  log "RUN_MIGRATIONS is on but AUTO_MIGRATE is off; skipping (run migrations as a job)"
fi

# --- Start -------------------------------------------------------------------

log "Starting: $*"
exec "$@"
