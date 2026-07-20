#!/usr/bin/env bash
# Restore TrackGuard from a restic snapshot (PLAN.md §12). DESTRUCTIVE: overwrites
# the current database and appdata volume. Requires explicit confirmation.
#
# Usage:
#   bash scripts/restore.sh latest
#   bash scripts/restore.sh <snapshot-id>          # see: bash scripts/backup.sh --list
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a; source .env; set +a
fi

: "${RESTIC_REPOSITORY:?Задайте RESTIC_REPOSITORY в .env}"
: "${RESTIC_PASSWORD:?Задайте RESTIC_PASSWORD в .env}"
SNAPSHOT="${1:?Использование: bash scripts/restore.sh <snapshot-id|latest>}"

echo "⚠️  ВНИМАНИЕ: это перезапишет ТЕКУЩУЮ базу данных и файлы (аудио/обложки/evidence)"
echo "    снимком '$SNAPSHOT'. Текущие данные, не попавшие в бэкап, будут потеряны."
read -r -p "Введите 'ВОССТАНОВИТЬ' для подтверждения: " CONFIRM
if [ "$CONFIRM" != "ВОССТАНОВИТЬ" ]; then
  echo "Отменено."
  exit 1
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

echo "==> Останавливаем web/worker/bot (не должны писать во время восстановления)"
docker compose stop web worker bot

echo "==> Извлекаем снимок из restic"
restic restore "$SNAPSHOT" --target "$WORKDIR" --tag trackguard

DB_DUMP=$(find "$WORKDIR" -name db.sql | head -1)
APPDATA_TAR=$(find "$WORKDIR" -name appdata.tar.gz | head -1)
: "${DB_DUMP:?В снимке не найден db.sql}"
: "${APPDATA_TAR:?В снимке не найден appdata.tar.gz}"

echo "==> Пересоздаём базу данных"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-trackguard}" -d postgres \
  -c "DROP DATABASE IF EXISTS ${POSTGRES_DB:-trackguard};"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-trackguard}" -d postgres \
  -c "CREATE DATABASE ${POSTGRES_DB:-trackguard};"

echo "==> Восстанавливаем данные из дампа"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-trackguard}" "${POSTGRES_DB:-trackguard}" \
  < "$DB_DUMP"

echo "==> Восстанавливаем appdata (аудио, обложки, evidence)"
docker run --rm \
  -v trackguard_appdata:/data \
  -v "$WORKDIR":/backup:ro \
  alpine sh -c "rm -rf /data/* && tar xzf /backup/$(basename "$APPDATA_TAR") -C /data"

echo "==> Применяем миграции (на случай, если бэкап старее текущей схемы)"
docker compose run --rm migrate

echo "==> Запускаем сервисы обратно"
docker compose up -d web worker bot

echo "✅ Восстановление завершено: $(date -Iseconds)"
echo "   Проверьте /healthz и загляните в дашборд, что данные на месте."
