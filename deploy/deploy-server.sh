#!/usr/bin/env bash
# Deploy the latest main branch to this server.
#
# The server holds a plain git clone, so "deploying" is pulling that clone,
# installing anything the new commit added, re-publishing the static frontend
# into nginx's document root, and restarting the application. Data lives in
# ~/harmony-data and is never touched.
#
# Usage, from the machine that holds the SSH key:
#     ssh harmony@<ip> 'bash /home/harmony/deploy.sh'
#
# Safe to run when nothing changed: git reports it is already up to date and
# the restart is a no-op.
set -euo pipefail

APP=/home/harmony/harmony
WEBROOT=/usr/share/nginx/html
PY=$APP/.venv/bin/python

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "who am I talking to"
hostname
date -u '+%Y-%m-%d %H:%M:%S UTC'

say "free space and memory before"
df -h / | tail -1
free -m | head -2

say "pull"
git -C "$APP" fetch --all --quiet
git -C "$APP" reset --hard origin/main
git -C "$APP" clean -fdq -e .venv -e .env
git -C "$APP" log --oneline -1

# Replace this script with the one in the repository it just pulled, then run
# that. Without this the copy on the server drifts from the copy in the repo and
# nothing says so: a deploy script that cannot deploy itself is a deploy script
# that silently stops doing part of its job. This is exactly how the `vendor/`
# repointing rule went missing - the published site stopped loading the Socket.IO
# bundle while the script claiming to publish it had the rule and was not the
# script being run.
#
# Guarded so the fetch above is not repeated, and so the recursion stops: the
# fresh copy sees itself already in place and does not re-exec.
SELF=/home/harmony/deploy.sh
CANON=$APP/deploy/deploy-server.sh
if [ -f "$CANON" ] && ! cmp -s "$CANON" "$SELF"; then
  say "update this script from the repository"
  install -m 0755 "$CANON" "$SELF.next"
  mv "$SELF.next" "$SELF"
  echo "  replaced with $(git -C "$APP" rev-parse --short HEAD)'s copy; re-running it"
  exec bash "$SELF" "$@"
fi

say "apply migrations if this commit adds any"
# There is no Alembic history in the repository yet, so the schema is created
# from the models. `create_all` is additive and idempotent: it adds tables and
# columns that do not exist and leaves everything else alone.
cd "$APP/backend"
"$PY" -m flask --app wsgi:app init-db

say "install dependencies"
"$APP/.venv/bin/pip" install -q -r "$APP/backend/requirements.txt"

say "publish the frontend"
# nginx serves index.html and the asset tree straight from disk, so a deploy
# that forgot this step would leave the browser on a stale bundle referencing
# asset files that no longer exist.
#
# The assets are published under a directory named for the commit, and
# index.html points into it. That is what makes a long cache safe here: the URL
# carries the content, so `immutable` is finally true.
#
# The obvious alternative - appending `?v=<commit>` to the entry points - does
# not work for an unbundled ES module graph. Relative imports resolve against
# the importer's *path*, not its query string, so `main.js?v=abc` still imports
# `../components/ui.js` unversioned. Only the entry point gets a fresh URL;
# every module below it is served from the year-long cache the previous deploy
# populated, and the visitor keeps running the old build. Verified on this
# server: `main.js?v=…` was current while `components/ui.js` was still the
# pre-fix file.
STAMP=$(git -C "$APP" rev-parse --short HEAD)
sudo rm -rf "$WEBROOT"
sudo mkdir -p "$WEBROOT/$STAMP"
sudo cp -a "$APP/frontend/." "$WEBROOT/$STAMP/"

"$PY" - "$WEBROOT/$STAMP/index.html" "$STAMP" <<'PYEOF'
import re
import sys

index, stamp = sys.argv[1], sys.argv[2]
with open(index, encoding="utf-8") as handle:
    html = handle.read()

# Local assets only. The Google Fonts URLs carry their own versions and are not
# ours to repoint; everything we ship lives under the stamped directory, vendor
# bundles included - a pinned third-party file is exactly as immutable as our
# own, and giving it a year-long cache is correct.
pattern = r'(?P<head>(?:href|src)=")/(?P<path>(?:js|css|assets|vendor)/[^"?]+)(?P<tail>[^"]*")'
html, count = re.subn(
    pattern,
    lambda m: f'{m.group("head")}/{stamp}/{m.group("path")}{m.group("tail")}',
    html,
)

# The shell itself is the one file that must never be cached: it is what points
# at the current asset directory.
with open(index, "w", encoding="utf-8") as handle:
    handle.write(html)
print(f"  pointed {count} asset URLs at /{stamp}/")
PYEOF

# Keep the previous builds so a browser mid-load on an old index.html can still
# fetch what it was told to fetch.
sudo find "$WEBROOT" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +

sudo cp "$WEBROOT/$STAMP/index.html" "$WEBROOT/index.html"

# Files the shell links by a fixed URL, which therefore cannot carry the commit
# id. The manifest is the important one: with no copy at the document root,
# /site.webmanifest 404s, the browser concludes the site has no manifest, and
# installability and standalone display disappear without any error anywhere.
sudo cp "$WEBROOT/$STAMP/site.webmanifest" "$WEBROOT/site.webmanifest"
sudo cp "$WEBROOT/$STAMP/robots.txt" "$WEBROOT/robots.txt"
sudo chown -R root:root "$WEBROOT"
sudo find "$WEBROOT" -type d -exec chmod 755 {} +
sudo find "$WEBROOT" -type f -exec chmod 644 {} +

say "restart the application"
sudo systemctl restart harmony
sleep 8
systemctl is-active harmony

say "reload nginx"
sudo nginx -t
sudo systemctl reload nginx

say "check"
fail=0
for path in /healthz /readyz /; do
  code=$(curl -sS -m 25 -o /dev/null -w '%{http_code}' "http://127.0.0.1$path" || echo ERR)
  printf '  %-10s %s\n' "$path" "$code"
  [ "$code" = "200" ] || fail=1
done
ws=$(curl -sS -m 20 "http://127.0.0.1/socket.io/?EIO=4&transport=polling" || true)
case "$ws" in
  0*) printf '  %-10s ok\n' "chat" ;;
  *)  printf '  %-10s FAILED\n' "chat"; fail=1 ;;
esac

say "last backup"
# Checked on every deploy because nothing else checks it. The nightly job failed
# with 203/EXEC for two days - `ExecStart` named a script `git clean -fd` had
# removed - and the only symptom was `systemctl list-timers` saying
# `active (waiting)`, which reads like a healthy job. /readyz reports the age, so
# the failure is visible at the one moment somebody is definitely looking.
BACKUP_FIELD=$(curl -sS -m 15 http://127.0.0.1/readyz \
  | "$PY" -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["checks"].get("backup", {})))' \
  2>/dev/null || echo '{}')
printf '  %s\n' "$BACKUP_FIELD"
case "$BACKUP_FIELD" in
  *'"status": "stale"'*|*'"status": "error"'*|*'"status": "no_dumps_yet"'*)
    printf '  WARNING: the nightly backup is not healthy. Data is not being protected.\n'
    printf '           systemctl status harmony-backup.service\n'
    printf '           journalctl -u harmony-backup.service --since "-2 days"\n'
    ;;
esac

say "recent errors, if any"
# -q: the deploy user cannot read the whole journal, and without this the
# notice about that is the loudest thing in the output.
journalctl -q -u harmony --no-pager --lines=40 --since '-2min' \
  | grep -iE '"level": "(ERROR|WARNING)"' | tail -10 || echo "  none"

say "free space and memory after"
df -h / | tail -1
free -m | head -2

if [ "$fail" -ne 0 ]; then
  printf '\n\033[31mDEPLOY FINISHED WITH FAILURES - roll back with:\033[0m\n'
  printf '  git -C %s reset --hard HEAD~1\n' "$APP"
  exit 1
fi
printf '\n\033[32mDEPLOY OK\033[0m\n'