"""Тесты для бюджетера лимитов и квот API в Redis."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from app.config import settings
from app.models import QuotaLedger
from app.redis_client import redis_client
from app.services.budgeter import (
    acquire_token,
    consume_youtube_search_quota,
    get_pacific_today,
    get_remaining_youtube_searches,
    is_circuit_breaker_active,
    seconds_until_pacific_midnight,
    trip_circuit_breaker,
)


def test_pacific_time_helpers():
    today = get_pacific_today()
    assert len(today) == 10  # YYYY-MM-DD
    assert today.count("-") == 2

    ttl = seconds_until_pacific_midnight()
    assert 0 < ttl <= 86400


@pytest.mark.asyncio
async def test_youtube_search_quota(db_session):
    await db_session.execute(delete(QuotaLedger))
    await db_session.commit()

    today = get_pacific_today()
    redis_key = f"quota:youtube:{today}"
    # Очищаем ключ перед тестом
    await redis_client.delete(redis_key)

    # Мокаем лимит дневных поисков до 2
    with patch.object(settings, "youtube_search_quota_daily", 2):
        # 1-й запрос должен пройти
        assert await consume_youtube_search_quota(db_session) is True
        # 2-й запрос должен пройти
        assert await consume_youtube_search_quota(db_session) is True
        # 3-й запрос должен вернуть False (лимит превышен)
        assert await consume_youtube_search_quota(db_session) is False

        # Проверяем, сколько осталось
        remaining = await get_remaining_youtube_searches()
        assert remaining == 0

        # Проверяем, что записи попали в QuotaLedger
        ledger_rows = list(await db_session.scalars(select(QuotaLedger)))
        assert len(ledger_rows) == 3
        assert ledger_rows[0].outcome == "success"
        assert ledger_rows[1].outcome == "success"
        assert ledger_rows[2].outcome == "quota_exceeded"


@pytest.mark.asyncio
async def test_token_bucket_rate_limiter():
    platform = "test_platform"
    redis_key = f"rate_limit:{platform}"
    await redis_client.delete(redis_key)

    # Емкость 2 токена, восполнение 10 токенов в секунду
    # Списываем 1 токен
    assert await acquire_token(platform, capacity=2, refill_rate=10, consume=1) is True
    # Списываем еще 1 токен
    assert await acquire_token(platform, capacity=2, refill_rate=10, consume=1) is True
    # Третий запрос сразу же должен быть отклонен (токены кончились)
    assert await acquire_token(platform, capacity=2, refill_rate=10, consume=1) is False

    # Спим 0.1 секунды (должен восполниться 1 токен)
    await asyncio.sleep(0.12)
    assert await acquire_token(platform, capacity=2, refill_rate=10, consume=1) is True
    # Сразу же после этого снова False
    assert await acquire_token(platform, capacity=2, refill_rate=10, consume=1) is False


@pytest.mark.asyncio
async def test_circuit_breaker():
    platform = "test_breaker"
    redis_key = f"circuit_breaker:{platform}:active"
    await redis_client.delete(redis_key)

    assert await is_circuit_breaker_active(platform) is False

    # Триггерим предохранитель с отправкой алерта админам
    mock_send = AsyncMock()
    with patch("app.services.notify.send_admin_alert", mock_send):
        await trip_circuit_breaker(platform, duration_seconds=2)
        assert await is_circuit_breaker_active(platform) is True
        mock_send.assert_called_once()

        # Повторный вызов не должен слать повторный алерт (предотвращение флуда)
        mock_send.reset_mock()
        await trip_circuit_breaker(platform, duration_seconds=2)
        mock_send.assert_not_called()

    # Ждем истечения TTL предохранителя
    await asyncio.sleep(2.1)
    assert await is_circuit_breaker_active(platform) is False
