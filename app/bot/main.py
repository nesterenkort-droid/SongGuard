"""Telegram bot entrypoint (aiogram 3).

M0 scope: the container must start and stay healthy whether or not a token is
configured, and it must publish a `heartbeat:bot` key so the health page can see
it. Finding cards, inline buttons, /check, /status and digests arrive in M3.

Run with:  python -m app.bot.main
"""

import asyncio
import logging
from datetime import UTC, datetime

from app.config import settings
from app.redis_client import redis_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trackguard.bot")

HEARTBEAT_INTERVAL = 60  # seconds


async def _heartbeat_loop() -> None:
    """Keep stamping `heartbeat:bot` so the health page reports the bot as alive."""
    while True:
        now = datetime.now(UTC).isoformat()
        try:
            await redis_client.set(
                "heartbeat:bot", now, ex=settings.heartbeat_ttl_seconds * 2
            )
        except Exception:  # noqa: BLE001
            logger.exception("failed to write bot heartbeat")
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def _run_polling() -> None:
    """Start aiogram long-polling.

    Handles deep-link `/start login-<nonce>` / `join-<nonce>` for passwordless web
    auth. Full command set (finding cards, /check, /status) lands in M3.
    """
    from aiogram import Bot, Dispatcher
    from aiogram.filters import CommandObject, CommandStart
    from aiogram.types import Message

    from app.auth.service import confirm_start
    from app.db import SessionLocal

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()

    @dp.message(CommandStart(deep_link=True))
    async def on_start_deeplink(message: Message, command: CommandObject) -> None:
        payload = command.args or ""
        async with SessionLocal() as session:
            result = await confirm_start(
                session, payload, message.from_user.id, message.from_user.full_name
            )
        await message.answer(result.get("message", "Готово."))

    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        await message.answer(
            "🛡️ TrackGuard на связи. Чтобы войти на сайт, откройте ссылку входа с сайта. "
            "Полноценные команды появятся на этапе M3."
        )

    logger.info("bot: starting long-polling")
    await dp.start_polling(bot)


async def main() -> None:
    # Heartbeat runs regardless of whether the bot can actually poll Telegram.
    heartbeat_task = asyncio.create_task(_heartbeat_loop())

    if not settings.telegram_bot_token:
        logger.warning(
            "TELEGRAM_BOT_TOKEN не задан — бот простаивает (heartbeat идёт). "
            "Задайте токен от @BotFather, чтобы включить polling."
        )
        await heartbeat_task
        return

    # Retry polling on failure so transient network issues (or no outbound internet
    # in local dev) don't crash-loop the container; the heartbeat keeps running.
    while True:
        try:
            await _run_polling()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bot polling failed; retrying in 30 s")
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
