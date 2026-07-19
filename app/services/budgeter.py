"""Бюджетер квот и лимитов API на базе Redis.

Реализует:
1. Атомарное списание дневного лимита поисковых запросов YouTube с учетом сброса
   квот Google в полночь по времени US/Pacific.
2. Token-Bucket лимитер частоты запросов к Spotify/iTunes для защиты от 429 ошибок.
3. Логику автоматического отключения очередей («предохранитель» / circuit breaker)
   при перегрузках API.

Все комментарии и лог-сообщения написаны на русском языке.
"""

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import QuotaLedger
from app.redis_client import redis_client
from app.services import notify

logger = logging.getLogger("trackguard.budgeter")

# Lua-скрипт для атомарной проверки и списания дневной квоты поиска YouTube.
YOUTUBE_QUOTA_LUA = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])

local current = redis.call('GET', key)
if not current then
    redis.call('SET', key, limit - 1, 'EX', ttl)
    return 1
else
    local val = tonumber(current)
    if val > 0 then
        redis.call('DECR', key)
        return 1
    else
        return 0
    end
end
"""

# Lua-скрипт для атомарного Token-Bucket лимитера.
TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local current_time = tonumber(ARGV[3])
local consume_count = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_updated')
local tokens = tonumber(data[1])
local last_updated = tonumber(data[2])

if not tokens then
    tokens = capacity
    last_updated = current_time
else
    local delta = (current_time - last_updated) * refill_rate
    tokens = math.min(capacity, tokens + delta)
end

if tokens >= consume_count then
    tokens = tokens - consume_count
    redis.call('HMSET', key, 'tokens', tokens, 'last_updated', current_time)
    redis.call('EXPIRE', key, 86400)
    return 1
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_updated', current_time)
    redis.call('EXPIRE', key, 86400)
    return 0
end
"""


def get_pacific_today() -> str:
    """Возвращает текущую дату в часовом поясе US/Pacific в формате YYYY-MM-DD."""
    tz = ZoneInfo("US/Pacific")
    return datetime.now(tz).date().isoformat()


def seconds_until_pacific_midnight() -> int:
    """Вычисляет количество секунд, оставшихся до полуночи по времени US/Pacific."""
    tz = ZoneInfo("US/Pacific")
    now = datetime.now(tz)
    tomorrow = datetime(now.year, now.month, now.day, tzinfo=tz) + timedelta(days=1)
    diff = tomorrow - now
    return int(diff.total_seconds())


async def consume_youtube_search_quota(session: AsyncSession) -> bool:
    """Попытка списать 1 поисковый запрос YouTube из дневного лимита в Redis.

    Записывает операцию в базу данных (QuotaLedger) как успешную или неуспешную.
    """
    today_str = get_pacific_today()
    redis_key = f"quota:youtube:{today_str}"
    limit = settings.youtube_search_quota_daily
    ttl = seconds_until_pacific_midnight() + 3600  # Буфер в 1 час

    # Выполняем атомарное списание в Redis
    result = await redis_client.eval(YOUTUBE_QUOTA_LUA, 1, redis_key, limit, ttl)
    success = int(result) == 1

    outcome = "success" if success else "quota_exceeded"
    # Записываем лог использования квот в базу данных для статистики и дашбордов
    ledger_entry = QuotaLedger(
        api_name="youtube_search",
        units_consumed=1,
        outcome=outcome,
    )
    session.add(ledger_entry)
    await session.flush()

    if not success:
        logger.warning("Дневной лимит поиска YouTube (%s) исчерпан на дату %s", limit, today_str)

    return success


async def get_remaining_youtube_searches() -> int:
    """Возвращает оставшееся количество поисков YouTube на сегодня."""
    today_str = get_pacific_today()
    redis_key = f"quota:youtube:{today_str}"
    val = await redis_client.get(redis_key)
    if val is None:
        return settings.youtube_search_quota_daily
    return max(0, int(val))


async def acquire_token(
    platform: str, capacity: float, refill_rate: float, consume: float = 1.0
) -> bool:
    """Проверяет лимиты частоты запросов через алгоритм Token Bucket в Redis.

    Возвращает True, если запрос разрешен, или False, если лимит превышен.
    """
    redis_key = f"rate_limit:{platform}"
    current_time = time.time()

    result = await redis_client.eval(
        TOKEN_BUCKET_LUA, 1, redis_key, capacity, refill_rate, current_time, consume
    )
    return int(result) == 1


async def trip_circuit_breaker(platform: str, duration_seconds: int = 300) -> None:
    """Активирует предохранитель для платформы при перегрузке (ошибка 429 или quotaExceeded).

    Блокирует запросы к платформе на указанное время и шлет предупреждение админу.
    """
    redis_key = f"circuit_breaker:{platform}:active"
    was_active = await redis_client.exists(redis_key)
    await redis_client.set(redis_key, "1", ex=duration_seconds)
    logger.warning(
        "Предохранитель сработал для платформы %s на %s сек.",
        platform,
        duration_seconds,
    )

    if not was_active:
        # Отправляем оповещение только при первом переходе состояния (защита от флуда)
        await notify.send_admin_alert(
            f"⚠️ **Сработал предохранитель для {platform.upper()}**\n\n"
            f"Получена ошибка лимитов (429/quotaExceeded). Запросы к платформе временно "
            f"заблокированы на {duration_seconds // 60} мин."
        )


async def is_circuit_breaker_active(platform: str) -> bool:
    """Возвращает True, если предохранитель активен и запросы заблокированы."""
    redis_key = f"circuit_breaker:{platform}:active"
    return await redis_client.exists(redis_key) > 0
