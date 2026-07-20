#!/usr/bin/env bash
# Nightly backup: pg_dump (database) + appdata volume (audio/covers/evidence) into
# a restic repository (PLAN.md §12). Run on the HOST (not inside a container) via
# cron; talks to Docker only through `docker compose`/`docker run`, so it works the
# same whether the restic repo is local disk (dev rehearsal) or Hetzner Storage Box
# (prod, via rclone/sftp — set RESTIC_REPOSITORY accordingly).
#
# Usage:
#   bash scripts/backup.sh          # take a backup
#   bash scripts/backup.sh --list   # list existing snapshots (freshness check)
#
# Required env (from .env or the shell):
#   RESTIC_REPOSITORY   e.g. /srv/trackguard-backups or sftp:user@host:/backups
#   RESTIC_PASSWORD     repo encryption password (KEEP THIS SAFE — lost password
#                        = unrecoverable backups)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a; source .env; set +a
fi

: "${RESTIC_REPOSITORY:?Задайте RESTIC_REPOSITORY в .env (путь к репозиторию бэкапов)}"
: "${RESTIC_PASSWORD:?Задайте RESTIC_PASSWORD в .env (пароль шифрования — не теряйте его)}"

if ! command -v restic >/dev/null 2>&1; then
  echo "ERROR: restic не установлен. На Debian/Ubuntu: apt-get install restic" >&2
  exit 1
fi

# Repo may not exist yet on first run (fresh Storage Box / fresh dev rehearsal dir).
restic snapshots >/dev/null 2>&1 || restic init

if [ "${1:-}" = "--list" ]; then
  restic snapshots --tag trackguard
  exit 0
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

echo "==> Дамп базы данных"
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-trackguard}" "${POSTGRES_DB:-trackguard}" \
  > "$WORKDIR/db.sql"

echo "==> Архивация appdata (аудио, обложки, evidence)"
docker run --rm \
  -v trackguard_appdata:/data:ro \
  -v "$WORKDIR":/backup \
  alpine tar czf /backup/appdata.tar.gz -C /data .

echo "==> Снимок в restic"
restic backup "$WORKDIR/db.sql" "$WORKDIR/appdata.tar.gz" \
  --tag trackguard --tag "$(date +%Y-%m-%d)"

echo "==> Удаление старых снимков (retention: 14 daily / 8 weekly / 6 monthly)"
restic forget --tag trackguard \
  --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune

echo "✅ Бэкап завершён: $(date -Iseconds)"
