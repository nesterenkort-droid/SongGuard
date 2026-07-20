"""Component health checks.

Every long-running piece of the system reports here so a single page (and the
`/healthz` endpoint, and the Telegram admin alerts later) can answer "is anything
broken?" in plain language. Database and Redis are *critical*: if either is down
the whole app is down. Worker and bot are reported via Redis heartbeats — if they
go stale we degrade but stay up.
"""

import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.redis_client import redis_client

DISK_WARN_PERCENT = 80

# Status values, ordered by severity.
OK = "ok"
WARN = "warn"
DOWN = "down"


@dataclass
class Component:
    name: str  # machine key, e.g. "database"
    label: str  # human RU label, e.g. "База данных"
    status: str  # OK | WARN | DOWN
    detail: str  # short human-readable note (RU)
    critical: bool  # if True, DOWN drags the whole system to DOWN

    def as_dict(self) -> dict:
        return asdict(self)


async def check_database() -> Component:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return Component("database", "База данных", OK, "PostgreSQL отвечает", True)
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return Component("database", "База данных", DOWN, f"нет связи: {exc}", True)


async def check_redis() -> Component:
    try:
        await redis_client.ping()
        return Component("redis", "Redis", OK, "PING успешен", True)
    except Exception as exc:  # noqa: BLE001
        return Component("redis", "Redis", DOWN, f"нет связи: {exc}", True)


async def _heartbeat_component(name: str, label: str, key: str) -> Component:
    """Read a `heartbeat:*` key written by the worker/bot and judge its freshness."""
    try:
        raw = await redis_client.get(key)
    except Exception as exc:  # noqa: BLE001
        return Component(name, label, DOWN, f"Redis недоступен: {exc}", False)

    if not raw:
        return Component(name, label, WARN, "ещё не отчитывался", False)

    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return Component(name, label, WARN, "некорректный heartbeat", False)

    age = (datetime.now(UTC) - last).total_seconds()
    if age <= settings.heartbeat_ttl_seconds:
        return Component(name, label, OK, f"активен {int(age)} с назад", False)
    return Component(name, label, WARN, f"молчит {int(age)} с", False)


def check_disk() -> Component:
    """PLAN.md §12: alert at 80% disk usage (audio/covers/evidence live under
    settings.data_dir, the volume most likely to fill up over time)."""
    try:
        usage = shutil.disk_usage(settings.data_dir)
    except FileNotFoundError:
        # data_dir doesn't exist yet (e.g. fresh checkout without a running
        # container) — not an operational problem worth alerting on.
        return Component("disk", "Диск", OK, "точка монтирования ещё не создана", False)
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return Component("disk", "Диск", DOWN, f"ошибка проверки: {exc}", False)

    percent_used = round(100 * usage.used / usage.total, 1)
    free_gb = usage.free / (1024**3)
    detail = f"занято {percent_used}% ({free_gb:.1f} ГБ свободно)"
    if percent_used >= DISK_WARN_PERCENT:
        return Component("disk", "Диск", WARN, detail, False)
    return Component("disk", "Диск", OK, detail, False)


async def check_ytdlp() -> Component:
    """PLAN.md §12: the weekly canary (services/ops.py) flags this key when
    YouTube changes break yt-dlp's extractor — audio signals silently degrade
    otherwise, with no other visible symptom."""
    from app.services.ops import YTDLP_DEGRADED_KEY

    try:
        since_raw = await redis_client.get(YTDLP_DEGRADED_KEY)
    except Exception as exc:  # noqa: BLE001
        return Component("ytdlp", "yt-dlp (аудио YouTube)", DOWN, f"Redis недоступен: {exc}", False)

    if not since_raw:
        return Component("ytdlp", "yt-dlp (аудио YouTube)", OK, "канарейка проходит", False)
    try:
        since = datetime.fromisoformat(since_raw)
        detail = f"аудио деградировано с {since.date().isoformat()}"
    except ValueError:
        detail = "аудио деградировано (дата неизвестна)"
    return Component("ytdlp", "yt-dlp (аудио YouTube)", WARN, detail, False)


async def check_worker() -> Component:
    return await _heartbeat_component("worker", "Воркер (сканер)", "heartbeat:worker")


async def check_bot() -> Component:
    return await _heartbeat_component("bot", "Telegram-бот", "heartbeat:bot")


async def gather_health() -> tuple[str, list[Component]]:
    """Run every check and compute an overall status.

    overall = DOWN if any critical component is DOWN,
              "degraded" if any component is WARN/DOWN,
              OK otherwise.
    """
    components = [
        await check_database(),
        await check_redis(),
        check_disk(),
        await check_ytdlp(),
        await check_worker(),
        await check_bot(),
    ]

    overall = OK
    if any(c.status == DOWN and c.critical for c in components):
        overall = DOWN
    elif any(c.status in (WARN, DOWN) for c in components):
        overall = "degraded"
    return overall, components
