#!/usr/bin/env bash
# Update TrackGuard to the latest code (PLAN.md §12). Records the pre-update
# commit so rollback.sh has a target if the update goes wrong.
#
# Usage:
#   bash update.sh              # backup, then update
#   bash update.sh --no-backup  # skip the pre-update backup (faster, riskier)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy .env.example to .env and fill it in." >&2
  exit 1
fi

PREV_COMMIT=$(git rev-parse HEAD)
echo "$PREV_COMMIT" > .last_deploy_commit
echo "==> Текущий коммит записан для отката: $PREV_COMMIT"

set -a; source .env; set +a
if [ "${1:-}" != "--no-backup" ]; then
  if [ -n "${RESTIC_REPOSITORY:-}" ]; then
    echo "==> Резервная копия перед обновлением"
    bash scripts/backup.sh
  else
    echo "==> RESTIC_REPOSITORY не настроен — пропускаем бэкап (см. docs/backup-restore.md)"
  fi
fi

echo "==> Загружаем новый код"
git pull --ff-only

echo "==> Пересобираем образы"
docker compose build

echo "==> Применяем миграции"
docker compose run --rm migrate

echo "==> Перезапускаем сервисы"
docker compose up -d

echo "==> Статус"
docker compose ps
echo "✅ Обновление завершено. Проверьте: curl -fsS http://localhost:\${NGINX_HTTP_PORT:-80}/healthz"
echo "   При проблемах: bash rollback.sh $PREV_COMMIT"
