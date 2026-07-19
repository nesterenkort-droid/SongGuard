# TrackGuard

Система обнаружения пиратства музыкальных треков: мониторит YouTube / Spotify /
Apple Music, находит нелегальные копии треков (в т.ч. slowed/sped/nightcore),
шлёт уведомления в Telegram и веб-дашборд, готовит пакеты жалоб.

Полное техническое задание и план по вехам — в [PLAN.md](PLAN.md).

## Статус

**Веха M0 — каркас и опора.** Готов рабочий скелет: FastAPI + PostgreSQL + Redis +
воркер (arq) + Telegram-бот (заглушка) за nginx, всё в Docker Compose, страница
здоровья, миграции, тесты.

## Стек

Python 3.12 · FastAPI · SQLAlchemy 2 + Alembic · PostgreSQL 16 · Redis 7 ·
arq (воркер) · aiogram 3 (бот) · Jinja2 + Tailwind/DaisyUI + HTMX · nginx ·
Docker Compose · uv (пакеты).

## Быстрый старт (локально)

Нужен только Docker с Compose. Python на хосте НЕ требуется.

```bash
cp .env.example .env          # при желании отредактируйте
docker compose up -d --build  # соберёт образ, накатит миграции, поднимет всё
```

Откройте страницу здоровья: <http://localhost:8080> (порт задаётся `NGINX_HTTP_PORT`).
JSON для мониторинга: <http://localhost:8080/healthz>.

### Тесты

```bash
docker compose run --rm web pytest -q
docker compose run --rm web ruff check .
```

### Полезные команды

```bash
docker compose ps                       # статус контейнеров
docker compose logs -f web              # логи веб-сервиса
docker compose run --rm migrate         # накатить миграции вручную
docker compose run --rm seed            # перезалить baseline-данные
docker compose down                     # остановить всё (данные в томах сохраняются)
```

## Структура

```
app/
  config.py          конфиг из окружения
  db.py              async SQLAlchemy engine/session
  redis_client.py    общий Redis-клиент
  health.py          проверки компонентов (БД, Redis, воркер, бот)
  main.py            FastAPI-приложение (web)
  models/            ORM-модели
  web/routes/        health + страницы
  web/templates/     Jinja2 (тёмная тема, DaisyUI)
  worker/            arq-воркер + задачи (пока heartbeat)
  bot/               Telegram-бот (aiogram, заглушка M0)
alembic/             миграции
scripts/seed.py      baseline-данные
tests/               pytest (гермётичные)
docker-compose.yml   вся оркестрация
Dockerfile           общий образ для всех Python-сервисов
nginx/nginx.conf     reverse proxy
deploy.sh            деплой/обновление на VPS
```

## Деплой на сервер

На VPS (Ubuntu LTS, Docker установлен), в каталоге проекта:

```bash
cp .env.example .env   # заполнить: SECRET_KEY, TELEGRAM_BOT_TOKEN, NGINX_HTTP_PORT=80
./deploy.sh
```
