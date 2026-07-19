#!/usr/bin/env bash
# Deploy / update TrackGuard on the VPS.
# Usage:  ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy .env.example to .env and fill it in." >&2
  exit 1
fi

echo "==> Pulling latest code"
git pull --ff-only

echo "==> Building images"
docker compose build

echo "==> Starting datastores + running migrations"
# 'migrate' and 'seed' are one-shot; 'up -d' runs them in dependency order,
# then brings up web/worker/bot/nginx.
docker compose up -d

echo "==> Applying any new migrations (explicit, in case images were cached)"
docker compose run --rm migrate

echo "==> Current status"
docker compose ps
echo "==> Done. Health: curl -fsS http://localhost:\${NGINX_HTTP_PORT:-80}/healthz"
