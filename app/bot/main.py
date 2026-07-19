"""Telegram bot entrypoint (aiogram 3).

Handles passwordless deep-link auth (M1), and from M3 on: finding cards with action
buttons, /check (manual scan trigger), /status, an outbox-flush loop that delivers
queued notifications (instant + daily/weekly digests, restart-safe via dedupe keys),
and admin alerts on health state changes (PLAN.md §9, §12).

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
OUTBOX_FLUSH_INTERVAL = 15  # seconds; keeps "finding -> card" well under the 60s target
HEALTH_CHECK_INTERVAL = 60  # seconds
LAST_HEALTH_KEY = "bot:last_health_status"


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


def _build_markup(rows):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb_rows = []
    for row in rows:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=b.text,
                    callback_data=b.callback_data,
                    url=b.url,
                )
                for b in row
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


async def _send_finding_card(bot, chat_id: int, finding_id: int, session) -> bool:
    """Render and send one finding card. Returns False if the finding vanished."""
    from app.bot.cards import build_finding_card
    from app.services import detection

    ctx = await detection.get_finding_context(session, finding_id)
    if ctx is None:
        return False
    finding, cand, track, artist = ctx
    text, rows = build_finding_card(finding, cand, track, artist.name)
    await bot.send_message(chat_id, text, reply_markup=_build_markup(rows), parse_mode="HTML")
    return True


async def _outbox_flush_loop(bot) -> None:
    """Drain due notification_outbox rows: single row -> full card, grouped rows for
    the same user (a daily/weekly digest slot) -> one summary message.

    Each row commits individually right after a successful send, so a crash mid-loop
    re-processes only the not-yet-sent tail — never a duplicate already-sent message.
    """
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import MAX_OUTBOX_ATTEMPTS, OUTBOX_FAILED, OUTBOX_SENT, NotificationOutbox, User

    while True:
        try:
            async with SessionLocal() as session:
                now = datetime.now(UTC)
                due = list(
                    await session.scalars(
                        select(NotificationOutbox)
                        .where(
                            NotificationOutbox.status == "pending",
                            NotificationOutbox.scheduled_for <= now,
                        )
                        .order_by(NotificationOutbox.user_id, NotificationOutbox.id)
                        .limit(200)
                    )
                )
                by_user: dict[int, list] = {}
                for row in due:
                    by_user.setdefault(row.user_id, []).append(row)

                for user_id, rows in by_user.items():
                    user = await session.get(User, user_id)
                    if user is None:
                        for row in rows:
                            row.status = OUTBOX_FAILED
                            row.last_error = "пользователь удалён"
                        await session.commit()
                        continue
                    try:
                        if len(rows) == 1 and rows[0].finding_id:
                            ok = await _send_finding_card(
                                bot, user.tg_user_id, rows[0].finding_id, session
                            )
                            if not ok:
                                rows[0].status = OUTBOX_FAILED
                                rows[0].last_error = "находка удалена"
                        else:
                            await _send_digest(bot, user.tg_user_id, rows, session)
                        for row in rows:
                            if row.status == "pending":
                                row.status = OUTBOX_SENT
                                row.sent_at = now
                        await session.commit()
                    except Exception as exc:  # noqa: BLE001 - one user's failure must not block others
                        logger.warning("outbox send failed for user %s: %s", user_id, exc)
                        for row in rows:
                            row.attempts += 1
                            row.last_error = str(exc)[:500]
                            if row.attempts >= MAX_OUTBOX_ATTEMPTS:
                                row.status = OUTBOX_FAILED
                        await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the flush loop itself must never die
            logger.exception("outbox flush loop iteration failed")
        await asyncio.sleep(OUTBOX_FLUSH_INTERVAL)


async def _send_digest(bot, chat_id: int, rows, session) -> None:
    from app.services import detection

    lines = [f"📬 Дайджест: {len(rows)} находок"]
    for row in rows:
        if not row.finding_id:
            continue
        ctx = await detection.get_finding_context(session, row.finding_id)
        if ctx is None:
            continue
        finding, cand, track, _artist = ctx
        lines.append(f"• «{cand.title}» → «{track.title}» — score {finding.score} ({finding.band})")
    lines.append(f"\nПодробности: {settings.base_url}/findings")
    await bot.send_message(chat_id, "\n".join(lines))


async def _health_alert_loop(bot) -> None:
    """Notify admins only on a health *transition* (not every poll) — avoids alert
    fatigue while still catching a dead scan/quota/key promptly (PLAN.md §9, §12)."""
    from app.health import gather_health

    while True:
        try:
            overall, components = await gather_health()
            previous = await redis_client.get(LAST_HEALTH_KEY)
            if previous != overall:
                await redis_client.set(LAST_HEALTH_KEY, overall)
                if previous is not None:  # skip the very first check after startup
                    await _notify_admins_health_change(bot, previous, overall, components)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("health alert loop iteration failed")
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)


async def _notify_admins_health_change(bot, previous: str, overall: str, components) -> None:
    if overall == "ok":
        text = "✅ Всё снова в порядке."
    else:
        bad = [c for c in components if c.status != "ok"]
        detail = "\n".join(f"• {c.label}: {c.detail}" for c in bad)
        title = "🔴 Критическая проблема" if overall == "down" else "🟡 Есть проблема"
        text = f"{title}\n{detail}"
    for tg_id in settings.admin_ids:
        try:
            await bot.send_message(tg_id, text)
        except Exception:  # noqa: BLE001 - one bad admin id must not block others
            logger.warning("failed to alert admin %s", tg_id)


async def _run_polling() -> None:
    """Start aiogram long-polling with the full M3 command/callback set."""
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import CommandObject, CommandStart
    from aiogram.types import CallbackQuery, Message
    from sqlalchemy import select

    from app.auth.service import confirm_start
    from app.bot.cards import build_finding_card, parse_callback_data
    from app.db import SessionLocal
    from app.models import Artist, Finding, Track, User
    from app.services import catalog, detection

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
            "🛡️ TrackGuard на связи. Чтобы войти на сайт, откройте ссылку входа с сайта.\n"
            "Команды: /check — запустить скан артиста, /status — статус системы."
        )

    async def _load_user(session, tg_user_id: int) -> User | None:
        return await session.scalar(select(User).where(User.tg_user_id == tg_user_id))

    @dp.message(F.text == "/check")
    async def on_check(message: Message) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        async with SessionLocal() as session:
            user = await _load_user(session, message.from_user.id)
            if user is None:
                await message.answer("Сначала войдите на сайте.")
                return
            artists = await catalog.list_artists_for_user(session, user)
        if not artists:
            await message.answer("У вас пока нет артистов в каталоге.")
            return
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"{a.name} ({n})", callback_data=f"check:{a.id}")]
                for a, n in artists
            ]
        )
        await message.answer("Какого артиста просканировать?", reply_markup=kb)

    @dp.callback_query(F.data.startswith("check:"))
    async def on_check_artist(callback: CallbackQuery) -> None:
        from arq import create_pool
        from arq.connections import RedisSettings

        artist_id = int(callback.data.split(":", 1)[1])
        async with SessionLocal() as session:
            user = await _load_user(session, callback.from_user.id)
            if user is None or not await catalog.user_can_access_artist(session, user, artist_id):
                await callback.answer("Нет доступа.", show_alert=True)
                return
            artist = await session.get(Artist, artist_id)
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        try:
            await pool.enqueue_job("scan_catalog", artist_id, user.id)
        finally:
            await pool.aclose()
        await callback.answer("Скан запущен")
        await callback.message.edit_text(f"🔍 Скан «{artist.name}» поставлен в очередь.")

    @dp.message(F.text == "/status")
    async def on_status(message: Message) -> None:
        from app.health import gather_health

        async with SessionLocal() as session:
            user = await _load_user(session, message.from_user.id)
            if user is None:
                await message.answer("Сначала войдите на сайте.")
                return
            artists = await catalog.list_artists_for_user(session, user)
            track_total = sum(n for _a, n in artists)

        overall, components = await gather_health()
        status_ru = {"ok": "🟢 всё в порядке", "degraded": "🟡 частично", "down": "🔴 авария"}
        lines = [
            f"Статус системы: {status_ru.get(overall, overall)}",
        ]
        for c in components:
            mark = "✅" if c.status == "ok" else ("⚠️" if c.status == "warn" else "🔴")
            lines.append(f"  {mark} {c.label}: {c.detail}")
        lines.append(f"\nВаш каталог: {len(artists)} артист(ов), {track_total} трек(ов)")
        await message.answer("\n".join(lines))

    @dp.callback_query(F.data.startswith("f:"))
    async def on_finding_action(callback: CallbackQuery) -> None:
        parsed = parse_callback_data(callback.data)
        if parsed is None:
            await callback.answer()
            return
        action, finding_id = parsed
        async with SessionLocal() as session:
            user = await _load_user(session, callback.from_user.id)
            finding = await session.get(Finding, finding_id)
            if user is None or finding is None:
                await callback.answer("Не найдено.", show_alert=True)
                return
            track = await session.get(Track, finding.track_id)
            if not await catalog.user_can_access_artist(session, user, track.primary_artist_id):
                await callback.answer("Нет доступа.", show_alert=True)
                return

            from app.models import WL_CHANNEL

            try:
                if action == "wl":
                    await detection.add_whitelist_from_finding(
                        session, finding, WL_CHANNEL, actor_user_id=user.id
                    )
                else:
                    await detection.transition(session, finding, action, actor_user_id=user.id)
            except ValueError:
                await callback.answer("Неизвестное действие.", show_alert=True)
                return
            await session.commit()

            ctx = await detection.get_finding_context(session, finding_id)
            if ctx is not None:
                f2, cand, trk, artist = ctx
                text, rows = build_finding_card(f2, cand, trk, artist.name)
                try:
                    await callback.message.edit_text(
                        text, reply_markup=_build_markup(rows), parse_mode="HTML"
                    )
                except Exception:  # noqa: BLE001 - "message not modified" etc. are harmless
                    pass
        await callback.answer("Готово")

    background = [
        asyncio.create_task(_outbox_flush_loop(bot)),
        asyncio.create_task(_health_alert_loop(bot)),
    ]
    try:
        logger.info("bot: starting long-polling")
        await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)


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
