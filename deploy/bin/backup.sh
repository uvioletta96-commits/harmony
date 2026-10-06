#!/usr/bin/env bash
# Nightly backup: a compressed pg_dump plus a tar of the uploads directory.
#
# Both live on the same disk as the database, so this does NOT survive losing
# the VM - it protects against a corrupt or accidentally-dropped table, which is
# the failure that actually happens. Off-site copies are a separate question and
# need a bucket.
set -uo pipefail

APP=/home/harmony/harmony
DATA=/home/harmony/harmony-data
PY=$APP/.venv/bin/python
BACKUP=$DATA/backups
KEEP_DAYS=7
MIN_FREE_MB=1500

mkdir -p "$BACKUP"
STAMP=$(date -u +%Y%m%d-%H%M%S)
LOG="$BACKUP/backup.log"

# systemd's `StandardOutput=append:` creates this file as root before the
# service drops to the harmony user, after which every `>> "$LOG"` and `tee -a`
# in this script fails with EACCES - which silently turns into "no dump was
# taken". Remove any file the unit created and let the script own its log.
if [ -e "$LOG" ] && [ ! -w "$LOG" ]; then
  sudo rm -f "$LOG" 2>/dev/null || rm -f "$LOG" 2>/dev/null || true
fi
if ! touch "$LOG" 2>/dev/null; then
  sudo rm -f "$LOG" 2>/dev/null || true
  touch "$LOG" 2>/dev/null || true
fi

log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

log "backup start"

FREE_MB=$(df -Pm "$BACKUP" | awk 'NR==2 {print $4}')
if [ "${FREE_MB:-0}" -lt "$MIN_FREE_MB" ]; then
  log "SKIPPED: only ${FREE_MB} MB free, need ${MIN_FREE_MB}"
  exit 1
fi

# The password is read from .env and never echoed, logged, or passed on a
# command line where another user could read it from /proc.
DBURL=$(grep -E '^DATABASE_URL=' "$APP/.env" | head -1 | cut -d= -f2-)
DBURL=${DBURL/postgresql+psycopg:/postgresql:}

DUMP="$BACKUP/harmony-$STAMP.dump"
if pg_dump --no-owner --no-acl -Fc "$DBURL" > "$DUMP" 2>>"$LOG"; then
  SIZE=$(du -h "$DUMP" | cut -f1)
  if [ -s "$DUMP" ]; then
    log "database dumped: $DUMP ($SIZE)"
  else
    log "FAILED: dump is empty - $DUMP removed"
    rm -f "$DUMP"
  fi
else
  log "FAILED: pg_dump exited non-zero"
  rm -f "$DUMP"
fi

TARBALL="$BACKUP/uploads-$STAMP.tar.gz"
if tar -czf "$TARBALL" -C "$DATA" uploads 2>>"$LOG"; then
  log "uploads archived: $TARBALL ($(du -h "$TARBALL" | cut -f1))"
else
  log "FAILED: tar exited non-zero"
  rm -f "$TARBALL"
fi

log "pruning older than ${KEEP_DAYS} days"
find "$BACKUP" -maxdepth 1 -name 'harmony-*.dump'   -mtime +"$KEEP_DAYS" -delete
find "$BACKUP" -maxdepth 1 -name 'uploads-*.tar.gz' -mtime +"$KEEP_DAYS" -delete

# Retention: expired sessions, one-off tokens, scheduled deletions, orphaned
# uploads. Same code the Celery worker would run; there is no worker on a
# 960 MB box, so it runs here instead.
cd "$APP/backend"
for task in tokens deletions orphans; do
  OUT=$("$PY" -m flask --app wsgi:app maintenance "$task" 2>>"$LOG" | tail -1)
  log "maintenance/$task: $OUT"
done

log "kept: $(find "$BACKUP" -maxdepth 1 -name 'harmony-*.dump' | wc -l) dumps, $(du -sh "$BACKUP" | cut -f1) total"
log "backup done"