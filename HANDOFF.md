# TrackGuard / SongGuard — передача контекста новому агенту

> Скопируй блок ниже в новую сессию Claude Code (рабочая папка `D:\NG`).
> Это ориентир; источник истины — `PLAN.md` и файлы памяти.

```
Продолжаем проект TrackGuard/SongGuard — система обнаружения пиратства музыкальных
треков. Рабочая папка: D:\NG (git-репозиторий; remote github nesterenkort-droid/SongGuard).

ПЕРВЫМ ДЕЛОМ прочитай:
1. D:\NG\PLAN.md — полное ТЗ и план по вехам M0–M7 (источник истины).
2. Память в C:\Users\Lestrikes\.claude\projects\D--NG\memory\ — особенно
   project-trackguard.md, user-profile.md, spotify-rate-limits.md.
3. D:\NG\README.md — быстрый старт.

СТАТУС: M0 (каркас) и M1 (авторизация + каталог) СДЕЛАНЫ, проверены и
закоммичены. Следующая веха — M2 (детекция: DSP-сканеры, дифф страницы
артиста = ярус 0, сигналы ISRC/duration-ratio/лейбл/обложки, скоринг, лента
находок). Скоуп M2 — в PLAN.md §7, §8, §13.

СТЕК: Python 3.12 монолит — FastAPI + SQLAlchemy 2 + Alembic (async) +
PostgreSQL 16 + Redis 7 + arq (worker) + aiogram 3 (bot) + Jinja2/DaisyUI/HTMX +
nginx, всё в Docker Compose, пакеты через uv. Код асинхронный, комментарии
английские; UI и все тексты для пользователя — русские.

КРИТИЧНЫЕ ОСОБЕННОСТИ ОКРУЖЕНИЯ (проверено, не переоткрывай):
- Хост Windows 11 + Docker Desktop + git bash. Python/uv на ХОСТЕ НЕТ — всё
  внутри контейнеров. gh CLI НЕ установлен.
- На хосте прокси 127.0.0.1:10809 (loopback-only). Из контейнеров доступны
  Telegram и Spotify, но НЕ Apple/iTunes (таймаут). netsh portproxy требует
  админа. Итог: живой iTunes-импорт локально не идёт; Spotify — идёт.
- Порт 80 занят → nginx локально на 8080 (NGINX_HTTP_PORT=8080; на проде 80).
  Postgres/Redis наружу не выставлены.
- Секреты в D:\NG\.env (НЕ в git). Настроены: бот @SongGuard_bot и Spotify
  Client ID/Secret. ADMIN_TG_IDS ПОКА ПУСТ — пользователь должен вписать свой
  личный Telegram ID, чтобы стать первым админом.
- Spotify у этого аккаунта: batch-эндпоинты (/albums?ids=, /tracks?ids=) дают
  403 → в app/importers/spotify.py используются singular /albums/{id},
  /tracks/{id}; max limit альбомов = 10. Троттлинг + 429/Retry-After уже есть.
  БЕРЕГИ Spotify API: не долби живой API в дебаге (см. spotify-rate-limits.md).
- В dev-БД есть тестовые данные: admin tg_user_id=42 (Verify Admin) и каталог
  TWXNY. Локальный вход: http://localhost:8080/login → dev-форма → 42.

КАК ЗАПУСКАТЬ И ПРОВЕРЯТЬ (из D:\NG):
- Поднять/пересобрать: docker compose up -d --build
- Тесты:  docker compose run --rm --no-deps -v D:/NG:/src -w /src web pytest -q
- Линтер: docker compose run --rm --no-deps -v D:/NG:/src -w /src web ruff check .
  (-v D:/NG:/src — гонять по свежему коду без пересборки; но для РАБОТАЮЩЕГО
   стека код надо пересобрать: docker compose build web && docker compose up -d)
- Миграции: docker compose run --rm migrate
- Скрипты: scripts/verify_m1.py, verify_http.py, verify_spotify.py
- В PowerShell docker-вывод идёт в stderr — «красный» текст про «Container ...
  Creating» это НЕ ошибка, смотри реальный результат.
- ruff: select E,F,I,UP,B; FastAPI Depends/Form/File в whitelist bugbear;
  alembic/versions и scripts/verify_* исключены из E501.

ПРАВИЛА РАБОТЫ:
- Пользователь НЕ программист, хочет «чтобы было красиво» и надёжно. Всё
  общение и UI — на русском.
- Береги внешние API: не хаммерь в цикле, батчи где можно, соблюдай 429.
- Иди по вехам маленькими проверяемыми шагами; каждый шаг верифицируй реальным
  прогоном, не только тестами.
- Модель данных M2: кандидаты ГЛОБАЛЬНЫЕ, находки ПО ТРЕКАМ (PLAN.md §6).
- Золотой тест-кейс детекции: оригинал YouTube pSkW_rR65-g (HEAVENLY JUMPSTYLE,
  ℗ 0to8) vs пиратки NzL0wDrGtYM / 5bwI-Q0k31A / -HF6cbZ3KGg (Slowed, DistroKid,
  ℗ 13207436 Records DK). 0to8 НИКОГДА не лился через DistroKid → DistroKid на
  их треке = пиратский сигнал.

Начни с чтения PLAN.md и памяти, предложи план реализации M2 и приступай.
```
