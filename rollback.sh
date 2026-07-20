#!/usr/bin/env bash
# Roll TrackGuard's code back to a previous commit/tag (PLAN.md §12).
#
# Rolls back CODE only — database migrations are not run backward (down-migrations
# are rarely safe in practice). If the update you're rolling back from included a
# schema change that's incompatible with the old code, restore from a backup
# instead (scripts/restore.sh) rather than rolling back code onto a newer schema.
#
# Usage:
#   bash rollback.sh                # roll back to the commit update.sh recorded
#   bash rollback.sh <commit-or-tag>
set -euo pipefail
cd "$(dirname "$0")"

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  if [ ! -f .last_deploy_commit ]; then
    echo "ERROR: не указан коммит и .last_deploy_commit не найден." >&2
    echo "Использование: bash rollback.sh <commit-или-тег>" >&2
    exit 1
  fi
  TARGET=$(cat .last_deploy_commit)
fi

echo "⚠️  Откат кода на: $TARGET (текущий: $(git rev-parse HEAD))"
read -r -p "Продолжить? [y/N] " CONFIRM
if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
  echo "Отменено."
  exit 1
fi

echo "==> Переключаем код"
git checkout "$TARGET"

echo "==> Пересобираем образы"
docker compose build

echo "==> Перезапускаем сервисы (миграции НЕ откатываются — см. заголовок скрипта)"
docker compose up -d

echo "==> Статус"
docker compose ps
echo "✅ Откат завершён. Проверьте: curl -fsS http://localhost:\${NGINX_HTTP_PORT:-80}/healthz"
