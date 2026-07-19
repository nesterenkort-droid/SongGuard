"""Notification scheduling: enqueue outbox rows for a finding, per recipient.

Every artist member is a recipient unless they've unsubscribed (mode is always one
of instant/daily/weekly — there's no "off" to keep this simple; PLAN.md doesn't ask
for one). `enqueue_finding_notifications` is called once per *newly created* finding
(never on a rescore) and fans out to every member with a dedupe-keyed outbox row, so
a bot restart or a duplicate scan never produces a duplicate Telegram message.

Digest timing: instant sends as soon as the flush loop next runs; daily/weekly are
scheduled for the next fixed UTC slot (08:00, and Monday 08:00 respectively) and the
flush loop groups same-slot rows into one digest message per user.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    KIND_FINDING,
    MODE_DAILY,
    MODE_INSTANT,
    MODE_WEEKLY,
    Artist,
    ArtistMember,
    Finding,
    NotificationOutbox,
    Subscription,
)

logger = logging.getLogger("trackguard.notify")

DIGEST_HOUR_UTC = 8  # fixed daily/weekly send slot


async def get_effective_subscription(
    session: AsyncSession, user_id: int, artist_id: int
) -> Subscription | None:
    """Artist-specific subscription wins over the user's global (artist_id=NULL) one.
    None means "no row at all" — the caller applies the instant/no-quiet-hours default.
    """
    specific = await session.scalar(
        select(Subscription).where(
            Subscription.user_id == user_id, Subscription.artist_id == artist_id
        )
    )
    if specific is not None:
        return specific
    return await session.scalar(
        select(Subscription).where(
            Subscription.user_id == user_id, Subscription.artist_id.is_(None)
        )
    )


def _next_daily_slot(now: datetime) -> datetime:
    slot = now.replace(hour=DIGEST_HOUR_UTC, minute=0, second=0, microsecond=0)
    if slot <= now:
        slot += timedelta(days=1)
    return slot


def _next_weekly_slot(now: datetime) -> datetime:
    slot = now.replace(hour=DIGEST_HOUR_UTC, minute=0, second=0, microsecond=0)
    days_ahead = (0 - slot.weekday()) % 7  # 0 = Monday
    slot += timedelta(days=days_ahead)
    if slot <= now:
        slot += timedelta(days=7)
    return slot


def apply_quiet_hours(
    scheduled: datetime, start_hour: int | None, end_hour: int | None
) -> datetime:
    """Push an instant notification past a quiet-hours window (both bounds in UTC).

    Supports overnight windows (e.g. 23 -> 7). If `scheduled` falls inside, return the
    next moment the window ends; otherwise return `scheduled` unchanged.
    """
    if start_hour is None or end_hour is None:
        return scheduled
    hour = scheduled.hour
    in_window = (
        start_hour <= hour < end_hour
        if start_hour < end_hour
        else (hour >= start_hour or hour < end_hour)
    )
    if not in_window:
        return scheduled
    end = scheduled.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if end <= scheduled:
        end += timedelta(days=1)
    return end


def _schedule_for(
    mode: str, now: datetime, quiet_start: int | None, quiet_end: int | None
) -> datetime:
    if mode == MODE_DAILY:
        return _next_daily_slot(now)
    if mode == MODE_WEEKLY:
        return _next_weekly_slot(now)
    return apply_quiet_hours(now, quiet_start, quiet_end)


async def enqueue_finding_notifications(
    session: AsyncSession, artist: Artist, finding: Finding
) -> int:
    """Fan out one newly-created finding to every artist member's outbox. Returns the
    number of rows created (0 if all recipients already have this dedupe key, or the
    artist has no members yet)."""
    members = list(
        await session.scalars(
            select(ArtistMember.user_id).where(ArtistMember.artist_id == artist.id)
        )
    )
    if not members:
        return 0

    now = datetime.now(UTC)
    created = 0
    for user_id in members:
        dedupe_key = f"finding:{finding.id}:user:{user_id}"
        exists = await session.scalar(
            select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == dedupe_key)
        )
        if exists is not None:
            continue
        sub = await get_effective_subscription(session, user_id, artist.id)
        mode = sub.mode if sub else MODE_INSTANT
        quiet_start = sub.quiet_hours_start if sub else None
        quiet_end = sub.quiet_hours_end if sub else None
        scheduled_for = _schedule_for(mode, now, quiet_start, quiet_end)
        session.add(
            NotificationOutbox(
                user_id=user_id,
                kind=KIND_FINDING,
                dedupe_key=dedupe_key,
                finding_id=finding.id,
                status="pending",
                scheduled_for=scheduled_for,
            )
        )
        created += 1
    await session.flush()
    return created





async def send_admin_alert(text: str) -> None:
    """Отправляет текстовое сообщение всем администраторам в Telegram."""
    if not settings.telegram_bot_token or not settings.admin_ids:
        logger.warning("Telegram-оповещения не настроены. Сообщение: %s", text)
        return

    from aiogram import Bot
    bot = Bot(token=settings.telegram_bot_token)
    try:
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode="Markdown")
            except Exception as e:
                logger.warning("Не удалось отправить оповещение админу %s: %s", admin_id, e)
    finally:
        await bot.session.close()
